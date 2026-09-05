"""Coordinate cache cleanup with concurrent message processing."""

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path


async def finish_io(function, *args, **kwargs):
    """Keep lifecycle ownership until a non-cancellable file operation has settled."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise


class CacheLifecycle:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = 0

    @asynccontextmanager
    async def use(self):
        async with self.condition:
            self.active += 1
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()

    @asynccontextmanager
    async def maintenance(self):
        async with self.condition:
            await self.condition.wait_for(lambda: self.active == 0)
            yield

    async def clean(self, directory: Path):
        async with self.maintenance():
            try:
                await finish_io(shutil.rmtree, directory)
            finally:
                directory.mkdir(parents=True, exist_ok=True)
