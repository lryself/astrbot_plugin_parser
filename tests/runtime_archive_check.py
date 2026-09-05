"""Run with the installed AstrBot dependencies; never sends a real QQ message."""

import asyncio
import importlib
import json
import re
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Json, Plain, Reply

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE.parent))
main = importlib.import_module(f"{PACKAGE.name}.main")
data = importlib.import_module(f"{PACKAGE.name}.core.data")
archive = importlib.import_module(f"{PACKAGE.name}.core.archive")
cache = importlib.import_module(f"{PACKAGE.name}.core.cache_lifecycle")
download = importlib.import_module(f"{PACKAGE.name}.core.download")


async def check():
    with tempfile.TemporaryDirectory(prefix="parser-archive-check-") as temp:
        root = Path(temp)
        old = root / "config.json"
        old.write_text(json.dumps({"clean_cron": "30 2 * * *"}))
        upgraded = AstrBotConfig(
            str(old), schema=json.loads((PACKAGE / "_conf_schema.json").read_text())
        )
        assert upgraded["archive_directory"] == "" and upgraded["archive_users"] == []
        (root / "cache").mkdir()
        (root / "web").mkdir()
        (root / "web" / "video.mp4").write_bytes(b"complete HTTP media fixture" * 1024)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            partial(SimpleHTTPRequestHandler, directory=str(root / "web")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        lifecycle = cache.CacheLifecycle()
        cfg = SimpleNamespace(
            whitelist=[],
            blacklist=[],
            require_at_in_group=False,
            enable_reply_parse=True,
            archive_users=["aiocqhttp:owner"],
            archive_groups=[],
            cache_lifecycle=lifecycle,
            cache_dir=root / "cache",
            source_max_size=10,
            download_timeout=10,
            download_retry_times=0,
        )
        downloader = download.Downloader(cfg)
        try:

            async def parse(*args):
                task = downloader.download_video(
                    f"http://127.0.0.1:{server.server_port}/video.mp4",
                    video_name="video.mp4",
                )
                return data.ParseResult(
                    data.Platform("bilibili", "B站"),
                    title="Runtime test",
                    url="https://bilibili.com/BVtest",
                    contents=[data.VideoContent(task)],
                )

            sender_calls = []

            async def fail_delivery(event, result):
                path = await result.contents[0].get_path()
                assert path.is_relative_to(root / "cache")
                assert len(list((root / "archive").rglob("*.mp4"))) == 1
                assert path.read_bytes() == (root / "web" / "video.mp4").read_bytes()
                sender_calls.append(path)
                raise RuntimeError("QQ transport unavailable")

            plugin = object.__new__(main.ParserPlugin)
            plugin.cfg = cfg
            plugin.archiver = archive.VideoArchiver(
                str(root / "archive"), cfg.cache_dir
            )
            plugin.key_pattern_list = [("b23.tv", re.compile(r"https://b23.tv/\w+"))]
            plugin.parser_map = {"b23.tv": SimpleNamespace(parse=parse)}
            plugin.debouncer = SimpleNamespace(
                hit_link=lambda *a: True, hit_resource=lambda *a: False
            )
            plugin.sender = SimpleNamespace(send_parse_result=fail_delivery)
            messages = []
            chain = [Plain("归档 https://b23.tv/test")]

            async def send(result):
                messages.append(result)

            event = SimpleNamespace(
                unified_msg_origin="default:FriendMessage:owner",
                message_str="归档 https://b23.tv/test",
                get_messages=lambda: chain,
                get_self_id=lambda: "bot",
                get_platform_name=lambda: "aiocqhttp",
                get_sender_id=lambda: "owner",
                is_private_chat=lambda: True,
                plain_result=lambda text: text,
                send=send,
            )
            for attempt in range(2):
                if attempt:
                    # A replied-to JSON card must preserve the explicit command's intent.
                    card = Json(
                        data={"meta": {"detail_1": {"qqdocurl": "https://b23.tv/test"}}}
                    )
                    chain[:] = [Reply(id="1", chain=[card]), Plain("归档")]
                    event.message_str = "归档"
                try:
                    await plugin.on_message(event)
                except RuntimeError as exc:
                    assert str(exc) == "QQ transport unavailable"
                else:
                    raise AssertionError("Expected the deliberate delivery failure")
            assert len(sender_calls) == 2
            assert messages == [
                "视频归档：新增 1，已存在 0，失败 0。",
                "视频归档：新增 0，已存在 1，失败 0。",
            ]
            await lifecycle.clean(cfg.cache_dir)
            assert len(list((root / "archive").rglob("*.mp4"))) == 1
            assert not list(cfg.cache_dir.iterdir())
            print(
                "PASS: old config, real HTTP download, explicit command, quoted JSON card, duplicate archive, failed QQ delivery, cache cleanup"
            )
        finally:
            await downloader.close()
            server.shutdown()
            thread.join()


asyncio.run(check())
