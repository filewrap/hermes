"""Last-used-route cache for the dangling-route rescue + ``fallback.auto_fallback``.

The rescue fires when the selected provider holds no credentials but is
still the route. It should prefer the route that just served a successful
turn over the first working provider's default model, and
``fallback.auto_fallback: false`` should disable the automatic rescue
without touching the explicit ``fallback_providers`` chain.

These tests pin:
  1. A cached (provider, model) pair wins over the first working provider's
     default model.
  2. With ``fallback.auto_fallback: false`` in a temp config.yaml, an
     exhausted chain on a credential-less route returns False (the turn
     fails instead of switching), while the explicit fallback chain still
     activates.
  3. Only completed turns record a route: a failed turn leaves no cache,
     and ``auto``/empty routes are never recorded.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_constants import get_hermes_home
from run_agent import AIAgent
from agent.error_classifier import FailoverReason
from agent.last_used_route import read_last_used_route, record_last_used_route


def _make_agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=None,
        )
        agent.client = MagicMock()
        agent.provider = "openai-codex"
        agent.model = "gpt-5.6-luna-900k"
        return agent


def _mock_client(base_url="https://api.openai.com/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


def _rescue_mocks(agent):
    """Patch the rescue's credential checks + client resolution.

    Only ``openai`` holds credentials; the first working provider would be
    ``gemini`` on its default model — so a rescue landing on OpenAI proves
    the cache won, not the default walk.
    """

    def _has_credentials(slug):
        return slug == "openai"

    patches = [
        patch(
            "agent.chat_completion_helpers._provider_has_hermes_credentials",
            side_effect=_has_credentials,
        ),
        patch(
            "agent.chat_completion_helpers._first_working_provider",
            return_value=("gemini", "gemini-2.5-flash"),
        ),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o-mini"),
        ),
    ]
    for p in patches:
        p.start()
    agent._rescue_patches = patches
    return patches


def _stop_rescue_mocks(agent):
    for p in getattr(agent, "_rescue_patches", []):
        p.stop()


class TestLastUsedRouteRescue:
    def test_rescue_prefers_cached_last_used_route_over_first_working_provider(self):
        assert record_last_used_route(SimpleNamespace(provider="openai", model="gpt-4o-mini")) is True
        assert read_last_used_route() == ("openai", "gpt-4o-mini")
        agent = _make_agent()
        _rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openai"
        assert agent.model == "gpt-4o-mini"
        assert agent._dangling_route_rescue == {
            "from_provider": "openai-codex", "to_provider": "openai", "to_model": "gpt-4o-mini",
        }

    def test_auto_fallback_off_disables_rescue_but_explicit_chain_still_works(self):
        home = get_hermes_home()
        (home / "config.yaml").write_text(
            "fallback:\n  auto_fallback: false\n", encoding="utf-8",
        )
        agent = _make_agent()
        _rescue_mocks(agent)
        try:
            # Automatic rescue disabled: the exhausted chain fails the turn.
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is False
            assert agent.provider == "openai-codex"
            assert getattr(agent, "_dangling_route_rescue", None) is None
            # The explicit chain is unaffected by the toggle.
            agent._fallback_chain = [{"provider": "openai", "model": "gpt-4o"}]
            agent._fallback_index = 0
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openai"
        assert agent.model == "gpt-4o"


class TestLastUsedRouteRecording:
    def test_only_completed_turns_record_a_route(self):
        from agent.conversation_loop import _record_last_used_route

        agent = SimpleNamespace(provider="openai", model="gpt-4o-mini")
        _record_last_used_route(agent, {"completed": False, "final_response": "boom"})
        assert read_last_used_route() is None
        _record_last_used_route(agent, {"completed": True, "final_response": "done"})
        assert read_last_used_route() == ("openai", "gpt-4o-mini")

    def test_auto_and_empty_routes_are_never_recorded(self):
        assert record_last_used_route(SimpleNamespace(provider="auto", model="gpt-4o")) is False
        assert record_last_used_route(SimpleNamespace(provider="openai", model="")) is False
        assert record_last_used_route(SimpleNamespace(provider="", model="gpt-4o")) is False
        assert read_last_used_route() is None

def _credentialed_rescue_mocks(agent, *, first_working=("openai", "gpt-4o-mini")):
    """Rescue mocks where OpenRouter still holds credentials: the failing
    route is a dead model / a broke paid model on a live provider, not a
    removed provider. The catalog walk would land on ``first_working`` —
    openai here because the harness can actually bind it (a nous target is
    skipped as locally unusable without a token, same as production)."""

    def _has_credentials(slug):
        return slug in {"openrouter", "openai", "nous"}

    patches = [
        patch(
            "agent.chat_completion_helpers._provider_has_hermes_credentials",
            side_effect=_has_credentials,
        ),
        patch(
            "agent.chat_completion_helpers._first_working_provider",
            return_value=first_working,
        ),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "x"),
        ),
    ]
    for p in patches:
        p.start()
    agent._rescue_patches = patches
    return patches


class TestDeadRouteRescue:
    """Retired-model 404 / billing 402 on a credentialed provider must rescue
    the turn to a working route instead of failing it — no top-up is the fix.

    The rescue prefers the cached last-used route (excluding only the dead
    route, not the whole provider — ``openrouter/free`` is a valid target
    when ``openrouter/gemini-2.5-flash`` 402s), else the first working
    provider/model in the catalog. Rate limits keep their old behaviour.
    """

    def test_404_rescues_to_cached_same_provider_route(self):
        assert record_last_used_route(SimpleNamespace(provider="openrouter", model="openrouter/free")) is True
        agent = _make_agent()
        agent.provider = "openrouter"
        agent.model = "meta-llama/llama-3.3-8b-instruct:free"
        _credentialed_rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.model_not_found) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openrouter"
        assert agent.model == "openrouter/free"
        assert agent._dangling_route_rescue == {
            "from_provider": "openrouter", "to_provider": "openrouter", "to_model": "openrouter/free",
        }

    def test_402_rescues_to_cached_free_route(self):
        assert record_last_used_route(SimpleNamespace(provider="openrouter", model="openrouter/free")) is True
        agent = _make_agent()
        agent.provider = "openrouter"
        agent.model = "google/gemini-2.5-flash"
        _credentialed_rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.billing) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openrouter"
        assert agent.model == "openrouter/free"
        assert agent._dangling_route_rescue == {
            "from_provider": "openrouter", "to_provider": "openrouter", "to_model": "openrouter/free",
        }

    def test_402_without_cached_route_rescues_to_first_working_provider(self):
        assert read_last_used_route() is None
        agent = _make_agent()
        agent.provider = "openrouter"
        agent.model = "google/gemini-2.5-flash"
        _credentialed_rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.billing) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openai"
        assert agent._dangling_route_rescue == {
            "from_provider": "openrouter", "to_provider": "openai", "to_model": "gpt-4o-mini",
        }

    def test_402_does_not_rescue_back_to_the_dead_route(self):
        """The cache holding the just-failed route must not win: the rescue
        skips the dead (provider, model) pair and walks to the catalog."""
        assert record_last_used_route(
            SimpleNamespace(provider="openrouter", model="google/gemini-2.5-flash")
        ) is True
        agent = _make_agent()
        agent.provider = "openrouter"
        agent.model = "google/gemini-2.5-flash"
        _credentialed_rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.billing) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openai"
        assert agent._dangling_route_rescue["to_model"] == "gpt-4o-mini"

    def test_billing_does_not_rescue_when_affordable_clamp_already_spent(self):
        """A second credit-limited 402 is genuine exhaustion, not a dead route:
        the affordable clamp already retried once with the affordable budget
        (#49769), so the turn ends billing instead of rescuing to another route."""
        agent = _make_agent()
        agent.provider = "openrouter"
        agent.model = "google/gemini-2.5-flash"
        _credentialed_rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(
                reason=FailoverReason.billing, affordable_clamp_attempted=True
            ) is False
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openrouter"
        assert getattr(agent, "_dangling_route_rescue_attempted", False) is False

    def test_rate_limit_on_credentialed_provider_does_not_rescue(self):
        """Control: a rate limit on a live provider keeps the old behaviour —
        no rescue, the turn fails (or retries) as before."""
        agent = _make_agent()
        agent.provider = "openrouter"
        agent.model = "google/gemini-2.5-flash"
        _credentialed_rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is False
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openrouter"
        assert agent.model == "google/gemini-2.5-flash"
        assert getattr(agent, "_dangling_route_rescue", None) is None

    def test_auto_fallback_off_disables_billing_rescue(self):
        home = get_hermes_home()
        (home / "config.yaml").write_text(
            "fallback:\n  auto_fallback: false\n", encoding="utf-8",
        )
        assert record_last_used_route(SimpleNamespace(provider="openrouter", model="openrouter/free")) is True
        agent = _make_agent()
        agent.provider = "openrouter"
        agent.model = "google/gemini-2.5-flash"
        _credentialed_rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.billing) is False
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openrouter"
        assert getattr(agent, "_dangling_route_rescue", None) is None
