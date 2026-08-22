from __future__ import annotations

import json
import time

from dotameta.cache import SCHEMA_VERSION, Cache, digest_for


def test_roundtrip(tmp_path):
    cache = Cache(tmp_path)
    cache.set("key", {"a": 1})
    assert cache.get("key") == {"a": 1}


def test_a_miss_is_none_not_an_error(tmp_path):
    assert Cache(tmp_path).get("never written") is None


def test_a_miss_can_use_an_explicit_sentinel(tmp_path):
    sentinel = object()
    assert Cache(tmp_path).get("never written", sentinel) is sentinel


def test_expiry(tmp_path):
    cache = Cache(tmp_path, ttl_seconds=0)
    cache.set("key", [1])
    time.sleep(0.01)
    assert cache.get("key") is None


def test_disabled_cache_neither_reads_nor_writes(tmp_path):
    cache = Cache(tmp_path, enabled=False)
    cache.set("key", [1])
    assert cache.get("key") is None
    assert not list(tmp_path.glob("*.json"))


def test_corrupt_json_is_a_miss_not_a_crash(tmp_path):
    cache = Cache(tmp_path)
    cache.set("key", [1])
    path = next(tmp_path.glob("*.json"))
    path.write_text("{not json", encoding="utf-8")
    assert cache.get("key") is None


def test_valid_json_with_the_wrong_shape_is_a_miss(tmp_path):
    """Regression: a well-formed but foreign document used to raise."""
    cache = Cache(tmp_path)
    cache.set("key", [1])
    path = next(tmp_path.glob("*.json"))
    path.write_text(json.dumps({"totally": "different"}), encoding="utf-8")
    assert cache.get("key") is None


def test_a_future_timestamp_is_not_trusted(tmp_path):
    cache = Cache(tmp_path)
    cache.set("key", [1])
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stored_at"] = time.time() + 10_000
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.get("key") is None


def test_clear_never_deletes_files_we_did_not_write(tmp_path):
    """Regression: --cache-dir is user-supplied and clear() globbed *.json.

    Pointed at a directory holding someone's own data, it deleted all of it.
    """
    cache = Cache(tmp_path)
    cache.set("ours", [1])

    innocent = tmp_path / "important-config.json"
    innocent.write_text(json.dumps({"keep": "me"}), encoding="utf-8")
    # Even a file named like one of ours, if its envelope does not match.
    impostor = tmp_path / f"{'0' * 32}.json"
    impostor.write_text(json.dumps({"schema": SCHEMA_VERSION}), encoding="utf-8")

    assert cache.clear() == 1
    assert innocent.exists()
    assert impostor.exists()


def test_clear_ignores_an_entry_whose_digest_does_not_match_its_name(tmp_path):
    cache = Cache(tmp_path)
    cache.set("ours", [1])
    path = next(p for p in tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["digest"] = digest_for("something else")
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.clear() == 0
    assert path.exists()


def test_get_ignores_an_entry_renamed_to_another_keys_path(tmp_path):
    cache = Cache(tmp_path)
    cache.set("first", [1])
    original = tmp_path / f"{digest_for('first')}.json"
    renamed = tmp_path / f"{digest_for('second')}.json"
    original.rename(renamed)

    assert cache.get("second") is None


def test_writers_do_not_share_a_fixed_temp_file(tmp_path):
    """Two processes writing the same key must not interleave into one file."""
    cache = Cache(tmp_path)
    cache.set("key", [1])
    cache.set("key", [2])
    assert cache.get("key") == [2]
    # No stray temp files left behind.
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".tmp-*"))


def test_a_write_failure_is_not_fatal(tmp_path, monkeypatch):
    """The HTTP response is already paid for; losing the cache must not lose it."""
    cache = Cache(tmp_path / "nested")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("dotameta.cache.tempfile.mkstemp", boom)
    cache.set("key", [1])  # must not raise
    assert cache.get("key") is None


def test_entries_counts_only_our_own(tmp_path):
    cache = Cache(tmp_path)
    cache.set("a", [1])
    cache.set("b", [2])
    (tmp_path / "stranger.json").write_text("{}", encoding="utf-8")
    assert cache.entries() == 2
