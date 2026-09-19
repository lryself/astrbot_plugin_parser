import asyncio
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
import pytest

from core.segmented import SegmentedDownloader


@asynccontextmanager
async def origin(data):
    state = {
        "data": data,
        "etag": '"v1"',
        "fail": None,
        "ignore": False,
        "calls": [],
        "active": 0,
        "peak": 0,
        "mirror_chunks": 0,
    }

    async def get(request):
        start, end = map(int, request.headers["Range"][6:].split("-"))
        state["calls"].append((start, end))
        mirror = bool(request.query.get("mirror"))
        if mirror:
            await asyncio.sleep(0.01)
            if end != 0:
                state["mirror_chunks"] += 1
        if start == state["fail"] and end != 0 and not mirror:
            return web.Response(status=503)
        headers = {
            "Content-Range": f"bytes {start}-{end}/{len(state['data'])}",
            "ETag": state["etag"],
        }
        if end != 0:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                await asyncio.sleep(0.02)
            finally:
                state["active"] -= 1
            if state["ignore"]:
                return web.Response(body=state["data"])
            assert request.headers["If-Range"] == state["etag"]
        return web.Response(
            body=state["data"][start : end + 1], status=206, headers=headers
        )

    app = web.Application()
    app.router.add_get("/file", get)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/file", state
    finally:
        await runner.cleanup()


async def download(worker, url, path):
    return await worker.download(
        (url,), path, headers={}, proxy=None, timeout=2, retries=0, max_bytes=10000
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("size,enabled", [(200, False), (201, True)])
async def test_strict_threshold_and_last_short_segment(tmp_path, size, enabled):
    payload = bytes(range(size))
    async with origin(payload) as (url, state), aiohttp.ClientSession() as client:
        worker = SegmentedDownloader(client, 200, 100, 4)
        target = tmp_path / "video.mp4"
        assert await download(worker, url, target) == enabled
        if enabled:
            assert target.read_bytes() == payload
            assert sorted(x for x in state["calls"] if x != (0, 0)) == [
                (0, 99),
                (100, 199),
                (200, 200),
            ]
            assert 1 < state["peak"] <= 4 and not list(tmp_path.glob("*.segments"))
        else:
            assert not target.exists() and state["calls"] == [(0, 0)]


@pytest.mark.asyncio
async def test_resume_verified_chunks_after_failure_and_restart(tmp_path):
    async with origin(b"a" * 250) as (url, state), aiohttp.ClientSession() as client:
        target = tmp_path / "video.mp4"
        state["fail"] = 100
        with pytest.raises(aiohttp.ClientError):
            await download(SegmentedDownloader(client, 200, 100, 1), url, target)
        assert not target.exists()
        state["fail"] = None
        assert await download(SegmentedDownloader(client, 200, 100, 1), url, target)
        assert state["calls"].count((0, 99)) == 1
        assert target.read_bytes() == b"a" * 250


@pytest.mark.asyncio
async def test_new_validator_does_not_mix_previously_completed_chunks(tmp_path):
    async with origin(b"a" * 250) as (url, state), aiohttp.ClientSession() as client:
        target = tmp_path / "video.mp4"
        state["fail"] = 100
        with pytest.raises(aiohttp.ClientError):
            await download(SegmentedDownloader(client, 200, 100, 1), url, target)
        state.update(fail=None, etag='"v2"', data=b"b" * 250)
        assert await download(SegmentedDownloader(client, 200, 100, 1), url, target)
        assert state["calls"].count((0, 99)) == 2 and target.read_bytes() == b"b" * 250


@pytest.mark.asyncio
async def test_server_ignoring_range_never_publishes_corrupt_file(tmp_path):
    async with origin(b"a" * 250) as (url, state), aiohttp.ClientSession() as client:
        state["ignore"] = True
        target = tmp_path / "video.mp4"
        assert not await download(SegmentedDownloader(client, 200, 100, 4), url, target)
        assert not target.exists() and not list(tmp_path.glob("*.segments"))


@pytest.mark.asyncio
async def test_connection_cap_is_shared_across_files(tmp_path):
    async with origin(b"a" * 1000) as (url, state), aiohttp.ClientSession() as client:
        worker = SegmentedDownloader(client, 200, 100, 4)
        assert all(
            await asyncio.gather(
                download(worker, url, tmp_path / "a.mp4"),
                download(worker, url, tmp_path / "b.mp4"),
            )
        )
        assert state["peak"] == 4


@pytest.mark.asyncio
async def test_failed_segment_uses_verified_backup(tmp_path):
    async with origin(b"a" * 250) as (url, state), aiohttp.ClientSession() as client:
        state["fail"] = 100
        worker = SegmentedDownloader(client, 200, 100, 1)
        target = tmp_path / "video.mp4"
        assert await worker.download(
            (url, url + "?mirror=1"),
            target,
            headers={},
            proxy=None,
            timeout=2,
            retries=0,
            max_bytes=1000,
        )
        assert target.read_bytes() == b"a" * 250 and state["mirror_chunks"] >= 1
