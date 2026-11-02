"""Guest policy lifecycle stamps do not constitute authorization grants."""
import pytest

from plugins.platforms.telegram.telegram_chat_settings import (
    chat_policy_generation, load_chat_settings, update_chat_settings,
)


def test_guest_policy_revision_detects_off_on_and_is_exact_group_scoped(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n")
    other = chat_policy_generation(tmp_path, "group", "-200")
    update_chat_settings(tmp_path, "group", "-100", {"allow_all": True})
    first = chat_policy_generation(tmp_path, "group", "-100")
    settings = load_chat_settings(tmp_path, "group", "-100")
    update_chat_settings(tmp_path, "group", "-100", {"allow_all": False})
    second = chat_policy_generation(tmp_path, "group", "-100")
    update_chat_settings(tmp_path, "group", "-100", {"allow_all": True})
    third = chat_policy_generation(tmp_path, "group", "-100")
    assert len({first, second, third}) == 3
    assert load_chat_settings(tmp_path, "group", "-100") == settings
    assert chat_policy_generation(tmp_path, "group", "-200") == other
    assert chat_policy_generation(tmp_path, "forum", "-100") == third
    assert "policy_revision" not in settings


def test_explicit_noop_guest_policy_write_invalidates_old_turns(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n")
    update_chat_settings(tmp_path, "group", "-100", {"allow_all": False})
    first = chat_policy_generation(tmp_path, "group", "-100")
    update_chat_settings(tmp_path, "group", "-100", {"allow_all": False})
    assert chat_policy_generation(tmp_path, "group", "-100") != first


@pytest.mark.parametrize("revision", ["invalid", 3, True, None, []])
def test_corrupt_guest_policy_revision_fails_closed(tmp_path, revision):
    import yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "platforms": {"telegram": {"chat_access": {"groups": {
            "-100": {"allow_all": True, "policy_revision": revision},
        }}}},
    }))
    with pytest.raises(ValueError, match="policy revision"):
        chat_policy_generation(tmp_path, "group", "-100")
