"""RAT — SQLite storage layer.

Metrics are precomputed at ingest time into these tables so that dashboard
queries are plain aggregations over indexed data (fast even for very large
histories, e.g. ~100k commits).
"""
from __future__ import annotations

import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPOS_DIR = DATA_DIR / "repos"
DB_PATH = DATA_DIR / "rat.db"

TABLES = """
CREATE TABLE IF NOT EXISTS repos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    source_type   TEXT    NOT NULL,              -- 'zip' | 'url'
    source        TEXT    NOT NULL,              -- original URL or zip filename
    ref           TEXT,                          -- ref requested by the user
    ref_hash      TEXT,                          -- resolved commit actually analysed
    status        TEXT    NOT NULL DEFAULT 'queued',
    progress      REAL    NOT NULL DEFAULT 0.0,
    progress_msg  TEXT,
    error         TEXT,
    commits_count INTEGER NOT NULL DEFAULT 0,
    authors_count INTEGER NOT NULL DEFAULT 0,
    files_count   INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS authors (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    name    TEXT    NOT NULL,
    email   TEXT    NOT NULL,
    UNIQUE (repo_id, name, email)
);

-- Manual author merges: identity from_id is folded into identity to_id.
CREATE TABLE IF NOT EXISTS author_merge (
    repo_id INTEGER NOT NULL,
    from_id INTEGER NOT NULL,
    to_id   INTEGER NOT NULL,
    PRIMARY KEY (repo_id, from_id)
);

CREATE TABLE IF NOT EXISTS commits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL,
    hash        TEXT    NOT NULL,
    parent_hash TEXT,
    cdate       INTEGER NOT NULL,                -- committer date, unix epoch
    author_id   INTEGER NOT NULL,
    subject     TEXT
);

-- One row per file-change entry of a non-merge commit.
-- Renames are attributed to the new path (old_path keeps the original one).
-- Binary entries are recorded with is_binary = 1 and are never measured.
CREATE TABLE IF NOT EXISTS changes (
    repo_id   INTEGER NOT NULL,
    commit_id INTEGER NOT NULL,
    path      TEXT    NOT NULL,
    added     INTEGER NOT NULL,
    deleted   INTEGER NOT NULL,
    old_path  TEXT,
    is_binary INTEGER NOT NULL DEFAULT 0
);

-- Per-commit directory rollups: one row per directory ('' = repository
-- root) whose subtree churned in that commit.
CREATE TABLE IF NOT EXISTS dir_changes (
    repo_id   INTEGER NOT NULL,
    commit_id INTEGER NOT NULL,
    path      TEXT    NOT NULL,
    added     INTEGER NOT NULL,
    deleted   INTEGER NOT NULL
);

-- Object universe: every path that exists or has ever existed.
CREATE TABLE IF NOT EXISTS files (
    repo_id   INTEGER NOT NULL,
    path      TEXT    NOT NULL,
    is_binary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, path)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_commits_repo_date ON commits (repo_id, cdate);
CREATE INDEX IF NOT EXISTS idx_commits_repo_hash ON commits (repo_id, hash);
CREATE INDEX IF NOT EXISTS idx_changes_repo_cmt ON changes (repo_id, commit_id);
CREATE INDEX IF NOT EXISTS idx_changes_repo_path ON changes (repo_id, path);
CREATE INDEX IF NOT EXISTS idx_dirs_repo_cmt ON dir_changes (repo_id, commit_id);
CREATE INDEX IF NOT EXISTS idx_dirs_repo_path ON dir_changes (repo_id, path);
"""

SCHEMA = TABLES + INDEXES


def connect(db_path: pathlib.Path = DB_PATH) -> sqlite3.Connection:
    """Open a connection tuned for concurrent readers + a single writer."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: pathlib.Path = DB_PATH) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def drop_indexes(conn: sqlite3.Connection) -> None:
    """Drop secondary indexes before a bulk load (recreated afterwards)."""
    conn.executescript(
        "DROP INDEX IF EXISTS idx_commits_repo_date;"
        "DROP INDEX IF EXISTS idx_commits_repo_hash;"
        "DROP INDEX IF EXISTS idx_changes_repo_cmt;"
        "DROP INDEX IF EXISTS idx_changes_repo_path;"
        "DROP INDEX IF EXISTS idx_dirs_repo_cmt;"
        "DROP INDEX IF EXISTS idx_dirs_repo_path;"
    )


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEXES)


def author_canonical(conn: sqlite3.Connection, repo_id: int) -> dict[int, int]:
    """Map every author identity to its canonical id (follows merge chains)."""
    chains = {
        row["from_id"]: row["to_id"]
        for row in conn.execute(
            "SELECT from_id, to_id FROM author_merge WHERE repo_id = ?", (repo_id,)
        )
    }
    result: dict[int, int] = {}
    for (aid,) in conn.execute("SELECT id FROM authors WHERE repo_id = ?", (repo_id,)):
        seen = set()
        cur = aid
        while cur in chains and cur not in seen:
            seen.add(cur)
            cur = chains[cur]
        result[aid] = cur
    return result
