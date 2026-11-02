"""Telegram env -> gateway config bridge (``PlatformEntry.env_overrides``).

Fork: the credential gate requires ALL THREE MTProto credentials (API_ID, API_HASH,
BOT_TOKEN) — the Telethon adapter cannot connect on a bot token alone. Upstream's
``_fallback_ips`` step is deliberately absent: the fork deleted the fallback-IPs
transport path (retired option; see ``adapter.py`` ``_apply_yaml_config``).
"""

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platform_env import EnvCred, home_step, reply_mode_step

ENV_ENABLE_CREDENTIALS = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN")
# Order matters: the reply mode may create the entry the home channel then attaches to.
ENV_OVERRIDES = (
    EnvCred(
        Platform.TELEGRAM,
        ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"),
        token="TELEGRAM_BOT_TOKEN",
    ),
    reply_mode_step(Platform.TELEGRAM, "TELEGRAM_REPLY_TO_MODE"),
    home_step(Platform.TELEGRAM, "TELEGRAM_HOME_CHANNEL"),
)

# Authorization env names (``PlatformEntry`` fields). Core reads them from this file when the
# live entry lacks them: a user plugin overriding this platform, safe mode, the plugin disabled.
AUTHZ = {
    "allowed_users_env": "TELEGRAM_ALLOWED_USERS",
    "allow_all_env": "TELEGRAM_ALLOW_ALL_USERS",
    "allow_bots_env": "TELEGRAM_ALLOW_BOTS",
    "group_allowed_chats_env": "TELEGRAM_GROUP_ALLOWED_CHATS",
    "allowlist_suppresses_pairing": True,
}
