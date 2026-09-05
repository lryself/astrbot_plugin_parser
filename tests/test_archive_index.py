from core.archive_index import ArchiveIndex


def test_receipt_skips_complete_archive_and_detects_missing_or_changed_file(tmp_path):
    archive, cache = tmp_path / "archive", tmp_path / "cache"
    archive.mkdir()
    cache.mkdir()
    video = archive / "file.mp4"
    video.write_bytes(b"complete")
    index = ArchiveIndex(tmp_path / "index.sqlite", archive, cache)
    assert index.lookup("video") == 0
    entry = {"archive": str(video), "cache": str(cache / "archive" / "video.mp4")}
    index.record("video", [entry], True)
    assert index.lookup("video") == 1
    video.write_bytes(b"changed")
    assert index.lookup("video") == 0
    video.unlink()
    assert index.lookup("video") == 0


def test_partial_collection_restores_completed_parts_without_network(tmp_path):
    archive, cache = tmp_path / "archive", tmp_path / "cache"
    archive.mkdir()
    cache.mkdir()
    part = archive / "part1.mp4"
    part.write_bytes(b"part one")
    target = cache / "archive" / "part1.mp4"
    index = ArchiveIndex(tmp_path / "index.sqlite", archive, cache)
    index.record("collection", [{"archive": str(part), "cache": str(target)}], False)
    assert index.lookup("collection") == 0
    index.restore("collection")
    assert target.read_bytes() == b"part one"


def test_forced_refresh_removes_only_selected_source_and_both_cache_tiers(tmp_path):
    archive, cache = tmp_path / "archive", tmp_path / "cache"
    folder = archive / "bilibili" / "video title"
    folder.mkdir(parents=True)
    target = folder / "P01.mp4"
    target.write_bytes(b"old")
    staging = cache / "archive" / "BV17x411w7KC-1.mp4"
    preview = cache / "preview" / staging.name
    for file in [staging, preview]:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"cache")
    other = archive / "unrelated.mp4"
    other.write_bytes(b"keep")
    index = ArchiveIndex(tmp_path / "index.sqlite", archive, cache)
    index.record(
        "bilibili:BV17x411w7KC", [{"archive": str(target), "cache": str(staging)}], True
    )
    index.remove("bilibili:BV17x411w7KC")
    assert not any(p.exists() for p in [target, staging, preview, folder])
    assert other.read_bytes() == b"keep"
    assert index.lookup("bilibili:BV17x411w7KC") == 0
