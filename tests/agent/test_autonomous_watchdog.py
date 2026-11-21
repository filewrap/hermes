"""Behaviour contracts for the autonomous watchdog (``agent/autonomous_watchdog.py``).

The watchdog observes only: scripted stall sequences (repeat tool+args,
repeat error, cycle, no-progress) fire exactly one stuck episode each,
the run is never halted (``observe`` only reports), and a progressing run
never fires. The pinned-message throttle allows an edit at most every 10
steps or 5 minutes.
"""

from agent.autonomous_watchdog import (
    AutonomousWatchdog,
    build_agent_note,
    build_owner_notice,
    build_pin_text,
)


def _feed(watchdog, calls, *, steps=0):
    """Feed *calls* [(tool, args, result)] in order; the fired episode, or None."""
    fired = None
    for i, (tool, args, result) in enumerate(calls):
        episode = watchdog.observe(tool, args, result, steps=steps + i)
        if episode is not None:
            fired = episode
    return fired


def test_identical_tool_and_args_fires_on_third_call():
    watchdog = AutonomousWatchdog()

    episode = _feed(watchdog, [
        ("read_file", {"path": "/tmp/x"}, "contents"),
        ("read_file", {"path": "/tmp/x"}, "contents"),
        ("read_file", {"path": "/tmp/x"}, "contents"),
    ])

    assert episode is not None
    assert episode.kind == "identical_args"
    assert episode.tool == "read_file"
    assert episode.count == 3
    assert "read_file" in episode.detail and "identical arguments" in episode.detail


def test_identical_args_do_not_refire_within_episode_but_do_after_progress():
    watchdog = AutonomousWatchdog()
    calls = [("read_file", {"path": "/tmp/x"}, "contents")] * 3

    assert _feed(watchdog, calls) is not None
    # The run is not halted and the episode is one-shot: more of the same
    # neither re-fires nor stops observation.
    assert _feed(watchdog, [("read_file", {"path": "/tmp/x"}, "contents")] * 2) is None
    assert watchdog.episode_active is True
    # One progressing call ends the episode; a fresh stall fires again.
    assert _feed(watchdog, [("terminal", {"command": "ls"}, "a\nb")]) is None
    assert watchdog.episode_active is False
    assert _feed(watchdog, calls) is not None


def test_identical_error_fires_with_different_args():
    watchdog = AutonomousWatchdog()

    episode = _feed(watchdog, [
        ("terminal", {"command": "a"}, '{"error": "boom"}'),
        ("terminal", {"command": "b"}, '{"error": "boom"}'),
        ("terminal", {"command": "c"}, '{"error": "boom"}'),
    ])

    assert episode is not None
    assert episode.kind == "identical_error"
    assert "boom" in episode.detail


def test_error_prefix_string_fires_and_different_errors_do_not():
    watchdog = AutonomousWatchdog()

    assert _feed(watchdog, [
        ("terminal", {"command": "a"}, "Error: first"),
        ("terminal", {"command": "b"}, "Error: second"),
        ("terminal", {"command": "c"}, "Error: third"),
    ]) is None
    episode = _feed(watchdog, [
        ("terminal", {"command": "d"}, "Error: third"),
        ("terminal", {"command": "e"}, "Error: third"),
    ])
    assert episode is not None and episode.kind == "identical_error"


def test_cycle_period_two_fires_on_third_repeat():
    watchdog = AutonomousWatchdog()

    episode = _feed(watchdog, [
        ("search", {"q": "a"}, "page 1"),
        ("read_file", {"path": "/p"}, "doc"),
        ("search", {"q": "a"}, "page 1"),
        ("read_file", {"path": "/p"}, "doc"),
        ("search", {"q": "a"}, "page 1"),
        ("read_file", {"path": "/p"}, "doc"),
    ])

    assert episode is not None
    assert episode.kind == "cycle"
    assert "2-call sequence" in episode.detail
    assert "search" in episode.detail and "read_file" in episode.detail


def test_cycle_period_three_fires():
    watchdog = AutonomousWatchdog()

    calls = [
        ("a", {"n": 1}, "r1"), ("b", {"n": 2}, "r2"), ("c", {"n": 3}, "r3"),
        ("a", {"n": 1}, "r1"), ("b", {"n": 2}, "r2"), ("c", {"n": 3}, "r3"),
        ("a", {"n": 1}, "r1"), ("b", {"n": 2}, "r2"), ("c", {"n": 3}, "r3"),
    ]
    episode = _feed(watchdog, calls)

    assert episode is not None and episode.kind == "cycle"
    assert "3-call sequence" in episode.detail


def test_no_progress_fires_on_identical_results_from_different_calls():
    watchdog = AutonomousWatchdog()

    episode = _feed(watchdog, [
        ("search", {"q": "a"}, "same page"),
        ("search", {"q": "b"}, "same page"),
        ("search", {"q": "c"}, "same page"),
    ])

    assert episode is not None
    assert episode.kind == "no_progress"
    assert "identical results" in episode.detail


def test_progressing_run_never_fires():
    watchdog = AutonomousWatchdog()

    calls = [
        ("search", {"q": "a"}, "page a"),
        ("read_file", {"path": "/a"}, "doc a"),
        ("terminal", {"command": "ls"}, "a\nb"),
        ("search", {"q": "b"}, "page b"),
        ("read_file", {"path": "/b"}, "doc b"),
        ("terminal", {"command": "pwd"}, "/home"),
        ("write_file", {"path": "/c"}, "ok"),
        ("search", {"q": "c"}, "page c"),
        ("read_file", {"path": "/d"}, "doc d"),
        ("terminal", {"command": "whoami"}, "kei"),
    ]
    assert _feed(watchdog, calls) is None
    assert watchdog.episode_active is False


def test_pin_throttle_steps_and_time():
    now = [1000.0]
    watchdog = AutonomousWatchdog(clock=lambda: now[0])

    assert watchdog.pin_update_allowed(3) is True
    watchdog.note_pin_updated(3)
    assert watchdog.pin_update_allowed(5) is False
    assert watchdog.pin_update_allowed(13) is True
    watchdog.note_pin_updated(13)
    assert watchdog.pin_update_allowed(14) is False
    now[0] += 301.0
    assert watchdog.pin_update_allowed(14) is True


def test_episode_texts_carry_goal_steps_tokens_and_stop():
    watchdog = AutonomousWatchdog()
    episode = _feed(watchdog, [
        ("read_file", {"path": "/tmp/x"}, "contents"),
        ("read_file", {"path": "/tmp/x"}, "contents"),
        ("read_file", {"path": "/tmp/x"}, "contents"),
    ])
    assert episode is not None
    episode.steps = 40
    episode.input_tokens = 1200
    episode.output_tokens = 300

    note = build_agent_note(episode)
    assert "read_file" in note and "identical arguments" in note
    assert "Change strategy, use what you have, or declare done." in note

    notice = build_owner_notice(goal="Tidy the downloads folder", episode=episode)
    assert "Tidy the downloads folder" in notice
    assert "40" in notice and "1500" in notice
    assert "read_file" in notice
    assert "/stop" in notice

    pin = build_pin_text(goal="Tidy the downloads folder", episode=episode)
    assert "Tidy the downloads folder" in pin
    assert "40" in pin and "1500" in pin
    assert "read_file" in pin and "/stop" in pin


def test_observe_never_raises_on_unusual_inputs():
    watchdog = AutonomousWatchdog()

    assert watchdog.observe(None, None, None) is None
    assert watchdog.observe("tool", object(), object()) is None
    assert watchdog.observe("tool", {"a": object()}, {"b": object()}) is None
