"""Durable source-to-file receipts for skipping completed downloads."""

import hashlib
import json
import re
import sqlite3
import shutil
from uuid import uuid4
from contextlib import closing
from pathlib import Path


class ArchiveIndex:
    def __init__(self, database: Path, archive: Path, cache: Path):
        self.database, self.archive, self.cache = (
            database,
            archive.resolve(),
            cache.resolve(),
        )

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS archived_sources (source TEXT PRIMARY KEY, files TEXT NOT NULL, complete INTEGER NOT NULL)"
        )
        return connection

    def lookup(self, source: str) -> int:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT files, complete FROM archived_sources WHERE source=?", (source,)
            ).fetchone()
        if not row or not row[1]:
            return 0
        entries = json.loads(row[0])
        for entry in entries:
            path = Path(entry["archive"])
            if not path.resolve().is_relative_to(self.archive) or not path.is_file():
                return 0
            with path.open("rb") as file:
                if hashlib.file_digest(file, "sha256").hexdigest() != entry["sha256"]:
                    return 0
        return len(entries)

    def record(self, source: str, entries: list[dict[str, str]], complete: bool):
        files = []
        for entry in entries:
            with Path(entry["archive"]).open("rb") as file:
                files.append(
                    {**entry, "sha256": hashlib.file_digest(file, "sha256").hexdigest()}
                )
        with closing(self.connect()) as db:
            db.execute(
                "INSERT OR REPLACE INTO archived_sources VALUES (?, ?, ?)",
                (source, json.dumps(files), int(complete)),
            )
            db.commit()

    def restore(self, source: str):
        """Reuse successful parts locally when retrying a partially archived collection."""
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT files FROM archived_sources WHERE source=?", (source,)
            ).fetchone()
        for entry in json.loads(row[0]) if row else []:
            original, target = Path(entry["archive"]), Path(entry["cache"])
            if not original.resolve().is_relative_to(
                self.archive
            ) or not target.resolve().is_relative_to(self.cache):
                raise ValueError(
                    "Archive receipt points outside its configured directory"
                )
            if not original.is_file():
                continue
            with original.open("rb") as file:
                if hashlib.file_digest(file, "sha256").hexdigest() != entry["sha256"]:
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".restore-{uuid4().hex}")
            try:
                shutil.copyfile(original, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def remove(self, source: str):
        """Remove only files owned by this source, then remove its receipt."""
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT files FROM archived_sources WHERE source=?", (source,)
            ).fetchone()
            files = json.loads(row[0]) if row else []
            targets = [
                (Path(e[k]), root)
                for e in files
                for k, root in [("archive", self.archive), ("cache", self.cache)]
            ]
            for entry in files:
                cached = Path(entry["cache"])
                if cached.is_relative_to(self.cache / "archive"):
                    targets.append(
                        (
                            self.cache
                            / "preview"
                            / cached.relative_to(self.cache / "archive"),
                            self.cache,
                        )
                    )
            # Bilibili's completed cache names are canonical even before the first receipt.
            if match := re.fullmatch(r"bilibili:(BV[0-9A-Za-z]{10})", source):
                targets.extend(
                    (p, self.cache) for p in self.cache.rglob(f"{match[1]}-*.mp4")
                )
            for path, root in targets:
                if not path.resolve().is_relative_to(root):
                    raise ValueError(
                        "Archive receipt points outside its configured directory"
                    )
            for path, _ in targets:
                path.unlink(missing_ok=True)
            for parent in {
                p.parent
                for p, root in targets
                if root == self.archive
                and p.parent != self.archive
                and p.parent.parent != self.archive
            }:
                try:
                    parent.rmdir()
                except OSError:
                    pass  # Keep non-empty collection directories and unrelated files.
            db.execute("DELETE FROM archived_sources WHERE source=?", (source,))
            db.commit()
