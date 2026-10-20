"""Telegram HTML formatting. HTML parse mode is used because only <, > and & need
escaping (MarkdownV2 has 18 reserved characters). plain() is the safe fallback."""

import json
import re
from html import escape, unescape

MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
_SEVERITY_ICON = {"critical": "🛑", "high": "🔴", "medium": "🟠", "low": "🟡", "info": "ℹ️"}
_STATE_ICON = {
    "succeeded": "✅",
    "failed": "❌",
    "cancelled": "⏹",
    "unknown": "❓",
    "running": "⏳",
    "waiting_for_approval": "🔐",
    "waiting_for_user": "💬",
    "queued": "🕒",
    "created": "🆕",
}


def code(text: str, lang: str | None = None) -> str:
    cls = f' class="language-{escape(lang)}"' if lang else ""
    return f"<pre><code{cls}>{escape(text, quote=False)}</code></pre>"


def json_block(obj) -> str:
    return code(json.dumps(obj, indent=2, ensure_ascii=False, default=str), "json")


def diff_block(diff: str, max_chars: int = 3000) -> str:
    return code(truncate(diff, max_chars), "diff")


def log_block(text: str, max_chars: int = 2500) -> str:
    """Logs/stack traces: keep the tail, where the error usually is."""
    if len(text) > max_chars:
        text = "…\n" + text[-max_chars:]
    return code(text)


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def bullet(items: list[str]) -> str:
    return "\n".join(f"• {i}" for i in items)


def task_line(task) -> str:
    icon = _STATE_ICON.get(task.state, "")
    return f"{icon} <code>{task.task_id}</code> <b>{escape(task.title)}</b> — {task.state}"


def task_summary(task) -> str:
    lines = [task_line(task), f"Workflow: {task.workflow}"]
    if task.provider:
        lines.append(f"Agent: {escape(task.provider)} ({escape(task.model or 'default model')})")
    if task.usage:
        cost = [u.cost_usd for u in task.usage if u.cost_usd is not None]
        dur = sum(u.duration_ms for u in task.usage) / 1000
        line = f"Runs: {len(task.usage)} · {dur:.1f}s agent time"
        if cost:
            line += f" · ~${sum(cost):.4f} (provider estimate)"
        lines.append(line)
    if task.error:
        lines.append(f"Error [{escape(task.error_category or 'error')}]: {escape(task.error)}")
    if task.result:
        lines.append("\n" + escape(truncate(task.result, 1500)))
    return "\n".join(lines)


def findings_report(findings) -> str:
    if not findings:
        return "No review findings."
    out = [f"<b>Review findings ({len(findings)})</b>"]
    for f in findings:
        out.append(
            f"{_SEVERITY_ICON.get(f.severity, '')} <b>{f.severity}</b> "
            f"{escape(f.location)} — {escape(truncate(f.description, 300))}"
            + (f"\n   ↳ fix: {escape(truncate(f.remediation, 200))}" if f.remediation else "")
        )
    return "\n".join(out)


def manifest_report(entries) -> str:
    out = ["<b>Asset manifest</b>"]
    for e in entries:
        ok = "✅" if e.validation_status == "passed" else "⚠️"
        out.append(
            f"{ok} {escape(e.name)} {e.width}x{e.height} {e.format} alpha={e.has_alpha} "
            f"[{e.approval_status}]"
            + (f"\n   {escape('; '.join(e.validation_issues))}" if e.validation_issues else "")
        )
    return "\n".join(out)


def plain(html_text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html_text))


def fit(html_text: str, full_text: str | None = None, limit: int = MESSAGE_LIMIT):
    """Return (message, attachment_text_or_None). Oversized content becomes a file."""
    if len(html_text) <= limit:
        return html_text, None
    head = plain(html_text)[: limit - 200]
    return escape(head) + "\n\n<i>…truncated; full report attached.</i>", full_text or plain(
        html_text
    )
