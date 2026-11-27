---
sidebar_position: 1
title: "Telegram"
description: "通过 Telethon 和 MTProto 将 Hermes 连接到 Telegram"
---

# Telegram

Hermes 通过 **Telethon 和 MTProto** 连接 Telegram bot。Gateway、实时媒体发送以及独立/cron 投递均使用此原生客户端；不使用 Bot API 轮询、`getUpdates`、webhook 或 HTTP 发送回退。

## 所需凭据

需要同时配置以下三项：

- 在 [my.telegram.org/apps](https://my.telegram.org/apps) 获取 **Telegram API ID** 和 **API hash**。
- 在 [@BotFather](https://t.me/BotFather) 获取 **bot token**。

仅有 bot token 不足以使用 MTProto。Hermes 会将 API hash 和 bot token 作为密钥保存；请勿放入 `config.yaml` 或提交到源代码仓库。

## 设置

运行 `hermes gateway setup` 并选择 Telegram，按提示输入 API ID、API hash 和 bot token。也可以配置以下环境变量：

```dotenv
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=123456:your_bot_token
```

可选的访问和投递设置：

```dotenv
TELEGRAM_ALLOWED_USERS=123456789,987654321
TELEGRAM_ALLOW_ALL_USERS=false
TELEGRAM_HOME_CHANNEL=-1001234567890
TELEGRAM_CRON_THREAD_ID=12345
TELEGRAM_PROXY=socks5://127.0.0.1:1080
```

`TELEGRAM_PROXY` 还支持 `socks4://` 和 `http://`（HTTP CONNECT）。未设置时，Hermes 使用直接连接或系统代理。Bot API 的备用 IP 和 HTTP 连接池设置不适用于此传输。

### 每个配置档的独立会话

Telethon 会在当前 Hermes 配置档中保存持久 bot 会话，通常位于该配置档 home 下的 `sessions/telegram/bot.session`。如果旧版 `telegram_sessions/` 目录中已有内容，则会继续使用。每个配置档都有独立会话；请勿在配置档或机器之间复制、共享 `.session` 文件，并将其视为凭据保护。

## 当前支持

- Telegram 文本、斜杠命令、照片、视频、音频/语音、文档和贴纸的收发。
- 私聊、群组和论坛话题回复路由。
- 群组 mention 门控、已配置的用户/聊天白名单，以及将回复 bot 自己的消息作为显式触发。
- 编辑/删除消息、输入状态、emoji reaction、链接预览控制，以及独立/cron 投递。
- 出站文本按 4096 个 UTF-16 code unit 分段；媒体 caption 上限为 1024 个 UTF-16 code unit。

像往常一样直接发送 Hermes 斜杠命令即可。可通过 `TELEGRAM_ALLOWED_USERS`、`TELEGRAM_GROUP_ALLOWED_USERS`、`TELEGRAM_GROUP_ALLOWED_CHATS` 及对应的平台白名单设置访问范围。

## 尚未从 Bot API 适配器迁移的功能

MTProto 传输不会使用下列 Bot API 功能或设置，也不会悄悄回退到 Bot API：

- `TELEGRAM_WEBHOOK_URL`、webhook 端口/密钥、Bot API 长轮询、`getUpdates` 和自托管/本地 Bot API 服务器。
- Bot API HTTP 超时/连接池参数、直接 IP 回退列表、Bot API file ID，以及 `api.telegram.org` REST 发送。
- Telegram UI 命令菜单注册、inline 查询结果选择器、inline 键盘 callback 工作流、按钮式审批/确认控件。交互式 Hermes 工作流使用文本/斜杠命令路径；旧的 inline 按钮不再是受支持的控制方式。
- Bot API 10.1 富文本/草稿渲染，以及 bot 全局资料状态/描述更新。
- Bot API 专属的高级 mention 模式路由、未提及群消息的被动观察和话题管理操作。基本显式 bot mention、白名单和回复路由仍可用。

如果旧配置中含有上述设置，请移除；它们不会配置 Telethon 客户端。

## 从 Bot API 传输迁移

1. 在 [my.telegram.org/apps](https://my.telegram.org/apps) 获取 API ID 和 API hash；保留现有 bot token。
2. 运行 `hermes gateway setup`，将三项凭据保存到正确的配置档。
3. 删除旧 webhook、HTTP 超时、备用 IP 或本地 Bot API 服务器设置。
4. 停止旧 gateway 进程并重新启动 Hermes。新客户端会创建或恢复该配置档的 MTProto 会话。

## 故障排查

- **缺少凭据：**检查当前配置档是否包含有效的 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH` 和 `TELEGRAM_BOT_TOKEN`。
- **代理连接失败：**检查代理 URL scheme 和凭据；支持 `socks5`、`socks4` 和 `http`。
- **群组消息未触发：**确认发送者/聊天白名单与 mention 要求；启用 mention 门控时，请提及 bot 或回复 bot 之前发送的消息。
- **会话文件访问问题：**移动配置档前先停止 gateway。Hermes 运行时不要删除或复制会话文件。

关于协议差异和 bot 登录要求，请参阅 Telethon 的 [Bot API 与 MTProto 指南](https://docs.telethon.dev/en/stable/concepts/botapi-vs-mtproto.html)及[登录文档](https://docs.telethon.dev/en/stable/basic/signing-in.html)。
