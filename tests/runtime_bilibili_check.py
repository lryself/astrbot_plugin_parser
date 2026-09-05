"""Check the installed Bilibili SDK contract, tier selection and multipart layout."""

import asyncio
import importlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.config.astrbot_config import AstrBotConfig

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE.parent))
config_module = importlib.import_module(f"{PACKAGE.name}.core.config")
bili = importlib.import_module(f"{PACKAGE.name}.core.parsers.bilibili")
login_module = importlib.import_module(f"{PACKAGE.name}.core.parsers.bilibili.login")
archive_module = importlib.import_module(f"{PACKAGE.name}.core.archive")
policy = importlib.import_module(f"{PACKAGE.name}.core.media_policy")
Downloader = importlib.import_module(f"{PACKAGE.name}.core.download").Downloader


def dash_video(quality, height, codec):
    return dict(
        id=quality,
        base_url=f"https://media.example/{height}",
        backup_url=[],
        bandwidth=height * 1000,
        codecs=codec,
        frame_rate="30",
        width=height * 16 // 9,
        height=height,
        sar="1:1",
        mime_type="video/mp4",
        segment_base={"initialization": "0-10", "index_range": "11-20"},
    )


async def check():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = AstrBotConfig(
            str(root / "config.json"),
            schema=json.loads((PACKAGE / "_conf_schema.json").read_text()),
        )
        settings["parsers_template"] = json.loads(
            (PACKAGE / "default_template.json").read_text()
        )
        cfg = config_module.PluginConfig(
            settings, SimpleNamespace(get_config=lambda: {})
        )
        cfg.cache_root = root / "cache"
        cfg.data_dir = root
        (root / "cookies").mkdir()
        downloader = Downloader(cfg)
        parser = bili.BilibiliParser(cfg, downloader)
        try:
            assert "尚未登录" in await parser.login.notice()
            parser.login._credential = SimpleNamespace(
                has_sessdata=lambda: True, check_valid=AsyncMock(return_value=False)
            )
            assert "已失效" in await parser.login.notice()
            parser.login._credential = SimpleNamespace(
                has_sessdata=lambda: True,
                check_valid=AsyncMock(side_effect=TimeoutError()),
            )
            assert "暂时无法确认" in await parser.login.notice()
            parser.login._credential = None
            keyword, matched = parser.search_url(
                "https://www.bilibili.com/video/BV17x411w7KC/?foo=bar&p=2"
            )
            assert parser.page_number(matched) == 2
            assert (await parser.prepare_request(keyword, matched, True))[
                2
            ] == "bilibili:BV17x411w7KC"
            kw, av = parser.search_url("https://bilibili.com/video/av170001?p=1")
            assert (await parser.prepare_request(kw, av, True))[
                2
            ] == "bilibili:BV17x411w7KC"
            api = SimpleNamespace(
                get_download_url=AsyncMock(
                    return_value={
                        "dash": {
                            "video": [
                                dash_video(80, 1080, "avc1"),
                                dash_video(120, 2160, "hvc1"),
                            ],
                            "audio": [],
                        }
                    }
                )
            )
            with policy.media_tier("preview"):
                assert (await parser.extract_download_urls(api))[0].endswith("/1080")
                assert "height<=1080" in downloader.video_format
                assert cfg.max_size == 300 * 1024 * 1024
            with policy.media_tier("archive"):
                assert (await parser.extract_download_urls(api))[0].endswith("/2160")
                assert downloader.video_format == "bv*+ba/b" and cfg.max_size == float(
                    "inf"
                )
            info = dict(
                bvid="BV17x411w7KC",
                title="多P课程",
                desc="",
                duration=30,
                owner={"mid": 1, "name": "teacher", "face": ""},
                stat={
                    k: 0
                    for k in [
                        "view",
                        "danmaku",
                        "reply",
                        "favorite",
                        "coin",
                        "share",
                        "like",
                    ]
                },
                pubdate=100,
                ctime=100,
                pic=None,
                pages=[
                    {"part": "第一课", "ctime": 100, "duration": 10},
                    {"part": "第二课", "ctime": 100, "duration": 20},
                ],
            )
            parser._get_video = AsyncMock(
                return_value=SimpleNamespace(get_info=AsyncMock(return_value=info))
            )

            async def write_media(*args, **kwargs):
                path = cfg.cache_dir / kwargs["file_name"]
                path.write_bytes(
                    f"{policy.download_tier.get()}-complete-video".encode()
                )
                return path

            parser.downloader = SimpleNamespace(
                streamd=write_media, checked_path=downloader.checked_path
            )
            parser.extract_download_urls = AsyncMock(
                return_value=("https://media.example/video", None)
            )
            archiver = archive_module.VideoArchiver(
                str(root / "archive"), cfg.cache_root
            )
            with policy.media_tier("archive"):
                result = await parser.parse_video(bvid="BV17x411w7KC", page_num=2)
                assert len(result.video_contents) == 2
                report = await archiver.archive(result)
                assert report.saved == 2
            directory = root / "archive" / "bilibili" / "多P课程"
            assert directory.is_dir()
            assert len(list(directory.glob("P01－第一课*.mp4"))) == 1
            assert len(list(directory.glob("P02－第二课*.mp4"))) == 1
            with policy.media_tier("preview"):
                result = await parser.parse_video(bvid="BV17x411w7KC", page_num=2)
                assert len(result.video_contents) == 1
                path = await result.video_contents[0].get_path()
                assert (
                    path.parent.name == "preview" and path.name == "BV17x411w7KC-2.mp4"
                )
                assert path.read_bytes().startswith(b"preview")
            print(
                "PASS: SDK stream selection 1080P vs 4K, separate limits, login notices, AV/BV identity, ordered multipart archive"
            )
        finally:
            await downloader.close()
            await parser.close_session()


asyncio.run(check())
