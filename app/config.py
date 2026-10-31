import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_control_chat_id: int | None = None
    telegram_report_channel_id: int | None = None
    # system notices (sessions coming and going, routing, gate, skills, lanes closed): this channel;
    # unset, they go to the control group with the work (questions, asks, reports, alerts)
    telegram_notice_chat_id: int | None = None
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
    # Important confirmations (release, deploy, commit/push, git pull) approved from Telegram need
    # this passcode as a reply; the reply is deleted at once and 3 wrong tries lock the question
    # (confirm on the machine). Unset: such confirmations can only be refused from Telegram.
    telegram_confirm_passcode: SecretStr | None = None

    # REST control API. Control endpoints are disabled when unset.
    api_token: SecretStr | None = None
    # Operator decisions over REST (approve, execute an action, answer a question). Agents hold
    # API_TOKEN, so it cannot be enough for these; unset = Telegram only.
    operator_token: SecretStr | None = None
    # false: /mcp accepts token-less requests, but only from this machine (loopback client, local
    # Host header, no foreign Origin), so local Claude Code sessions connect without AGW_API_TOKEN.
    mcp_require_token: bool = True
    # folder whose git sub-folders /open can start (default: the folder containing this gateway)
    projects_root: Path | None = None
    # Claude model of the sessions the gateway opens: child sessions do the work (Opus), a temporary
    # router only routes and reports (cheaper). Per project with /model, per task with dispatch(model=).
    child_model: str = "opus"
    router_model: str = "sonnet"

    # Storage and workspaces
    data_dir: Path = Path("var")
    workspace_root: Path | None = None  # git repository agents may work on
    state_db: str = "state.db"
    config_dir: Path | None = Path(
        "config"
    )  # YAML gateway configuration; None disables it  # SQLite file under DATA_DIR (":memory:" disables durability)

    # Plugins: module names under app.plugins (or dotted paths). Permissions are the most any
    # plugin tool may hold; tool_permissions is what a workflow step is granted at runtime.
    plugins: Annotated[list[str], NoDecode] = [
        "software",
        "documents",
        "data",
        "media",
        "unity",
        "game_research",
    ]
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
    # project -> skills its agents must load first for every request through the gateway (the first
    # one leads), e.g. CORE_SKILLS='{"UIUXAgent": ["uiux-art-director"]}'
    core_skills: dict[str, list[str]] = {}
    # central routing: requests the rules (config/routing/routing.yaml) cannot place go to the main
    # session of this project, the shared Gateway Agent
    gateway_agent_project: str = "Agent Gateway"
    # projects that keep a main session besides the gateway project (default: none; the gateway
    # routes every other project's work), e.g. MAIN_PROJECTS='["Some Project"]'
    main_projects: Annotated[list[str], NoDecode] = []
    gate_cli: bool = (
        False  # the gateway opens and keeps alive its own CLI session as the Gateway Agent
    )
    tool_parallel: int = 3  # a tool project's requests run in at most this many sessions at once
    # offer/bid routing (pilot): a new request of these projects is first offered to their idle
    # sessions; a yes with the most context takes it, else a new session. BID_PROJECTS='["UIUXAgent"]'
    bid_projects: Annotated[list[str], NoDecode] = []
    offer_wait_s: float = 90  # seconds the offers stay open
    offer_max: int = 3  # sessions asked at most, best context first
    retention_exempt_domains: list[str] = ["game_research"]  # their tasks/artifacts are evidence
    data_retention_days: int = (
        30  # finished tasks/messages/logs older than this are deleted daily; 0 = keep
    )
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
        "bid_projects",
        "main_projects",
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
                self.operator_token,
                self.openai_api_key,
                self.deepseek_api_key,
                self.opencode_password,
                self.telegram_webhook_secret,
                self.telegram_confirm_passcode,
            )
            if s
        ]


# Environments run side by side from this folder: own .env file, port and data folder (DATA_DIR in
# .env.<env>). Agent configs (Claude Code, OpenCode, Codex, ...) always point at prod.
ENV_PORTS = {"prod": 8000, "dev": 8001, "uat": 8002}


def env_name() -> str:
    env = os.environ.get("AGW_ENV", "prod").lower()
    if env not in ENV_PORTS:
        raise ValueError(f"AGW_ENV={env!r}: use one of {', '.join(ENV_PORTS)}")
    return env


def env_file() -> str:
    return ".env" if env_name() == "prod" else f".env.{env_name()}"


def require_prod(what: str) -> None:
    """Agent configs belong to prod: dev/uat never rewrite them."""
    if env_name() != "prod":
        raise ValueError(f"{what} is prod-only; this is {env_name()} (agent configs stay on prod)")


def load_settings() -> Settings:
    s = Settings(_env_file=env_file())
    if env_name() != "prod" and s.telegram_bot_token:
        from dotenv import dotenv_values

        if s.telegram_bot_token.get_secret_value() == dotenv_values(".env").get(
            "TELEGRAM_BOT_TOKEN"
        ):
            # two pollers on one bot steal each other's updates: prod would go deaf
            raise ValueError(
                f"{env_file()} uses prod's TELEGRAM_BOT_TOKEN; give {env_name()} its own bot"
            )
    return s


@lru_cache
def get_settings() -> Settings:
    return load_settings()


@lru_cache
def _provider_keys() -> frozenset[str]:
    """api_key_env of every provider in the config (FAL_KEY, MESHY_API_KEY, ...)."""
    try:
        from app.gateway_config import load_or_raise

        d = get_settings().config_dir
        if d is None or not (d / "app.yaml").exists():
            return frozenset()
        return frozenset(
            p.api_key_env for p in load_or_raise(d).providers.values() if p.api_key_env
        )
    except Exception:  # noqa: BLE001 - without a config the other names still apply
        return frozenset()


def secret_env_names() -> set[str]:
    """Variables holding the gateway's own secrets: every SecretStr setting, AGW_SECRET_KEY, the
    OpenCode server password and every provider key (from the config or set at runtime with /keys)."""
    from app.keys import RUNTIME, SECRET_ENV

    fields = Settings.model_fields.items()
    names = {n.upper() for n, f in fields if SecretStr in (f.annotation, *get_args(f.annotation))}
    return names | {SECRET_ENV, "OPENCODE_SERVER_PASSWORD"} | _provider_keys() | RUNTIME


def child_env(environ: dict | None = None) -> dict:
    """The environment for an agent or tool subprocess: ours without the gateway's secrets (and
    CLAUDECODE, so `claude` starts). Agents keep their own keys (ANTHROPIC_API_KEY, logins, ...)."""
    drop = secret_env_names() | {"CLAUDECODE"}
    return {k: v for k, v in (os.environ if environ is None else environ).items() if k not in drop}
