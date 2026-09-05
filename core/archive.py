"""Permanent video archives, independent of the disposable parser cache."""

import asyncio
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .data import ParseResult, VideoContent


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
        self.directory = Path(directory).expanduser().resolve() if directory else None
        cache = cache_dir.resolve()
        if self.directory and (
            self.directory.is_relative_to(cache) or cache.is_relative_to(self.directory)
        ):
            raise ValueError("Archive directory and cache directory must be separate")

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
            and sender in users
            and (
                private
                or (
                    origin in groups
                    and re.match(
                        r"^\s*(?:请)?(?:归档|保存到\s*NAS)(?:\s|[:：]|这|该|视频|一下|$)",
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
                    async with self.lock:
                        operation = asyncio.create_task(
                            asyncio.to_thread(
                                save_video,
                                source,
                                self.directory / safe_name(owner.platform.name),
                                owner.title or "video",
                                identity,
                            )
                        )
                        # File I/O keeps running after task cancellation; keep the cache lease
                        # until the copy finishes, so cleanup cannot race an orphaned worker.
                        try:
                            path, created = await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            await operation
                            raise
                    content.path_task = path
                    if created:
                        report.saved += 1
                    else:
                        report.existing += 1
                except Exception:
                    report.failed += 1
                    logger.exception("Video archive failed for %s", identity)
        return report
