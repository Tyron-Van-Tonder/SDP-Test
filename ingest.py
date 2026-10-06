"""RAT — repository ingestion.

Pipeline (runs in a background thread, one per repository):

1. Obtain the repository: extract an uploaded zip, or deep-clone a URL.
2. Resolve the reference commit (default HEAD).
3. Single streaming pass over ``git log --no-merges --numstat -M50% -z``
   building per-commit file changes and per-commit directory rollups.
4. Bulk-load into SQLite, then populate the object universe.

Git's own machinery supplies the semantics the brief mandates: rename
detection at 50% similarity, binary detection, and non-merge history.

Verified byte format of ``git log --no-merges --numstat -M50% -z``:

    header   := 0x1e hash 0x1f parent 0x1f committer-date 0x1f
                author-name 0x1f author-email 0x1f subject '\0'
    entry    := ('-'|N) '\t' ('-'|N) '\t' path '\0'
    rename   := ('-'|N) '\t' ('-'|N) '\t' '\0' old '\0' new '\0'

The first diff entry of each commit carries a stray leading '\n' which the
parser strips.  For renames the change is attributed to the *new* path.
"""
from __future__ import annotations

import collections
import os
import pathlib
import re
import shutil
import subprocess
import threading
import zipfile

import db

RS = b"\x1e"  # commit record start
US = b"\x1f"  # field separator inside a commit record
LOG_FORMAT = "%x1e%H%x1f%P%x1f%ct%x1f%aN%x1f%aE%x1f%s"

_NUMSTAT_RE = re.compile(rb"^(-|\d+)\t(-|\d+)\t(.*)\Z", re.DOTALL)


class IngestError(Exception):
    """Raised when ingestion cannot proceed; message is user-facing."""


def _dec(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _git(repo_path: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:400]
        raise IngestError(f"git {' '.join(args[:2])} failed: {detail}")
    return proc


# ---------------------------------------------------------------------------
# Repository intake


def find_repo_root(base: pathlib.Path) -> pathlib.Path:
    """Locate the repository inside an extracted archive (depth <= 1)."""
    candidates = [base] + sorted(p for p in base.iterdir() if p.is_dir())
    for cand in candidates:
        if (cand / ".git").exists():
            return cand
    for cand in candidates:  # bare repositories have no .git entry
        if _git(cand, "rev-parse", "--git-dir", check=False).returncode == 0:
            return cand
    raise IngestError("The archive does not contain a git repository (.git not found).")


def extract_zip(zip_path: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        root = dest.resolve()
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if target != root and root not in target.parents:
                raise IngestError("Archive contains an unsafe path; refusing to extract.")
        zf.extractall(dest)
    return find_repo_root(dest)


def clone_repo(url: str, dest: pathlib.Path, on_progress) -> None:
    """Full (deep) clone; streams git's progress from stderr."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.Popen(
        ["git", "clone", "--no-checkout", "--progress", url, str(dest)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    tail: collections.deque = collections.deque(maxlen=40)
    pct_re = re.compile(rb"(\d+)%")

    def pump() -> None:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            tail.append(line)
            m = pct_re.search(line)
            if m:
                on_progress(min(int(m.group(1)), 100) / 100.0, line[:200])
            else:
                on_progress(None, line[:200])

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    code = proc.wait()
    reader.join(timeout=10)
    if code != 0:
        raise IngestError("git clone failed: " + (" | ".join(list(tail)[-5:]) or "unknown error")[:500])
    if not (dest / ".git").exists() and not (dest / "HEAD").exists():
        raise IngestError("The cloned repository looks empty or invalid.")


def resolve_ref(repo_path: pathlib.Path, ref: str) -> tuple[str, str]:
    ref = (ref or "HEAD").strip() or "HEAD"
    proc = _git(repo_path, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if proc.returncode != 0:
        raise IngestError(f"Reference '{ref}' not found in the repository.")
    return ref, proc.stdout.strip()


def wipe_repo_data(conn, repo_id: int) -> None:
    """Remove all derived rows for a repository (idempotent re-ingest)."""
    for table in ("changes", "dir_changes", "commits", "authors", "author_merge", "files"):
        conn.execute(f"DELETE FROM {table} WHERE repo_id = ?", (repo_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Streaming parser


def _iter_tokens(stream) -> "collections.abc.Iterator[bytes]":
    """Yield NUL-separated tokens from a binary stream."""
    buf = b""
    while True:
        chunk = stream.read(1 << 20)
        if not chunk:
            if buf:
                yield buf
            return
        buf += chunk
        parts = buf.split(b"\0")
        buf = parts.pop()
        for part in parts:
            yield part


def _ancestor_dirs(path: str):
    """All directories containing `path`, deepest first; includes root ''."""
    parts = path.split("/")
    for i in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:i])
    yield ""


def parse_and_load(repo_id: int, repo_path: pathlib.Path, ref: str, ref_hash: str, on_progress,
                   conn=None) -> None:
    """Stream the non-merge history of `ref_hash` into SQLite.

    Pass `conn` to reuse the caller's writer connection: the progress callback
    may write to the same database, and two writers held by one thread would
    deadlock on the SQLite write lock (the busy timeout then surfaces as
    "database is locked").
    """
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
    try:
        cur = conn.cursor()
        total = int(_git(repo_path, "rev-list", "--no-merges", "--count", ref_hash).stdout.strip() or "1")

        mailmap = []
        if _git(repo_path, "cat-file", "-e", f"{ref_hash}:.mailmap", check=False).returncode == 0:
            mailmap = ["-c", f"mailmap.blob={ref_hash}:.mailmap"]

        cmd = [
            "git", "-C", str(repo_path), *mailmap, "log",
            "--no-merges", "--numstat", "-M50%", "-z",
            f"--format={LOG_FORMAT}", "--use-mailmap", ref_hash,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        db.drop_indexes(conn)

        authors: dict[tuple[str, str], int] = {}
        change_rows: list[tuple] = []
        dir_rows: list[tuple] = []
        seen = 0
        last_commit_id: int | None = None
        rollup: dict[str, list[int]] = {}

        def close_commit() -> None:
            nonlocal rollup
            if rollup and last_commit_id is not None:
                for d, (a, dd) in rollup.items():
                    if a or dd:
                        dir_rows.append((repo_id, last_commit_id, d, a, dd))
            rollup = {}

        def emit_change(path: str, old_path: str | None, added: int, deleted: int, is_bin: bool) -> None:
            change_rows.append((repo_id, last_commit_id, path, added, deleted, old_path, 1 if is_bin else 0))
            if not is_bin and (added or deleted):
                for d in _ancestor_dirs(path):
                    acc = rollup.setdefault(d, [0, 0])
                    acc[0] += added
                    acc[1] += deleted

        def flush(force: bool = False) -> None:
            nonlocal change_rows, dir_rows
            if not force and seen % 25 != 0:
                return
            if change_rows:
                conn.executemany(
                    "INSERT INTO changes (repo_id, commit_id, path, added, deleted, old_path, is_binary)"
                    " VALUES (?,?,?,?,?,?,?)",
                    change_rows,
                )
                change_rows = []
            if dir_rows:
                conn.executemany(
                    "INSERT INTO dir_changes (repo_id, commit_id, path, added, deleted) VALUES (?,?,?,?,?)",
                    dir_rows,
                )
                dir_rows = []
            conn.commit()

        ren_added = ren_deleted = ren_binary = None  # pending rename marker
        ren_old: bytes | None = None

        for tok in _iter_tokens(proc.stdout):
            if not tok or tok == b"\n":
                continue
            if tok[:1] == RS:  # new commit record
                close_commit()
                parts = tok[1:].split(US, 5)
                if len(parts) < 6:
                    continue
                h, parents, cdate, an, ae, subject = parts
                name, email = _dec(an).strip(), _dec(ae).strip()
                key = (name, email)
                aid = authors.get(key)
                if aid is None:
                    cur.execute(
                        "INSERT OR IGNORE INTO authors (repo_id, name, email) VALUES (?,?,?)",
                        (repo_id, name, email),
                    )
                    cur.execute(
                        "SELECT id FROM authors WHERE repo_id=? AND name=? AND email=?",
                        (repo_id, name, email),
                    )
                    aid = cur.fetchone()[0]
                    authors[key] = aid
                parent = _dec(parents).strip()
                parent = parent.split()[0] if parent else None
                cur.execute(
                    "INSERT INTO commits (repo_id, hash, parent_hash, cdate, author_id, subject)"
                    " VALUES (?,?,?,?,?,?)",
                    (repo_id, _dec(h), parent, int(cdate), aid, _dec(subject)),
                )
                last_commit_id = cur.lastrowid
                seen += 1
                if seen % 500 == 0:
                    on_progress(min(seen / total, 1.0), f"Parsing history… {seen:,}/{total:,} commits")
                flush()
                continue

            if ren_added is not None:  # consuming old path, then new path
                if ren_old is None:
                    ren_old = tok
                else:
                    emit_change(_dec(tok), _dec(ren_old), ren_added, ren_deleted, ren_binary)
                    ren_added = ren_deleted = ren_binary = ren_old = None
                continue

            if tok[:1] == b"\n":
                tok = tok[1:]
            m = _NUMSTAT_RE.match(tok)
            if not m:
                continue
            adds, dels, path = m.group(1), m.group(2), m.group(3)
            is_bin = adds == b"-"
            added = 0 if is_bin else int(adds)
            deleted = 0 if is_bin else int(dels)
            if path == b"":
                # rename marker: `A\tD\t` followed by old\0new\0
                ren_added, ren_deleted, ren_binary = added, deleted, is_bin
                ren_old = None
                continue
            emit_change(_dec(path), None, added, deleted, is_bin)

        close_commit()
        flush(force=True)
        proc.stdout.close()

        stderr_tail = proc.stderr.read().decode("utf-8", "replace").strip()
        proc.stderr.close()
        if proc.wait() != 0:
            raise IngestError(f"git log failed while reading history: {stderr_tail[-400:]}")

        finalize(repo_id, repo_path, ref, ref_hash, conn)
        db.create_indexes(conn)
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def finalize(repo_id: int, repo_path: pathlib.Path, ref: str, ref_hash: str, conn) -> None:
    """Populate the object universe and repository statistics."""
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO files (repo_id, path, is_binary)"
        " SELECT DISTINCT repo_id, path, 0 FROM changes WHERE repo_id = ?",
        (repo_id,),
    )
    cur.execute(
        "UPDATE files SET is_binary = 1 WHERE repo_id = ? AND path IN"
        " (SELECT DISTINCT path FROM changes WHERE repo_id = ? AND is_binary = 1)",
        (repo_id, repo_id),
    )
    tree = subprocess.run(
        ["git", "-C", str(repo_path), "ls-tree", "-r", "-z", "--name-only", ref_hash],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if tree.returncode == 0:
        rows = [(repo_id, _dec(p), 0) for p in tree.stdout.split(b"\0") if p]
        cur.executemany(
            "INSERT OR IGNORE INTO files (repo_id, path, is_binary) VALUES (?,?,?)", rows
        )
    cur.execute(
        "UPDATE repos SET ref = ?, ref_hash = ?,"
        " commits_count = (SELECT COUNT(*) FROM commits  WHERE repo_id = ?),"
        " authors_count = (SELECT COUNT(*) FROM authors  WHERE repo_id = ?),"
        " files_count   = (SELECT COUNT(*) FROM files    WHERE repo_id = ?)"
        " WHERE id = ?",
        (ref, ref_hash, repo_id, repo_id, repo_id, repo_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Background job


def ingest_repo_job(repo_id: int, source_type: str, source: str, ref: str) -> None:
    """Thread entry point: run the full pipeline, mirroring status into the DB."""
    pconn = db.connect()
    clone_pct = [0.0]  # git restarts its '%' counter per phase; keep it monotonic

    def status(state: str, progress: float, msg: str, error: str | None = None) -> None:
        pconn.execute(
            "UPDATE repos SET status=?, progress=?, progress_msg=?, error=? WHERE id=?",
            (state, round(progress, 4), msg[:300], error, repo_id),
        )
        pconn.commit()

    dest = db.REPOS_DIR / str(repo_id)
    try:
        wipe_repo_data(pconn, repo_id)
        if source_type == "url":
            repo_path_dest = dest / "repo"

            def clone_progress(pct: float | None, msg: str) -> None:
                if pct is not None:
                    clone_pct[0] = max(clone_pct[0], pct)
                status("cloning", 0.02 + 0.48 * clone_pct[0], msg)

            status("cloning", 0.02, "Cloning repository (full history)…")
            clone_repo(source, repo_path_dest, clone_progress)
            repo_path = repo_path_dest
        else:
            status("parsing", 0.05, "Extracting archive…")
            repo_path = extract_zip(pathlib.Path(source), dest / "src")

        status("parsing", 0.5, "Resolving reference…")
        refname, ref_hash = resolve_ref(repo_path, ref)
        parse_and_load(
            repo_id,
            repo_path,
            refname,
            ref_hash,
            lambda frac, msg: status("parsing", 0.5 + 0.45 * frac, msg),
            conn=pconn,
        )
        row = pconn.execute("SELECT commits_count FROM repos WHERE id=?", (repo_id,)).fetchone()
        status("ready", 1.0, f"Ready — {row[0]:,} commits analysed")
    except IngestError as exc:
        _fail(pconn, repo_id, str(exc))
    except Exception as exc:  # unexpected; surface a readable message
        _fail(pconn, repo_id, f"{type(exc).__name__}: {exc}")
    finally:
        pconn.close()


def _fail(conn, repo_id: int, message: str) -> None:
    try:
        wipe_repo_data(conn, repo_id)
        db.create_indexes(conn)
    except Exception:
        pass
    conn.execute(
        "UPDATE repos SET status='error', progress=0, progress_msg='Ingestion failed', error=? WHERE id=?",
        (message[:500], repo_id),
    )
    conn.commit()
