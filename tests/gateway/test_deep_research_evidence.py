import threading
from types import SimpleNamespace

import pytest

from gateway.deep_research_sessions import (
    create_session,
    load_checkpoint,
    merge_research_evidence,
    save_checkpoint,
    validate_research_artifacts,
)
from gateway.deep_research_sources import successful_read_sources
from plugins.platforms.telegram.deep_research_ui import created_topic_id_from_updates
from gateway.run_turn_runner import TurnRunner


class MessageActionTopicCreate:
    pass


class UpdateNewChannelMessage:
    def __init__(self, message):
        self.message = message


class UpdateShort:
    def __init__(self, update):
        self.update = update


class UpdatesCombined:
    def __init__(self, updates):
        self.updates = updates


class UpdateMessageID:
    def __init__(self, random_id, message_id):
        self.random_id = random_id
        self.id = message_id


def test_extracts_topic_root_from_nested_short_mtproto_update():
    service = SimpleNamespace(id=731, action=MessageActionTopicCreate())
    response = UpdateShort(UpdateNewChannelMessage(service))
    assert created_topic_id_from_updates(response) == 731


def test_topic_root_prefers_service_message_and_only_uses_matching_random_id_mapping():
    service = SimpleNamespace(id=731, action=MessageActionTopicCreate())
    response = UpdatesCombined([UpdateMessageID(99, 999), UpdateNewChannelMessage(service)])
    assert created_topic_id_from_updates(response, random_id=99) == 731
    assert created_topic_id_from_updates(UpdatesCombined([UpdateMessageID(99, 999)]), random_id=100) is None
    assert created_topic_id_from_updates(UpdatesCombined([UpdateMessageID(99, 999)]), random_id=99) == 999


def test_apparent_completion_requires_observed_reads_documented_in_notes_and_cited_in_report(tmp_path):
    record = create_session(tmp_path, "12345", "Compare public reports", chat_id="42", chat_type="dm")
    session_dir = tmp_path / "deep_research_sessions" / "12345" / record["session_id"]
    urls = ["https://agency.example/reports/one", "https://university.example/paper/two"]
    evidence = {"searches": 2, "result_urls": urls, "read_urls": urls}
    (session_dir / "research_notes.md").write_text(
        "Source notes\n- Agency finding. " + urls[0] + "\n- Independent study. " + urls[1],
        encoding="utf-8",
    )
    (session_dir / "report.md").write_text(
        "Findings support claim [1] and independently confirm it [2].\n\n"
        "## References\n[1] Agency report — " + urls[0] + "\n[2] University paper — " + urls[1] + "\n",
        encoding="utf-8",
    )
    assert validate_research_artifacts(session_dir, evidence) == (True, "")
    assert validate_research_artifacts(session_dir, {"read_urls": [urls[0]]})[0] is False
    (session_dir / "report.md").write_text("Generic unreferenced summary.", encoding="utf-8")
    valid, reason = validate_research_artifacts(session_dir, evidence)
    assert not valid and "References" in reason


def test_evidence_failure_states_recorded_page_and_host_counts(tmp_path):
    """The pause message must say why: two reads from one host fail the
    independent-websites rule, and the counts make that visible."""
    record = create_session(tmp_path, "12345", "Same-host reads", chat_id="42", chat_type="dm")
    session_dir = tmp_path / "deep_research_sessions" / "12345" / record["session_id"]
    evidence = {"read_urls": [
        "https://en.wikipedia.org/wiki/Category:One",
        "https://en.wikipedia.org/wiki/Category:Two",
    ]}
    valid, reason = validate_research_artifacts(session_dir, evidence)
    assert valid is False
    assert "2 page(s) from 1 website(s)" in reason


def test_checkpoint_merges_measured_evidence_across_resumed_turns(tmp_path):
    record = create_session(tmp_path, "12345", "Topic", chat_id="42", chat_type="dm")
    save_checkpoint(tmp_path, "12345", record["session_id"], research_evidence={
        "searches": 1, "result_urls": ["https://example.org/a"], "read_urls": ["https://example.org/a"],
    })
    save_checkpoint(tmp_path, "12345", record["session_id"], research_evidence={
        "searches": 2, "result_urls": ["https://other.org/b"], "read_urls": ["https://other.org/b"],
    })
    evidence = load_checkpoint(tmp_path, "12345", record["session_id"])["research_evidence"]
    assert evidence["searches"] == 2
    assert set(evidence["read_urls"]) == {"https://example.org/a", "https://other.org/b"}


_EVIDENCE_URLS = ["https://agency.example/reports/one", "https://university.example/paper/two"]


def _layout_report(layout: str) -> str:
    """One fully-evidenced artifact in each References layout the agent may produce.

    The agent is only instructed to use "[1] citations + a References section
    with exact URLs" — no dialect is specified — so every layout below must verify.
    """
    one, two = _EVIDENCE_URLS
    if layout == "numbered-list":
        return ("Findings [1] and [2].\n\n## References\n"
                f"1. Agency report — {one}\n2. University paper — {two}\n")
    if layout == "markdown-links":
        return ("Findings [1] and [2].\n\n## References\n"
                f"- [Agency report]({one})\n- [University paper]({two})\n")
    if layout == "stray-bracket-number":
        return ("Findings [1] and [2]. See appendix item [3] for the method note.\n\n"
                f"## References\n[1] Agency — {one}\n[2] Univ — {two}\n")
    if layout == "early-sources-heading":
        return ("# Report\n\n## Sources of disagreement\nThe two sources disagree on method.\n\n"
                f"Findings [1] and [2].\n\n## References\n[1] Agency — {one}\n[2] Univ — {two}\n")
    if layout == "extra-unread-url-on-row":
        return ("Findings [1] and [2].\n\n## References\n"
                f"[1] Agency — {one} and https://agency.example/\n[2] Univ — {two}\n")
    raise ValueError(layout)


@pytest.mark.parametrize("layout", [
    "numbered-list",
    "markdown-links",
    "stray-bracket-number",
    "early-sources-heading",
    "extra-unread-url-on-row",
])
def test_report_citations_accept_alternate_reference_layouts(tmp_path, layout):
    record = create_session(tmp_path, "12345", "Compare public reports", chat_id="42", chat_type="dm")
    session_dir = tmp_path / "deep_research_sessions" / "12345" / record["session_id"]
    (session_dir / "research_notes.md").write_text(
        "Source notes\n- Agency finding. " + _EVIDENCE_URLS[0]
        + "\n- Independent study. " + _EVIDENCE_URLS[1],
        encoding="utf-8",
    )
    (session_dir / "report.md").write_text(_layout_report(layout), encoding="utf-8")
    assert validate_research_artifacts(
        session_dir, {"read_urls": _EVIDENCE_URLS}) == (True, "")


def test_report_citations_match_www_and_tracking_param_variants(tmp_path):
    """A citation of the URL the agent was given must match the recorded final URL.

    The tool records the www./UTM form; the agent cites the same pages without
    them. The checkpoint ledger carries the tool URLs, so the notes check passes
    on the ledger while the report cites the agent's forms.
    """
    record = create_session(tmp_path, "12345", "Compare public reports", chat_id="42", chat_type="dm")
    session_dir = tmp_path / "deep_research_sessions" / "12345" / record["session_id"]
    tool_urls = [
        "https://www.nature.com/articles/s41586-024-12345",
        "https://www.science.org/doi/10.1126/science.abc1234?utm_source=rss&utm_medium=rss",
    ]
    agent_urls = [
        "https://nature.com/articles/s41586-024-12345",
        "https://www.science.org/doi/10.1126/science.abc1234",
    ]
    evidence = {"searches": 2, "result_urls": tool_urls, "read_urls": tool_urls,
                "read_sources": [{"url": url, "host": "x", "title": "T"} for url in tool_urls]}
    save_checkpoint(tmp_path, "12345", record["session_id"], research_evidence=evidence)
    (session_dir / "report.md").write_text(
        "Findings [1] and [2].\n\n## References\n[1] Nature — " + agent_urls[0]
        + "\n[2] Science — " + agent_urls[1] + "\n",
        encoding="utf-8",
    )
    stored = load_checkpoint(tmp_path, "12345", record["session_id"])["research_evidence"]
    assert validate_research_artifacts(session_dir, stored) == (True, "")


def test_report_citations_match_redirect_requested_url(tmp_path):
    """A citation of the requested URL must match the redirect target that was read.

    web_extract reports the publisher final URL with metadata.sourceURL set to
    the doi URL the agent asked for; the report cites the doi URL.
    """
    record = create_session(tmp_path, "12345", "Compare public reports", chat_id="42", chat_type="dm")
    session_dir = tmp_path / "deep_research_sessions" / "12345" / record["session_id"]
    tool_urls = [
        "https://www.nature.com/articles/s41586-024-12345",
        "https://www.science.org/doi/10.1126/science.abc1234",
    ]
    requested_urls = [
        "https://doi.org/10.1038/s41586-024-12345",
        "https://doi.org/10.1126/science.abc1234",
    ]
    payload = {"results": [
        {"url": tool_urls[0], "title": "Paper", "content": "Full page text.",
         "metadata": {"sourceURL": requested_urls[0]}},
        {"url": tool_urls[1], "title": "Study", "content": "More page text.",
         "metadata": {"sourceURL": requested_urls[1]}},
    ]}
    sources = successful_read_sources("web_extract", payload)
    evidence = {"searches": 2, "result_urls": tool_urls, "read_urls": tool_urls,
                "read_sources": sources}
    save_checkpoint(tmp_path, "12345", record["session_id"], research_evidence=evidence)
    (session_dir / "report.md").write_text(
        "Findings [1] and [2].\n\n## References\n[1] Nature — " + requested_urls[0]
        + "\n[2] Science — " + requested_urls[1] + "\n",
        encoding="utf-8",
    )
    stored = load_checkpoint(tmp_path, "12345", record["session_id"])["research_evidence"]
    assert validate_research_artifacts(session_dir, stored) == (True, "")


def test_redirect_alias_is_not_a_second_read_page(tmp_path):
    """The requested URL is an alias of the one page read, not a second read.

    A single page read through a doi redirect must still fail gate 1, which
    requires two independently hosted read pages.
    """
    record = create_session(tmp_path, "12345", "Compare public reports", chat_id="42", chat_type="dm")
    session_dir = tmp_path / "deep_research_sessions" / "12345" / record["session_id"]
    payload = {"results": [
        {"url": "https://www.nature.com/articles/s41586-024-12345", "title": "Paper",
         "content": "Full page text.",
         "metadata": {"sourceURL": "https://doi.org/10.1038/s41586-024-12345"}},
    ]}
    sources = successful_read_sources("web_extract", payload)
    merged = merge_research_evidence({}, {
        "searches": 1, "result_urls": ["https://www.nature.com/articles/s41586-024-12345"],
        "read_urls": ["https://www.nature.com/articles/s41586-024-12345"],
        "read_sources": sources,
    })
    assert len(merged["read_urls"]) == 1
    save_checkpoint(tmp_path, "12345", record["session_id"], research_evidence={
        "searches": 1,
        "result_urls": ["https://www.nature.com/articles/s41586-024-12345"],
        "read_urls": ["https://www.nature.com/articles/s41586-024-12345"],
        "read_sources": sources,
    })
    (session_dir / "report.md").write_text(
        "Findings [1].\n\n## References\n[1] Nature — https://doi.org/10.1038/s41586-024-12345\n",
        encoding="utf-8",
    )
    valid, reason = validate_research_artifacts(session_dir, merged)
    assert valid is False
    assert "Fewer than two" in reason


def test_only_successful_source_content_counts_as_a_read():
    runner = object.__new__(TurnRunner)
    runner._search_metrics = {"searches": 0, "result_urls": set(), "sites": set(),
                              "pages_read": set(), "read_sites": set()}
    runner._search_metrics_lock = threading.Lock()
    assert runner._record_search_tool_result("web_extract", {"result": {
        "results": [{"url": "https://read.example/page", "content": "Retrieved source text."},
                    {"url": "https://failed.example/page", "error": "fetch failed"}],
    }}) == "Read 1 source page(s)."
    assert runner._record_search_tool_result("browser_read", {"result": {
        "url": "https://browser.example/article", "content": "Visible browser article text.",
    }}) == "Read 1 browser source page(s)."
    assert runner._record_search_tool_result("browser_navigate", {"result": {
        "url": "https://metadata-only.example/", "title": "No retrieved content",
    }}) is None
    evidence = runner._research_evidence_snapshot()
    assert set(evidence["read_urls"]) == {
        "https://read.example/page", "https://browser.example/article",
    }
