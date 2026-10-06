"""Shared deterministic git fixture for the unit tests.

Builds a miniature repository exercising the semantic corners the metric
engine must handle, then ingests it into a throwaway SQLite database through
the real production path (``ingest.resolve_ref`` + ``ingest.parse_and_load``):

    commit  author  date        contents                                used for
    c1      Alice   2024-01-01  README.md +3, src/a.py +4               adds
    c2      Bob     2024-01-02  src/a.py +2, src/sub/b.txt +2,          edits, deletion
                                 README.md -1
    c3      Alice*  2024-01-03  rename src/a.py -> src/c.py (+1),        rename (-M50%), .mailmap
                                 .mailmap +2
    c4      Carol   2024-01-04  src/sub/e.txt +5,                        binary (unmeasured)
                                 assets/logo.png (binary)
    c5      Bob     2024-01-05  feature.txt +1   (branch `feature`)      branch history
    c6      Bob     2024-01-06  src/sub/d.md +1  (main)                  -
    c7      Dave    2024-01-07  merge `feature` (--no-ff)                merges excluded

    (*) authored as "A. Lovelace <alice@example.com>"; the two-line .mailmap
        rewrites both Alice identities to "Alice Example <alice@example.com>".

Hand-counted totals over the six non-merge commits (see EXPECTED).
"""
from __future__ import annotations

import atexit
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db      # noqa: E402
import ingest  # noqa: E402

BASE_EPOCH = 1704067200  # 2024-01-01T00:00:00Z
DAY = 86400

#: Hand-derived expectations for the fixture history above.
EXPECTED = {
    "commits": 6,        # non-merge commits reachable from HEAD
    "authors": 3,        # Alice Example, Bob Builder, Carol Coder
    "files": 9,          # incl. src/a.py (historical) and the binary asset
    "added": 21,
    "removed": 1,
    "churn": 22,
    "growth": 20,
    "src_added": 15,     # src/ subtree
    "src_sub_added": 8,  # src/sub/ subtree
    "root_files_added": 6,  # README.md 3 + .mailmap 2 + feature.txt 1
}


def _git(repo: pathlib.Path, *args: str, env: dict | None = None) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in fixture:\n{proc.stderr.decode('utf-8', 'replace')}"
        )


def _ident(name: str, email: str, epoch: int) -> dict:
    """Deterministic author/committer identity and date for one commit."""
    stamp = f"{epoch} +0000"
    return {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
    }


def _write(repo: pathlib.Path, rel: str, data: bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _commit(repo: pathlib.Path, msg: str, epoch: int, name: str, email: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--no-gpg-sign", "-m", msg, env=_ident(name, email, epoch))


def build_fixture_repo(repo: pathlib.Path) -> pathlib.Path:
    """Create the repository described in the module docstring."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")

    alice = ("Alice Example", "alice@example.com")
    bob = ("Bob Builder", "bob@example.com")
    carol = ("Carol Coder", "carol@example.com")

    # c1 — initial commit by Alice.
    _write(repo, "README.md", b"one\ntwo\nthree\n")
    _write(repo, "src/a.py", b"a1\na2\na3\na4\n")
    _commit(repo, "Initial commit", BASE_EPOCH, *alice)

    # c2 — Bob edits a.py, adds b.txt and deletes a README line.
    _write(repo, "src/a.py", b"a1\na2\na3\na4\na5\na6\n")
    _write(repo, "src/sub/b.txt", b"b1\nb2\n")
    _write(repo, "README.md", b"one\nthree\n")
    _commit(repo, "Extend module and docs", BASE_EPOCH + DAY, *bob)

    # c3 — rename src/a.py -> src/c.py (+1 line) and add the .mailmap,
    # authored under an alias the mailmap folds into Alice.
    _git(repo, "mv", "src/a.py", "src/c.py")
    _write(repo, "src/c.py", b"a1\na2\na3\na4\na5\na6\na7\n")
    _write(repo, ".mailmap",
           b"Alice Example <alice@example.com> A. Lovelace <alice@example.com>\n"
           b"Alice Example <alice@example.com> <alice@example.com>\n")
    _commit(repo, "Rename module, add mailmap", BASE_EPOCH + 2 * DAY,
            "A. Lovelace", "alice@example.com")

    # c4 — Carol adds a text file and a binary asset (binary never measured).
    _write(repo, "assets/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00")
    _write(repo, "src/sub/e.txt", b"e1\ne2\ne3\ne4\ne5\n")
    _commit(repo, "Add assets", BASE_EPOCH + 3 * DAY, *carol)

    # c5 — Bob commits on a side branch…
    _git(repo, "checkout", "-b", "feature")
    _write(repo, "feature.txt", b"f1\n")
    _commit(repo, "Start feature", BASE_EPOCH + 4 * DAY, *bob)

    # c6 — …and on main, then c7 merges the branch (Dave's merge is excluded).
    _git(repo, "checkout", "main")
    _write(repo, "src/sub/d.md", b"d1\n")
    _commit(repo, "Document submodule", BASE_EPOCH + 5 * DAY, *bob)
    _git(repo, "merge", "--no-ff", "-m", "Merge feature into main", "feature",
         env=_ident("Dave Merger", "dave@example.com", BASE_EPOCH + 6 * DAY))

    return repo


class IngestedFixture:
    """The fixture repository loaded into a temporary SQLite database."""

    def __init__(self, base: pathlib.Path):
        self.repo = build_fixture_repo(base / "repo")
        self.conn = db.connect(base / "rat-test.db")
        self.conn.executescript(db.SCHEMA)
        cur = self.conn.execute(
            "INSERT INTO repos (name, source_type, source, ref, status, progress, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("fixture", "url", str(self.repo), "HEAD", "parsing", 0.5, BASE_EPOCH),
        )
        self.repo_id = int(cur.lastrowid)
        self.conn.commit()

        ref, ref_hash = ingest.resolve_ref(self.repo, "HEAD")
        ingest.parse_and_load(
            self.repo_id, self.repo, ref, ref_hash, lambda *_: None, conn=self.conn
        )
        self.hashes = [
            row[0] for row in self.conn.execute(
                "SELECT hash FROM commits WHERE repo_id = ? ORDER BY cdate DESC, id DESC",
                (self.repo_id,),
            )
        ]

    @property
    def newest_hash(self) -> str:
        return self.hashes[0]

    def author_id(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM authors WHERE repo_id = ? AND name = ?", (self.repo_id, name)
        ).fetchone()
        if row is None:
            raise KeyError(f"no author named {name!r} in fixture")
        return int(row[0])


_SHARED: IngestedFixture | None = None


def _cleanup(fx: IngestedFixture, tmp: tempfile.TemporaryDirectory) -> None:
    try:
        fx.conn.close()
    finally:
        tmp.cleanup()


def shared() -> IngestedFixture:
    """One lazily-built fixture shared by every test module in the process."""
    global _SHARED
    if _SHARED is None:
        tmp = tempfile.TemporaryDirectory(prefix="rat-test-")
        _SHARED = IngestedFixture(pathlib.Path(tmp.name))
        atexit.register(_cleanup, _SHARED, tmp)
    return _SHARED
