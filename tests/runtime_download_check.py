"""Verify in-flight HTTP media is never reused as a completed cache entry."""

import asyncio
import importlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE.parent))
Downloader = importlib.import_module(f"{PACKAGE.name}.core.download").Downloader
CacheLifecycle = importlib.import_module(
    f"{PACKAGE.name}.core.cache_lifecycle"
).CacheLifecycle


async def check():
    begun, release = threading.Event(), threading.Event()
    chunk = b"a" * (1024 * 1024)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(chunk) * 2))
            self.end_headers()
            self.wfile.write(chunk)
            self.wfile.flush()
            begun.set()
            release.wait(5)
            self.wfile.write(chunk)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as temp:
        cfg = SimpleNamespace(
            cache_dir=Path(temp),
            cache_lifecycle=CacheLifecycle(),
            source_max_size=10,
            download_timeout=10,
            download_retry_times=0,
        )
        downloader = Downloader(cfg)
        tasks = []
        try:
            url = f"http://127.0.0.1:{server.server_port}/video"
            tasks.append(downloader.download_video(url, video_name="video.mp4"))
            assert await asyncio.to_thread(begun.wait, 5)
            for _ in range(100):
                if any(p.stat().st_size for p in Path(temp).iterdir()):
                    break
                await asyncio.sleep(0.01)
            tasks.append(downloader.download_video(url, video_name="video.mp4"))
            await asyncio.sleep(0.05)
            assert not tasks[1].done(), "Second request reused an incomplete cache file"
            release.set()
            paths = await asyncio.gather(*tasks)
            assert all(p.read_bytes() == chunk * 2 for p in paths)
            assert list(Path(temp).iterdir()) == [Path(temp) / "video.mp4"]
            print("PASS: concurrent HTTP requests only expose complete cache files")
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await downloader.close()
            server.shutdown()


asyncio.run(check())
