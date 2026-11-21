"""AgentRouter provider profile.

AgentRouter (agentrouter.org) is a unified LLM gateway (One API fork) with an
OpenAI-compatible surface at https://agentrouter.org/v1. Two quirks shape this
profile, both verified live 2026-11-21:

- WAF gate: every request is rejected (401 ``unauthorized_client_error``)
  unless ``User-Agent`` is exactly ``claude-cli/2.0.0 (external, cli)``. The
  exact UA is baked in as ``default_headers`` so it reaches the turn client,
  the aux client, and the live model fetch — the profile's own declaration,
  not user config.
- Wire: the models this account serves (``gpt-5.6-sol``, ``gpt-6.1-sol``) 400
  on ``/v1/chat/completions`` ("does not support /v1/chat/completions, please
  use /v1/responses") and work on ``/v1/responses``. ``api_mode="codex_responses"``
  + the ``agentrouter.org`` host mandate in ``hermes_cli/providers.py`` (and
  its mirror in ``hermes_cli/runtime_provider.py``) keep every path on the
  native wire — the Ramp Router precedent. ``api_mode_for_model`` returns
  ``codex_responses`` for every model id as well: the base hook returns None,
  and the resolver's fallback heuristic only matches ``gpt-5*``, so without
  the override ``gpt-6.1-sol`` would stay on chat completions at agent init
  and 400. (The mandate alone does not cover init: ``_resolve_api_mode``
  checks it last, after the provider rewrites, and the init auto-upgrade
  consults the profile hook.)

The account's model list drifts (``gpt-6-astra`` was listed Nov 15, gone
Nov 21): the picker/live paths fetch ``/v1/models`` first, and
``fallback_models`` / ``static_models`` pin the two ids verified live as the
fallback only. ``max_output_tokens`` is omitted by default and AgentRouter
accepts that (verified 200); the >= 16 rule applies only when the field is
sent, so nothing floors it here.
"""

from providers import register_provider
from providers.base import ProviderProfile

# The WAF gate: this exact string, no variation. Anything else (curl, plain
# httpx, the OpenAI/Python SDK UA, claude-code/codex UAs) 401s.
_AGENTROUTER_USER_AGENT = "claude-cli/2.0.0 (external, cli)"


class AgentRouterProfile(ProviderProfile):
    """AgentRouter — Responses-only routing, WAF User-Agent baked in."""

    def api_mode_for_model(self, model: str, *, api_key: str | None = None) -> str | None:
        """Every model AgentRouter serves goes over the Responses API."""
        return "codex_responses"


agentrouter = AgentRouterProfile(
    name="agentrouter",
    aliases=("agent-router",),
    api_mode="codex_responses",
    env_vars=("AGENTROUTER_API_KEY",),
    display_name="AgentRouter",
    description="AgentRouter — unified LLM gateway (OpenAI-compatible)",
    signup_url="https://agentrouter.org/",
    base_url="https://agentrouter.org/v1",
    hostnames=("agentrouter.org",),
    fallback_models=("gpt-6.1-sol", "gpt-5.6-sol"),
    static_models=("gpt-6.1-sol", "gpt-5.6-sol"),
    default_aux_model="gpt-6.1-sol",
    default_headers={"User-Agent": _AGENTROUTER_USER_AGENT},
)

register_provider(agentrouter)
