"""Permanent video archives, independent of the disposable parser cache."""

import asyncio
import hashlib
import os
import re
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .data import ParseResult, VideoContent
from .archive_index import ArchiveIndex
from .cache_lifecycle import finish_io


def safe_name(value: str) -> str:
    """Return a bounded filename component without separators or control characters."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value).strip(" .")
    return value.encode("utf-8")[:120].decode("utf-8", errors="ignore") or "video"


def save_video(
    source: Path, directory: Path, title: str, identity: str
) -> tuple[Path, bool]:
    """Publish a verified copy without replacing any existing archive.

    Returns:
        The permanent path and whether this call created it.

    Raises:
        OSError: Copy, integrity, or destination validation failed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() or ".mp4"
    filename = f"{safe_name(title)}--{identity}{suffix}"
    target = directory / filename
    temporary = None
    try:
        with (
            source.open("rb") as src,
            tempfile.NamedTemporaryFile(
                dir=directory, prefix=".archive-", suffix=".part", delete=False
            ) as dst,
        ):
            temporary = Path(dst.name)
            digest = hashlib.sha256()
            size = 0
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            if not size:
                raise OSError("Video is empty")
            dst.flush()
            os.chmod(temporary, 0o644)
            os.fsync(dst.fileno())
        with temporary.open("rb") as copied:
            if hashlib.file_digest(copied, "sha256").digest() != digest.digest():
                raise OSError("Archive copy verification failed")

        # Identity survives title edits; identical media must not create a second file.
        existing = list(directory.glob(f"*--{identity}.*"))
        if len(existing) > 1:
            raise OSError("Multiple archives have the same media identity")
        if existing:
            target = existing[0]
        try:
            os.link(temporary, target)
            created = True
        except FileExistsError:
            if target.is_symlink() or not target.is_file():
                raise OSError("Archive destination is not a regular file")
            with target.open("rb") as current:
                if hashlib.file_digest(current, "sha256").digest() != digest.digest():
                    raise OSError("Existing archive differs; no file was overwritten")
            created = False
        return target, created
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass
class ArchiveReport:
    saved: int = 0
    existing: int = 0
    failed: int = 0
    files: list[dict[str, str]] = field(default_factory=list)

    def message(self) -> str:
        if not (self.saved or self.existing or self.failed):
            return "未发现可归档的视频。"
        return (
            f"视频归档：新增 {self.saved}，已存在 {self.existing}，失败 {self.failed}。"
        )


class VideoArchiver:
    def __init__(self, directory: str, cache_dir: Path):
        if directory and not Path(directory).expanduser().is_absolute():
            raise ValueError("Archive directory must be absolute")
        self.lock = asyncio.Lock()
        self.requests = {}
        self.directory = Path(directory).expanduser().resolve() if directory else None
        cache = cache_dir.resolve()
        self.index = (
            ArchiveIndex(cache.parent / "archive-index.sqlite", self.directory, cache)
            if self.directory
            else None
        )
        if self.directory and (
            self.directory.is_relative_to(cache) or cache.is_relative_to(self.directory)
        ):
            raise ValueError("Archive directory and cache directory must be separate")

    @asynccontextmanager
    async def request(self, key: str):
        entry = self.requests.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self.requests[key]

    def accepts(
        self,
        *,
        sender: str,
        users: list[str],
        private: bool,
        origin: str,
        groups: list[str],
        text: str,
    ) -> bool:
        """Require an allowed sender; group archives also need an explicit command."""
        return bool(
            self.directory
            and (sender in users or (private and "*" in users))
            and (
                private
                or (
                    origin in groups
                    and re.match(
                        r"^\s*(?:请)?(?:归档|保存到\s*NAS|重新下载)(?:\s|[:：]|这|该|视频|一下|$)",
                        text,
                        re.I,
                    )
                )
            )
        )

    async def archive(self, result: ParseResult) -> ArchiveReport:
        """Archive completed videos before any QQ delivery can fail."""
        from astrbot.api import logger

        report = ArchiveReport()
        if not self.directory:
            return report
        results = [result]
        seen: set[int] = set()
        while results:
            owner = results.pop(0)
            if owner.repost:
                results.append(owner.repost)
            contents = list(owner.contents)
            contents.extend(c for group in owner.send_groups for c in group.contents)
            index = 0
            for content in contents:
                if not isinstance(content, VideoContent) or id(content) in seen:
                    continue
                seen.add(id(content))
                index += 1
                media_id = safe_name(
                    urlparse(owner.url or "").path.rstrip("/").split("/")[-1]
                )
                identity = f"{media_id[:32]}-{owner.get_resource_id()}-P{index:02d}"
                try:
                    source = await content.get_path()
                    directory = self.directory / safe_name(owner.platform.name)
                    title = owner.title or "video"
                    if owner.extra.get("archive_collection"):
                        directory /= safe_name(title)
                        part_titles = owner.extra["archive_part_titles"]
                        title = f"P{index:02d}－{part_titles[index - 1]}"
                    async with self.lock:
                        path, created = await finish_io(
                            save_video, source, directory, title, identity
                        )
                    report.files.append({"archive": str(path), "cache": str(source)})
                    if created:
                        report.saved += 1
                    else:
                        report.existing += 1
                except Exception:
                    report.failed += 1
                    logger.exception("Video archive failed for %s", identity)
        return report
