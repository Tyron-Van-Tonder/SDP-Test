"""RAT — FastAPI application.

Serves the dashboard frontend plus a small JSON API.

Ingestion runs in one background worker thread so SQLite only ever sees a
single bulk writer; every other request is a pure read over indexed tables.

    POST   /api/repos                       ingest a repository from a URL
    POST   /api/repos/upload                ingest a repository from a zip
    GET    /api/repos                       list repositories (+ live status)
    GET    /api/repos/{id}                  repository detail + overview
    DELETE /api/repos/{id}                  remove a repository and its data
    GET    /api/repos/{id}/metrics          metrics tables (repo | file | dir)
    GET    /api/repos/{id}/series           commit/churn time series
    GET    /api/repos/{id}/object           one object + ownership breakdown
    GET    /api/repos/{id}/authors          merged author overview
    POST   /api/repos/{id}/authors/merge    merge two author identities
    POST   /api/repos/{id}/authors/unmerge  undo an author merge
    GET    /api/repos/{id}/commits          paged commit browser
    GET    /api/repos/{id}/objects          browse / search the object universe
"""
from __future__ import annotations

import logging
import pathlib
import queue
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import db
import ingest
import metrics

ROOT = pathlib.Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

_URL_RE = re.compile(r"^(https?://|ssh://|git://|git@)[^\s]+$")

_jobs: "queue.Queue[tuple[int, str, str, str]]" = queue.Queue()

_log = logging.getLogger("rat.ingest")


def _worker() -> None:
    """Serial ingestion worker — SQLite gets exactly one bulk writer."""
    while True:
        repo_id, source_type, source, ref = _jobs.get()
        try:
            ingest.ingest_repo_job(repo_id, source_type, source, ref)
        except Exception:  # never let the worker thread die
            _log.exception("Ingestion job failed for repo %s", repo_id)
        finally:
            _jobs.task_done()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    threading.Thread(target=_worker, daemon=True, name="rat-ingest").start()
    yield


app = FastAPI(title="RAT — Repo Analysis Tool", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers


def _repo_or_404(conn, repo_id: int):
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Repository not found.")
    return row


def _require_ready(row) -> None:
    if row["status"] != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Repository is not ready yet (status: {row['status']}).",
        )


def _split_param(raw: str) -> list[str]:
    return [part for part in (raw or "").replace(",", " ").split() if part]


def _authors_param(raw: str) -> list[int] | None:
    ids = []
    for part in _split_param(raw):
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids or None


def _build_filter(mode: str, start, end, hashes: str) -> metrics.CommitFilter:
    return metrics.filter_from_payload(
        {"mode": mode, "start": start, "end": end, "hashes": _split_param(hashes)}
    )


def _author_scope(conn, repo_id: int, authors: str):
    """(canonical map, member ids for the author filter or None)."""
    canonical = db.author_canonical(conn, repo_id)
    members = metrics.resolve_author_members(canonical, _authors_param(authors))
    return canonical, members


def _create_repo(name: str, source_type: str, source: str, ref: str) -> int:
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO repos (name, source_type, source, ref, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (name, source_type, source, ref or "HEAD", "queued", int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _repo_payload(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "source_type": row["source_type"],
        "source": row["source"],
        "ref": row["ref"],
        "ref_hash": row["ref_hash"],
        "status": row["status"],
        "progress": row["progress"],
        "progress_msg": row["progress_msg"],
        "error": row["error"],
        "commits_count": row["commits_count"],
        "authors_count": row["authors_count"],
        "files_count": row["files_count"],
        "created_at": row["created_at"],
    }


def _derive_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


# ---------------------------------------------------------------------------
# Repositories


@app.get("/api/repos")
def list_repos():
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM repos ORDER BY id DESC").fetchall()
        return {"repos": [_repo_payload(r) for r in rows], "queue": _jobs.qsize()}
    finally:
        conn.close()


@app.post("/api/repos", status_code=201)
def create_repo(payload: dict | None = None):
    payload = payload or {}
    url = str(payload.get("url") or "").strip()
    if not _URL_RE.match(url):
        raise HTTPException(400, "Provide a valid repository URL (https, ssh or git).")
    name = str(payload.get("name") or "").strip() or _derive_name(url) or "repository"
    ref = str(payload.get("ref") or "HEAD").strip() or "HEAD"
    repo_id = _create_repo(name, "url", url, ref)
    _jobs.put((repo_id, "url", url, ref))
    return {"id": repo_id, "name": name, "ref": ref}


@app.post("/api/repos/upload", status_code=201)
def upload_repo(file: UploadFile = File(...), ref: str = Form("HEAD"), name: str = Form("")):
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            400,
            "Please upload a .zip archive of the repository (including its .git directory).",
        )
    ref = (ref or "HEAD").strip() or "HEAD"
    clean_name = name.strip() or filename[:-4] or "archive"
    repo_id = _create_repo(clean_name, "zip", filename, ref)
    dest_zip = db.REPOS_DIR / f"{repo_id}.zip"
    try:
        (db.REPOS_DIR / str(repo_id)).mkdir(parents=True, exist_ok=True)
        with open(dest_zip, "wb") as out:
            shutil.copyfileobj(file.file, out, length=1 << 20)
    except OSError as exc:
        conn = db.connect()
        try:
            conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
            conn.commit()
        finally:
            conn.close()
        raise HTTPException(500, f"Could not store the uploaded archive: {exc}")
    conn = db.connect()
    try:
        conn.execute("UPDATE repos SET source = ? WHERE id = ?", (str(dest_zip), repo_id))
        conn.commit()
    finally:
        conn.close()
    _jobs.put((repo_id, "zip", str(dest_zip), ref))
    return {"id": repo_id, "name": clean_name, "ref": ref}


@app.get("/api/repos/{repo_id}")
def get_repo(repo_id: int):
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        payload = _repo_payload(row)
        if row["status"] == "ready":
            payload["overview"] = metrics.repo_overview(conn, repo_id)
        return payload
    finally:
        conn.close()


@app.delete("/api/repos/{repo_id}")
def delete_repo(repo_id: int):
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        if row["status"] in ("queued", "cloning", "parsing"):
            raise HTTPException(
                409, "Wait for ingestion to finish before removing this repository."
            )
        ingest.wipe_repo_data(conn, repo_id)
        conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(db.REPOS_DIR / str(repo_id), ignore_errors=True)
    (db.REPOS_DIR / f"{repo_id}.zip").unlink(missing_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Metrics


@app.get("/api/repos/{repo_id}/metrics")
def repo_metrics(
    repo_id: int,
    kind: str = "repo",
    mode: str = "all",
    start: int | None = None,
    end: int | None = None,
    hashes: str = "",
    authors: str = "",
    include_untouched: bool = False,
):
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        _require_ready(row)
        flt = _build_filter(mode, start, end, hashes)
        _canonical, members = _author_scope(conn, repo_id, authors)
        payload: dict = {
            "kind": kind,
            "mode": flt.mode,
            "commits": metrics.commit_count(conn, repo_id, flt, members),
        }
        if kind == "repo":
            payload["totals"] = metrics.totals(conn, repo_id, flt, members)
        elif kind == "file":
            payload["items"] = metrics.file_metrics(
                conn, repo_id, flt, members, include_untouched
            )
        elif kind == "dir":
            payload["items"] = metrics.dir_metrics(
                conn, repo_id, flt, members, include_untouched
            )
        else:
            raise HTTPException(400, "kind must be one of: repo, file, dir.")
        if flt.mode == "list":
            payload["unknown_hashes"] = metrics.unknown_hashes(conn, repo_id, flt.hashes)
        return payload
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/series")
def repo_series(
    repo_id: int,
    bucket: str = "month",
    mode: str = "all",
    start: int | None = None,
    end: int | None = None,
    hashes: str = "",
    authors: str = "",
):
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        _require_ready(row)
        flt = _build_filter(mode, start, end, hashes)
        _canonical, members = _author_scope(conn, repo_id, authors)
        return {"bucket": bucket, "points": metrics.series(conn, repo_id, flt, bucket, members)}
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/object")
def repo_object(
    repo_id: int,
    kind: str = "file",
    path: str = "",
    mode: str = "all",
    start: int | None = None,
    end: int | None = None,
    hashes: str = "",
    authors: str = "",
    bucket: str = "day",
):
    if kind not in ("file", "dir"):
        raise HTTPException(400, "kind must be 'file' or 'dir'.")
    path = (path or "").strip()
    if kind == "file" and not path:
        raise HTTPException(400, "A file path is required.")
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        _require_ready(row)
        flt = _build_filter(mode, start, end, hashes)
        canonical, members = _author_scope(conn, repo_id, authors)
        detail = metrics.object_detail(conn, repo_id, flt, kind, path, canonical, members)
        if kind == "file":
            universe = conn.execute(
                "SELECT is_binary FROM files WHERE repo_id = ? AND path = ?", (repo_id, path)
            ).fetchone()
            detail["is_binary"] = int(universe[0]) if universe else 0
            detail["known"] = universe is not None
        detail["series"] = metrics.object_series(
            conn, repo_id, flt, kind, path, bucket, members
        )
        return detail
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Authors


@app.get("/api/repos/{repo_id}/authors")
def repo_authors(
    repo_id: int,
    mode: str = "all",
    start: int | None = None,
    end: int | None = None,
    hashes: str = "",
    authors: str = "",
):
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        _require_ready(row)
        flt = _build_filter(mode, start, end, hashes)
        canonical, members = _author_scope(conn, repo_id, authors)
        return {
            "authors": metrics.authors_overview(conn, repo_id, canonical, flt, members),
            "canonical": {str(k): v for k, v in canonical.items()},
        }
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/authors/merge")
def merge_authors_endpoint(repo_id: int, payload: dict | None = None):
    payload = payload or {}
    try:
        from_id, to_id = int(payload.get("from_id")), int(payload.get("to_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "from_id and to_id must be integer author ids.")
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        _require_ready(row)
        try:
            metrics.merge_authors(conn, repo_id, from_id, to_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/authors/unmerge")
def unmerge_author_endpoint(repo_id: int, payload: dict | None = None):
    payload = payload or {}
    try:
        from_id = int(payload.get("from_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "from_id must be an integer author id.")
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        _require_ready(row)
        metrics.unmerge_author(conn, repo_id, from_id)
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Commits & objects


@app.get("/api/repos/{repo_id}/commits")
def repo_commits(repo_id: int, search: str = "", limit: int = 50, offset: int = 0):
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        _require_ready(row)
        return metrics.commits_page(conn, repo_id, limit, offset, search)
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/objects")
def repo_objects(repo_id: int, prefix: str = "", q: str = ""):
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        _require_ready(row)
        if (q or "").strip():
            return metrics.search_objects(conn, repo_id, q)
        return metrics.browse_objects(conn, repo_id, prefix)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Frontend


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"detail": "Frontend not installed yet."}, status_code=503)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    icon = STATIC_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return Response(status_code=204)


if __name__ == "__main__":  # `python3 app.py` convenience
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000)
