"""Reasoning summary-part boundary repair (agent/reasoning_summaries.py)."""

from agent.reasoning_summaries import separate_glued_reasoning_blocks


def _stream(deltas):
    """Accumulate *deltas* the way the chat-completions stream loop does."""
    parts: list[str] = []
    for delta in deltas:
        parts.append(
            separate_glued_reasoning_blocks(parts[-1] if parts else "", delta)
        )
    return "".join(parts)


def test_heading_only_parts_do_not_glue_into_one_run():
    # The shape observed live on Nous Portal's openai/gpt-5.6-sol: each delta
    # is a bare heading, so consecutive parts produce a `****` run.
    text = _stream(
        [
            "**Investigating likely culprit PRs**",
            "**Inspecting message schema and tool_calls content**",
            "**Analyzing interrupted tool call impact**",
        ]
    )

    assert "****" not in text
    assert text.splitlines() == [
        "**Investigating likely culprit PRs**",
        "",
        "**Inspecting message schema and tool_calls content**",
        "",
        "**Analyzing interrupted tool call impact**",
    ]


def test_prose_body_does_not_glue_onto_the_next_heading():
    # vercel/ai#6742's repro: a part ends in prose and the next heading butts
    # straight onto it, with no `****` run to key off.
    text = _stream(
        [
            "**Simulating a greeting stream**\n\nIt feels like a streaming interaction!",
            "**Simulating a greeting stream**\n\nI want to meet the request.",
        ]
    )

    assert "interaction!**" not in text
    assert "interaction!\n\n**Simulating" in text


def test_token_streamed_reasoning_is_untouched():
    deltas = ["Looking at", " the session", " logs, I see", " one bold word."]

    assert _stream(deltas) == "".join(deltas)


def test_sentence_boundary_after_full_stop_gets_a_space():
    # Provider deltas split at a sentence boundary with the space in
    # neither chunk: "...stop." + "New line..." must not glue.
    assert _stream(["The first stop.", "New line follows."]) == (
        "The first stop. New line follows.")


def test_sentence_boundary_after_bang_and_question_gets_a_space():
    assert _stream(["Really?", "Yes, it works."]) == "Really? Yes, it works."
    assert _stream(["Watch out!", "Here it comes."]) == "Watch out! Here it comes."


def test_numbered_continuation_after_full_stop_gets_a_space():
    assert _stream(["Step one.", "5 more things."]) == "Step one. 5 more things."


def test_mid_word_token_split_is_untouched():
    # A token split inside a word ends in a letter, never in .!? — the
    # boundary rule must not touch it.
    assert _stream(["rea", "soning"]) == "reasoning"
    assert _stream(["The value is 3", ".5 total"]) == "The value is 3.5 total"


def test_lowercase_and_code_deltas_are_untouched():
    # Narrow on purpose: only an uppercase letter or a digit opens a new
    # sentence after .!? — a lowercase continuation or a code/markdown
    # opener is left exactly as streamed.
    assert _stream(["Done.", "and then more."]) == "Done.and then more."
    assert _stream(["Done.", "`code` here"]) == "Done.`code` here"
    assert _stream(["Done.", "- item"]) == "Done.- item"


def test_bold_word_mid_sentence_is_not_a_boundary():
    # Emphasis inside token-streamed prose arrives after a space.
    assert separate_glued_reasoning_blocks("I see the ", "**signature**") == "**signature**"


def test_unclosed_emphasis_fragment_is_not_a_boundary():
    # A token stream splitting emphasis across deltas opens but never closes.
    assert separate_glued_reasoning_blocks("weighing", "**") == "**"


def test_boundary_needs_a_bold_opener():
    assert separate_glued_reasoning_blocks("**Closing**", "plain head") == "plain head"


def test_empty_operands_pass_through():
    assert separate_glued_reasoning_blocks("", "**first**") == "**first**"
    assert separate_glued_reasoning_blocks("**first**", "") == ""
