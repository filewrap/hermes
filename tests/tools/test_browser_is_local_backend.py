"""``is_local_backend``: no cloud provider configured means a local backend.

The browser SSRF enforcement this file used to pin is gone (browser_exec no longer
refuses private or metadata URLs); what remains is the backend-selection predicate
``browser_session_cloud.is_local_backend`` still consults.
"""

from tools import browser_session_cdp, browser_session_cloud


class TestIsLocalBackend:
    def test_no_cloud_provider_is_local(self, monkeypatch):
        """No cloud provider configured → local backend."""
        monkeypatch.setattr(browser_session_cdp, "get_cdp_override_raw", lambda: "")
        monkeypatch.setattr(browser_session_cloud, "get_cloud_provider", lambda: None)

        assert browser_session_cloud.is_local_backend() is True
