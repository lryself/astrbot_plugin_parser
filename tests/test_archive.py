import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from core.archive import VideoArchiver, save_video
from core.cache_lifecycle import CacheLifecycle
from core.data import ParseResult, Platform, SendGroup, VideoContent


@pytest.fixture(autouse=True)
def logger(monkeypatch):
    api = ModuleType("astrbot.api")
    api.logger = SimpleNamespace(exception=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)


def test_verified_copy_survives_cache_cleanup_and_title_change(tmp_path):
    source = tmp_path / "cache" / "video.mp4"
    source.parent.mkdir()
    source.write_bytes(b"complete video" * 1000)
    path, created = save_video(
        source, tmp_path / "archive", "../中文/标题", "BV123-P01"
    )
    assert created and path.read_bytes() == source.read_bytes()
    again, created = save_video(
        source, tmp_path / "archive", "changed title", "BV123-P01"
    )
    assert again == path and not created
    source.unlink()
    source.parent.rmdir()
    assert path.read_bytes() == b"complete video" * 1000
    assert not list(path.parent.glob("*.part"))
    assert path.parent == tmp_path / "archive"


def test_copy_failure_and_conflict_never_publish_partial_or_overwrite(tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"")
    with pytest.raises(OSError, match="empty"):
        save_video(source, tmp_path / "archive", "title", "id")
    assert list((tmp_path / "archive").iterdir()) == []
    source.write_bytes(b"original")
    path, _ = save_video(source, tmp_path / "archive", "title", "id")
    source.write_bytes(b"different")
    with pytest.raises(OSError, match="differs"):
        save_video(source, tmp_path / "archive", "title", "id")
    assert path.read_bytes() == b"original"
    assert len(list(path.parent.iterdir())) == 1


@pytest.mark.parametrize("relative", ["cache", "cache/subdir", "."])
def test_archive_cannot_overlap_cache(tmp_path, relative):
    with pytest.raises(ValueError):
        VideoArchiver(str(tmp_path / relative), tmp_path / "cache")


def test_archive_scope_requires_owner_and_group_intent(tmp_path):
    a = VideoArchiver(str(tmp_path / "archive"), tmp_path / "cache")

    def allowed(sender="owner", private=True, origin="group", text="归档"):
        return a.accepts(
            sender=sender,
            users=["owner"],
            private=private,
            origin=origin,
            groups=["group"],
            text=text,
        )

    assert allowed()
    assert not allowed(sender="stranger")
    assert allowed(text="")
    assert not allowed(private=False, text="")
    assert not allowed(private=False, text="不要下载这个视频")
    assert allowed(private=False, text="归档这个视频")
    assert not allowed(private=False, origin="other", text="归档")
    assert not VideoArchiver("", tmp_path / "cache").accepts(
        sender="owner", users=["owner"], private=True, origin="", groups=[], text=""
    )


@pytest.mark.asyncio
async def test_multiple_videos_dedup_groups_and_report_failed_download(tmp_path):
    async def download():
        return source

    async def failure():
        raise OSError("download failed")

    source = tmp_path / "part.mp4"
    source.write_bytes(b"media")
    v1 = VideoContent(asyncio.create_task(download()))
    v2 = VideoContent(source)
    broken = VideoContent(asyncio.create_task(failure()))
    result = ParseResult(
        Platform("bilibili", "B站"),
        title="title",
        url="https://bilibili.com/BV123",
        contents=[v1, broken],
        send_groups=[SendGroup([v1, v2])],
    )
    a = VideoArchiver(str(tmp_path / "archive"), tmp_path / "cache")
    report = await a.archive(result)
    assert (report.saved, report.existing, report.failed) == (2, 0, 1)
    assert (await v1.get_path()).is_relative_to(tmp_path / "archive")
    assert (await v1.get_path()) != (await v2.get_path())
    assert len(list((tmp_path / "archive").rglob("*.mp4"))) == 2
    report = await a.archive(result)
    assert (report.saved, report.existing, report.failed) == (0, 2, 1)


@pytest.mark.asyncio
async def test_cleanup_waits_for_inflight_work_and_preserves_archive(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    source = cache / "video.mp4"
    source.write_bytes(b"final video")
    lifecycle = CacheLifecycle()
    async with lifecycle.use():
        cleaning = asyncio.create_task(lifecycle.clean(cache))
        await asyncio.sleep(0)
        assert not cleaning.done() and source.exists()
        archived, _ = save_video(source, tmp_path / "archive", "title", "id")
    await cleaning
    assert cache.is_dir() and not source.exists()
    assert archived.read_bytes() == b"final video"


@pytest.mark.asyncio
async def test_concurrent_archival_is_idempotent(tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    a = VideoArchiver(str(tmp_path / "archive"), tmp_path / "cache")

    def result(title):
        return ParseResult(
            Platform("bilibili", "B站"),
            title=title,
            url="https://bilibili.com/BV1",
            contents=[VideoContent(source)],
        )

    reports = await asyncio.gather(
        a.archive(result("first")), a.archive(result("renamed"))
    )
    assert sum(r.saved for r in reports) == 1
    assert sum(r.existing for r in reports) == 1
    assert len(list((tmp_path / "archive").rglob("*.mp4"))) == 1


@pytest.mark.asyncio
async def test_cancelled_copy_finishes_before_cleanup_can_start(tmp_path, monkeypatch):
    import threading
    import core.archive as module

    source = tmp_path / "cache" / "video.mp4"
    source.parent.mkdir()
    source.write_bytes(b"complete media")
    started, release = threading.Event(), threading.Event()
    original = module.save_video

    def blocked_copy(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(module, "save_video", blocked_copy)
    lifecycle = CacheLifecycle()
    archiver = VideoArchiver(str(tmp_path / "archive"), source.parent)
    result = ParseResult(Platform("bilibili", "B站"), contents=[VideoContent(source)])

    async def run():
        async with lifecycle.use():
            await archiver.archive(result)

    task = asyncio.create_task(run())
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    cleaning = asyncio.create_task(lifecycle.clean(source.parent))
    await asyncio.sleep(0)
    assert not cleaning.done() and source.exists()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cleaning
    archived = list((tmp_path / "archive").rglob("*.mp4"))
    assert len(archived) == 1 and archived[0].read_bytes() == b"complete media"
    assert archived[0].stat().st_mode & 0o444 == 0o444
