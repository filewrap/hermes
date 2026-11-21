"""Tests for tools/music_generation_tasks.py — background task records and duplicate guard."""

from __future__ import annotations

import threading

from tools import music_generation_tasks as tasks


def _key(**overrides):
    base = {
        "prompt": "lofi loop", "provider": None, "model": None, "lyrics": None,
        "instrumental": None, "duration": None, "format": None, "filename": None,
        "reference_image_urls": None,
    }
    base.update(overrides)
    return tasks.build_request_key(**base)


class TestRequestKey:
    def test_same_request_same_key(self):
        assert _key() == _key()

    def test_prompt_change_changes_key(self):
        assert _key() != _key(prompt="metal riff")

    def test_reference_image_order_does_not_change_key(self):
        assert _key(reference_image_urls=["b", "a"]) == _key(reference_image_urls=["a", "b"])


class TestTaskLifecycle:
    def test_create_get_list_roundtrip(self, tmp_path):
        record = tasks.create_task(tmp_path, request_key=_key(), prompt="lofi loop",
                                   provider=None, model=None)
        assert record["status"] == "pending"
        assert tasks.get_task(tmp_path, record["id"]) is not None
        assert tasks.get_task(tmp_path, record["id"])["id"] == record["id"]
        # A pending record with no live worker reads back as interrupted.
        assert tasks.get_task(tmp_path, record["id"])["status"] == "interrupted"
        assert tasks.list_tasks(tmp_path)[0]["id"] == record["id"]

    def test_live_task_stays_active(self, tmp_path):
        record = tasks.create_task(tmp_path, request_key=_key(), prompt="x",
                                   provider=None, model=None)
        tasks.mark_live(tmp_path, record["id"])
        try:
            assert tasks.get_task(tmp_path, record["id"])["status"] == "pending"
            tasks.update_task(tmp_path, record["id"], status="running", progress="Generating")
            assert tasks.get_task(tmp_path, record["id"])["status"] == "running"
            tasks.update_task(tmp_path, record["id"], status="succeeded", audio="/tmp/x.mp3")
            assert tasks.get_task(tmp_path, record["id"])["status"] == "succeeded"
        finally:
            tasks.mark_done(tmp_path, record["id"])

    def test_unknown_task_is_none(self, tmp_path):
        assert tasks.get_task(tmp_path, "nope") is None

    def test_spawn_thread_tracks_liveness(self, tmp_path):
        record = tasks.create_task(tmp_path, request_key=_key(), prompt="x",
                                   provider=None, model=None)
        started = threading.Event()
        done = threading.Event()

        def _work():
            started.set()
            done.wait(timeout=5)

        thread = tasks.spawn_task_thread(tmp_path, record["id"], _work)
        assert started.wait(timeout=5)
        assert tasks.is_live(tmp_path, record["id"])
        done.set()
        thread.join(timeout=5)
        assert not tasks.is_live(tmp_path, record["id"])


class TestDuplicateGuard:
    def test_active_duplicate_is_returned(self, tmp_path):
        key = _key()
        record = tasks.create_task(tmp_path, request_key=key, prompt="x",
                                   provider=None, model=None)
        tasks.mark_live(tmp_path, record["id"])
        try:
            found = tasks.find_duplicate(tmp_path, key)
            assert found is not None and found["id"] == record["id"]
        finally:
            tasks.mark_done(tmp_path, record["id"])

    def test_succeeded_duplicate_is_reused(self, tmp_path):
        key = _key()
        record = tasks.create_task(tmp_path, request_key=key, prompt="x",
                                   provider=None, model=None)
        tasks.mark_live(tmp_path, record["id"])
        tasks.update_task(tmp_path, record["id"], status="succeeded", audio="/tmp/x.mp3")
        tasks.mark_done(tmp_path, record["id"])
        found = tasks.find_duplicate(tmp_path, key)
        assert found is not None and found["id"] == record["id"]

    def test_failed_duplicate_is_not_reused(self, tmp_path):
        key = _key()
        record = tasks.create_task(tmp_path, request_key=key, prompt="x",
                                   provider=None, model=None)
        tasks.mark_live(tmp_path, record["id"])
        tasks.update_task(tmp_path, record["id"], status="failed", error="boom")
        tasks.mark_done(tmp_path, record["id"])
        assert tasks.find_duplicate(tmp_path, key) is None

    def test_most_recent_active(self, tmp_path):
        first = tasks.create_task(tmp_path, request_key=_key(prompt="one"), prompt="one",
                                  provider=None, model=None)
        second = tasks.create_task(tmp_path, request_key=_key(prompt="two"), prompt="two",
                                   provider=None, model=None)
        tasks.mark_live(tmp_path, first["id"])
        tasks.mark_live(tmp_path, second["id"])
        try:
            active = tasks.most_recent_active(tmp_path)
            assert active is not None and active["id"] == second["id"]
        finally:
            tasks.mark_done(tmp_path, first["id"])
            tasks.mark_done(tmp_path, second["id"])
        # No live workers left: nothing is active (records reconcile to interrupted).
        assert tasks.most_recent_active(tmp_path) is None
