"""UpdateManager against a mock file server and worker."""

import asyncio
import hashlib
import io
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from agent.updates import UpdateManager

type Handler = Callable[[httpx.Request], httpx.Response]


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.running = "job_1"

    async def reload_phase1(self) -> None:
        self.calls.append("phase1")

    async def reload_phase2(self) -> None:
        self.calls.append("phase2")

    async def version(self) -> str | None:
        return self.running


def manager(tmp_path: Path, handler: Handler, worker: FakeWorker | None = None,
            default_dir: Path | None = None) -> UpdateManager:
    (tmp_path / "hypertree").mkdir(exist_ok=True)
    return UpdateManager(
        worker or FakeWorker(),  # type: ignore[arg-type]
        download_dir=tmp_path / "downloads", bundle_dir=tmp_path / "update_bundle",
        etc_dir=tmp_path / "etc",
        bundle_symlink_mapping={Path("vision/model.bin"): Path("vision/model.bin"),
                                Path("spam/rules.txt"): Path("routers/spam/rules.txt")},
        bundle_symlink_default_dir=default_dir,
        hypertree_dir=tmp_path / "hypertree", download_timeout=5, hypertree_timeout=5,
        verify_timeout=0.2, verify_interval=0.05, transport=httpx.MockTransport(handler),
    )


def serve(artifacts: dict[str, bytes]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        data = artifacts.get(request.url.path)
        return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
    return handler


def bundle(version: str) -> bytes:
    return zip_bytes({"vision/model.bin": version.encode(), "spam/rules.txt": version.encode()})


def test_download_links_each_file_and_leaves_the_folders_real(tmp_path: Path) -> None:
    (tmp_path / "etc/vision").mkdir(parents=True)
    (tmp_path / "etc/vision/default.bin").write_text("default")  # not mapped: left alone
    data = bundle("job_2")
    m = manager(tmp_path, serve({"/jobs/job_2/artifact": data}))

    asyncio.run(m.download("http://fs/jobs/job_2/artifact", hashlib.sha256(data).hexdigest(), "job_2"))

    model = tmp_path / "etc/vision/model.bin"
    assert model.is_symlink() and model.read_text() == "job_2"
    assert not (tmp_path / "etc/vision").is_symlink()            # the folder stays real
    assert (tmp_path / "etc/vision/default.bin").read_text() == "default"
    assert (tmp_path / "etc/routers/spam/rules.txt").read_text() == "job_2"
    assert not list((tmp_path / "downloads").iterdir())          # no leftover zip


def test_a_real_file_at_a_target_is_refused_and_left_alone(tmp_path: Path) -> None:
    (tmp_path / "etc/vision").mkdir(parents=True)
    (tmp_path / "etc/vision/model.bin").write_text("real file")
    data = bundle("job_2")
    m = manager(tmp_path, serve({"/a": data}))
    with pytest.raises(RuntimeError, match="real file, not a link"):
        asyncio.run(m.download("http://fs/a", hashlib.sha256(data).hexdigest(), "job_2"))
    assert (tmp_path / "etc/vision/model.bin").read_text() == "real file"
    assert not (tmp_path / "etc/routers/spam/rules.txt").exists()   # nothing linked


def test_a_newer_download_relinks_and_removes_the_old_bundle(tmp_path: Path) -> None:
    one, two = bundle("job_2"), bundle("job_3")
    m = manager(tmp_path, serve({"/a/2": one, "/a/3": two}))
    asyncio.run(m.download("http://fs/a/2", hashlib.sha256(one).hexdigest(), "job_2"))
    old_target = (tmp_path / "etc/vision/model.bin").resolve()
    asyncio.run(m.download("http://fs/a/3", hashlib.sha256(two).hexdigest(), "job_3"))
    assert (tmp_path / "etc/vision/model.bin").read_text() == "job_3"
    assert [p.name for p in (tmp_path / "update_bundle").iterdir()] == ["job_3"]
    assert old_target.parent.parent.name == "job_2"              # relinked away from job_2 before it was removed


def test_repeating_a_download_fetches_nothing(tmp_path: Path) -> None:
    data = bundle("job_2")
    fetches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetches.append(request.url.path)
        return httpx.Response(200, content=data)

    m = manager(tmp_path, handler)
    sha = hashlib.sha256(data).hexdigest()
    asyncio.run(m.download("http://fs/a", sha, "job_2"))
    asyncio.run(m.download("http://fs/a", sha, "job_2"))
    assert fetches == ["/a"]


def test_a_wrong_sha256_changes_nothing(tmp_path: Path) -> None:
    m = manager(tmp_path, serve({"/a": bundle("job_2")}))
    with pytest.raises(RuntimeError, match="sha256"):
        asyncio.run(m.download("http://fs/a", "0" * 64, "job_2"))
    assert not (tmp_path / "etc/vision/model.bin").exists()
    assert not (tmp_path / "update_bundle/job_2").exists()


def test_a_bundle_missing_a_mapped_path_changes_nothing(tmp_path: Path) -> None:
    data = zip_bytes({"vision/model.bin": b"x"})                  # no spam/rules.txt
    m = manager(tmp_path, serve({"/a": data}))
    with pytest.raises(RuntimeError, match="has no file spam"):
        asyncio.run(m.download("http://fs/a", hashlib.sha256(data).hexdigest(), "job_2"))
    assert not (tmp_path / "etc/vision/model.bin").exists()


def test_a_bad_bundle_symlink_mapping_is_refused() -> None:
    with pytest.raises(ValueError, match="relative"):
        UpdateManager(FakeWorker(), download_dir=Path("d"), bundle_dir=Path("b"), etc_dir=Path("e"),  # type: ignore[arg-type]
                      bundle_symlink_mapping={Path("a"): Path("../escape")}, bundle_symlink_default_dir=None,
                      hypertree_dir=Path("h"), download_timeout=1, hypertree_timeout=1,
                      verify_timeout=1, verify_interval=1)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


DEFAULTS = Path("defaults")


def test_unmapped_files_are_linked_by_name_in_the_default_dir(tmp_path: Path) -> None:
    data = zip_bytes({"vision/model.bin": b"model", "spam/rules.txt": b"rules",
                      "top.conf": b"top", "deep/er/extra.bin": b"extra"})
    m = manager(tmp_path, serve({"/a": data}), default_dir=DEFAULTS)
    asyncio.run(m.download("http://fs/a", sha(data), "job_2"))

    defaults = tmp_path / "etc/defaults"
    assert sorted(p.name for p in defaults.iterdir()) == ["extra.bin", "top.conf"]   # mapped files aren't repeated
    assert (defaults / "extra.bin").is_symlink() and (defaults / "extra.bin").read_text() == "extra"
    assert (defaults / "top.conf").read_text() == "top"
    assert (tmp_path / "etc/vision/model.bin").read_text() == "model"


def test_two_unmapped_files_with_one_name_change_nothing(tmp_path: Path) -> None:
    data = zip_bytes({"vision/model.bin": b"m", "spam/rules.txt": b"r", "a/x.conf": b"1", "b/x.conf": b"2"})
    m = manager(tmp_path, serve({"/a": data}), default_dir=DEFAULTS)
    with pytest.raises(RuntimeError, match="a/x.conf and b/x.conf would both link to"):
        asyncio.run(m.download("http://fs/a", sha(data), "job_2"))
    assert not (tmp_path / "etc/vision/model.bin").exists()      # checked before anything was linked


def test_a_real_file_in_the_default_dir_is_refused_and_left_alone(tmp_path: Path) -> None:
    (tmp_path / "etc/defaults").mkdir(parents=True)
    (tmp_path / "etc/defaults/top.conf").write_text("real file")
    data = zip_bytes({"vision/model.bin": b"m", "spam/rules.txt": b"r", "top.conf": b"top"})
    m = manager(tmp_path, serve({"/a": data}), default_dir=DEFAULTS)
    with pytest.raises(RuntimeError, match="real file, not a link"):
        asyncio.run(m.download("http://fs/a", sha(data), "job_2"))
    assert (tmp_path / "etc/defaults/top.conf").read_text() == "real file"


def test_a_newer_version_removes_only_its_stale_default_links(tmp_path: Path) -> None:
    old = zip_bytes({"vision/model.bin": b"m", "spam/rules.txt": b"r", "keep.conf": b"2", "gone.conf": b"2"})
    new = zip_bytes({"vision/model.bin": b"m", "spam/rules.txt": b"r", "keep.conf": b"3"})
    defaults = tmp_path / "etc/defaults"
    defaults.mkdir(parents=True)
    (defaults / "mine.conf").write_text("real file")              # a real file: never touched
    (tmp_path / "elsewhere.conf").write_text("other")
    (defaults / "other.conf").symlink_to(tmp_path / "elsewhere.conf")   # a link outside bundle_dir: never touched
    m = manager(tmp_path, serve({"/2": old, "/3": new}), default_dir=DEFAULTS)

    asyncio.run(m.download("http://fs/2", sha(old), "job_2"))
    assert (defaults / "gone.conf").is_symlink()
    asyncio.run(m.download("http://fs/3", sha(new), "job_3"))

    assert sorted(p.name for p in defaults.iterdir()) == ["keep.conf", "mine.conf", "other.conf"]
    assert (defaults / "keep.conf").read_text() == "3"
    assert (defaults / "mine.conf").read_text() == "real file"
    assert (defaults / "other.conf").read_text() == "other"


def test_without_a_default_dir_unmapped_files_are_not_linked(tmp_path: Path) -> None:
    data = zip_bytes({"vision/model.bin": b"m", "spam/rules.txt": b"r", "top.conf": b"top"})
    m = manager(tmp_path, serve({"/a": data}))
    asyncio.run(m.download("http://fs/a", sha(data), "job_2"))
    assert sorted(p.relative_to(tmp_path / "etc").as_posix() for p in (tmp_path / "etc").rglob("*")
                  if p.is_symlink()) == ["routers/spam/rules.txt", "vision/model.bin"]


def test_a_bad_default_dir_is_refused() -> None:
    for bad in (Path("/abs"), Path("../escape")):
        with pytest.raises(ValueError, match="bundle_symlink_default_dir"):
            UpdateManager(FakeWorker(), download_dir=Path("d"), bundle_dir=Path("b"), etc_dir=Path("e"),  # type: ignore[arg-type]
                          bundle_symlink_mapping={}, bundle_symlink_default_dir=bad,
                          hypertree_dir=Path("h"), download_timeout=1, hypertree_timeout=1,
                          verify_timeout=1, verify_interval=1)


def test_reload_calls_both_phases_in_order(tmp_path: Path) -> None:
    worker = FakeWorker()
    asyncio.run(manager(tmp_path, serve({}), worker).reload())
    assert worker.calls == ["phase1", "phase2"]


def test_verify(tmp_path: Path) -> None:
    m = manager(tmp_path, serve({}))
    asyncio.run(m.verify("job_1"))
    with pytest.raises(RuntimeError, match="expected job_9"):
        asyncio.run(m.verify("job_9"))


def test_hypertree_round_trip(tmp_path: Path) -> None:
    stored: dict[str, bytes] = {}

    def file_server(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            if "tree" in stored:
                return httpx.Response(409)
            stored["tree"] = request.read()
            return httpx.Response(201)
        return httpx.Response(200, content=stored["tree"]) if "tree" in stored else httpx.Response(404)

    m = manager(tmp_path, file_server)
    hypertree = tmp_path / "hypertree"
    (hypertree / "old_hash").mkdir()
    asyncio.run(m.clear_hypertree())
    assert not list(hypertree.iterdir())

    asyncio.run(m.fetch_hypertree("http://fs/jobs/j/hypertree", "j"))     # 404: none yet, quiet
    (hypertree / "a1b2c3/tier1").mkdir(parents=True)                       # the worker generated one
    (hypertree / "a1b2c3/tier1/data.bin").write_bytes(b"tree")
    asyncio.run(m.upload_hypertree("http://fs/jobs/j/hypertree", "j"))
    asyncio.run(m.upload_hypertree("http://fs/jobs/j/hypertree", "j"))    # 409 counts as success

    asyncio.run(m.clear_hypertree())
    asyncio.run(m.fetch_hypertree("http://fs/jobs/j/hypertree", "j"))
    assert (hypertree / "a1b2c3/tier1/data.bin").read_bytes() == b"tree"   # hash folder intact


def test_upload_refuses_when_the_folder_was_not_cleared(tmp_path: Path) -> None:
    m = manager(tmp_path, serve({}))
    (tmp_path / "hypertree/one").mkdir()
    (tmp_path / "hypertree/two").mkdir()
    with pytest.raises(RuntimeError, match="expected one folder"):
        asyncio.run(m.upload_hypertree("http://fs/x", "j"))
