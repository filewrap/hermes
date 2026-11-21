"""Tests for the gateway-side clarify primitive (tools/clarify_gateway.py).

The clarify tool needs to ask the user a question and block the agent
thread until they respond.  These tests cover the module-level state
machine: register, wait, resolve via button, resolve via text-fallback,
"Other"-button text-capture flip, timeout, session boundary cleanup.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from tests.waiting import wait_until


def _clear_clarify_state():
    """Reset module-level state between tests."""
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


class TestClarifyPrimitive:
    """Core register/wait/resolve mechanics."""

    def setup_method(self):
        _clear_clarify_state()

    def test_button_choice_resolves_wait(self):
        """resolve_gateway_clarify unblocks wait_for_response with the chosen string."""
        from tools import clarify_gateway as cm

        cm.register("id1", "sk1", "Pick one", ["A", "B", "C"])

        def resolver():
            time.sleep(0.05)
            cm.resolve_gateway_clarify("id1", "B")

        threading.Thread(target=resolver).start()
        result = cm.wait_for_response("id1", timeout=10.0)
        assert result == "B"

    def test_first_resolution_wins(self):
        """A late cancellation must not overwrite an already-selected choice."""
        from tools import clarify_gateway as cm

        entry = cm.register("id-race", "sk-race", "Pick one", ["A", "B"])

        assert cm.resolve_gateway_clarify("id-race", "A") is True
        assert cm.resolve_gateway_clarify("id-race", "") is False
        assert entry.response == "A"

    def test_get_pending_tracks_lifecycle(self):
        """Adapters read a pending entry through ``get_pending``; answered or unknown ids read None."""
        from tools import clarify_gateway as cm

        entry = cm.register("id-pend", "sk-pend", "Pick", ["A", "B"])
        assert cm.get_pending("id-pend") is entry
        cm.resolve_gateway_clarify("id-pend", "A")
        assert cm.get_pending("id-pend") is None
        assert cm.get_pending("never-registered") is None

    def test_open_ended_auto_awaits_text(self):
        """Clarify with no choices is in text-capture mode immediately."""
        from tools import clarify_gateway as cm

        entry = cm.register("id2", "sk2", "Free form?", None)
        assert entry.awaiting_text is True

        # get_pending_for_session returns the entry so the gateway
        # text-intercept can find it.
        pending = cm.get_pending_for_session("sk2")
        assert pending is not None
        assert pending.clarify_id == "id2"


    def test_include_choice_prompts_returns_multi_choice_entry(self):
        """Gateway typed replies must see active choice prompts too."""
        from tools import clarify_gateway as cm

        cm.register("id3b", "sk3b", "Pick", ["X", "Y"])
        pending = cm.get_pending_for_session("sk3b", include_choice_prompts=True)
        assert pending is not None
        assert pending.clarify_id == "id3b"


    def test_clear_session_cancels_pending_entries(self):
        """clear_session unblocks blocked threads."""
        from tools import clarify_gateway as cm

        cm.register("id7", "sk7", "Q?", ["A"])

        def waiter():
            return cm.wait_for_response("id7", timeout=10.0)

        with ThreadPoolExecutor(1) as pool:
            fut = pool.submit(waiter)
            time.sleep(0.05)
            cancelled = cm.clear_session("sk7")
            assert cancelled == 1
            result = fut.result(timeout=10.0)
            assert result == cm.CANCELLED


    def test_clear_session_preserves_resolved_response(self):
        """clear_session must not clobber an answer that already won.

        First-writer-wins (doryani-ai on PR #75732): a button callback that
        resolved the entry before session cleanup must keep its response.
        clear_session only cancels entries whose event is not yet set, so
        the racing waiter observes the real answer, not the empty sentinel.
        """
        from tools import clarify_gateway as cm

        cm.register("id-race", "sk-race", "Pick one", ["A", "B"])

        def waiter():
            return cm.wait_for_response("id-race", timeout=10.0)

        with ThreadPoolExecutor(1) as pool:
            fut = pool.submit(waiter)
            time.sleep(0.05)
            # Button wins the race first...
            assert cm.resolve_gateway_clarify("id-race", "B") is True
            # ...then session cleanup runs before the waiter wakes.
            cancelled = cm.clear_session("sk-race")
            assert cancelled == 0
            result = fut.result(timeout=10.0)
            # The real answer must survive cleanup, not the "" cancellation.
            assert result == "B"


    def test_session_index_isolation(self):
        """Entries from different sessions don't leak across get_pending lookups."""
        from tools import clarify_gateway as cm

        cm.register("idA", "alpha", "Q?", None)  # auto-await text
        cm.register("idB", "beta", "Q?", None)   # auto-await text

        a = cm.get_pending_for_session("alpha")
        b = cm.get_pending_for_session("beta")
        assert a is not None and a.clarify_id == "idA"
        assert b is not None and b.clarify_id == "idB"



class TestGatewayTextIntercept:
    """The gateway's _handle_message intercepts text replies to pending clarifies."""

    def setup_method(self):
        _clear_clarify_state()

    def test_get_pending_for_session_returns_oldest_text_awaiting(self):
        """When two clarifies are pending, get_pending_for_session returns the
        first that is awaiting_text (the older one if both)."""
        from tools import clarify_gateway as cm

        # Older multi-choice (not awaiting text)
        cm.register("first", "sk", "Q1?", ["A"])
        # Newer open-ended (awaiting text)
        cm.register("second", "sk", "Q2?", None)

        pending = cm.get_pending_for_session("sk")
        # The newer one is awaiting text; the older isn't.
        assert pending is not None
        assert pending.clarify_id == "second"

        # Now flip the first to text mode too.  Both are awaiting text,
        # FIFO returns the older one.
        cm.mark_awaiting_text("first")
        pending2 = cm.get_pending_for_session("sk")
        assert pending2 is not None
        assert pending2.clarify_id == "first"
    def test_text_fallback_enables_awaiting_text_for_multi_choice(self):
        """When base send_clarify renders choices as text, mark_awaiting_text
        is called so the gateway text-intercept can capture the reply."""
        from tools import clarify_gateway as cm

        entry = cm.register("id-tf", "sk-tf", "Pick one", ["A", "B", "C"])
        # Initially, multi-choice does NOT await text (button path)
        assert entry.awaiting_text is False

        # After the base send_clarify text fallback calls mark_awaiting_text:
        flipped = cm.mark_awaiting_text("id-tf")
        assert flipped is True

        # Now get_pending_for_session should find it
        pending = cm.get_pending_for_session("sk-tf")
        assert pending is not None
        assert pending.clarify_id == "id-tf"
        
        # Clean up
        cm.clear_session("sk-tf")


class TestCoverageGaps:
    """Cover remaining branches: unknown-id wait, timeout config exception."""

    def setup_method(self):
        _clear_clarify_state()

    def test_wait_for_response_unknown_id_returns_none(self):
        """wait_for_response on a non-existent id returns None immediately."""
        from tools import clarify_gateway as cm

        assert cm.wait_for_response("nonexistent-id", timeout=0.1) is None


    def test_get_clarify_timeout_returns_fixed_ten_minutes(self, monkeypatch):
        """get_clarify_timeout is fixed at ten minutes even when load_config raises."""
        from tools import clarify_gateway as cm

        monkeypatch.setattr("hermes_cli.config.load_config",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cm.get_clarify_timeout() == 600


class TestClarifyTimeoutResolution:
    """resolve_clarify_timeout is the single source of truth for the clarify
    timeout, shared by the CLI, TUI/desktop, and messaging-gateway paths: fixed
    at ten minutes, no config key changes it."""


    def test_timeout_is_fixed_ten_minutes_regardless_of_config(self):
        """Neither the legacy ``clarify.timeout`` nor ``agent.clarify_timeout``
        moves the timeout: a clarify poll always expires after ten minutes."""
        from tools import clarify_gateway as cm

        assert cm.resolve_clarify_timeout({}) == 600
        assert cm.resolve_clarify_timeout({"agent": {"clarify_timeout": 900}}) == 600
        assert cm.resolve_clarify_timeout({"clarify": {"timeout": 120}}) == 600
        assert cm.resolve_clarify_timeout({"agent": {"clarify_timeout": 0}}) == 600
        assert cm.resolve_clarify_timeout({"clarify": {"timeout": -1}}) == 600


class TestUnlimitedWait:
    """timeout <= 0 makes wait_for_response block until the answer arrives
    instead of auto-skipping."""

    def setup_method(self):
        _clear_clarify_state()

    def test_zero_timeout_waits_until_resolved(self):
        from tools import clarify_gateway as cm

        cm.register("u1", "sk", "Q?", ["A", "B"])
        result_box = {}

        def waiter():
            result_box["r"] = cm.wait_for_response("u1", timeout=0)

        t = threading.Thread(target=waiter)
        t.start()
        # An unlimited wait cannot finish while nothing resolves it: still
        # running after a comfortable margin (old code auto-skipped at once).
        t.join(timeout=0.3)
        assert t.is_alive()

        # Once resolved, the unlimited wait returns the real answer.
        cm.resolve_gateway_clarify("u1", "B")
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert result_box["r"] == "B"

    def test_interrupt_on_waiting_thread_withdraws_unlimited_wait(self):
        """/stop or gateway shutdown signals the turn's thread; the wait is released as cancelled
        (as the approval wait is) instead of holding the turn for the whole clarify timeout."""
        from tools import clarify_gateway as cm
        from tools.interrupt import set_interrupt

        cm.register("u2", "sk", "Q?", ["A", "B"])
        box = {}

        def waiter():
            box["tid"] = threading.get_ident()
            box["r"] = cm.wait_for_response("u2", timeout=0)

        t = threading.Thread(target=waiter, daemon=True)  # a regression must fail, not hang exit
        t.start()
        wait_until(lambda: "tid" in box, timeout=5, what="the waiter thread to start")
        try:
            set_interrupt(True, box["tid"], reason="gateway shutdown")
            t.join(timeout=5.0)
        finally:
            set_interrupt(False, box["tid"])
        assert not t.is_alive()
        assert box["r"] == cm.CANCELLED
        assert not cm.has_pending("sk")
        assert cm.resolve_gateway_clarify("u2", "late") is False


class TestMultiSelectTextFallback:
    """Multi-select clarifies via the gateway text fallback.

    The adapter's numbered-list fallback asks the user to reply with
    comma/space-separated numbers; _coerce_text_response must map those to a
    JSON array of choice labels (which _parse_multi_select_response on the
    tool side decodes into a list).
    """

    def setup_method(self):
        _clear_clarify_state()

    def _register_multi(self, cid="m1", choices=("A", "B", "C")):
        from tools import clarify_gateway as cm
        entry = cm.register(cid, "sk", "Pick some", list(choices), multi_select=True)
        # Text fallback path always flips awaiting_text on.
        cm.mark_awaiting_text(cid)
        return entry



    def test_multi_select_without_choices_is_ignored(self):
        """multi_select on an open-ended clarify is meaningless — dropped."""
        from tools import clarify_gateway as cm
        entry = cm.register("s2", "sk", "Q?", None, multi_select=True)
        assert entry.multi_select is False


    def test_duplicate_selections_deduped(self):
        import json
        from tools import clarify_gateway as cm
        entry = self._register_multi()
        coerced = cm._coerce_text_response(entry, "1, 1, 2")
        assert json.loads(coerced) == ["A", "B"]

    def test_resolve_text_response_end_to_end(self):
        """resolve_text_response_for_session delivers the JSON array to the waiter."""
        import json
        from tools import clarify_gateway as cm
        self._register_multi(cid="m3")
        result_box = {}

        def waiter():
            result_box["r"] = cm.wait_for_response("m3", timeout=5)

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        assert cm.resolve_text_response_for_session("sk", "1,2") is True
        t.join(timeout=5)
        assert json.loads(result_box["r"]) == ["A", "B"]

    def test_single_select_regression_numeric(self):
        """Single-select coercion unchanged: '2' maps to the choice label string."""
        from tools import clarify_gateway as cm
        entry = cm.register("s3", "sk", "Q?", ["A", "B", "C"])
        assert cm._coerce_text_response(entry, "2") == "B"

    def test_single_select_regression_label(self):
        from tools import clarify_gateway as cm
        entry = cm.register("s4", "sk", "Q?", ["A", "B"])
        assert cm._coerce_text_response(entry, "b") == "B"


class TestNativeRejectClassification:
    """Rejected typed replies must distinguish free prose from bad selections.

    Free prose cancels/falls through (deadlock break). Selection-shaped but
    invalid replies (out-of-range number, unrecognised comma-list) keep the
    pending clarify armed so the user can retry.
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_multi_select_out_of_range_is_invalid_selection(self):
        from tools import clarify_gateway as cm

        entry = cm.register(
            "ms-oor", "sk-ms", "Pick some", ["A", "B", "C"], multi_select=True,
        )
        assert entry.awaiting_text is False
        value, reason = cm._coerce_text_response_detailed(entry, "99")
        assert value is None
        assert reason == "invalid_selection"
        assert cm.attempt_text_response_for_session("sk-ms", "99") == (
            cm.TEXT_REJECTED_SELECTION
        )
        pending = cm.get_pending_for_session("sk-ms", include_choice_prompts=True)
        assert pending is not None
        assert not pending.event.is_set()

    def test_multi_select_bad_comma_list_is_invalid_selection(self):
        from tools import clarify_gateway as cm

        entry = cm.register(
            "ms-bad", "sk-ms2", "Pick some", ["A", "B", "C"], multi_select=True,
        )
        value, reason = cm._coerce_text_response_detailed(entry, "1,99")
        assert value is None
        assert reason == "invalid_selection"
        assert cm.attempt_text_response_for_session("sk-ms2", "nope,nope") == (
            cm.TEXT_REJECTED_SELECTION
        )
        pending = cm.get_pending_for_session("sk-ms2", include_choice_prompts=True)
        assert pending is not None
        assert not pending.event.is_set()

    def test_multi_select_free_prose_is_rejected_prose(self):
        from tools import clarify_gateway as cm

        entry = cm.register(
            "ms-prose", "sk-ms3", "Pick some", ["A", "B"], multi_select=True,
        )
        value, reason = cm._coerce_text_response_detailed(
            entry, "just checking the visual UI, no need to pass any data",
        )
        assert value is None
        assert reason == "prose"
        assert cm.attempt_text_response_for_session(
            "sk-ms3", "just checking the visual UI, no need to pass any data",
        ) == cm.TEXT_REJECTED_PROSE

    def test_single_select_out_of_range_is_invalid_selection(self):
        from tools import clarify_gateway as cm

        entry = cm.register("ss-oor", "sk-ss", "Pick one", ["A", "B"])
        value, reason = cm._coerce_text_response_detailed(entry, "9")
        assert value is None
        assert reason == "invalid_selection"
        assert cm.attempt_text_response_for_session("sk-ss", "9") == (
            cm.TEXT_REJECTED_SELECTION
        )

    def test_single_select_prose_is_rejected_prose(self):
        from tools import clarify_gateway as cm

        entry = cm.register("ss-prose", "sk-ss2", "Pick one", ["A", "B"])
        value, reason = cm._coerce_text_response_detailed(
            entry, "one more unrelated thought",
        )
        assert value is None
        assert reason == "prose"


class TestClarifyRespondentAndPollRegistry:
    """The clarify entry records who the question was asked to, and a Telegram
    clarify poll is tracked by poll id so a vote can resolve it."""

    def setup_method(self):
        _clear_clarify_state()
        from tools import clarify_gateway as cm

        with cm._lock:
            getattr(cm, "_polls", {}).clear()
            getattr(cm, "_poll_index", {}).clear()

    def test_register_defaults_to_no_respondent(self):
        from tools import clarify_gateway as cm

        entry = cm.register("resp-none", "sk-rn", "Pick one", ["A", "B"])
        assert entry.respondent_id is None

    def test_register_stores_respondent_id(self):
        from tools import clarify_gateway as cm

        entry = cm.register(
            "resp-1", "sk-r1", "Pick one", ["A", "B"], respondent_id="6802929470",
        )
        assert entry.respondent_id == "6802929470"

    def test_poll_registry_roundtrip_by_clarify_and_poll_id(self):
        from tools import clarify_gateway as cm

        cm.register("poll-1", "sk-p1", "Pick one", ["A", "B"])
        poll = cm.register_clarify_poll(
            "poll-1", poll_id=12345, message_id=77, chat_id="-1001", thread_id="2",
        )
        assert poll is not None
        assert poll.clarify_id == "poll-1"
        assert poll.poll_id == 12345
        assert poll.message_id == 77
        assert poll.chat_id == "-1001"
        assert poll.thread_id == "2"
        assert cm.get_clarify_poll("poll-1") is poll
        found_poll, found_entry = cm.get_clarify_poll_for_poll_id(12345)
        assert found_poll is poll
        assert found_entry is not None
        assert found_entry.clarify_id == "poll-1"
        assert cm.get_clarify_poll_for_poll_id(99999) is None
        assert cm.get_clarify_poll("nope") is None

    def test_poll_registration_requires_a_pending_entry(self):
        from tools import clarify_gateway as cm

        assert cm.register_clarify_poll("ghost", poll_id=1, message_id=1, chat_id="c") is None
        assert cm.get_clarify_poll_for_poll_id(1) is None

    def test_poll_lookup_survives_entry_resolution_as_stale(self):
        """A vote arriving after the entry resolved still finds the poll record
        (entry None) so the handler can stop the stale poll instead of ignoring it."""
        from tools import clarify_gateway as cm

        cm.register("poll-stale", "sk-ps", "Pick one", ["A", "B"])
        cm.register_clarify_poll("poll-stale", poll_id=555, message_id=9, chat_id="c")
        assert cm.resolve_gateway_clarify("poll-stale", "A") is True
        found_poll, found_entry = cm.get_clarify_poll_for_poll_id(555)
        assert found_poll is not None
        assert found_entry is None

    def test_drop_poll_removes_both_lookups(self):
        from tools import clarify_gateway as cm

        cm.register("poll-drop", "sk-pd", "Pick one", ["A", "B"])
        cm.register_clarify_poll("poll-drop", poll_id=777, message_id=3, chat_id="c")
        assert cm.drop_clarify_poll("poll-drop") is True
        assert cm.get_clarify_poll("poll-drop") is None
        assert cm.get_clarify_poll_for_poll_id(777) is None
        assert cm.drop_clarify_poll("poll-drop") is False

    def test_clear_session_drops_polls_for_the_session(self):
        from tools import clarify_gateway as cm

        cm.register("poll-c1", "sk-pc", "Pick one", ["A", "B"])
        cm.register("poll-c2", "sk-pc", "Pick two", ["A", "B"])
        cm.register("poll-other", "sk-po", "Pick one", ["A", "B"])
        cm.register_clarify_poll("poll-c1", poll_id=11, message_id=1, chat_id="c")
        cm.register_clarify_poll("poll-c2", poll_id=22, message_id=2, chat_id="c")
        cm.register_clarify_poll("poll-other", poll_id=33, message_id=3, chat_id="c")
        cm.clear_session("sk-pc")
        assert cm.get_clarify_poll("poll-c1") is None
        assert cm.get_clarify_poll_for_poll_id(11) is None
        assert cm.get_clarify_poll("poll-c2") is None
        assert cm.get_clarify_poll("poll-other") is not None


class TestWaitForResponseLogging:
    """The waiting turn's wake is logged: an intercepted answer sets the
    event, but nothing said whether the waiter ever received it."""

    def setup_method(self):
        _clear_clarify_state()

    def test_wait_returning_an_answer_is_logged(self, caplog):
        from tools import clarify_gateway as cm

        cm.register("log-ans", "sk-log", "Pick one", ["A", "B"])
        caplog.set_level(logging.INFO, logger="tools.clarify_gateway")
        result = {}

        def _wait():
            result["response"] = cm.wait_for_response("log-ans", timeout=5.0)

        worker = threading.Thread(target=_wait)
        worker.start()
        time.sleep(0.05)
        assert cm.resolve_gateway_clarify("log-ans", "B") is True
        worker.join(timeout=5)
        assert result["response"] == "B"
        assert "Clarify wait for log-ans returned an answer" in caplog.text

    def test_wait_timing_out_is_logged(self, caplog):
        from tools import clarify_gateway as cm

        cm.register("log-timeout", "sk-log", "Pick one", ["A", "B"])
        caplog.set_level(logging.INFO, logger="tools.clarify_gateway")
        assert cm.wait_for_response("log-timeout", timeout=0.05) is None
        assert "Clarify wait for log-timeout timed out" in caplog.text
