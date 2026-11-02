"""The gateway CLI setup surfaces expose only Telegram."""


import pytest


def test_all_platforms_contains_only_telegram():
    import hermes_cli.gateway as gateway_mod

    platforms = gateway_mod._all_platforms()
    assert {platform["key"] for platform in platforms} == {"telegram"}
class TestMatrixHiddenOnWindows:

    @pytest.mark.platforms("windows")
    def test_matrix_absent_on_windows(self):
        """The gate itself: matrix must be dropped on a real Windows host.

        A patched ``sys.platform`` proved only that the ``if`` branch runs;
        on native Windows this also proves the picker the user actually sees
        omits the platform whose dependency cannot build here.
        """
        import hermes_cli.gateway as gateway_mod

        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        assert "matrix" not in keys, "matrix must be hidden on Windows"
