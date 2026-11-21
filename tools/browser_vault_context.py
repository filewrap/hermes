"""Run a vault fill, or an automatic one-time-code entry, inside the selected backend's ``fill_context``
(a plugin backend's lock/snapshot/manager session).

A plugin backend's failure arrives as ``PluginBackendError`` (fixed text, see
``agent/vault_backends/plugin.py``) and becomes a typed refusal; built-in backends behave as before.
"""
import json
from functools import wraps


def plugin_errors_as_refusal(fn):
    """A ``PluginBackendError`` raised anywhere in ``fn`` (metadata read, context enter/exit, resolve)
    becomes the ``backend_unavailable`` refusal."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from agent.vault_backends.plugin import PluginBackendError

        try:
            return fn(*args, **kwargs)
        except PluginBackendError as exc:
            return json.dumps({"success": False, "error_type": "backend_unavailable", "error": str(exc)})
    return wrapped


def write_backend_code(backend, handle, write):
    """Mint the backend's one-time code for ``handle`` and ``write`` it, both inside ``fill_context``, as a
    password fill is. ``None`` when the backend has no code (or failed to give one): the caller asks the user."""
    from agent.vault_backends.plugin import PluginBackendError

    written = None
    try:
        with backend.fill_context():
            try:
                code = backend.resolve_otp(handle)
            except Exception:  # health: allow BLE001 -- no automatic code; the user is asked instead
                code = None
            if code:
                written = write(code)
    except PluginBackendError:  # the plugin's context failed to open or close; a code already written stands
        pass
    return written
