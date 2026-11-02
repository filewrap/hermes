from gateway.stream_consumer import GatewayStreamConsumer
from gateway.tool_protocol_filter import (
    ToolProtocolOutputFilter,
    filter_tool_protocol_output,
)


def test_raw_tool_call_and_argument_wrapper_is_removed_but_surrounding_prose_remains():
    text = (
        "Answer first. <|tool_call:start|>execute_code "
        "<|tool_arg:start|>{\"code\":\"print(1)\"} "
        "<|tool_arg:end|><|tool_call:end|> Answer after."
    )

    cleaned = filter_tool_protocol_output(text)

    assert cleaned == "Answer first.  Answer after."
    assert "tool_call" not in cleaned
    assert "tool_arg" not in cleaned
    assert "print(1)" not in cleaned


def test_unclosed_raw_tool_wrapper_suppresses_the_remaining_content():
    assert filter_tool_protocol_output(
        "Visible prefix. <|tool_call:start|>execute_code private arguments"
    ) == "Visible prefix. "


def test_orphan_close_marker_is_removed_without_swallowing_following_prose():
    assert filter_tool_protocol_output(
        "The provider stopped here: <|tool_call:end|> normal answer continues."
    ) == "The provider stopped here:  normal answer continues."


def test_protocol_looking_text_inside_fenced_or_inline_code_is_preserved():
    fenced = (
        "Example:\n```text\n"
        "<|tool_call:start|>execute_code\n"
        "<|tool_arg:start|>{\"code\": \"print(1)\"}\n"
        "<|tool_arg:end|><|tool_call:end|>\n```.\n"
    )
    inline = "Use `<|tool_call:start|>` as a literal example."

    assert filter_tool_protocol_output(fenced) == fenced
    assert filter_tool_protocol_output(inline) == inline


def test_escaped_backtick_does_not_open_inline_code_and_hide_a_wrapper():
    text = "Visible \\` <|tool_call:start|>payload<|tool_call:end|> Done."

    assert filter_tool_protocol_output(text) == "Visible \\`  Done."


def test_unmatched_backtick_does_not_hide_a_raw_wrapper():
    text = "`literal <|tool_call:start|>secret<|tool_call:end|> visible tail"

    assert filter_tool_protocol_output(text) == "`literal  visible tail"


def test_matched_inline_code_span_split_across_deltas_is_preserved():
    stream_filter = ToolProtocolOutputFilter()
    first = stream_filter.feed("Prefix `")
    second = stream_filter.feed(
        "<|tool_call:start|>literal<|tool_call:end|>` suffix"
    )
    final = stream_filter.feed("", final=True)

    assert first == "Prefix "
    assert second + final == "`<|tool_call:start|>literal<|tool_call:end|>` suffix"


def test_stream_filter_holds_split_marker_and_suppresses_payload_before_releasing_tail():
    stream_filter = ToolProtocolOutputFilter()
    visible = []
    visible.append(stream_filter.feed("Visible prefix. <|tool_ca"))
    assert "<|tool" not in "".join(visible)
    visible.append(stream_filter.feed("ll:start|>execute_code"))
    visible.append(stream_filter.feed("<|tool_arg:start|>{\"code\": 1}"))
    visible.append(stream_filter.feed("<|tool_arg:end|><|tool_call:end|> Safe tail."))
    visible.append(stream_filter.feed("", final=True))

    assert "".join(visible) == "Visible prefix.  Safe tail."
    assert all("tool_call" not in part and "tool_arg" not in part for part in visible)


def test_stream_consumer_filters_before_accumulating_and_keeps_fenced_code():
    consumer = GatewayStreamConsumer(adapter=object(), chat_id="42")
    consumer._filter_and_accumulate("Visible. <|tool_call:start|>execute_code")
    consumer._filter_and_accumulate("<|tool_arg:start|>{}\n<|tool_call:end|>\n")
    fenced = "```python\nprint('<|tool_call:start|>')\n```"
    consumer._filter_and_accumulate(fenced)
    consumer._flush_tool_protocol_filter()
    consumer._flush_think_buffer()

    assert consumer._accumulated == "Visible. \n" + fenced
    assert "execute_code" not in consumer._accumulated
    assert "<|tool_call:start|>" in consumer._accumulated  # inside fenced code


def test_authoritative_final_text_is_filtered_before_stream_reconciliation():
    consumer = GatewayStreamConsumer(adapter=object(), chat_id="42")
    consumer._filter_and_accumulate("Prefix ")

    consumer._adopt_final_text(
        "Prefix <|tool_call:start|>execute_code secret<|tool_call:end|> Tail."
    )

    assert consumer._accumulated == "Prefix  Tail."
    assert consumer._stream_ledger == "Prefix  Tail."
