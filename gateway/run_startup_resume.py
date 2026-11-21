"""Auto-resume gating for interrupted turns on GatewayRunner.

Split out of ``gateway.run_startup`` (file-size ratchet); ``GatewayStartupMixin`` inherits it,
so these stay bound onto ``GatewayRunner`` via the MRO.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("gateway.run")


class GatewayStartupResumeMixin:
    """Resume gating: which interrupted sessions may auto-resume, and under which model."""

    def _resume_pending_candidates(self, platform=None) -> Optional[list]:
        """Snapshot resume-pending entries (optionally scoped to ``platform``); None when
        enumeration failed or the restart-loop breaker tripped for this boot."""
        try:
            with self.session_store._lock:
                self.session_store._ensure_loaded_locked()
                candidates = [
                    entry for entry in self.session_store._entries.values()
                    if entry.resume_pending
                    and not entry.suspended
                    and entry.origin is not None
                    and entry.resume_reason in self._AUTO_RESUME_REASONS
                    and (platform is None or entry.origin.platform == platform)
                ]
        except Exception as exc:
            logger.warning("Failed to enumerate resume-pending sessions: %s", exc)
            return None
        # Restart-loop breaker: only boots WITH restart-interrupted sessions count; when tripped, skip
        # auto-resume for THIS boot only (inbound still served; sessions stay resume_pending).
        if candidates:
            try:
                from gateway import restart_loop_guard as _rlg
                _max_restarts, _window, _max_gap = self._restart_loop_guard_config()
                if _rlg.check_and_record(_max_restarts, _window, max_gap_seconds=_max_gap):
                    return None
            except Exception as exc:
                logger.debug("Restart-loop guard check skipped: %s", exc)
        return candidates

    def _resume_owner_authorized(self, session_key: str, source) -> bool:
        """Validate the session owner against the CURRENT allowlist: a session created before the
        allowlist existed (or whose owner was since removed) must not silently receive a full agent
        response just because it carries a resume marker."""
        try:
            if self._is_user_authorized_for_source(source):
                return True
            logger.warning(
                "Skipping auto-resume for %s: session owner is no "
                "longer authorized under the current allowlist", session_key,
            )
        except Exception as exc:
            logger.warning("Skipping auto-resume for %s: authorization check failed: %s", session_key, exc)
        return False

    def _resume_model_allows(self, entry, source) -> bool:
        """True when the interrupted turn may resume under the session's current model.

        The marker carries the (provider, model) the interrupted turn ran under
        (``entry.resume_model``); the resume turn would resolve its model through the
        same chain as a normal turn, so a known mismatch means the model changed
        since the interruption — clear the flag (the partial transcript stays as
        history) and skip the resume turn instead of continuing it under a new
        model. Unknown on either side fails open: a marker that predates the
        stamp, or a resolution failure here, must not block a resume the turn
        itself would have handled.
        """
        from gateway.session_lifecycle import resume_model_matches, sanitize_turn_model, turn_model_key
        stamped = sanitize_turn_model(getattr(entry, "resume_model", None))
        if stamped is None:
            return True
        try:
            from gateway.run import _load_gateway_config
            from gateway.session_authorities import active_authority
            from gateway.session_policy import policy_for_source
            policy = policy_for_source(self, source)
            user_config = (
                policy.config(active_authority(self), secrets=False)
                if policy is not None
                else _load_gateway_config()
            )
            notice = getattr(self, "_pre_agent_fallback_notice", None)
            try:
                model, runtime_kwargs = self._resolve_session_agent_runtime(
                    source=source, session_key=entry.session_key,
                    user_config=user_config, enforce_visibility=False,
                )
            finally:
                self._pre_agent_fallback_notice = notice
        except Exception as exc:
            logger.debug(
                "Auto-resume model check failed for %s — allowing resume: %s",
                entry.session_key, exc, exc_info=True,
            )
            return True
        current = {"model": model, "provider": (runtime_kwargs or {}).get("provider")}
        if resume_model_matches(stamped, current):
            return True
        logger.info(
            "Skipping auto-resume for %s: model changed since the interrupted turn "
            "(%s -> %s); history kept, no resume turn",
            entry.session_key, turn_model_key(stamped), turn_model_key(current),
        )
        try:
            self.session_store.clear_resume_pending(entry.session_key)
        except Exception:
            logger.debug(
                "clear_resume_pending failed for model-changed %s", entry.session_key, exc_info=True,
            )
        return False
