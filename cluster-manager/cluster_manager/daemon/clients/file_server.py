"""The daemon's client for the internal file server (file_server/file_server.py), and the only
daemon code that knows its API. Upstream and the file server call a version a job, so this module
says `job_id`; everywhere else says `version`."""

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class JobInfo(BaseModel):
    """The file server's record of a stored job (its job.json)."""
    model_config = ConfigDict(frozen=True, extra="ignore")

    job_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")      # checked here, where the hash enters
    size: int
    upstream_url: str
    fetched_at: AwareDatetime


class FileServerError(Exception):
    """The file server answered with an error status."""


class ArtifactMismatch(FileServerError):
    """The artifact upstream serves has another sha256 than the one expected; nothing was stored."""


class FileServerClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float,
        fetch_timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._fetch_timeout = fetch_timeout
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=timeout, transport=transport)

    def artifact_url(self, job_id: str) -> str:
        """Where agents download the job's artifact from."""
        return f"{self._base_url}/jobs/{job_id}/artifact"

    def hypertree_url(self, job_id: str) -> str:
        """Where agents fetch (GET) and upload (PUT) the job's hypertree."""
        return f"{self._base_url}/jobs/{job_id}/hypertree"

    async def fetch(self, upstream_url: str, job_id: str, *, expected_sha256: str | None = None) -> JobInfo:
        """Has the file server store the job's artifact from `upstream_url`, and returns it with
        its sha256. Idempotent: a job already stored comes back as it is, and one whose artifact
        is gone is fetched again. With `expected_sha256`, raises ArtifactMismatch when the
        artifact has another hash. Raises on any failure."""
        response = await self._http.post(
            f"/jobs/{job_id}",
            json={"upstream_url": upstream_url, "expected_sha256": expected_sha256},
            timeout=self._fetch_timeout,
        )
        if response.status_code == 409:
            raise ArtifactMismatch(response.json().get("detail", response.text[:200]))
        _raise_for_status(response)
        return JobInfo.model_validate_json(response.content)

    async def aclose(self) -> None:
        await self._http.aclose()


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_error:
        raise FileServerError(
            f"{response.request.method} {response.request.url.path}: "
            f"{response.status_code} {response.text[:200]}"
        )
