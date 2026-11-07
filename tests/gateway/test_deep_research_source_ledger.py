"""Observed reads must survive model-authored notes and remain distinct from citations."""
import threading

import pytest

from gateway.deep_research_sessions import (
    create_session, load_checkpoint, resolve_session_dir, save_checkpoint,
    validate_research_artifacts, validate_resume_state,
)
from gateway.run_turn_runner import TurnRunner


def observed_runner():
    runner = object.__new__(TurnRunner)
    runner._search_metrics = {"searches": 0, "result_urls": set(), "sites": set(),
                              "pages_read": set(), "read_sites": set()}
    runner._search_metrics_lock = threading.Lock()
    return runner


def test_observed_reads_generate_exact_private_ledger_across_checkpoints(tmp_path):
    record = create_session(tmp_path, "12345", "Research", chat_id="42", chat_type="dm", thread_id="70")
    sid = record["session_id"]
    directory = resolve_session_dir(tmp_path, "12345", sid)
    notes = directory / "research_notes.md"
    notes.write_text("# Findings\nSources to Cite (URLs): Agency, Museum\n")
    urls = ["https://agency.example/reports/one/?lang=en#summary", "https://museum.example/item/two"]
    runner = observed_runner()
    runner._record_search_tool_result("web_extract", {"result": {"results": [
        {"url": urls[0], "title": "Agency primary report", "content": "Source text."},
        {"url": "https://failed.example/no", "title": "Failed", "content": "Error body", "success": False},
    ]}})
    runner._record_search_tool_result("browser_navigate", {"result": {
        "success": True, "url": urls[1], "title": "Museum object history", "snapshot": "Independent source text.",
    }})
    runner._record_search_tool_result("browser_snapshot", {"result": {
        "success": True, "url": urls[1], "snapshot": "The same source, without title metadata.",
    }})
    runner._record_search_tool_result("browser_navigate", {"result": {
        "success": True, "url": "https://blocked.example/", "title": "Access denied",
        "snapshot": "Please verify you are human", "bot_detection_warning": "Blocked",
    }})
    runner._record_search_tool_result("browser_snapshot", {"result": {
        "success": True, "snapshot": "A tree without an identifiable URL is not source evidence.",
    }})
    runner._record_search_tool_result("web_search", {"result": {"results": [
        {"url": "https://search-only.example/result", "title": "Not read"},
    ]}})
    evidence = runner._research_evidence_snapshot()
    save_checkpoint(tmp_path, "12345", sid, research_evidence=evidence,
                    tool_history=[{"name": "web_extract", "success": True}])
    checkpoint = load_checkpoint(tmp_path, "12345", sid)
    ledger = notes.read_text()
    assert "Sources to Cite (URLs): Agency, Museum" in ledger
    for url, title, host in zip(urls, ["Agency primary report", "Museum object history"],
                                ["agency.example", "museum.example"]):
        assert url in ledger and title in ledger and host in ledger
    assert "https://failed.example/no" not in ledger
    assert "https://search-only.example/result" not in ledger
    assert "https://blocked.example/" not in ledger
    assert checkpoint["source_ledger"]["content"] == ledger
    assert {s["url"] for s in checkpoint["research_evidence"]["read_sources"]} == set(urls)
    # A model rewrite must not erase runtime observations on the next checkpoint.
    notes.write_text("# Updated findings\nActual source analysis remains model-authored.\n")
    save_checkpoint(tmp_path, "12345", sid, research_evidence={})
    assert all(url in notes.read_text() for url in urls)
    first = notes.read_text()
    save_checkpoint(tmp_path, "12345", sid, research_evidence={})
    assert notes.read_text() == first
    validate_resume_state(tmp_path, "12345", sid)
    # Recording sources does NOT manufacture a report or bypass the citation gate.
    assert validate_research_artifacts(directory, checkpoint["research_evidence"])[0] is False
    (directory / "report.md").write_text("Supported [1] and corroborated [2].\n\n## References\n[1] " + urls[0] + "\n[2] " + urls[1])
    assert validate_research_artifacts(directory, checkpoint["research_evidence"]) == (True, "")
    # The runtime ledger must never follow a model-supplied link out of the session.
    external = tmp_path / "unrelated-notes.md"
    external.write_text("Do not alter")
    notes.unlink()
    notes.symlink_to(external)
    with pytest.raises(ValueError, match="regular session file"):
        save_checkpoint(tmp_path, "12345", sid, research_evidence=evidence)
    assert external.read_text() == "Do not alter"


def test_notes_must_document_every_read_including_independent_hosts(tmp_path):
    record = create_session(tmp_path, "12345", "Research", chat_id="42", chat_type="dm")
    directory = resolve_session_dir(tmp_path, "12345", record["session_id"])
    urls = ["https://one.example/a", "https://one.example/b", "https://two.example/c"]
    (directory / "research_notes.md").write_text("Read pages\n" + "\n".join(urls[:2]))
    (directory / "report.md").write_text("Claims [1] and [2].\n\n## References\n[1] " + urls[0] + "\n[2] " + urls[2])
    valid, reason = validate_research_artifacts(directory, {"read_urls": urls})
    assert not valid and "source ledger" in reason
    (directory / "research_notes.md").write_text("Read pages\n" + "\n".join(urls))
    assert validate_research_artifacts(directory, {"read_urls": urls}) == (True, "")
    report = directory / "report.md"
    good = report.read_text()
    report.write_text(good.replace("Claims [1] and [2].", "Claims [1], [2], and [3].") +
                      "\n[3] Search-only source — https://unread.example/claim\n")
    valid, reason = validate_research_artifacts(directory, {"read_urls": urls})
    assert not valid and "successfully read page" in reason
