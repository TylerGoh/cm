"""Internal file server. Only the cluster manager daemon and the agents call it.

One directory per job (the job id is the worker's version string):

    <root>/<job_id>/artifact    the artifact (a zip, checked before it is stored), exactly as fetched
    <root>/<job_id>/job.json    JobInfo; written last, after the artifact
    <root>/<job_id>/hypertree   a zip of the worker's <hash>/ hypertree folder, uploaded by the first
                                agent that loads the job; stored as given, not checked

A job is stored while both are there and the artifact has job.json's size. One missing (deleted
by hand) is fetched again by the next POST, which the daemon sends regularly for every version
agents may still be pointed at.

Run: the `file-server` command. Settings come only from .env in the project folder.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"     # the project folder
BIND_HOST = "0.0.0.0"                                       # every service listens on all interfaces
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"   # as the agent's and the daemon's .env.example
CHUNK_SIZE = 1024 * 1024
JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

log = logging.getLogger("file_server")


class FileServerConfig(BaseSettings):
    """Same rules as the other services: only .env, no defaults, no unknown keys."""
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="forbid", frozen=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, dotenv_settings)          # init_settings lets tests pass values directly

    file_server_root: Path                   # one directory per job; nothing is ever deleted
    file_server_upstream_timeout: float      # seconds; below the daemon's FILE_SERVER_FETCH_TIMEOUT
    file_server_port: int


if not ENV_FILE.exists():
    raise FileNotFoundError(f"file server settings file missing: {ENV_FILE}")
config = FileServerConfig()
ROOT = config.file_server_root
UPSTREAM_TIMEOUT = config.file_server_upstream_timeout


class FetchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_url: str
    expected_sha256: str | None = None


class JobInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    sha256: str
    size: int
    upstream_url: str
    fetched_at: AwareDatetime


app = FastAPI()
_job_locks: dict[str, asyncio.Lock] = {}


def _job_dir(job_id: str) -> Path:
    # The job id becomes a directory name, so anything that could escape ROOT is refused.
    if not JOB_ID.match(job_id) or job_id in {".", ".."}:
        raise HTTPException(status_code=422, detail=f"invalid job id: {job_id!r}")
    return ROOT / job_id


def _lock(job_id: str) -> asyncio.Lock:
    if job_id not in _job_locks:
        _job_locks[job_id] = asyncio.Lock()
    return _job_locks[job_id]


def _read_info(job_dir: Path) -> JobInfo | None:
    info_path = job_dir / "job.json"
    if not info_path.exists():
        return None
    return JobInfo.model_validate_json(info_path.read_bytes())


def _stored(job_dir: Path) -> JobInfo | None:
    """The job's info while its artifact is there with the recorded size, otherwise None."""
    info = _read_info(job_dir)
    if info is None:
        return None
    artifact = job_dir / "artifact"
    if not artifact.exists() or artifact.stat().st_size != info.size:
        return None
    return info


def _mismatch(job_id: str, expected: str, got: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"artifact for {job_id} has sha256 {got}, expected {expected}")


def _zip_problem(path: Path) -> str | None:
    """None if `path` is a complete zip whose every member passes its CRC check, otherwise why
    not. Agents unzip the artifact into place, so a file that isn't one is refused here."""
    if not zipfile.is_zipfile(path):
        return "not a zip file"
    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
    except (zipfile.BadZipFile, OSError) as error:
        return f"unreadable zip: {error}"
    if bad_member is not None:
        return f"corrupt zip member: {bad_member}"
    return None


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@app.post("/jobs/{job_id}")
async def fetch_job(job_id: str, request: FetchRequest) -> JobInfo:
    """Fetches the job's artifact from upstream and stores it. Idempotent: a job that is
    already stored is returned as it is, without fetching again. With expected_sha256, an
    artifact with another hash is refused with 409, and a fetched one is not stored."""
    job_dir = _job_dir(job_id)
    expected = request.expected_sha256.lower() if request.expected_sha256 is not None else None
    async with _lock(job_id):
        existing = _stored(job_dir)
        if existing is not None:
            if expected is not None and existing.sha256 != expected:
                raise _mismatch(job_id, expected, existing.sha256)
            return existing
        if _read_info(job_dir) is not None:
            log.warning("artifact for %s is missing or incomplete; fetching it again", job_id)

        job_dir.mkdir(parents=True, exist_ok=True)
        tmp = job_dir / "artifact.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, follow_redirects=True) as client:
                async with client.stream("GET", request.upstream_url) as upstream:
                    if upstream.is_error:
                        raise HTTPException(
                            status_code=502,
                            detail=f"upstream answered {upstream.status_code} for {request.upstream_url}",
                        )
                    with open(tmp, "wb") as f:
                        async for chunk in upstream.aiter_bytes(CHUNK_SIZE):
                            f.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                        f.flush()
                        os.fsync(f.fileno())
        except httpx.HTTPError as error:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=502, detail=f"upstream fetch failed: {error!r}") from None
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        if expected is not None and digest.hexdigest() != expected:
            tmp.unlink(missing_ok=True)
            raise _mismatch(job_id, expected, digest.hexdigest())

        problem = _zip_problem(tmp)
        if problem is not None:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=502, detail=f"upstream artifact for {job_id}: {problem}")

        os.replace(tmp, job_dir / "artifact")
        info = JobInfo(
            job_id=job_id,
            sha256=digest.hexdigest(),
            size=size,
            upstream_url=request.upstream_url,
            fetched_at=datetime.now(UTC),
        )
        _write_atomic(job_dir / "job.json", info.model_dump_json().encode())
        return info


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> JobInfo:
    info = _read_info(_job_dir(job_id))
    if info is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return info


@app.get("/jobs/{job_id}/artifact")
async def get_artifact(job_id: str) -> FileResponse:
    job_dir = _job_dir(job_id)
    if _stored(job_dir) is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return FileResponse(job_dir / "artifact", media_type="application/octet-stream")


@app.get("/jobs/{job_id}/hypertree")
async def get_hypertree(job_id: str) -> FileResponse:
    path = _job_dir(job_id) / "hypertree"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no hypertree for job {job_id}")
    return FileResponse(path, media_type="application/octet-stream")


@app.put("/jobs/{job_id}/hypertree", status_code=201)
async def put_hypertree(job_id: str, request: Request) -> None:
    """Stores the job's hypertree. The first upload wins; any later one gets 409."""
    job_dir = _job_dir(job_id)
    async with _lock(job_id):
        if _read_info(job_dir) is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        path = job_dir / "hypertree"
        if path.exists():
            raise HTTPException(status_code=409, detail=f"job {job_id} already has a hypertree")
        tmp = job_dir / "hypertree.tmp"
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)


def main() -> None:
    """The `file-server` command, which systemd also runs. The app object, not an import string:
    one process, since the per-job locks live in memory."""
    logging.Formatter.converter = time.gmtime      # %(asctime)s in UTC, like every other timestamp
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # log_config=None: uvicorn's records go through the root handler set up above.
    uvicorn.run(app, host=BIND_HOST, port=config.file_server_port, log_config=None)


if __name__ == "__main__":
    main()
