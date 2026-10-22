#!/usr/bin/env bash
# Quick install of the agent gateway on this machine (idempotent: safe to run again).
#
#   scripts/install.sh [--env prod|dev|uat] [--agents LIST] [--no-global] [--start]
#
#   --env       environment to set up (default prod): .env, or .env.dev / .env.uat beside it
#   --agents    also connect other agents: opencode,codex,antigravity,cursor | detected | all (prod only)
#   --no-global prod: only the .env file, no Claude Code global setup (skill, hook, user MCP)
#   --start     start the gateway at the end (foreground; Ctrl-C stops it)
#
# Telegram (optional): export TELEGRAM_BOT_TOKEN, TELEGRAM_CONTROL_CHAT_ID, TELEGRAM_ALLOWED_USER_IDS
# (and TELEGRAM_REPORT_CHANNEL_ID, default: the control group) before running. They are written to the
# env file only where it has no value yet, and never printed. Agents: see docs/AGENT_CONNECT.md.
set -euo pipefail

ENV=prod AGENTS="" GLOBAL=1 START=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV="$2"; shift 2 ;;
    --agents) AGENTS="$2"; shift 2 ;;
    --no-global) GLOBAL=0; shift ;;
    --start) START=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
case "$ENV" in prod|dev|uat) ;; *) echo "--env must be prod, dev or uat" >&2; exit 2 ;; esac

cd "$(dirname "$0")/.."
step() { printf '\n== %s\n' "$*"; }

step "requirements"
command -v git >/dev/null || { echo "git is missing: install Xcode command line tools (xcode-select --install)"; exit 1; }
if ! command -v uv >/dev/null; then
  echo "installing uv (Python and package manager)"
  if command -v brew >/dev/null; then brew install uv
  else curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; fi
  command -v uv >/dev/null || { echo "uv install failed: see https://docs.astral.sh/uv/"; exit 1; }
fi
command -v claude >/dev/null && echo "claude: $(claude --version 2>/dev/null | head -1)" || echo "claude CLI not found: Claude Code parts are skipped"
uv sync --locked -q && echo "python environment ready"

step "settings ($ENV)"
if [ "$ENV" = prod ]; then
  FILE=.env
  if [ ! -f .env ]; then
    # a fresh .env: local agents connect without a token; placeholder Telegram ids cleared
    sed -e 's/^MCP_REQUIRE_TOKEN=.*/MCP_REQUIRE_TOKEN=false/' \
        -e 's/^TELEGRAM_CONTROL_CHAT_ID=.*/TELEGRAM_CONTROL_CHAT_ID=/' \
        -e 's/^TELEGRAM_REPORT_CHANNEL_ID=.*/TELEGRAM_REPORT_CHANNEL_ID=/' \
        -e 's/^TELEGRAM_ALLOWED_USER_IDS=.*/TELEGRAM_ALLOWED_USER_IDS=/' .env.example > .env
    chmod 600 .env
    echo "created .env"
  fi
  uv run agw init-env
else
  FILE=".env.$ENV"
  uv run agw --env "$ENV" setup
fi
uv run python - "$FILE" <<'EOF'
import os, sys
from pathlib import Path
from app.cli import set_env_values
keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CONTROL_CHAT_ID", "TELEGRAM_REPORT_CHANNEL_ID", "TELEGRAM_ALLOWED_USER_IDS"]
values = {k: os.environ[k] for k in keys if os.environ.get(k)}
if "TELEGRAM_CONTROL_CHAT_ID" in values:  # single-group mode: reports go to the control group
    values.setdefault("TELEGRAM_REPORT_CHANNEL_ID", values["TELEGRAM_CONTROL_CHAT_ID"])
changed = set_env_values(values, Path(sys.argv[1])) if values else []
print(f"Telegram values written: {', '.join(changed)}" if changed else "Telegram values: unchanged")
EOF

if [ "$ENV" = prod ] && [ "$GLOBAL" = 1 ]; then
  step "Claude Code: every git repo in the projects folder"
  uv run agw global-setup
fi
if [ -n "$AGENTS" ]; then
  step "other agents: $AGENTS"
  if [ "$ENV" = prod ]; then uv run agw setup --agents "$AGENTS"; else echo "skipped: agent configs stay on prod"; fi
fi

step "check"
uv run agw --env "$ENV" doctor || true

if [ "$START" = 1 ]; then
  step "starting the $ENV gateway (Ctrl-C stops it)"
  exec uv run agw --env "$ENV" dev
fi
step "done"
echo "Start the gateway: uv run agw --env $ENV dev   (then send /menu to the bot on Telegram)"
echo "Restart open agent sessions so they load the gateway tools."
