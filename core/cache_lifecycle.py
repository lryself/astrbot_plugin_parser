"""Coordinate cache cleanup with concurrent message processing."""

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path


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

    async def clean(self, directory: Path):
        async with self.condition:
            await self.condition.wait_for(lambda: self.active == 0)
            operation = asyncio.create_task(asyncio.to_thread(shutil.rmtree, directory))
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise
            finally:
                directory.mkdir(parents=True, exist_ok=True)
