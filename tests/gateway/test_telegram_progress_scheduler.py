import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway.telegram_progress_scheduler import minimum_edit_spacing, scheduler_for


def test_minimum_intermediate_edit_spacing_tracks_shared_chat_load():
    assert minimum_edit_spacing(["search"]) == 10
    assert minimum_edit_spacing(["deep_research"]) == 25
    assert minimum_edit_spacing(["search", "search"]) == 25
    assert minimum_edit_spacing(["search", "deep_research", "search"]) == 45
    assert minimum_edit_spacing(["search", "search", "search", "search"]) == 60
    assert minimum_edit_spacing(["deep_research"] * 6) == 60


@pytest.mark.asyncio
async def test_duplicate_update_is_discarded_and_start_floodwait_uses_latest_terminal_state():
    calls = []

    class Adapter:
        async def send_start(self, content):
            calls.append(("start", content))
            if len([item for item in calls if item[0] == "start"]) == 1:
                return SimpleNamespace(success=False, retry_after=0.02, error="FloodWait")
            return SimpleNamespace(success=True, message_id="101")

        async def edit(self, _message_id, content):
            calls.append(("edit", content))
            return SimpleNamespace(success=True, message_id="101")

    adapter = Adapter()
    scheduler = scheduler_for(adapter, "-1001")
    message_id = await scheduler.begin(
        "task-1", "search", "Starting", send_start=adapter.send_start, edit=adapter.edit,
    )
    assert message_id is None
    scheduler.update("task-1", "Starting")  # exact duplicate: no extra operation
    scheduler.update("task-1", "Completed", terminal=True)

    for _ in range(100):
        if calls.count(("start", "Completed")):
            break
        await asyncio.sleep(0.005)

    assert calls == [("start", "Starting"), ("start", "Completed")]


@pytest.mark.asyncio
async def test_refused_edit_falls_back_to_fresh_card_instead_of_disabling():
    """A single refused edit must not freeze the only source of truth.

    The first edit is refused (e.g. strict-topic verification of a stale
    card); the scheduler re-sends the latest text as a fresh card once, and
    later updates edit that replacement. Only a second failure disables.
    """
    calls = []
    resent = asyncio.Event()
    replacement_edited = asyncio.Event()

    class Adapter:
        def __init__(self):
            self.starts = 0

        async def send_start(self, content):
            calls.append(("start", content))
            self.starts += 1
            if self.starts == 2:
                resent.set()
            return SimpleNamespace(success=True, message_id=f"card-{self.starts}")

        async def edit(self, message_id, content):
            calls.append(("edit", message_id, content))
            if message_id == "card-1":
                return SimpleNamespace(success=False, error="Strict Telegram topic edit refused")
            replacement_edited.set()
            return SimpleNamespace(success=True, message_id=message_id)

    adapter = Adapter()
    scheduler = scheduler_for(adapter, "-1003")
    message_id = await scheduler.begin(
        "task-1", "deep_research", "Starting", send_start=adapter.send_start, edit=adapter.edit,
    )
    assert message_id == "card-1"
    scheduler._last_intermediate_edit = time.monotonic() - 100
    scheduler.update("task-1", "Intermediate")

    await asyncio.wait_for(resent.wait(), timeout=5)
    assert ("start", "Intermediate") in calls
    assert scheduler._cards["task-1"].message_id == "card-2"

    scheduler._last_intermediate_edit = time.monotonic() - 100
    scheduler.update("task-1", "Final", terminal=True)
    await asyncio.wait_for(replacement_edited.wait(), timeout=5)
    assert ("edit", "card-2", "Final") in calls


@pytest.mark.asyncio
async def test_floodwait_edit_retains_latest_terminal_state_and_other_cards_serialize():
    calls = []
    entered = asyncio.Event()
    all_edits_done = asyncio.Event()
    in_flight = 0
    max_in_flight = 0
    edit_count = 0

    class Adapter:
        async def send_start(self, content):
            return SimpleNamespace(success=True, message_id=f"{content}-id")

        async def edit(self, message_id, content):
            nonlocal in_flight, max_in_flight, edit_count
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            edit_count += 1
            calls.append((message_id, content))
            if edit_count == 1:
                entered.set()
                in_flight -= 1
                return SimpleNamespace(success=False, retry_after=0.02, error="FloodWait")
            await asyncio.sleep(0.005)
            in_flight -= 1
            if edit_count >= 3:
                all_edits_done.set()
            return SimpleNamespace(success=True, message_id=message_id)

    adapter = Adapter()
    scheduler = scheduler_for(adapter, "-1002")
    await scheduler.begin("one", "search", "One", send_start=adapter.send_start, edit=adapter.edit)
    await scheduler.begin("two", "deep_research", "Two", send_start=adapter.send_start, edit=adapter.edit)
    scheduler._last_intermediate_edit = time.monotonic() - 100
    scheduler.update("one", "Intermediate")
    await asyncio.wait_for(entered.wait(), timeout=1)
    scheduler.update("one", "Final one", terminal=True)
    scheduler.update("two", "Final two", terminal=True)
    await asyncio.wait_for(all_edits_done.wait(), timeout=1)

    assert calls[0] == ("One-id", "Intermediate")
    assert calls[1][1] == "Final two"  # the other card is not blocked by One's FloodWait
    assert calls[2][1] == "Final one"
    assert max_in_flight == 1
