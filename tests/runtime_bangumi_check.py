"""Run with AstrBot dependencies: episode routing, API envelope and permission gates."""

import asyncio
import copy
import importlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.config.astrbot_config import AstrBotConfig

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE.parent))
bili = importlib.import_module(f"{PACKAGE.name}.core.parsers.bilibili")
Config = importlib.import_module(f"{PACKAGE.name}.core.config").PluginConfig
policy = importlib.import_module(f"{PACKAGE.name}.core.media_policy")
TipException = importlib.import_module(f"{PACKAGE.name}.core.exception").TipException


class Login:
    @property
    async def credential(self):
        return None


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
        cfg = Config(settings, SimpleNamespace(get_config=lambda: {}))
        cfg.cache_root = root / "cache"
        downloads = []

        async def streamd(url, *, file_name, **kwargs):
            downloads.append(url)
            output = cfg.cache_dir / file_name
            output.write_bytes(b"complete episode")
            return output

        parser = bili.BilibiliParser(cfg, SimpleNamespace(streamd=streamd))
        parser.login = Login()
        plain = "https://www.bilibili.com/bangumi/play/ep6084344"
        for url in [plain, plain + "?share_source=QQ", "ep6084344"]:
            key, match = parser.search_url(url)
            assert (await parser.prepare_request(key, match, True))[
                2
            ] == "bilibili:ep6084344"
        parser.get_final_url = AsyncMock(return_value=plain + "?share_source=QQ")
        key, match = parser.search_url("https://b23.tv/ep6084344")
        key, match, identity = await parser.prepare_request(key, match, True)
        assert identity == "bilibili:ep6084344"
        selected = []

        def select(data, **kwargs):
            selected.append(data)
            return "https://media.example/authorized-stream", None

        parser.select_download_urls = select
        payload = {
            "video_info": {
                "is_drm": False,
                "is_preview": 0,
                "code": 0,
                "timelength": 7558868,
                "dash": {"video": []},
            },
            "play_view_business_info": {"episode_info": {"title": "正片"}},
        }
        api = SimpleNamespace(get_download_url=AsyncMock(return_value=payload))
        overview = SimpleNamespace(
            get_overview=AsyncMock(
                return_value={"title": "Sample movie", "cover": None}
            )
        )
        with (
            patch.object(bili, "Episode", return_value=api),
            patch.object(bili, "Bangumi", return_value=overview),
        ):
            with policy.media_tier("archive"):
                result = await parser.parse(key, match)
                file = await result.video_contents[0].get_path()
                assert (
                    file.name == "ep6084344.mp4"
                    and file.read_bytes() == b"complete episode"
                )
                assert result.url == plain and result.title == "Sample movie - 正片"
                assert selected == [payload["video_info"]]
            for field, value, notice in [
                ("is_drm", True, "DRM"),
                ("is_preview", 1, "试看"),
                ("code", -10403, "未提供"),
            ]:
                blocked = copy.deepcopy(payload)
                blocked["video_info"][field] = value
                api.get_download_url.return_value = blocked
                try:
                    await parser.parse(key, match)
                except TipException as exc:
                    assert notice in str(exc)
                else:
                    raise AssertionError("Restricted content was accepted")
            assert len(downloads) == 1
        await parser.close_session()
        print(
            "PASS: ep URL and shortlink identity, correct PGC envelope, complete authorized stream, DRM/trial/access rejection"
        )


asyncio.run(check())
