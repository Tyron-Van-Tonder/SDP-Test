"""RAT — metric engine.

At ingestion time every non-merge commit is decomposed into per-file line
counts (`changes`) and per-directory recursive rollups (`dir_changes`).
The queries here are therefore pure indexed aggregations — no per-request
history walking — which keeps the dashboard fast even on large histories
(~100k commits, e.g. git.git).

Notation (from the brief), for an object o (file or directory) and a commit
set H centred on the reference commit h_r:

    l+       added lines                      (SUM added)
    l-       removed lines                    (SUM deleted)
    delta    growth  = l+ - l-
    lambda   churn   = l+ + l-
    n        modifications = |{h in H : lambda_{h,o} > 0}|
    eta      modification frequency = n / |H|
    rho      churn rate = lambda_{H,o} / |H|
    omega    ownership  = lambda_{H,o,a} / lambda_{H,o}

Merge commits are excluded and binary files are never measured (they keep
zero line counts) — both are handled during ingestion.
"""
from __future__ import annotations

from dataclasses import dataclass

import db

BUCKETS = {"day": "%Y-%m-%d", "week": "%Y-%W", "month": "%Y-%m"}


# ---------------------------------------------------------------------------
# Commit-set filters


@dataclass(frozen=True)
class CommitFilter:
    """Selector for the commit set H.

    ``all``   — every non-merge commit reachable from the analysed reference.
    ``range`` — H_{i,j} = {h | start <= committer date < end}, epoch seconds.
    ``list``  — an explicit set of commit hashes.
    """

    mode: str = "all"
    start: int | None = None
    end: int | None = None
    hashes: tuple[str, ...] = ()


def filter_from_payload(payload: dict | None) -> CommitFilter:
    """Build a CommitFilter from a JSON payload, defensively."""
    payload = payload or {}
    mode = payload.get("mode") or "all"
    if mode not in ("all", "range", "list"):
        mode = "all"

    def _int(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    raw_hashes = payload.get("hashes") or ()
    if isinstance(raw_hashes, str):
        raw_hashes = raw_hashes.replace(",", " ").split()
    hashes = tuple(str(h).strip() for h in raw_hashes if str(h).strip())[:2000]
    return CommitFilter(
        mode=mode,
        start=_int(payload.get("start")),
        end=_int(payload.get("end")),
        hashes=hashes,
    )


def resolve_author_members(canonical: dict[int, int], wanted) -> set[int] | None:
    """Expand author ids (merged or canonical) to all member identity ids.

    Returns None when no author filter is active.
    """
    if not wanted:
        return None
    targets = {canonical.get(w, w) for w in wanted}
    return {aid for aid, canon in canonical.items() if canon in targets}


_HEX = set("0123456789abcdef")


def _is_hex_prefix(h: str) -> bool:
    """True when *h* looks like a (possibly abbreviated) object hash."""
    return 4 <= len(h) <= 40 and all(ch in _HEX for ch in h)


def _commit_where(repo_id: int, flt: CommitFilter, author_members=None) -> tuple[str, list]:
    """WHERE fragment over ``commits c`` selecting the commit set H, + params."""
    cond = ["c.repo_id = ?"]
    params: list = [repo_id]
    if flt.mode == "range":
        if flt.start is not None:
            cond.append("c.cdate >= ?")
            params.append(int(flt.start))
        if flt.end is not None:
            cond.append("c.cdate < ?")
            params.append(int(flt.end))
    elif flt.mode == "list":
        # Accept full and abbreviated hashes alike (git-style prefix match).
        prefixes = list(dict.fromkeys(
            h.lower() for h in (flt.hashes or ()) if _is_hex_prefix(h.lower())
        ))
        if prefixes:
            cond.append("(" + " OR ".join("c.hash = ? OR c.hash GLOB ?" for _ in prefixes) + ")")
            for h in prefixes:
                params.extend((h, h + "*"))
        else:
            cond.append("0")  # empty or malformed list => empty H
    if author_members is not None:
        members = sorted(author_members)
        if members:
            cond.append("c.author_id IN (" + ",".join("?" * len(members)) + ")")
            params.extend(members)
        else:
            cond.append("0")
    return " AND ".join(cond), params


# ---------------------------------------------------------------------------
# Core statistics


def _shape(added: int, removed: int, modifications: int, commits: int) -> dict:
    """The full line-count statistic block of one object over H."""
    churn = added + removed
    return {
        "added": added,
        "removed": removed,
        "growth": added - removed,
        "churn": churn,
        "modifications": modifications,
        "commits": commits,
        "mod_freq": (modifications / commits) if commits else 0.0,
        "churn_rate": (churn / commits) if commits else 0.0,
    }


def commit_count(conn, repo_id: int, flt: CommitFilter, author_members=None) -> int:
    """|H| — number of commits in the selected set."""
    cond, params = _commit_where(repo_id, flt, author_members)
    return int(conn.execute(f"SELECT COUNT(*) FROM commits c WHERE {cond}", params).fetchone()[0])


def totals(conn, repo_id: int, flt: CommitFilter, author_members=None) -> dict:
    """Repository-level metrics — the recursive rollup of the root directory."""
    cond, params = _commit_where(repo_id, flt, author_members)
    added, removed, mods = conn.execute(
        "SELECT COALESCE(SUM(x.added),0), COALESCE(SUM(x.deleted),0),"
        " COUNT(DISTINCT CASE WHEN x.added + x.deleted > 0 THEN x.commit_id END)"
        " FROM dir_changes x JOIN commits c ON c.id = x.commit_id"
        f" WHERE x.repo_id = ? AND x.path = '' AND {cond}",
        [repo_id, *params],
    ).fetchone()
    return _shape(
        int(added), int(removed), int(mods),
        commit_count(conn, repo_id, flt, author_members),
    )


# ---------------------------------------------------------------------------
# Object (file / directory) tables


def _file_universe(conn, repo_id: int) -> dict[str, int]:
    """Every path that exists or ever existed → is_binary flag."""
    return {
        row[0]: int(row[1])
        for row in conn.execute(
            "SELECT path, is_binary FROM files WHERE repo_id = ?", (repo_id,)
        )
    }


def _dir_universe(conn, repo_id: int) -> set[str]:
    """Every directory that exists in the analysed tree (root '' included)."""
    dirs = {""}
    for (path,) in conn.execute("SELECT path FROM files WHERE repo_id = ?", (repo_id,)):
        parts = path.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return dirs


def _aggregate(conn, table: str, repo_id: int, cond: str, params: list) -> dict[str, tuple]:
    """Per-path (added, removed, modifications) over H for `changes`/`dir_changes`."""
    rows = conn.execute(
        "SELECT x.path, COALESCE(SUM(x.added),0), COALESCE(SUM(x.deleted),0),"
        " COUNT(DISTINCT CASE WHEN x.added + x.deleted > 0 THEN x.commit_id END)"
        f" FROM {table} x JOIN commits c ON c.id = x.commit_id"
        f" WHERE x.repo_id = ? AND {cond}"
        " GROUP BY x.path",
        [repo_id, *params],
    ).fetchall()
    return {row[0]: (int(row[1]), int(row[2]), int(row[3])) for row in rows}


def file_metrics(conn, repo_id: int, flt: CommitFilter, author_members=None,
                 include_untouched: bool = False) -> list[dict]:
    """Per-file metrics over H (binary files are listed but never measured)."""
    cond, params = _commit_where(repo_id, flt, author_members)
    h = commit_count(conn, repo_id, flt, author_members)
    agg = _aggregate(conn, "changes", repo_id, cond, params)
    universe = _file_universe(conn, repo_id)
    if include_untouched:
        for path in universe:
            agg.setdefault(path, (0, 0, 0))
    items = []
    for path, (added, removed, mods) in agg.items():
        item = _shape(added, removed, mods, h)
        item["path"] = path
        item["is_binary"] = universe.get(path, 0)
        items.append(item)
    items.sort(key=lambda r: (-r["churn"], r["path"]))
    return items


def dir_metrics(conn, repo_id: int, flt: CommitFilter, author_members=None,
                include_untouched: bool = False) -> list[dict]:
    """Per-directory metrics over H (recursive sums over the subtree)."""
    cond, params = _commit_where(repo_id, flt, author_members)
    h = commit_count(conn, repo_id, flt, author_members)
    agg = _aggregate(conn, "dir_changes", repo_id, cond, params)
    if include_untouched:
        for path in _dir_universe(conn, repo_id):
            agg.setdefault(path, (0, 0, 0))
    items = []
    for path, (added, removed, mods) in agg.items():
        item = _shape(added, removed, mods, h)
        item["path"] = path
        items.append(item)
    items.sort(key=lambda r: (-r["churn"], r["path"]))
    return items


# ---------------------------------------------------------------------------
# Object detail & navigation


def object_detail(conn, repo_id: int, flt: CommitFilter, kind: str, path: str,
                  canonical: dict[int, int], author_members=None) -> dict:
    """Object metrics plus the per-author ownership breakdown (omega)."""
    table = "changes" if kind == "file" else "dir_changes"
    cond, params = _commit_where(repo_id, flt, author_members)
    h = commit_count(conn, repo_id, flt, author_members)
    identities = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT id, name, email FROM authors WHERE repo_id = ?", (repo_id,)
        )
    }
    rows = conn.execute(
        "SELECT c.author_id, COALESCE(SUM(x.added),0), COALESCE(SUM(x.deleted),0),"
        " COUNT(DISTINCT CASE WHEN x.added + x.deleted > 0 THEN x.commit_id END)"
        f" FROM {table} x JOIN commits c ON c.id = x.commit_id"
        f" WHERE x.repo_id = ? AND x.path = ? AND {cond}"
        " GROUP BY c.author_id",
        [repo_id, path, *params],
    ).fetchall()

    groups: dict[int, dict] = {}
    for aid, added, removed, mods in rows:
        canon = canonical.get(aid, aid)
        group = groups.setdefault(canon, {
            "id": canon, "added": 0, "removed": 0, "modifications": 0, "members": [],
        })
        group["added"] += int(added)
        group["removed"] += int(removed)
        group["modifications"] += int(mods)
        name, email = identities.get(aid, ("", ""))
        group["members"].append({"id": aid, "name": name, "email": email})

    added_total = sum(g["added"] for g in groups.values())
    removed_total = sum(g["removed"] for g in groups.values())
    churn_total = added_total + removed_total
    authors = []
    for group in groups.values():
        group.update(_shape(group["added"], group["removed"], group["modifications"], h))
        group["ownership"] = (group["churn"] / churn_total) if churn_total else 0.0
        group["members"].sort(key=lambda m: m["name"].lower())
        authors.append(group)
    authors.sort(key=lambda g: (-g["churn"], g["id"]))
    return {
        "kind": kind,
        "path": path,
        "totals": _shape(
            added_total, removed_total,
            sum(g["modifications"] for g in groups.values()), h,
        ),
        "commits": h,
        "authors": authors,
    }


def browse_objects(conn, repo_id: int, prefix: str = "") -> dict:
    """Immediate children of `prefix` — powers the lazy object browser."""
    prefix = (prefix or "").strip("/")
    lo = prefix + "/" if prefix else ""
    hi = lo + "\U0010ffff"
    dirs: set[str] = set()
    files: list[str] = []
    for (path,) in conn.execute(
        "SELECT path FROM files WHERE repo_id = ? AND path >= ? AND path < ? ORDER BY path",
        (repo_id, lo, hi),
    ):
        rest = path[len(lo):]
        if not rest:
            continue
        head, sep, _ = rest.partition("/")
        if sep:
            dirs.add(lo + head)
        else:
            files.append(path)
    return {"prefix": prefix, "dirs": sorted(dirs), "files": files}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_objects(conn, repo_id: int, query: str, limit: int = 40) -> dict:
    """Substring search over file paths and the directories on their way."""
    query = (query or "").strip()
    if not query:
        return {"files": [], "dirs": []}
    rows = conn.execute(
        "SELECT path FROM files WHERE repo_id = ? AND path LIKE ? ESCAPE '\\'"
        " ORDER BY path LIMIT ?",
        (repo_id, f"%{_like_escape(query)}%", max(1, int(limit))),
    ).fetchall()
    files = [row[0] for row in rows]
    dirs: set[str] = set()
    needle = query.lower()
    for path in files:
        parts = path.split("/")
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            if needle in ancestor.lower():
                dirs.add(ancestor)
    return {"files": files, "dirs": sorted(dirs)}


# ---------------------------------------------------------------------------
# Time series


def series(conn, repo_id: int, flt: CommitFilter, bucket: str = "day",
           author_members=None) -> list[dict]:
    """Commit/churn time series bucketed by day, week or month (UTC)."""
    fmt = BUCKETS.get(bucket) or BUCKETS["day"]
    cond, params = _commit_where(repo_id, flt, author_members)
    counts = {
        row[0]: int(row[1])
        for row in conn.execute(
            "SELECT strftime(?, cdate, 'unixepoch') AS bucket, COUNT(*)"
            " FROM commits c"
            f" WHERE {cond} GROUP BY bucket",
            [fmt, *params],
        )
    }
    churn = {
        row[0]: (int(row[1]), int(row[2]), int(row[3]))
        for row in conn.execute(
            "SELECT strftime(?, c.cdate, 'unixepoch') AS bucket,"
            " COALESCE(SUM(x.added),0), COALESCE(SUM(x.deleted),0),"
            " SUM(CASE WHEN x.added + x.deleted > 0 THEN 1 ELSE 0 END)"
            " FROM dir_changes x JOIN commits c ON c.id = x.commit_id"
            f" WHERE x.repo_id = ? AND x.path = '' AND {cond}"
            " GROUP BY bucket",
            [fmt, repo_id, *params],
        )
    }
    points = []
    for bucket_id in sorted(counts):
        added, removed, mods = churn.get(bucket_id, (0, 0, 0))
        points.append({
            "bucket": bucket_id,
            "added": added,
            "removed": removed,
            "growth": added - removed,
            "churn": added + removed,
            "commits": counts[bucket_id],
            "modifications": mods,
        })
    return points


def object_series(conn, repo_id: int, flt: CommitFilter, kind: str, path: str,
                  bucket: str = "day", author_members=None) -> list[dict]:
    """Time series restricted to one object (file or directory)."""
    table = "changes" if kind == "file" else "dir_changes"
    fmt = BUCKETS.get(bucket) or BUCKETS["day"]
    cond, params = _commit_where(repo_id, flt, author_members)
    rows = conn.execute(
        "SELECT strftime(?, c.cdate, 'unixepoch') AS bucket,"
        " COALESCE(SUM(x.added),0), COALESCE(SUM(x.deleted),0),"
        " COUNT(DISTINCT CASE WHEN x.added + x.deleted > 0 THEN x.commit_id END)"
        f" FROM {table} x JOIN commits c ON c.id = x.commit_id"
        f" WHERE x.repo_id = ? AND x.path = ? AND {cond}"
        " GROUP BY bucket ORDER BY bucket",
        [fmt, repo_id, path, *params],
    ).fetchall()
    points = []
    for bucket_id, added, removed, mods in rows:
        added, removed = int(added), int(removed)
        points.append({
            "bucket": bucket_id,
            "added": added,
            "removed": removed,
            "growth": added - removed,
            "churn": added + removed,
            "modifications": int(mods),
        })
    return points


# ---------------------------------------------------------------------------
# Commit browser


def commits_page(conn, repo_id: int, limit: int = 50, offset: int = 0,
                 search: str | None = None) -> dict:
    """Paged commit listing (newest first); used by the manual-hash picker."""
    where = "c.repo_id = ?"
    params: list = [repo_id]
    search = (search or "").strip()
    if search:
        where += " AND (c.hash LIKE ? OR c.subject LIKE ?)"
        like = f"%{search}%"
        params += [like, like]
    total = int(conn.execute(f"SELECT COUNT(*) FROM commits c WHERE {where}", params).fetchone()[0])
    rows = conn.execute(
        "SELECT c.id, c.hash, c.parent_hash, c.cdate, c.subject, c.author_id,"
        " a.name, a.email"
        " FROM commits c JOIN authors a ON a.id = c.author_id"
        f" WHERE {where} ORDER BY c.cdate DESC, c.id DESC LIMIT ? OFFSET ?",
        [*params, max(1, int(limit)), max(0, int(offset))],
    ).fetchall()
    ids = [row[0] for row in rows]
    churn: dict[int, tuple[int, int]] = {}
    if ids:
        churn = {
            row[0]: (int(row[1]), int(row[2]))
            for row in conn.execute(
                "SELECT commit_id, added, deleted FROM dir_changes"
                " WHERE repo_id = ? AND path = '' AND commit_id IN ("
                + ",".join("?" * len(ids)) + ")",
                [repo_id, *ids],
            )
        }
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": row[0], "hash": row[1], "parent_hash": row[2], "cdate": row[3],
                "subject": row[4] or "", "author_id": row[5], "author_name": row[6],
                "author_email": row[7],
                "added": churn.get(row[0], (0, 0))[0],
                "removed": churn.get(row[0], (0, 0))[1],
                "churn": sum(churn.get(row[0], (0, 0))),
            }
            for row in rows
        ],
    }


def unknown_hashes(conn, repo_id: int, hashes) -> list[str]:
    """Entries of a manual commit list that match no commit (or are malformed).

    Full hashes and abbreviations (down to 4 hex chars) are resolved by
    prefix, mirroring ``git rev-parse``.  Duplicates are probed once.
    """
    entries = [str(h).strip() for h in (hashes or ()) if str(h).strip()]
    resolved: dict[str, bool] = {}
    unknown: list[str] = []
    for raw in entries:
        low = raw.lower()
        if low not in resolved:
            ok = False
            if _is_hex_prefix(low):
                ok = conn.execute(
                    "SELECT 1 FROM commits WHERE repo_id = ?"
                    " AND (hash = ? OR hash GLOB ?) LIMIT 1",
                    (repo_id, low, low + "*"),
                ).fetchone() is not None
            resolved[low] = ok
        if not resolved[low]:
            unknown.append(raw)
    return unknown


# ---------------------------------------------------------------------------
# Authors & identity merging


def authors_overview(conn, repo_id: int, canonical: dict[int, int],
                     flt: CommitFilter | None = None, author_members=None) -> list[dict]:
    """One row per canonical identity, with member identities for the merge UI."""
    flt = flt or CommitFilter()
    cond, params = _commit_where(repo_id, flt, author_members)
    identities = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT id, name, email FROM authors WHERE repo_id = ?", (repo_id,)
        )
    }

    groups: dict[int, dict] = {}
    for aid, (name, email) in identities.items():
        canon = canonical.get(aid, aid)
        group = groups.setdefault(canon, {
            "id": canon, "name": name, "email": email,
            "added": 0, "removed": 0, "modifications": 0, "commits": 0, "members": [],
        })
        if aid == canon:
            group["name"], group["email"] = name, email
        group["members"].append(
            {"id": aid, "name": name, "email": email, "commits": 0, "added": 0, "removed": 0}
        )
    member_index = {m["id"]: m for group in groups.values() for m in group["members"]}

    for aid, count in conn.execute(
        f"SELECT c.author_id, COUNT(*) FROM commits c WHERE {cond} GROUP BY c.author_id",
        params,
    ):
        group = groups.get(canonical.get(aid, aid))
        if group is not None:
            group["commits"] += int(count)
        member = member_index.get(aid)
        if member is not None:
            member["commits"] = int(count)

    for aid, added, removed, mods in conn.execute(
        "SELECT c.author_id, COALESCE(SUM(x.added),0), COALESCE(SUM(x.deleted),0),"
        " COUNT(DISTINCT CASE WHEN x.added + x.deleted > 0 THEN c.id END)"
        " FROM dir_changes x JOIN commits c ON c.id = x.commit_id"
        f" WHERE x.repo_id = ? AND x.path = '' AND {cond} GROUP BY c.author_id",
        [repo_id, *params],
    ):
        group = groups.get(canonical.get(aid, aid))
        if group is None:
            continue
        group["added"] += int(added)
        group["removed"] += int(removed)
        group["modifications"] += int(mods)
        member = member_index.get(aid)
        if member is not None:
            member["added"] += int(added)
            member["removed"] += int(removed)

    result = []
    for group in groups.values():
        group.update(
            _shape(group["added"], group["removed"], group["modifications"], group["commits"])
        )
        for member in group["members"]:
            member["churn"] = member["added"] + member["removed"]
        group["members"].sort(key=lambda m: (-m["churn"], m["name"].lower()))
        group["merged"] = len(group["members"]) > 1
        result.append(group)
    result.sort(key=lambda g: (-g["churn"], g["name"].lower()))
    return result


def merge_authors(conn, repo_id: int, from_id: int, to_id: int) -> dict[int, int]:
    """Fold the `from_id` identity group into `to_id`; returns canonical map."""
    canonical = db.author_canonical(conn, repo_id)
    if from_id not in canonical or to_id not in canonical:
        raise ValueError("Unknown author identity.")
    from_canon = canonical.get(from_id, from_id)
    to_canon = canonical.get(to_id, to_id)
    if from_canon == to_canon:
        raise ValueError("These identities are already merged.")
    conn.execute(
        "INSERT OR REPLACE INTO author_merge (repo_id, from_id, to_id) VALUES (?,?,?)",
        (repo_id, from_canon, to_canon),
    )
    conn.commit()
    return db.author_canonical(conn, repo_id)


def unmerge_author(conn, repo_id: int, from_id: int) -> dict[int, int]:
    """Undo a manual merge for the identity group rooted at `from_id`."""
    conn.execute(
        "DELETE FROM author_merge WHERE repo_id = ? AND from_id = ?", (repo_id, from_id)
    )
    conn.commit()
    return db.author_canonical(conn, repo_id)


def repo_overview(conn, repo_id: int) -> dict:
    """Headline numbers for the dashboard header."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MIN(cdate),0), COALESCE(MAX(cdate),0)"
        " FROM commits WHERE repo_id = ?",
        (repo_id,),
    ).fetchone()
    return {
        "commits": int(row[0]),
        "first_cdate": int(row[1]),
        "last_cdate": int(row[2]),
    }
