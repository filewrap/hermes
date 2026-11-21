from gateway.run import _normalize_empty_agent_response, _recover_missing_final_response


def test_recovers_final_answer_left_in_current_turn_messages():
    messages = [
        {"role": "assistant", "content": "Old answer must not leak."},
        {"role": "user", "content": "/search urban heat islands in Indian cities"},
        {"role": "assistant", "tool_calls": [{"id": "call-1"}], "content": ""},
        {"role": "tool", "tool_call_id": "call-1", "content": "Retrieved evidence."},
        {
            "role": "assistant",
            "content": [
                {"type": "reasoning", "text": "private reasoning"},
                {"type": "output_text", "text": "Urban heat islands raise night-time temperatures. See https://example.org/study"},
            ],
        },
    ]
    result = {"messages": messages, "history_offset": 1, "api_calls": 2}

    recovered = _recover_missing_final_response(result, "", history_len=1)

    assert recovered == "Urban heat islands raise night-time temperatures. See https://example.org/study"
    assert "Old answer" not in recovered
    assert "private reasoning" not in recovered
    assert _normalize_empty_agent_response(result, recovered) == recovered


def test_recovers_empty_sentinel_but_keeps_existing_response_unchanged():
    result = {
        "messages": [
            {"role": "user", "content": "/search query"},
            {"role": "assistant", "content": "Recovered answer."},
        ],
        "history_offset": 0,
    }

    assert _recover_missing_final_response(result, "(empty)") == "Recovered answer."
    assert _recover_missing_final_response(result, "Already delivered.") == "Already delivered."


def test_does_not_recover_a_stale_answer_after_compaction():
    result = {
        "messages": [
            {"role": "assistant", "content": "Old answer."},
            {"role": "user", "content": "/search new query"},
        ],
        "history_offset": 0,
    }

    assert _recover_missing_final_response(result, "") == ""


def test_does_not_recover_from_incomplete_or_nonfinal_assistant_messages():
    tool_call_result = {
        "messages": [
            {"role": "user", "content": "/search query"},
            {"role": "assistant", "content": "Not a final answer.", "tool_calls": [{"id": "call-2"}]},
        ],
        "history_offset": 0,
    }
    partial_result = {
        "messages": [
            {"role": "user", "content": "/search query"},
            {"role": "assistant", "content": "Possibly incomplete."},
        ],
        "history_offset": 0,
        "partial": True,
    }
    sentinel_result = {
        "messages": [
            {"role": "user", "content": "/search query"},
            {"role": "assistant", "content": "[SILENT]"},
        ],
        "history_offset": 0,
    }

    assert _recover_missing_final_response(tool_call_result, "") == ""
    assert _recover_missing_final_response(partial_result, "") == ""
    assert _recover_missing_final_response(sentinel_result, "") == ""
