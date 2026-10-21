import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_control_chat_id: int | None = None
    telegram_report_channel_id: int | None = None
    telegram_allowed_user_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    telegram_progress_updates: bool = False
    telegram_discussion_chat_id: int | None = (
        None  # overrides config/app.yaml telegram.discussion_chat_id
    )
    # polling: the gateway asks Telegram for updates (no public endpoint).
    # webhook: Telegram pushes updates to TELEGRAM_WEBHOOK_URL + /telegram/webhook (public HTTPS),
    # authenticated by TELEGRAM_WEBHOOK_SECRET. `agw dev` opens a cloudflared tunnel when the URL is unset.
    telegram_mode: Literal["polling", "webhook"] = "polling"
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: SecretStr | None = None

    # REST control API. Control endpoints are disabled when unset.
    api_token: SecretStr | None = None
    # false: /mcp accepts token-less requests, but only from this machine (loopback client, local
    # Host header, no foreign Origin), so local Claude Code sessions connect without AGW_API_TOKEN.
    mcp_require_token: bool = True

    # Storage and workspaces
    data_dir: Path = Path("var")
    workspace_root: Path | None = None  # git repository agents may work on
    state_db: str = "state.db"
    config_dir: Path | None = Path(
        "config"
    )  # YAML gateway configuration; None disables it  # SQLite file under DATA_DIR (":memory:" disables durability)

    # Plugins: module names under app.plugins (or dotted paths). Permissions are the most any
    # plugin tool may hold; tool_permissions is what a workflow step is granted at runtime.
    plugins: Annotated[list[str], NoDecode] = ["software", "documents", "data", "media", "unity"]
    plugin_permissions: Annotated[list[str], NoDecode] = [
        "workspace:read",
        "workspace:write",
        "process:execute",
        "artifact:write",
        "data:read",
        "provider:image",
    ]
    tool_permissions: Annotated[list[str], NoDecode] = [
        "workspace:read", "workspace:write", "process:execute", "artifact:write", "data:read",
        "provider:image",
    ]  # fmt: skip  # "mcp:execute" is NOT granted by default
    plugin_config: dict[str, dict] = {}  # per-plugin options, keyed by plugin id
    mcp_servers: list[dict] = []  # see app.mcp_client.McpServerConfig

    # Limits
    max_attachment_bytes: int = 20 * 1024 * 1024  # Bot API getFile limit is 20 MB
    task_timeout_s: int = 1800
    max_concurrent_tasks: int = 2
    action_ttl_s: int = 3600
    max_retries: int = 2
    max_fix_rounds: int = 1
    log_level: str = "INFO"

    # Providers (each disabled unless its key / flag is set)
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str | None = None
    openai_image_model: str | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_image_input: bool = False  # documented by DeepSeek, not smoke-tested here
    http_timeout_s: float = 120.0

    claude_code_enabled: bool = False
    claude_code_bin: str = "claude"
    claude_code_model: str | None = None
    claude_code_allowed_tools: str = ""  # extra --allowedTools for implementer runs
    claude_code_mcp_config: Path | None = None  # MCP servers for the Claude Code runtime
    claude_code_max_budget_usd: float | None = None

    # OpenCode runtime: the server is started separately with `opencode serve`
    opencode_enabled: bool = False
    opencode_url: str = "http://127.0.0.1:4096"
    opencode_username: str = "opencode"
    opencode_password: SecretStr | None = None  # same value as OPENCODE_SERVER_PASSWORD
    opencode_model: str | None = None  # "providerID/modelID"; default = server default model
    opencode_shell_allowlist: Annotated[list[str], NoDecode] = [
        "python3 -m pytest",
        "pytest",
        "git status",
        "git diff",
    ]

    # Agent role -> ordered connector preference
    implementer_preference: str = "claude_code"
    designer_preference: str = "openai,claude_code"
    reviewer_preference: str = "claude_code,openai"

    # QA: name -> command line (JSON in env), run without a shell
    qa_commands: dict[str, str] = {}
    qa_timeout_s: int = 900
    qa_max_output_bytes: int = 64_000
    qa_sandbox: Literal["none", "docker"] = "none"
    qa_docker_image: str | None = None
    qa_env_passthrough: Annotated[list[str], NoDecode] = []  # extra env vars for QA commands

    # Asset validation rules
    asset_require_alpha: bool = True
    asset_max_dimension: int = 4096
    asset_power_of_two: bool = False

    @field_validator(
        "plugins",
        "plugin_permissions",
        "tool_permissions",
        "qa_env_passthrough",
        "opencode_shell_allowlist",
        mode="before",
    )
    @classmethod
    def _split_list(cls, v):
        if isinstance(v, str):
            if v.strip().startswith("["):
                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return frozenset(int(x) for x in v.replace(" ", "").split(",") if x)
        return v

    @model_validator(mode="after")
    def _check(self):
        if self.telegram_bot_token:
            missing = [
                n
                for n in ("telegram_control_chat_id", "telegram_report_channel_id")
                if getattr(self, n) is None
            ]
            if missing:
                raise ValueError(f"TELEGRAM_BOT_TOKEN set but missing: {', '.join(missing)}")
            if not self.telegram_allowed_user_ids:
                raise ValueError("TELEGRAM_ALLOWED_USER_IDS must not be empty")
        if self.telegram_mode == "webhook":
            secret = (
                self.telegram_webhook_secret.get_secret_value()
                if self.telegram_webhook_secret
                else ""
            )
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", secret):
                raise ValueError(
                    "TELEGRAM_MODE=webhook needs TELEGRAM_WEBHOOK_SECRET (16-256 chars A-Z a-z 0-9 _ -); "
                    "run `uv run agw init-env`"
                )
            if self.telegram_webhook_url and not self.telegram_webhook_url.startswith("https://"):
                raise ValueError("TELEGRAM_WEBHOOK_URL must be https://")
        if self.qa_sandbox == "docker" and not self.qa_docker_image:
            raise ValueError("QA_SANDBOX=docker requires QA_DOCKER_IMAGE")
        if self.workspace_root is not None:
            self.workspace_root = self.workspace_root.expanduser().resolve()
        self.data_dir = self.data_dir.expanduser().resolve()
        return self

    def secret_values(self) -> list[str]:
        return [
            s.get_secret_value()
            for s in (
                self.telegram_bot_token,
                self.api_token,
                self.openai_api_key,
                self.deepseek_api_key,
                self.opencode_password,
                self.telegram_webhook_secret,
            )
            if s
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
