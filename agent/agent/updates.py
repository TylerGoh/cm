import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
import zipfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import httpx

from agent.worker_client import WorkerClient

log = logging.getLogger("agent.updates")

CHUNK_SIZE = 1024 * 1024
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")    # same rule as the file server's job ids


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with open(path, "rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            yield chunk


async def _extract(zip_path: Path, root: Path) -> None:
    """Unzips into root in chunks, yielding between them, so a large zip neither blocks the event
    loop (heartbeats keep going) nor ignores cancellation. Refuses members outside root."""
    root = root.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (root / member.filename).resolve()
            if not target.is_relative_to(root):
                raise RuntimeError(f"zip member outside {root}: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, open(target, "wb") as out:
                while chunk := source.read(CHUNK_SIZE):
                    out.write(chunk)
                    await asyncio.sleep(0)


async def _zip_folder(folder: Path, zip_path: Path) -> None:
    """Zips folder with its own name as the top entry, so whoever unzips it gets <hash>/... intact."""
    base = folder.parent
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.mkdir(folder.name)
        for path in sorted(folder.rglob("*")):
            name = path.relative_to(base).as_posix()
            if path.is_dir():
                archive.mkdir(name)
                continue
            with open(path, "rb") as source, archive.open(name, "w", force_zip64=True) as out:
                while chunk := source.read(CHUNK_SIZE):
                    out.write(chunk)
                    await asyncio.sleep(0)


def _remove(entry: Path) -> None:
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry)
    else:
        entry.unlink()


class UpdateManager:
    """Puts artifacts and hypertrees in place and has the worker load them. The only manager that
    sends commands to the worker (through the WorkerClient), and the only agent code that talks
    to the file server.

    Artifacts: each version's zip is unpacked into <bundle_dir>/<version>, and each configured
    bundle_symlink_mapping entry is a symlink from a file path under etc_dir to a file in the bundle. With a
    bundle_symlink_default_dir, every other file in the bundle is linked at <etc_dir>/<bundle_symlink_default_dir>/
    <its name>, flattened. Only links are ever created, replaced or removed; no real file is moved
    or deleted."""

    def __init__(self, worker: WorkerClient, *, download_dir: Path, bundle_dir: Path, etc_dir: Path,
                 bundle_symlink_mapping: dict[Path, Path], bundle_symlink_default_dir: Path | None,
                 hypertree_dir: Path, download_timeout: float,
                 hypertree_timeout: float, verify_timeout: float, verify_interval: float,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        for source_rel, target_rel in bundle_symlink_mapping.items():
            for rel in (source_rel, target_rel):
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"bundle_symlink_mapping: {rel} must be relative, without '..'")
        default_dir = bundle_symlink_default_dir
        if default_dir is not None and (default_dir.is_absolute() or ".." in default_dir.parts):
            raise ValueError(f"bundle_symlink_default_dir: {bundle_symlink_default_dir} must be relative, without '..'")
        self._worker = worker
        self._download_dir = download_dir            # temporary zips (artifact and hypertree)
        self._bundle_dir = bundle_dir                # "update_bundle": one unpacked folder per version
        self._etc_dir = etc_dir
        self._bundle_symlink_mapping = bundle_symlink_mapping            # file inside a bundle -> file path under etc_dir
        self._bundle_symlink_default_dir = bundle_symlink_default_dir    # folder under etc_dir for every other file; None: off
        self._hypertree_dir = hypertree_dir          # where the worker reads and generates hypertrees
        self._download_timeout = download_timeout    # bounds fetching and unpacking a bundle
        self._hypertree_timeout = hypertree_timeout  # bounds each hypertree step
        self._verify_timeout = verify_timeout
        self._verify_interval = verify_interval
        self._http = httpx.AsyncClient(transport=transport)   # file server; transport is for tests

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- steps ---

    async def download(self, artifact_url: str, artifact_sha256: str, version: str) -> None:
        """Makes `version` the linked bundle: fetches and unpacks it unless its bundle is already
        complete, points every mapped path under etc_dir at it, then removes the other bundles.
        Safe to repeat: relinking to the same bundle changes nothing."""
        if not VERSION_PATTERN.fullmatch(version) or version in (".", ".."):
            raise ValueError(f"download: {version!r} can't be a folder name")
        bundle = self._bundle_dir / version
        if not bundle.is_dir():
            await self._fetch_bundle(artifact_url, artifact_sha256.lower(), bundle)
        self._link(bundle)
        self._remove_other_bundles(bundle)

    async def clear_hypertree(self) -> None:
        """Non-critical. Empties hypertree_dir, so after the next load the only folder in it is the
        one the worker generated. Its hash name can't be read any other way. Safe while the worker
        runs."""
        for entry in self._hypertree_dir.iterdir():
            _remove(entry)

    async def fetch_hypertree(self, hypertree_get_url: str, version: str) -> None:
        """Non-critical. Unpacks the version's hypertree zip into hypertree_dir, <hash>/ intact.
        A 404 (none yet) returns quietly; the worker then generates one while loading."""
        self._download_dir.mkdir(parents=True, exist_ok=True)
        part = self._download_dir / "hypertree.zip.part"
        try:
            async with asyncio.timeout(self._hypertree_timeout):
                async with self._http.stream("GET", hypertree_get_url) as response:
                    if response.status_code == 404:
                        log.info("no hypertree for %s on the file server yet", version)
                        return
                    response.raise_for_status()
                    with open(part, "wb") as out:
                        async for chunk in response.aiter_bytes(CHUNK_SIZE):
                            out.write(chunk)
                await _extract(part, self._hypertree_dir)
        finally:
            part.unlink(missing_ok=True)

    async def reload(self) -> None:
        """Has the running worker load what download linked: phase 1, then phase 2 straight after.
        A retry runs both again."""
        await self._worker.reload_phase1()
        await self._worker.reload_phase2()

    async def verify(self, version: str) -> None:
        """Running `version` (answering the version endpoint at all means healthy), within
        verify_timeout. Raises otherwise."""
        deadline = time.monotonic() + self._verify_timeout
        while True:
            running = await self._worker.version()
            if running == version:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"verify: expected {version}, got {running}")
            await asyncio.sleep(self._verify_interval)

    async def upload_hypertree(self, hypertree_upload_url: str, version: str) -> None:
        """Non-critical. Zips the one hash folder in hypertree_dir and uploads it. Anything other
        than exactly one folder means the clear didn't happen, so it raises rather than guess.
        A 409 (the job already has one) counts as success."""
        folders = [entry for entry in self._hypertree_dir.iterdir() if entry.is_dir()]
        if len(folders) != 1:
            raise RuntimeError(f"upload_hypertree: expected one folder in {self._hypertree_dir}, "
                               f"found {sorted(f.name for f in folders)}")
        self._download_dir.mkdir(parents=True, exist_ok=True)
        part = self._download_dir / "hypertree-upload.zip.part"
        try:
            async with asyncio.timeout(self._hypertree_timeout):
                await _zip_folder(folders[0], part)
                response = await self._http.put(hypertree_upload_url, content=_file_chunks(part))
        finally:
            part.unlink(missing_ok=True)
        if response.status_code == 409:
            log.info("file server already has a hypertree for %s", version)
            return
        if response.is_error:
            raise RuntimeError(f"upload_hypertree: HTTP {response.status_code}: {response.text[:200]}")

    # --- private ---

    async def _fetch_bundle(self, artifact_url: str, expected_sha256: str, bundle: Path) -> None:
        """Fetches the zip, checks its sha256, unpacks it into <bundle>.part, then renames that to
        <bundle>. A bundle folder therefore only exists once it is complete."""
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._bundle_dir.mkdir(parents=True, exist_ok=True)
        part_zip = self._download_dir / f"{bundle.name}.zip.part"
        part_dir = bundle.with_name(bundle.name + ".part")
        try:
            async with asyncio.timeout(self._download_timeout):
                actual = await self._fetch_to(artifact_url, part_zip)
                if actual != expected_sha256:
                    raise RuntimeError(f"download: sha256 {actual}, expected {expected_sha256}")
                if part_dir.exists():
                    shutil.rmtree(part_dir)          # left by an interrupted run
                part_dir.mkdir(parents=True)
                await _extract(part_zip, part_dir)
            part_dir.rename(bundle)
        finally:
            part_zip.unlink(missing_ok=True)

    async def _fetch_to(self, url: str, path: Path) -> str:
        """Streams url into path, hashing as it writes. Returns the sha256 hex digest."""
        digest = hashlib.sha256()
        async with self._http.stream("GET", url) as response:
            response.raise_for_status()
            with open(path, "wb") as out:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    digest.update(chunk)
                    out.write(chunk)
        return digest.hexdigest()

    def _link(self, bundle: Path) -> None:
        """Plans every link first (_plan_links), so a bad bundle or a real file in the way changes
        nothing. Then points each target at its file in the bundle. Only links are touched: a new
        link is made under a temporary name and renamed over the old one, which replaces the old
        link (an unlink and a symlink with no moment where the target is missing). The files a link
        pointed at are never touched, and a real file at a target path is never moved or deleted.
        Last, removes the default links this bundle no longer has."""
        plan = self._plan_links(bundle)
        for target, source in plan.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.link-tmp")
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(source)
            tmp.replace(target)
        self._remove_stale_default_links(keep=plan.keys())

    def _plan_links(self, bundle: Path) -> dict[Path, Path]:
        """Every link for this bundle, target -> source: the bundle_symlink_mapping entries, then, with a
        bundle_symlink_default_dir, every other file in the bundle at <default dir>/<its name>. Raises,
        before anything changes, if a mapped file is missing, two links would share a target
        (including two unmapped files with the same name), or a target is a real file."""
        plan: dict[Path, Path] = {}
        origin: dict[Path, Path] = {}                # target -> the bundle file it comes from, for errors

        def add(target: Path, source_rel: Path) -> None:
            if target in plan:
                raise RuntimeError(f"bundle {bundle.name}: {origin[target]} and {source_rel} "
                                   f"would both link to {target}")
            plan[target] = bundle / source_rel
            origin[target] = source_rel

        for source_rel, target_rel in self._bundle_symlink_mapping.items():
            if not (bundle / source_rel).is_file():
                raise RuntimeError(f"bundle {bundle.name} has no file {source_rel}")
            add(self._etc_dir / target_rel, source_rel)
        if self._bundle_symlink_default_dir is not None:
            default_dir = self._etc_dir / self._bundle_symlink_default_dir
            for path in sorted(bundle.rglob("*")):
                source_rel = path.relative_to(bundle)
                if path.is_file() and source_rel not in self._bundle_symlink_mapping:
                    add(default_dir / path.name, source_rel)
        for target in plan:
            if target.exists() and not target.is_symlink():
                raise RuntimeError(f"{target} is a real file, not a link; it is left alone")
        return plan

    def _remove_stale_default_links(self, keep: Iterable[Path]) -> None:
        """Best effort. Removes the links in the default folder that point into bundle_dir but
        aren't in this bundle's plan: files an older version had. Real files, and links pointing
        anywhere else, are left alone."""
        if self._bundle_symlink_default_dir is None:
            return
        default_dir = self._etc_dir / self._bundle_symlink_default_dir
        if not default_dir.is_dir():
            return
        kept = set(keep)
        for entry in default_dir.iterdir():
            if entry in kept or not entry.is_symlink():
                continue
            try:
                if (entry.parent / os.readlink(entry)).is_relative_to(self._bundle_dir):
                    entry.unlink()
                    log.info("removed stale link %s", entry)
            except OSError:
                log.warning("couldn't remove stale link %s", entry, exc_info=True)

    def _remove_other_bundles(self, keep: Path) -> None:
        """Best effort: a failed removal is logged and doesn't fail the download."""
        for entry in self._bundle_dir.iterdir():
            if entry == keep:
                continue
            try:
                _remove(entry)
            except OSError:
                log.warning("couldn't remove old bundle %s", entry, exc_info=True)
