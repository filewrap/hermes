"""Portable, profile-scoped Telegram setup fields backed by existing runtime settings."""
from __future__ import annotations

FIELDS = {
    # Select existing profile values only; prerequisite provisioning remains CLI-only.
    "terminal.backend": {
        "section": "terminal", "label": "Terminal backend", "type": "option", "confirm": True,
        "options": tuple((value, label) for value, label in (
            ("local", "Local machine"), ("docker", "Docker/Podman"),
            ("singularity", "Singularity/Apptainer (Linux)"), ("modal", "Modal cloud sandbox"),
            ("ssh", "SSH remote machine"), ("daytona", "Daytona cloud environment"),
            ("vercel_sandbox", "Vercel Sandbox"),
        )),
        "help": "Choose only among built-in backend names. This changes where shell/code tools execute and may affect isolation, file access and costs. It does not install, authenticate, probe or provision the backend; setup prerequisites must already be satisfied. Plugin backends remain CLI-only.",
        "confirmation_help": "Confirm that you intend to change the execution environment for future sessions. No connection or command is tested.",
    },
    "terminal.cwd": {
        "section": "terminal", "label": "Working directory", "type": "path",
        "help": "Directory used as the terminal working directory for messaging tools.",
    },
    "terminal.timeout": {
        "section": "terminal", "label": "Terminal command timeout", "type": "int",
        "min": 1, "max": 3600, "help": "Seconds allowed for one foreground terminal command.",
    },
    "compression.enabled": {
        "section": "compression", "label": "Context compression", "type": "bool",
        "help": "Allow Hermes to compact long conversations when context is running low.",
    },
    "compression.threshold": {
        "section": "compression", "label": "Compression threshold", "type": "float",
        "min": 0.1, "max": 0.95, "help": "Fraction of the model context window that triggers compression.",
    },
    "compression.target_ratio": {
        "section": "compression", "label": "Compression target ratio", "type": "float",
        "min": 0.05, "max": 0.5, "help": "Fraction of the threshold to preserve as recent context.",
    },
    "compression.protect_last_n": {
        "section": "compression", "label": "Recent messages to protect", "type": "int",
        "min": 0, "max": 200, "help": "Minimum recent messages kept outside the compressed summary.",
    },
    "compression.progress_notices": {
        "section": "compression", "label": "Compression progress notices", "type": "bool",
        "help": "Show progress notices during compression where the current platform supports them.",
    },
    "compression.checkpoint_required": {
        "section": "compression", "label": "Require compression checkpoint", "type": "bool",
        "help": "Require a recoverable checkpoint before compression; some providers/modes cannot honor this and may refuse the setting.",
    },
    "display.show_reasoning": {
        "section": "display", "label": "Show reasoning", "type": "bool",
        "help": "Display reasoning where the selected provider/model supplies it.",
    },
    "display.interim_assistant_messages": {
        "section": "display", "label": "Interim assistant messages", "type": "bool",
        "help": "Show natural progress messages between tool calls in gateway conversations.",
    },
    "display.compact": {
        "section": "display", "label": "Compact output", "type": "bool",
        "help": "Use compact response presentation where supported.",
    },
    "display.timestamps": {
        "section": "display", "label": "Show timestamps", "type": "bool",
        "help": "Show message timestamps in supported interfaces.",
    },
    "display.show_cost": {
        "section": "display", "label": "Show estimated cost", "type": "bool",
        "help": "Show local estimated cost in the CLI status bar (not a billing total).",
    },
    "display.platforms.telegram.tool_progress": {
        "section": "display", "label": "Tool progress", "type": "option",
        "options": (("off", "Off"), ("new", "New tools"), ("all", "All tools"), ("log", "Log only")),
        "help": "Progress visibility for future turns. Verbose argument/result output remains CLI-only to avoid exposing sensitive data.",
    },
    "stt.enabled": {
        "section": "voice", "label": "Speech transcription", "type": "bool",
        "help": "Allow incoming voice/audio transcription where a platform supports it.",
    },
    "stt.echo_transcripts": {
        "section": "voice", "label": "Echo transcripts", "type": "bool",
        "help": "Send the recognized transcript back as a separate message.",
    },
    "stt.language": {
        "section": "voice", "label": "Transcription language", "type": "language",
        "help": "Language hint such as en, es, zh, or auto.",
    },
    "tts.provider": {
        "section": "voice", "label": "TTS provider", "type": "option", "confirm": True,
        "options": tuple((value, label) for value, label in (
            ("edge", "Edge TTS (cloud, no key)"), ("elevenlabs", "ElevenLabs (API key required)"),
            ("openai", "OpenAI (API key or managed account required)"), ("xai", "xAI (OAuth or API key required)"),
            ("minimax", "MiniMax (API key required)"), ("mistral", "Mistral Voxtral (API key required)"),
            ("gemini", "Google Gemini (API key required)"), ("neutts", "NeuTTS (local install/model required)"),
            ("kittentts", "KittenTTS (local install/model required)"),
        )),
        "help": "Sets only the existing tts.provider profile value. It does not capture credentials, start OAuth, install local packages/download models, or generate audio. A provider without its prerequisites may not work.",
        "confirmation_help": "Confirm the provider preference only; no secrets are requested and no audio is generated.",
    },
    "tts.edge.voice": {
        "section": "voice", "label": "Edge TTS voice", "type": "voice",
        "help": "Voice name used when the existing TTS provider is Edge; provider/key setup stays in the CLI.",
    },
    "tts.openai.voice": {
        "section": "voice", "label": "OpenAI TTS voice", "type": "option",
        "options": tuple((voice, voice.title()) for voice in (
            "alloy", "ash", "ballad", "cedar", "coral", "echo", "fable", "marin",
            "nova", "onyx", "sage", "shimmer", "verse",
        )),
        "help": "Voice preference used only when your already-configured TTS provider is OpenAI. Does not select a provider or generate paid audio.",
    },
    "streaming.enabled": {
        "section": "gateway", "label": "Gateway response streaming", "type": "bool",
        "help": "Stream gateway responses progressively; may add platform edit/draft requests and needs a gateway restart.",
    },
    "display.platforms.telegram.streaming": {
        "section": "gateway", "label": "Telegram streaming preference", "type": "bool",
        "help": "Per-Telegram preference; the global streaming switch still gates streaming. Restart the gateway to apply.",
    },
    "gateway.message_timestamps.enabled": {
        "section": "gateway", "label": "Add message timestamps to context", "type": "bool",
        "help": "Include send-time metadata in future gateway model context; existing transcript storage is unchanged.",
    },
    "agent.gateway_timeout": {
        "section": "agent", "label": "Gateway inactivity timeout", "type": "int",
        "min": 0, "max": 86400, "help": "Seconds before an idle messaging turn is stopped; 0 means unlimited.",
    },
    "agent.max_turns": {
        "section": "agent", "label": "Maximum tool iterations", "type": "int",
        "min": 0, "max": 10000,
        "help": "Tool-calling iteration cap for newly built sessions; 0 means unlimited. Higher limits can use more tokens.",
    },
    "agent.tool_use_enforcement": {
        "section": "agent", "label": "Tool-use guidance", "type": "option",
        "options": (("auto", "Automatic"), (True, "Always on"), (False, "Off")),
        "help": "Prompt guidance to use tools instead of only describing actions; changes apply to newly built sessions.",
    },
    "agent.stall_guards": {
        "section": "agent", "label": "Stall guards", "type": "bool",
        "help": "Enable safeguards that detect stalled or repeating agent work.",
    },
    "agent.task_completion_guidance": {
        "section": "agent", "label": "Task-completion guidance", "type": "bool",
        "help": "Include task-completion guidance in newly built agent sessions.",
    },
    "agent.verify_guidance": {
        "section": "agent", "label": "Verification guidance", "type": "bool",
        "help": "Include verification guidance in newly built agent sessions.",
    },
    "agent.verify_on_stop": {
        "section": "agent", "label": "Verify before stopping", "type": "bool",
        "help": "Enable the existing verify-on-stop behavior for new sessions.",
    },
    "agent.environment_probe": {
        "section": "agent", "label": "Environment probe", "type": "bool",
        "help": "Allow the existing environment probe to inform newly built sessions.",
    },
    "memory.memory_enabled": {
        "section": "memory", "label": "Persistent memory", "type": "bool",
        "help": "Include Hermes' built-in persistent memory in newly built session prompts.",
    },
    "memory.user_profile_enabled": {
        "section": "memory", "label": "User profile memory", "type": "bool",
        "help": "Include the saved user profile in newly built session prompts.",
    },
    "memory.write_approval": {
        "section": "memory", "label": "Approve memory writes", "type": "bool", "confirm": True,
        "help": "Require explicit approval before memory changes are committed.",
    },
    "memory.nudge_interval": {
        "section": "memory", "label": "Memory review interval", "type": "int",
        "min": 0, "max": 1000, "help": "Completed turns between built-in memory reviews; 0 disables periodic review.",
    },
    "skills.write_approval": {
        "section": "skills", "label": "Approve skill changes", "type": "bool", "confirm": True,
        "help": "Stage skill changes for review instead of committing them immediately.",
    },
    "browser.command_timeout": {
        "section": "browser", "label": "Browser command timeout", "type": "int",
        "min": 1, "max": 3600, "help": "Seconds allowed for one browser command. Backend setup remains in the CLI.",
    },
    "browser.inactivity_timeout": {
        "section": "browser", "label": "Browser inactivity timeout", "type": "int",
        "min": 0, "max": 86400, "help": "Seconds before an inactive browser session expires; 0 disables the timeout.",
    },
}

SECTIONS = {
    "terminal": ("Terminal", ("terminal.backend", "terminal.cwd", "terminal.timeout")),
    "voice": ("Voice", ("stt.enabled", "stt.echo_transcripts", "stt.language", "tts.provider", "tts.edge.voice", "tts.openai.voice")),
    "gateway": ("Gateway and platform preferences", (
        "streaming.enabled", "display.platforms.telegram.streaming",
        "gateway.message_timestamps.enabled",
    )),
    "compression": ("Context compression", (
        "compression.enabled", "compression.threshold", "compression.target_ratio",
        "compression.protect_last_n", "compression.progress_notices", "compression.checkpoint_required",
    )),
    "agent": ("Agent settings", (
        "agent.gateway_timeout", "agent.max_turns", "agent.tool_use_enforcement",
        "agent.stall_guards", "agent.task_completion_guidance", "agent.verify_guidance",
        "agent.verify_on_stop", "agent.environment_probe",
    )),
    "memory": ("Memory", (
        "memory.memory_enabled", "memory.user_profile_enabled", "memory.write_approval",
        "memory.nudge_interval",
    )),
    "skills": ("Skills", ("skills.write_approval",)),
    "display": ("Display", (
        "display.show_reasoning", "display.interim_assistant_messages", "display.compact",
        "display.timestamps", "display.show_cost", "display.platforms.telegram.tool_progress",
    )),
    "browser": ("Browser preferences", ("browser.command_timeout", "browser.inactivity_timeout")),
}

# Menu entries route editable fields to this UI, and other functions to the existing controls.
HOME_ITEMS = (
    ("Terminal", "Backend selection, working directory and timeout", "section:terminal"),
    ("Voice", "Speech transcription, TTS provider and voice preferences", "section:voice"),
    ("Gateway preferences", "Streaming and message timestamps", "section:gateway"),
    ("Context compression", "Compression behavior, progress and preservation limits", "section:compression"),
    ("Agent settings", "Gateway inactivity, tool limits and prompt guidance", "section:agent"),
    ("Memory", "Profile memory and write approval", "section:memory"),
    ("Skills", "Skill-change approval; manage/install skills in the existing controller", "section:skills"),
    ("Personal settings and memory", "Your existing private Telegram preferences", "nav:settings"),
    ("Display", "Reasoning, progress, and presentation preferences", "section:display"),
    ("Models", "Existing model controls", "nav:model"),
    ("Providers", "Existing provider controls", "nav:providers"),
    ("Tools", "Review staged Telegram toolsets for the next message turn", "nav:setuptools"),
    ("Browser", "Existing browser controls and profile timeouts", "nav:browser"),
    ("Browser timeouts", "Command and inactivity limits", "section:browser"),
    ("Dashboard / Web UI", "Existing Dashboard and Web UI service controls", "nav:dashboard"),
    ("Threads", "Existing Telegram topic controls", "nav:thread"),
    ("CLI command inventory", "All commands, nested options and CLI-only paths", "inventory:0"),
    ("Logs and diagnostics", "Existing private diagnostics and log controls", "nav:logs"),
)

HUB_COMMANDS = (
    ("Personal settings and memory", "settings"), ("Models", "model"),
    ("Providers", "providers"), ("Skills", "skills"),
    ("Browser", "browser"), ("Dashboard / Web UI", "dashboard"),
    ("Threads", "thread"), ("Logs and diagnostics", "logs"), ("Tools", "setuptools"),
    ("MCP servers", "mcp"),
)

HOME_PAGE_SIZE = 6
