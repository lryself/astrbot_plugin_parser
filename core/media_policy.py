"""Per-request media settings shared by parsers and download workers."""

from contextlib import contextmanager
from contextvars import ContextVar

download_tier = ContextVar("parser_download_tier", default="preview")


@contextmanager
def media_tier(tier: str):
    token = download_tier.set(tier)
    try:
        yield
    finally:
        download_tier.reset(token)


HEIGHTS = {
    "360P": 360,
    "480P": 480,
    "720P": 720,
    "1080P": 1080,
    "4K": 2160,
    "8K": 4320,
    "BEST": None,
}
