"""Load-gate behaviour contracts (audit 6.1): load only for website /
webapp requests from admitted users, one active run per thread, every
decline carrying a one-line redirect — never a silent load.
"""
from gateway.web_designer import load_gate, store


def _decide(tmp_path, text, *, admitted=True):
    home = tmp_path / "home"
    return load_gate.decide_load(
        text, admitted=admitted, home=str(home),
        thread_id="t1", user_id="u1", chat_id="c1",
    )


def test_a_website_request_loads_and_starts_the_run(tmp_path):
    result = _decide(tmp_path, "Build me a portfolio website for my photos")
    assert result["load"] is True
    assert result["redirect"] is None
    record = result["run"]
    assert record.status == "running"
    assert "load_gate" in record.completed_stages
    assert record.stage == "intake"
    assert store.load_run(str(tmp_path / "home"), record.run_id) is not None


def test_a_webapp_request_with_a_tech_signal_loads(tmp_path):
    result = _decide(tmp_path, "Make a page in HTML and CSS for our launch")
    assert result["load"] is True


def test_a_mobile_app_request_is_declined_with_a_redirect(tmp_path):
    result = _decide(tmp_path, "Build me an iOS app for tracking habits")
    assert result["load"] is False
    assert result["reason"] == "out_of_scope"
    assert "website" in result["redirect"]
    assert result["run"] is None


def test_branding_and_general_coding_are_declined(tmp_path):
    for text in ("Design a logo for my bakery",
                 "Write a python script to rename files"):
        result = _decide(tmp_path, text)
        assert result["load"] is False
        assert result["redirect"], text


def test_an_ambiguous_app_request_asks_one_question_and_does_not_load(tmp_path):
    result = _decide(tmp_path, "Build me an app")
    assert result["load"] is False
    assert result["reason"] == "ambiguous"
    assert result["redirect"].endswith("?")
    assert result["run"] is None


def test_an_unadmitted_user_never_loads(tmp_path):
    result = _decide(tmp_path, "Build me a website", admitted=False)
    assert result["load"] is False
    assert result["reason"] == "not_admitted"


def test_a_second_run_on_the_same_thread_is_refused(tmp_path):
    first = _decide(tmp_path, "Build me a website")
    assert first["load"] is True
    second = _decide(tmp_path, "Build me another website")
    assert second["load"] is False
    assert second["reason"] == "active_run"
    assert first["run"].run_id in second["redirect"]
