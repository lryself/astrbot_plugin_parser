"""Exercise the real ffmpeg merge boundary inside the AstrBot runtime."""

import asyncio
import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE.parent))
merge_av = importlib.import_module(f"{PACKAGE.name}.core.utils").merge_av


async def check():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        video, audio, output = (
            root / "video.mp4",
            root / "audio.m4a",
            root / "final.mp4",
        )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=0.2",
                "-c:v",
                "libx264",
                str(video),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.2",
                "-c:a",
                "aac",
                str(audio),
            ],
            check=True,
        )
        await merge_av(v_path=video, a_path=audio, output_path=output)
        info = json.loads(
            subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]
            )
        )
        assert {s["codec_type"] for s in info["streams"]} == {"audio", "video"}
        original = output.read_bytes()
        audio.write_bytes(b"invalid audio")
        try:
            await merge_av(v_path=output, a_path=audio, output_path=output)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Invalid audio unexpectedly merged")
        assert output.read_bytes() == original
        assert not list(root.glob(".*.mp4"))
        print(
            "PASS: real ffmpeg AV merge; failed merge preserves prior completed video"
        )


asyncio.run(check())
