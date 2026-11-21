"""Small turn-runner support helpers: approval-button probing, viewer turns, persisted-turn
receipts, API runtime locks and resolution-failure replies.

Split out of ``gateway.run_turn_runner`` (file-size ratchet); the names stay importable from the
facade so existing imports keep working.
"""

from __future__ import annotations

from agent.i18n import t
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter


def _renders_exec_approval_buttons(adapter_cls: type) -> bool:
    """True when the adapter class renders native approval buttons. BasePlatformAdapter subclasses
    say so through ``supports_exec_approval_buttons``; anything else (test doubles, relay-style
    duck types) counts when it defines ``send_exec_approval`` itself."""
    probe = getattr(adapter_cls, "supports_exec_approval_buttons", None)
    if callable(probe) and issubclass(adapter_cls, BasePlatformAdapter):
        return bool(probe())
    return getattr(adapter_cls, "send_exec_approval", None) is not None


# Rendered on a native clarify card whose wait ended without a click (mirrors the notice the
# Slack click handler shows on a dead entry).
def _clarify_expired_notice() -> str:
    return t("gateway.clarify.expired")


def _finite_viewer_turn() -> bool:
    from gateway.session_finite import finite_turn_required
    return finite_turn_required() is True


def _persisted_turn(agent, agent_history, result, compressions) -> dict | None:
    """``message.complete.persisted_turn`` for this turn, the receipt the TUI gateway's own turns
    publish: clients bind the streamed reply to its stored row instead of comparing words. The
    agent copies ``conversation_history`` into its message list, so an untouched pre-turn prefix
    is the same dicts (transcript rows here carry no ``_row_id`` to compare)."""
    from agent.persisted_turn_receipt import persisted_turn_receipt
    if not isinstance(result, dict):
        return None
    status = ("interrupted" if result.get("interrupted")
              else "error" if result.get("failed") or result.get("error") else "complete")
    return persisted_turn_receipt(
        result.get("messages"), getattr(agent, "_persist_user_message_idx", None), agent_history,
        result.get("final_response"), status,
        compression_unchanged=type(compressions) is int and compressions == getattr(
            getattr(agent, "context_compressor", None), "compression_count", None),
        prefix_row_matches=lambda before, after: before is after)


def _settle_api_runtime_lock(agent, result, captured):
    """A confirmed API model lock settles only on the runtime it named (main's
    ``_finish_turn_result``): the served runtime rides the usage, a mismatch fails the turn."""
    from gateway.session_api_turn import api_runtime_locked
    api = api_runtime_locked()
    if api is None or not isinstance(result, dict):
        return
    settings = api['settings']
    try:
        captured['usage']['runtime'] = api['adapter']._turn_runtime_metadata(
            agent, route=settings.get('route'), requested_runtime=settings.get('requested_runtime'),
            route_source=settings.get('route_source') or '', confirmed_runtime_lock=True)
    except RuntimeError as exc:
        result.update(final_response='', failed=True, completed=False, error=str(exc))


def _resolution_failure_reply(exc: Exception, platform: Platform) -> tuple[str, str, bool]:
    """``(chat text, failure_reason, retryable)`` for a model/credential resolution failure before the turn."""
    from agent.error_classifier import FailoverReason
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    from hermes_cli.runtime_provider_custom import LocalRuntimeUnavailableError
    from hermes_state_runtime import RuntimeStoreError
    auth_permanent = FailoverReason.auth_permanent.value
    if isinstance(exc, LocalRuntimeUnavailableError):
        # The chosen model is configured; its local server is down. Not a credential verdict, so
        # the client must not offer "Update API key" or sign-in: the sentence names the fix.
        return f"⚠️ {exc}", exc.error_code, exc.retryable
    if isinstance(exc, RuntimeStoreError) and exc.reason == 'launch_credentials_unavailable':
        # The session's launch key is gone (a restart revoked a CLI --api-key, a route key changed):
        # the shared refusal copy names /new, never a resend.
        from agent.turn_failure_copy import launch_credentials_copy
        return launch_credentials_copy(exc), 'launch_credentials_unavailable', False
    if isinstance(exc, RuntimeStoreError):
        # Session-policy refusals carry a stable reason code (e.g. a CLI launch key revoked by
        # daemon restart): keep it in the reply so clients can act on it, and do not suggest
        # /login — the profile's own credentials were never in play.
        return (f"⚠️ This session's launch credentials are no longer available ({exc.reason}), so this "
                "message wasn't processed. Start a new session from the CLI to bind them again.", auth_permanent, False)
    if is_rate_limited_auth_error(exc.__cause__):
        # Quota cap with valid credentials: /login cannot help; name the reset window (#89401).
        from gateway.run import _gateway_provider_error_reply
        return _gateway_provider_error_reply(str(exc)), auth_permanent, False
    if platform == Platform.LOCAL:
        # The local operator reads the terminal: the resolver's own sentence ("provider 'custom'
        # resolved without credentials ...") is the diagnosis; /login is not.
        return f"⚠️ {exc}", auth_permanent, False
    seen: set[int] = set()
    host = auth = False
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        host = host or isinstance(link, OSError)
        auth = auth or isinstance(link, AuthError)
        link = link.__cause__ or link.__context__
    if host and not auth:
        # A host-side failure (a CA bundle deleted under a long-lived process, an unreadable file)
        # with no credential verdict: /login cannot fix it, the gateway log can (#140344).
        # host_error sits on the gateway layer, so clients don't offer "Switch provider"; not retryable,
        # since a deleted bundle or unreadable store fails the same way until the host is fixed.
        return t("gateway.errors.generic_failed"), "host_error", False
    return t("gateway.errors.no_credentials"), auth_permanent, False
