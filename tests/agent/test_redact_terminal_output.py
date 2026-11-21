"""Tests for agent.redact_egress -- terminal-output redaction (moved from test_redact.py).

is_env_dump_command + redact_terminal_output -- issue #43025.
"""

import pytest


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    """Ensure HERMES_REDACT_SECRETS is not disabled by prior test imports."""
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    # Also patch the module-level snapshot so it reflects the cleared env var
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


class TestTerminalOutputRedaction:
    """is_env_dump_command + redact_terminal_output — issue #43025.

    Terminal/process stdout must be redacted on every surface (foreground
    `terminal` AND background `process(poll/log/wait)`). Env-dump commands
    and commands that read ``.env`` files get the ENV-assignment pass so
    opaque tokens (no vendor prefix) are masked; other commands stay on
    the code_file path to avoid false positives.
    """

    def test_is_env_dump_command_detection(self):
        from agent.redact import is_env_dump_command
        assert is_env_dump_command("printenv")
        assert is_env_dump_command("env")
        assert is_env_dump_command("env | grep API")
        assert is_env_dump_command("set")
        assert is_env_dump_command("export")
        assert is_env_dump_command("declare -x")
        assert is_env_dump_command("cat /tmp/x && printenv")
        assert not is_env_dump_command("python app.py")
        assert not is_env_dump_command("cat config.py")
        assert not is_env_dump_command("printf 'TOKEN=x'")
        assert not is_env_dump_command("")
        assert not is_env_dump_command(None)

    # ── .env file detection (issue #61352 v2) ──

    def test_command_reads_secret_file_detection(self):
        from agent.redact import _command_reads_secret_file
        # Basic detection
        assert _command_reads_secret_file("cat .env")
        assert _command_reads_secret_file("cat .env.local")
        assert _command_reads_secret_file("cat .env.production")
        assert _command_reads_secret_file("cat .envrc")
        assert _command_reads_secret_file("head .env")
        assert _command_reads_secret_file("tail .env")
        assert _command_reads_secret_file("type .env")
        assert _command_reads_secret_file("nl .env")
        assert _command_reads_secret_file("bat .env")
        # With flags
        assert _command_reads_secret_file("cat -n .env")
        assert _command_reads_secret_file("cat -A .env")
        # With paths
        assert _command_reads_secret_file("cat ~/.hermes/.env")
        assert _command_reads_secret_file("cat /home/user/project/.env")
        assert _command_reads_secret_file("cat ./config/.env.local")
        # In a pipeline / sequence
        assert _command_reads_secret_file("cat .env | grep KEY")
        assert _command_reads_secret_file("echo '---' && cat .env")
        # Windows-style backslash paths
        assert _command_reads_secret_file("cat C:\\Users\\test\\.env")
        # Quoted paths (plain split leaves the quotes attached)
        assert _command_reads_secret_file('cat ".env"')
        assert _command_reads_secret_file("cat '.env'")
        # Case-insensitive basename (macOS/Windows filesystems)
        assert _command_reads_secret_file("cat .ENV")

    def test_command_reads_secret_file_excludes_templates(self):
        from agent.redact import _command_reads_secret_file
        # Templates/examples should NOT trigger
        assert not _command_reads_secret_file("cat .env.example")
        assert not _command_reads_secret_file("cat .env.sample")
        assert not _command_reads_secret_file("cat .env.template")
        assert not _command_reads_secret_file("cat .env.dist")

    def test_command_reads_secret_file_rejects_non_env_files(self):
        from agent.redact import _command_reads_secret_file
        assert not _command_reads_secret_file("cat config.py")
        assert not _command_reads_secret_file("cat README.md")
        assert not _command_reads_secret_file("cat .envrc.bak")  # .bak not in list
        assert not _command_reads_secret_file("python app.py")
        assert not _command_reads_secret_file("echo .env")  # echo is not a file-read cmd
        assert not _command_reads_secret_file("")
        assert not _command_reads_secret_file(None)

    def test_cat_env_file_masks_opaque_token(self):
        """cat .env → code_file=False → generic ENV pass redacts opaque keys."""
        from agent.redact import redact_terminal_output
        out = (
            "MISTRAL_API_KEY=abc123opaqueSecretValue\n"
            "NOUS_API_KEY=xyz789opaqueKey\n"
            "DEBUG=true\n"
        )
        red = redact_terminal_output(out, "cat .env")
        assert "abc123opaqueSecretValue" not in red
        assert "xyz789opaqueKey" not in red
        assert "DEBUG=true" in red  # non-secret key preserved

    def test_cat_env_file_with_flags_masks_opaque_token(self):
        """cat -n .env → still detected as .env read."""
        from agent.redact import redact_terminal_output
        out = "     1\tMISTRAL_API_KEY=abc123opaqueSecretValue\n"
        red = redact_terminal_output(out, "cat -n .env")
        assert "abc123opaqueSecretValue" not in red

    def test_cat_env_file_in_pipeline_masks_opaque_token(self):
        """cat .env | grep KEY → still detected as .env read."""
        from agent.redact import redact_terminal_output
        out = "MISTRAL_API_KEY=abc123opaqueSecretValue"
        red = redact_terminal_output(out, "cat .env | grep MISTRAL")
        assert "abc123opaqueSecretValue" not in red

    def test_cat_env_example_not_redacted_as_env(self):
        """cat .env.example → NOT treated as .env read (template file)."""
        from agent.redact import redact_terminal_output
        out = "MISTRAL_API_KEY=placeholder_value_here"
        red = redact_terminal_output(out, "cat .env.example")
        # Should NOT be redacted by the ENV-assignment pass (code_file=True).
        # The placeholder value should survive since it has no vendor prefix.
        assert "placeholder_value_here" in red

    def test_cat_env_local_masks_opaque_token(self):
        """cat .env.local → detected as .env read."""
        from agent.redact import redact_terminal_output
        out = "CUSTOM_API_KEY=opaquecustomkey123456"
        red = redact_terminal_output(out, "cat .env.local")
        assert "opaquecustomkey123456" not in red

    def test_cat_env_inline_comment_preserved(self):
        """Inline comments after env values are preserved (issue #61352 review)."""
        from agent.redact import redact_terminal_output
        out = "MISTRAL_API_KEY=abc123secret # used for tests"
        red = redact_terminal_output(out, "cat .env")
        assert "abc123secret" not in red
        assert "MISTRAL_API_KEY=*** # used for tests" in red

    def test_cat_env_export_inline_comment_preserved(self):
        """export KEY=VALUE # comment — comment preserved."""
        from agent.redact import redact_terminal_output
        out = "export MISTRAL_API_KEY=abc123secret # prod key"
        red = redact_terminal_output(out, "cat .env")
        assert "abc123secret" not in red
        assert "export MISTRAL_API_KEY=*** # prod key" in red

    @pytest.mark.parametrize(
        ("command", "output", "secret"),
        [
            ("cat ~/.hermes/config.yaml", "api_key: hermesConfigSecret123", "hermesConfigSecret123"),
            (
                "head ~/.hermes/profiles/work/config.yaml",
                "provider.token=profileConfigSecret456",
                "profileConfigSecret456",
            ),
            ("tail ~/.bashrc", "export SERVICE_TOKEN=bashRcSecret789", "bashRcSecret789"),
            ("grep TOKEN ~/.zshrc", "SERVICE_TOKEN=zshRcSecret123", "zshRcSecret123"),
            (
                "awk -F= '/TOKEN/ {print $2}' ~/.profile",
                "SERVICE_TOKEN=profileSecret456",
                "profileSecret456",
            ),
            ("sed -n '1,20p' ~/.zprofile", "api_key: zprofileSecret789", "zprofileSecret789"),
            (
                'cat "$HERMES_HOME/config.yaml"',
                "SERVICE_TOKEN=variablePathSecret123456789",
                "variablePathSecret123456789",
            ),
            (
                'cat "${HERMES_HOME}/config.yaml"',
                "SERVICE_TOKEN=variablePathSecret123456789",
                "variablePathSecret123456789",
            ),
            (
                "cat $HOME/.hermes/config.yaml",
                "SERVICE_TOKEN=homeVariablePathSecret123456",
                "homeVariablePathSecret123456",
            ),
            (
                "awk '{print $1; print $2}' ~/.bashrc",
                "export SERVICE_TOKEN=awkQuotedSecret123",
                "awkQuotedSecret123",
            ),
            (
                "grep 'foo|bar' ~/.hermes/config.yaml",
                "SERVICE_TOKEN=grepQuotedSecret456",
                "grepQuotedSecret456",
            ),
        ],
    )
    def test_secret_bearing_file_commands_mask_assignments(self, command, output, secret):
        from agent.redact import redact_terminal_output

        assert secret not in redact_terminal_output(output, command)

    @pytest.mark.parametrize(
        "command",
        [
            "cat config.yaml",
            "cat /project/config.yaml",
            "cat ~/.hermes/config.example.yaml",
            "cat ~/.hermes/config.template.yaml",
            "cat ~/.bashrc.example",
            'cat "$OTHER/config.yaml"',
            "grep TOKEN app.py",
            "grep .bashrc app.py",
            "grep -n .env src/settings.py",
            "awk '/TOKEN/' settings.yaml",
            "sed -n '1,20p' template.yaml",
        ],
    )
    def test_arbitrary_yaml_and_source_reads_stay_unredacted(self, command):
        """Only the known secret-bearing files flip the gate: the same opaque value read
        from project YAML / source code is left alone (code_file path), while the
        identical text under ``cat .env`` is masked."""
        from agent.redact import redact_terminal_output

        output = "SERVICE_TOKEN=3JcQ1UzX9vQ2mL7pR4tY8wA1sD5fG6hJ2kSbn7Q0"
        assert redact_terminal_output(output, command) == output
        assert "3JcQ1UzX9vQ2mL7pR4tY8wA1sD5fG6hJ2kSbn7Q0" not in redact_terminal_output(output, "cat .env")


    def test_disabled_passes_through(self, monkeypatch):
        from agent.redact import redact_terminal_output
        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
        out = "CUSTOM_TOKEN=zzzopaque1234567890abcdef"
        red = redact_terminal_output(out, "printenv")
        assert "zzzopaque1234567890abcdef" in red

    @pytest.mark.parametrize(
        "command",
        [
            "python -m pytest",
            "uv run pytest",
            "poetry run pytest",
            "coverage run -m pytest",
            "PYTHONPATH=. pytest",
            "/usr/bin/env pytest",
            "bash -lc 'pytest tests/agent/test_redact.py'",
        ],
    )
    def test_pytest_diagnostic_masks_python_repr_secret_field(self, command):
        from agent.redact import redact_terminal_output

        secret = "opaque-brave-value-1234567890"
        out = f"E       kwargs={{'env': {{'BRAVE_API_KEY': '{secret}'}}}}"

        red = redact_terminal_output(out, command, force=True)

        assert secret not in red
        assert "'BRAVE_API_KEY': '***'" in red

    def test_exception_line_masks_python_repr_secret_field(self):
        from agent.redact import redact_terminal_output

        secret = "opaque-exception-value-1234567890"
        out = f"RuntimeError: payload={{'API_KEY': '{secret}'}}"

        red = redact_terminal_output(out, "python app.py", force=True)

        assert secret not in red
        assert "'API_KEY': '***'" in red

    def test_source_dump_preserves_python_repr_fixture(self):
        from agent.redact import redact_terminal_output

        out = "CONFIG = {'BRAVE_API_KEY': 'fixture-value-1234567890'}"
        assert redact_terminal_output(out, "cat config.py", force=True) == out
        assert redact_terminal_output(out, "python dump_source.py", force=True) == out

    def test_pytest_source_line_preserves_python_repr_fixture(self):
        from agent.redact import redact_terminal_output

        source = "    CONFIG = {'BRAVE_API_KEY': 'fixture-value-1234567890'}"
        out = source + "\nE       assert False"

        red = redact_terminal_output(out, "uv run pytest", force=True)

        assert source in red

    def test_disabled_pytest_diagnostic_passes_through(self, monkeypatch):
        from agent.redact import redact_terminal_output

        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
        secret = "opaque-disabled-value-1234567890"
        out = f"E       payload={{'API_KEY': '{secret}'}}"

        assert redact_terminal_output(out, "uv run pytest") == out


