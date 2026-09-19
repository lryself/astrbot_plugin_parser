"""Real HTTP failure/backup selection and stable identity without signed-URL persistence."""

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
module = importlib.import_module(f"{PACKAGE.name}.core.download")
CacheLifecycle = importlib.import_module(
    f"{PACKAGE.name}.core.cache_lifecycle"
).CacheLifecycle


async def check():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path.startswith("/bad"):
                self.send_response(503)
                self.end_headers()
                return
            data = b"complete alternate media"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as temp:
        cfg = SimpleNamespace(
            cache_dir=Path(temp),
            cache_lifecycle=CacheLifecycle(),
            max_size=1024,
            download_timeout=2,
            download_retry_times=0,
        )
        d = module.Downloader(cfg)
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            source = module.DownloadSource(
                (url + "/bad?signature=first", url + "/good?signature=first"),
                "/cid/audio.m4s",
            )
            result = await d.download_audio(source)
            assert result.read_bytes() == b"complete alternate media"
            assert calls == ["/bad?signature=first", "/good?signature=first"]
            changed = module.DownloadSource(
                (url + "/good?signature=renewed",), "/cid/audio.m4s"
            )
            assert await d.download_audio(changed) == result and len(calls) == 2
            assert not list(Path(temp).glob("*.part"))
            print(
                "PASS: unavailable primary uses SDK backup, refreshed signatures reuse completed stream"
            )
        finally:
            await d.close()
            server.shutdown()


asyncio.run(check())
