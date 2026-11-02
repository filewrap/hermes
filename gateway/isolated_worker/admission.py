"""Trusted transport-to-worker admission, separate from invocation triggers.

Not wired into the deployed adapter. Guests keep the restricted runtime. Global
allow users do not inherit guest capability restrictions. All store errors deny.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import Binding, BoundaryDenied


@dataclass(frozen=True)
class Admission:
    binding: Binding
    _store: Any
    _source: Any

    def current(self, binding: Binding) -> bool:
        from gateway.tenant_runtime import _TransportIdentity, _authorization_stamp
        try:
            identity = _TransportIdentity.capture(self._source)
            global_access = self._store.get_access(identity.user_id)
            return (binding == self.binding
                    and identity.user_id == binding.identity
                    and identity.chat_id == binding.chat_id
                    and identity.chat_type == binding.chat_type
                    and identity.thread_id == binding.thread_id
                    and _authorization_stamp(self._store, identity) == binding.authorization_stamp
                    and global_access.get("allowed") is True
                    and global_access.get("banned") is False)
        except Exception:
            return False


def capture_admission(store: Any, source: Any) -> Admission:
    """Only transport-normalized source and durable global grants authorize.

    Read global access separately: scoped sudo grants must not upgrade a group
    guest into a globally authorized identity.
    """
    from gateway.tenant_runtime import _TransportIdentity, _authorization_stamp
    try:
        identity = _TransportIdentity.capture(source)
        stamp = _authorization_stamp(store, identity)
        access = store.get_access(identity.user_id)
        if access.get("allowed") is not True or access.get("banned") is not False:
            raise ValueError
        binding = Binding(identity.user_id, stamp, identity.chat_id, identity.chat_type,
                          identity.thread_id)
        admission = Admission(binding, store, source)
        if not admission.current(binding):
            raise ValueError
        return admission
    except Exception:
        raise BoundaryDenied("Full worker authorization unavailable") from None
