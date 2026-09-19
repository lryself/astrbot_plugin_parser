"""Validated HTTP byte ranges with bounded concurrency and resumable cache artifacts."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import weakref
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiofiles
from aiohttp import ClientError, ClientTimeout

from .cache_lifecycle import finish_io
from .exception import SizeLimitException


class RangeRejected(Exception):
    pass


class SegmentedDownloader:
    def __init__(self, client, threshold: int, chunk_size: int, concurrency: int):
        if threshold < 0 or chunk_size <= 0 or not 1 <= concurrency <= 16:
            raise ValueError("Invalid segmented download configuration")
        self.client, self.threshold, self.chunk_size = client, threshold, chunk_size
        self.concurrency = concurrency
        self.slots = asyncio.Semaphore(concurrency)
        self.locks = weakref.WeakValueDictionary()

    async def _probe(self, url, headers, proxy, timeout):
        started = time.monotonic()
        try:
            async with (
                self.slots,
                self.client.get(
                    url,
                    headers={
                        **headers,
                        "Range": "bytes=0-0",
                        "Accept-Encoding": "identity",
                    },
                    proxy=proxy,
                    timeout=ClientTimeout(total=min(timeout, 10)),
                ) as response,
            ):
                match = re.fullmatch(
                    r"bytes 0-0/(\d+)", response.headers.get("Content-Range", "")
                )
                if (
                    response.status != 206
                    or not match
                    or response.headers.get("Content-Encoding", "identity")
                    != "identity"
                ):
                    return None
                if len(await response.content.read(2)) != 1:
                    return None
                etag = response.headers.get("ETag", "")
                if etag.startswith('"') and etag.endswith('"'):
                    validator = ("ETag", etag)
                else:
                    modified, date = (
                        response.headers.get("Last-Modified"),
                        response.headers.get("Date"),
                    )
                    if (
                        not modified
                        or not date
                        or (
                            parsedate_to_datetime(date)
                            - parsedate_to_datetime(modified)
                        ).total_seconds()
                        < 60
                    ):
                        return None
                    validator = ("Last-Modified", modified)
                return url, int(match[1]), validator, time.monotonic() - started
        except (ClientError, TimeoutError, ValueError, TypeError):
            return None

    @staticmethod
    def _assemble(directory, target, size, chunk_size, completed):
        temporary = directory / "assembled.tmp"
        try:
            with temporary.open("wb") as output:
                for start in range(0, size, chunk_size):
                    digest = hashlib.sha256()
                    with (directory / f"{start}.chunk").open("rb") as part:
                        while chunk := part.read(1024 * 1024):
                            digest.update(chunk)
                            output.write(chunk)
                    if digest.hexdigest() != completed[str(start)]:
                        raise RangeRejected("Segment checksum changed")
                if output.tell() != size:
                    raise RangeRejected("Assembled file length mismatch")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    async def download(
        self, sources, target: Path, *, headers, proxy, timeout, retries, max_bytes
    ):
        if not self.threshold:
            return False
        lock = self.locks.setdefault(str(target), asyncio.Lock())
        async with lock:
            if target.exists():
                if target.stat().st_size > max_bytes:
                    raise SizeLimitException
                return True
            probes = await asyncio.gather(
                *(self._probe(u, headers, proxy, timeout) for u in sources)
            )
            probes = sorted(
                (p for p in probes if p is not None),
                key=lambda p: (p[2][0] != "ETag", p[3]),
            )
            if not probes:
                return False
            _, size, validator, _ = probes[0]
            if size > max_bytes:
                raise SizeLimitException
            if size <= self.threshold:
                return False
            # Only nodes proving the same complete representation may contribute ranges.
            nodes = [p[0] for p in probes if p[1:3] == (size, validator)]
            directory = target.with_name(f".{target.name}.segments")
            manifest = directory / "manifest.json"
            identity = {
                "size": size,
                "validator": list(validator),
                "chunk_size": self.chunk_size,
            }
            state = {}
            if manifest.exists():
                try:
                    state = json.loads(manifest.read_text())
                except (ValueError, OSError):
                    pass
            if state.get("identity") != identity:
                if directory.exists():
                    await finish_io(shutil.rmtree, directory)
                directory.mkdir(parents=True)
                state = {"identity": identity, "completed": {}}
            completed = state["completed"]
            checkpoint = asyncio.Lock()
            queue = asyncio.Queue()
            for start in range(0, size, self.chunk_size):
                queue.put_nowait(start)

            async def worker():
                while not queue.empty():
                    start = queue.get_nowait()
                    end = min(start + self.chunk_size, size) - 1
                    part = directory / f"{start}.chunk"
                    if (
                        part.is_file()
                        and part.stat().st_size == end - start + 1
                        and str(start) in completed
                    ):

                        def valid():
                            with part.open("rb") as f:
                                return (
                                    hashlib.file_digest(f, "sha256").hexdigest()
                                    == completed[str(start)]
                                )

                        if await finish_io(valid):
                            continue
                    attempts = max(retries + 1, len(nodes))
                    temporary = directory / f"{start}.tmp"
                    for attempt in range(attempts):
                        try:
                            async with (
                                self.slots,
                                self.client.get(
                                    nodes[attempt % len(nodes)],
                                    headers={
                                        **headers,
                                        "Range": f"bytes={start}-{end}",
                                        "If-Range": validator[1],
                                        "Accept-Encoding": "identity",
                                    },
                                    proxy=proxy,
                                    timeout=ClientTimeout(total=timeout),
                                ) as response,
                            ):
                                if response.status >= 400:
                                    raise ClientError(f"HTTP {response.status}")
                                expected = f"bytes {start}-{end}/{size}"
                                if (
                                    response.status != 206
                                    or response.headers.get("Content-Range") != expected
                                    or response.headers.get(validator[0])
                                    != validator[1]
                                    or response.headers.get(
                                        "Content-Encoding", "identity"
                                    )
                                    != "identity"
                                ):
                                    raise RangeRejected(
                                        "Range or representation changed"
                                    )
                                digest = hashlib.sha256()
                                received = 0
                                async with aiofiles.open(temporary, "wb") as output:
                                    async for chunk in response.content.iter_chunked(
                                        1024 * 1024
                                    ):
                                        received += len(chunk)
                                        if received > end - start + 1:
                                            raise RangeRejected(
                                                "Segment exceeded requested size"
                                            )
                                        digest.update(chunk)
                                        await output.write(chunk)
                                if received != end - start + 1:
                                    raise ClientError("Incomplete segment")
                            temporary.replace(part)
                            async with checkpoint:
                                completed[str(start)] = digest.hexdigest()
                                pending = manifest.with_suffix(".tmp")
                                pending.write_text(json.dumps(state))
                                pending.replace(manifest)
                            break
                        except (ClientError, TimeoutError):
                            if attempt + 1 == attempts:
                                raise
                        finally:
                            temporary.unlink(missing_ok=True)

            tasks = [
                asyncio.create_task(worker())
                for _ in range(min(self.concurrency, queue.qsize()))
            ]
            try:
                await asyncio.gather(*tasks)
                await finish_io(
                    self._assemble, directory, target, size, self.chunk_size, completed
                )
            except BaseException as exc:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if isinstance(exc, RangeRejected):
                    await finish_io(shutil.rmtree, directory)
                    return False
                raise
            await finish_io(shutil.rmtree, directory)
            return True
