"""Telegram control interface (control group) and report delivery (channel), aiogram 3.

Long polling only: no public webhook endpoint is exposed. Polling refuses to start
if a webhook is already configured for the bot (it would conflict).
"""

import asyncio
import io
import json
import logging
import os
import re
import time
from datetime import datetime
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
)
from aiogram.types import Message as TgMessage

from app import formatting as fmt
from app.actions import Action, ActionError, ActionRegistry
from app.collaboration import CollaborationError
from app.config import Settings, env_name
from app.connectors.base import ConnectorError
from app.intake import TaskRequest
from app.models import TaskState, Workflow, now
from app.orchestrator import Orchestrator
from app.storage import AttachmentError, AttachmentStore

log = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"
ALLOWED_UPDATES = ["message", "callback_query"]
PHOTO_LIMIT = 10 * 1024 * 1024
ALBUM_WAIT_S = 1.5

HELP = (
    """<b>Agent Gateway</b> · <code>/menu</code> for buttons

<b>Sessions &amp; work</b>
/list — agent sessions (s1, s2…) and projects (p1, p2…)
/send &lt;s#|p#|project&gt; &lt;text&gt; — give an agent a request
/queue — requests waiting or in progress (m1…) · /cancel &lt;m#&gt;
/open &lt;folder|p#&gt; [new] — open a Claude session for a project (/open alone: buttons)
/close &lt;s#&gt; · /off &lt;s#&gt; · /on &lt;s#&gt; — close a session, or stop/resume giving it work
/agents — every agent: 🖥 app (desktop/IDE), ⌨️ CLI, ☁️ API, with on/off and close buttons
Reply to any gateway message (text or photos) to give feedback or answer it.

<b>Setup</b>
/setup — status, install or remove the global setup
/mcp — switch the tools agents call on or off (no restart)
/keys — provider API keys and models (values only in a private chat with the bot)

<b>More</b> (not in the / menu)
/task · /status · /report · /steps · /retry · /stop · /answer · /feedback · /approvals ·
/templates · /tools · /tool · /plugins · /capabilities · /roles · /agents_status · /sessions

<b>Message prefixes</b>
"""
    + " · ".join(f"{i} {k}" for i, k in fmt.KINDS.values())
    + """
First line: <code>PREFIX · #project · #task_id</code> (tap a hashtag to find every related message)."""
)

MENU = [  # the "/" menu: everyday commands only; the rest still work (see /help)
    ("menu", "Main panel with buttons"),
    ("list", "Sessions (s#) and projects (p#)"),
    ("agents", "Agents: app / CLI / API, on/off"),
    ("send", "Send a request: /send s1|p1 <text>"),
    ("queue", "Requests waiting / in progress (m#)"),
    ("cancel", "Cancel a request: /cancel m1"),
    ("open", "Open a project: /open, /open p1, /open k09"),
    ("close", "Close a session: /close s1"),
    ("off", "Stop giving a session work"),
    ("on", "Resume giving a session work"),
    ("setup", "Status and global setup"),
    ("mcp", "Agent tools on/off"),
    ("keys", "API keys and models"),
    ("help", "All commands"),
]
MENU_BUTTONS = [
    [("🖥 Sessions", "m:list"), ("🤖 Agents", "m:agents"), ("📥 Queue", "m:queue")],
    [("📂 Open project", "m:open"), ("🔑 Keys", "m:keys")],
    [("⚙️ Setup", "m:setup"), ("🔀 Agent tools", "m:mcp"), ("🧩 Templates", "m:templates")],
    [("❓ Help", "m:help")],
]
_ID = re.compile(r"\b(tsk|rep|hin|apr|dep)_[0-9a-f]{10}\b")


def parse_feedback(text: str) -> tuple[str | None, str]:
    """`/feedback [#tags…] <tsk_|rep_ id> <text>` -> (id, text). Leading hashtags (e.g. a copied
    `#project` tag) are skipped; the id may also appear later. (None, text) when there is no id."""
    words = text.removeprefix("/feedback").split()
    ref = None
    for i, w in enumerate(words):
        if m := re.fullmatch(r"#?((?:tsk|rep)_[0-9a-f]{10})", w):
            ref = m.group(1)
            words = words[:i] + words[i + 1 :]
            break
    while words and words[0].startswith("#"):
        words = words[1:]
    return ref, " ".join(words)


KIND_ICON = {"app": "🖥", "cli": "⌨️"}


def session_line(s) -> str:
    """One /live row: mode, state, project, id, folder."""
    mode = "⚡ push" if s.mode == "push" else "🔄 pull"
    who = (
        f"{KIND_ICON[s.kind]} {escape(s.agent or ('claude_code' if s.mode == 'push' else 'agent'))}"
    )
    state = "🚫 blocked (org policy)" if s.blocked else ("🟢 on" if s.enabled else "⏸ off")
    folder = escape(s.cwd.rstrip("/").rsplit("/", 1)[-1]) if s.cwd else "-"
    return (f"<b>s{s.num}</b> · {who} · {state} · {mode} · {escape(s.project)} · {folder}"
            f" · since {s.connected_at.astimezone():%H:%M} · <code>{escape(s.session_id)}</code>")  # fmt: skip


def templates(text: str = "") -> str:
    """Tap-to-copy command templates, prefilled with the ids found in a replied-to message."""
    ids: dict[str, str] = {}
    for m in _ID.finditer(text):
        ids.setdefault(m.group(1), m.group(0))
    rows = []
    if t := ids.get("tsk"):
        rows += [
            (f"/report {t}", "full report"),
            (f"/steps {t}", "steps and errors"),
            (f"/feedback {t} ", "request a change"),
            (f"/retry {t}", "run again"),
            (f"/stop {t}", "cancel"),
        ]
    if r := ids.get("rep"):
        rows.append((f"/feedback {r} ", "feedback on this report"))
    if h := ids.get("hin"):
        rows.append((f"/answer {h} ", "answer this question"))
    if not rows:
        rows = [
            ("/task ", "new task: describe it, attach files"),
            ("/status", "recent tasks"),
            ("/report tsk_", "full report of a task"),
            ("/feedback tsk_ ", "request a change on a task"),
            ("/answer hin_ ", "answer a question"),
            ("/approvals", "pending approvals"),
        ]
    head = "🧩 <b>TEMPLATES</b>" + (
        " · " + " · ".join(f"#{v}" for v in ids.values()) if ids else ""
    )
    lines = [f"<code>{escape(c)}</code> — {d}" for c, d in rows]
    images = [("/task ", "caption of photo(s) or an album: new task with the images")]
    if target := ids.get("rep") or ids.get("tsk"):
        images.append((f"/feedback {target} ", "caption of photo(s): feedback with screenshots"))
    lines += ["", "📎 <b>With images</b>"] + [f"<code>{escape(c)}</code> — {d}" for c, d in images]
    lines.append("Or reply to a report with photo(s); the caption is your note")
    return (
        head + "\n" + "\n".join(lines) + "\n\n<i>Tap a command to copy it, then add your text.</i>"
    )


DOMAIN_CHOICES = [
    ("auto", None, "🤖 Auto"),
    ("documents", None, "📄 Docs"),
    ("data", None, "📊 Data"),
    ("media", None, "🖼 Media"),
]
WORKFLOW_LABELS = {
    Workflow.ask: "💬 Ask",
    Workflow.code: "🛠 Code",
    Workflow.delegate_review: "🔍 Code + Review",
    Workflow.parallel: "⚖️ Parallel",
    Workflow.design_implement_review: "🎨 Design→Code→Review",
}


def _image_doc(d) -> bool:
    return bool(d) and (getattr(d, "mime_type", "") or "").startswith("image/")


def keyboard(actions: list[Action]) -> InlineKeyboardMarkup | None:
    if not actions:
        return None
    rows = [actions[i : i + 3] for i in range(0, len(actions), 3)]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=a.label, callback_data=f"a:{a.token}") for a in row]
            for row in rows
        ]
    )


class TelegramGateway:
    def __init__(
        self,
        settings: Settings,
        orchestrator: Orchestrator,
        attachments: AttachmentStore,
        actions: ActionRegistry,
        bot: Bot | None = None,
    ):
        self.s = settings
        self.orch = orchestrator
        self.attachments = attachments
        self.actions = actions
        self.bot = bot or Bot(
            settings.telegram_bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.router = Router()
        self._albums: dict[str, list[TgMessage]] = {}
        self._key_wait: dict[
            int, tuple[str, float]
        ] = {}  # user -> (variable, deadline): /keys entry
        self._album_timers: dict[str, asyncio.Task] = {}
        self._dp: Dispatcher | None = None
        self._register()

    # ---------- authorization ----------

    def authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        return (
            user_id is not None
            and user_id in self.s.telegram_allowed_user_ids
            and chat_id == self.s.telegram_control_chat_id
        )

    def _authorized_msg(self, m: TgMessage) -> bool:
        ok = self.authorized(m.from_user.id if m.from_user else None, m.chat.id)
        if not ok:
            log.warning(
                "unauthorized message from user=%s chat=%s",
                m.from_user and m.from_user.id,
                m.chat.id,
            )
        return ok

    # ---------- delivery (Notifier) ----------

    async def _send(self, fn, **kw):
        """Send with Telegram rate-limit backoff and a plain-text fallback on parse errors."""
        for attempt in range(3):
            try:
                return await fn(**kw)
            except TelegramRetryAfter as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(e.retry_after)
            except TelegramBadRequest as e:
                if "parse" not in str(e).lower() or kw.get("parse_mode", "") is None:
                    raise
                for key in ("text", "caption"):
                    if key in kw:
                        kw[key] = fmt.plain(kw[key])
                kw["parse_mode"] = None

    async def report(self, task, html, *, actions=(), files=(), photos=(), control=False):
        chat = self.s.telegram_control_chat_id if control else self.s.telegram_report_channel_id
        await self._send(
            self.bot.send_message, chat_id=chat, text=html, reply_markup=keyboard(list(actions))
        )
        for name, data in photos:
            if len(data) <= PHOTO_LIMIT:
                await self._send(
                    self.bot.send_photo,
                    chat_id=chat,
                    photo=BufferedInputFile(data, name),
                    caption=escape(name),
                )
            else:
                files = [*files, (name, data)]
        for name, data in files:
            await self._send(
                self.bot.send_document, chat_id=chat, document=BufferedInputFile(data, name)
            )

    async def publish_channel(self, html: str) -> int | None:
        msg = await self._send(
            self.bot.send_message, chat_id=self.s.telegram_report_channel_id, text=html
        )
        return getattr(msg, "message_id", None)

    async def send_control(self, html: str) -> None:
        await self._send(self.bot.send_message, chat_id=self.s.telegram_control_chat_id, text=html)

    @property
    def discussion_chat_id(self) -> int | None:
        cfg = self.orch.gateway_cfg
        return self.s.telegram_discussion_chat_id or (
            cfg.telegram.discussion_chat_id if cfg else None
        )

    async def publish_report(
        self, task, html: str, photos: list[tuple[str, bytes]], files=(), actions=()
    ):
        """Post to the channel; photos/files reply to the message. Returns ids for reply correlation."""
        from app.collaboration import TelegramRef

        chat = self.s.telegram_report_channel_id
        msg = await self._send(
            self.bot.send_message, chat_id=chat, text=html, reply_markup=keyboard(list(actions))
        )
        ref = TelegramRef(channel_chat_id=chat, channel_message_id=getattr(msg, "message_id", None))
        reply = (
            ReplyParameters(message_id=ref.channel_message_id) if ref.channel_message_id else None
        )
        for name, data in photos:
            if len(data) > PHOTO_LIMIT:
                files = [*files, (name, data)]
                continue
            p = await self._send(
                self.bot.send_photo,
                chat_id=chat,
                photo=BufferedInputFile(data, name),
                caption=escape(name),
                reply_parameters=reply,
            )
            if getattr(p, "message_id", None) is not None:
                ref.photo_message_ids.append(p.message_id)
        for name, data in files:
            await self._send(
                self.bot.send_document,
                chat_id=chat,
                document=BufferedInputFile(data, name),
                reply_parameters=reply,
            )
        return ref

    async def check_destinations(self) -> dict[str, str]:
        """Best-effort startup check that the bot can see both chats."""
        out = {}
        me = await self.bot.get_me()
        for label, chat_id in (
            ("control", self.s.telegram_control_chat_id),
            ("report", self.s.telegram_report_channel_id),
        ):
            try:
                chat = await self.bot.get_chat(chat_id)
                member = await self.bot.get_chat_member(chat_id, me.id)
                out[label] = f"{chat.type} ok, bot is {member.status}"
            except Exception as e:  # report, don't crash startup
                out[label] = f"error: {type(e).__name__}"
        return out

    @property
    def dispatcher(self) -> Dispatcher:
        if self._dp is None:
            self._dp = Dispatcher()
            self._dp.include_router(self.router)
        return self._dp

    async def _startup(self) -> None:
        log.info("telegram destinations: %s", await self.check_destinations())
        try:  # the "/" menu in the control group
            await self.bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in MENU],
                scope=BotCommandScopeChat(chat_id=self.s.telegram_control_chat_id),
            )
        except Exception as e:
            log.warning("could not set the command menu: %s", type(e).__name__)

    async def run_polling(self) -> None:
        info = await self.bot.get_webhook_info()
        if info.url:
            if not info.url.endswith(WEBHOOK_PATH):
                log.error(
                    "A foreign webhook is configured for this bot; refusing to start polling."
                )
                return
            await self.bot.delete_webhook()  # ours, left by an unclean webhook-mode shutdown
            log.info("removed a stale gateway webhook; switching to polling")
        await self._startup()
        await self.dispatcher.start_polling(
            self.bot, allowed_updates=ALLOWED_UPDATES, handle_signals=False
        )

    async def start_webhook(self, base_url: str, secret: str) -> str:
        """Register the webhook (Telegram then pushes updates to feed()). Returns the URL."""
        url = base_url.rstrip("/") + WEBHOOK_PATH
        await self._startup()
        await self.bot.set_webhook(url, secret_token=secret, allowed_updates=ALLOWED_UPDATES)
        log.info("telegram webhook registered: %s", url)
        return url

    async def stop_webhook(self) -> None:
        await self.bot.delete_webhook()
        log.info("telegram webhook removed")

    async def feed(self, data: dict) -> None:
        """Process one pushed update through the same handlers as polling."""
        update = Update.model_validate(data, context={"bot": self.bot})
        await self.dispatcher.feed_update(self.bot, update)

    # ---------- handlers ----------

    def _register(self) -> None:
        r = self.router
        r.message(F.chat.type == "private", F.text)(
            self.on_key_value
        )  # first: secrets never reach others
        r.message(Command("start", "menu"))(self.on_menu)
        r.message(Command("keys"))(self.on_keys)
        r.message(Command("help"))(self.on_help)
        r.message(Command("setup"))(self.on_setup)
        r.message(Command("mcp"))(self.on_mcp)
        r.message(Command("agents"))(self.on_agents)
        r.message(Command("roles"))(self.on_roles)
        r.message(Command("agents_status"))(self.on_agents_status)
        r.message(Command("sessions"))(self.on_sessions)
        r.message(Command("status"))(self.on_status)
        r.message(Command("report"))(self.on_report)
        r.message(Command("stop"))(self.on_stop)
        r.message(Command("retry"))(self.on_retry)
        r.message(F.chat.func(lambda c: c.id == self.discussion_chat_id))(self.on_discussion)
        r.message(F.media_group_id)(self.on_album_part)
        r.message(Command("feedback"))(self.on_feedback_command)
        r.message(Command("steps"))(self.on_steps)
        r.message(Command("answer"))(self.on_answer)
        r.message(Command("approvals"))(self.on_approvals)
        r.message(Command("templates"))(self.on_templates)
        r.message(Command("send"))(self.on_send)
        r.message(Command("list", "live"))(self.on_live)
        r.message(Command("on", "off"))(self.on_switch)
        r.message(Command("queue"))(self.on_queue)
        r.message(Command("open"))(self.on_open)
        r.message(Command("close"))(self.on_close)
        r.message(Command("cancel"))(self.on_cancel)
        r.message(Command("tools"))(self.on_tools)
        r.message(Command("tool"))(self.on_tool)
        r.message(Command("plugins"))(self.on_plugins)
        r.message(Command("capabilities"))(self.on_capabilities)
        r.message(Command("task"))(self.on_task)
        r.message(F.photo | F.document | F.video | F.audio | F.voice)(self.on_stray_media)
        r.callback_query(F.data.startswith("a:"))(self.on_callback)
        r.callback_query(F.data.startswith("o:"))(self.on_open_button)
        r.callback_query(F.data.startswith("k:"))(self.on_ask_button)
        r.callback_query(F.data.startswith("m:"))(self.on_menu_button)
        r.callback_query(F.data.startswith("g:"))(self.on_setup_button)
        r.callback_query(F.data.startswith("t:"))(self.on_mcp_button)
        r.callback_query(F.data.startswith("y:"))(self.on_agents_button)
        r.callback_query(F.data.startswith("x:"))(self.on_keys_button)

    async def on_help(self, m: TgMessage):
        if self._authorized_msg(m):
            await m.answer(HELP)

    async def on_menu(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        name = escape(m.from_user.full_name) if m.from_user else ""
        live = self.orch.channel.live()
        waiting = len(self.orch.channel.open_messages())
        stuck = sum(a["state"] == "escalated" for a in self.orch.asks.open_asks())
        text = f"👋 Xin chào {name}! Bot đã sẵn sàng lắng nghe.\n🖥 {len(live)} sessions · 📥 {waiting} requests open"
        if stuck:
            text += f" · 🆘 {stuck} agents waiting"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in MENU_BUTTONS
        ])  # fmt: skip
        await m.answer(text, reply_markup=kb)

    async def on_menu_button(self, q: CallbackQuery):
        chat_id = q.message.chat.id if q.message else None
        if not self.authorized(q.from_user.id, chat_id):
            await q.answer("Not authorized.", show_alert=True)
            return
        await q.answer()
        view = q.data[2:]
        answer = q.message.answer
        if view == "list":
            await answer(self._list_text())
        elif view == "queue":
            await answer(self._queue_text())
        elif view == "open":
            await self._open_menu(answer)
        elif view == "setup":
            await self._setup_panel(answer)
        elif view == "mcp":
            await self._mcp_panel(answer)
        elif view == "agents":
            await self._agents_panel(answer)
        elif view == "keys":
            await self._keys_panel(answer)
        elif view == "templates":
            await answer(templates(""))
        else:
            await answer(HELP)

    async def on_mcp(self, m: TgMessage):
        if self._authorized_msg(m):
            await self._mcp_panel(m.answer)

    async def _mcp_panel(self, answer, note: str = "") -> None:
        from app.mcp_server import TOOL_NAMES

        sw = self.orch.switches
        buttons = [InlineKeyboardButton(text=f"{'✅' if sw.on(t) else '⛔'} {t}", callback_data=f"t:{t}")
                   for t in TOOL_NAMES]  # fmt: skip
        kb = InlineKeyboardMarkup(
            inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        )
        off = [t for t in TOOL_NAMES if not sw.on(t)]
        text = ("🔀 <b>AGENT TOOLS</b> (tap to switch; applies to the next call, no restart)\n"
                + (f"Off: {escape(', '.join(off))}" if off else "All on."))  # fmt: skip
        await answer(text + (f"\n{note}" if note else ""), reply_markup=kb)

    async def on_mcp_button(self, q: CallbackQuery):
        from app.mcp_server import TOOL_NAMES

        chat_id = q.message.chat.id if q.message else None
        if not self.authorized(q.from_user.id, chat_id):
            log.warning("unauthorized tool switch from user=%s chat=%s", q.from_user.id, chat_id)
            await q.answer("Not authorized.", show_alert=True)
            return
        name = q.data[2:]
        if name not in TOOL_NAMES:
            await q.answer("Unknown tool.")
            return
        on = not self.orch.switches.on(name)
        self.orch.switches.set(name, on)
        log.info("tool %s switched %s by user=%s", name, "on" if on else "off", q.from_user.id)
        await q.answer(f"{name}: {'on' if on else 'off'}")
        await self._mcp_panel(
            q.message.answer,
            f"{'✅' if on else '⛔'} {escape(name)} is now {'on' if on else 'off'}",
        )

    async def on_setup(self, m: TgMessage):
        if self._authorized_msg(m):
            await self._setup_panel(m.answer)

    async def _setup_panel(self, answer, note: str = "") -> None:
        from app.doctor import collect

        checks = await asyncio.to_thread(
            collect, self.s, check_gateway=False
        )  # this is the gateway
        lines = [
            f"{'✅' if c.ok else '⚠️'} <b>{escape(c.label)}</b>: {escape(c.detail)}" for c in checks
        ]
        from app import agents_setup

        agent_row = [
            InlineKeyboardButton(text=(f"🔌 Disconnect {a}" if agents_setup.configured(a) else f"🔗 Connect {a}"),
                                 callback_data=f"g:{'agentoff' if agents_setup.configured(a) else 'agenton'}:{a}")
            for a in agents_setup.detected()
        ]  # fmt: skip
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="g:refresh"),
             InlineKeyboardButton(text="⬇️ Install / update global", callback_data="g:install")],
            *[agent_row[i:i + 2] for i in range(0, len(agent_row), 2)],
            [InlineKeyboardButton(text="🗑 Remove global", callback_data="g:remove")],
        ])  # fmt: skip
        head = "⚙️ <b>SETUP</b>" + (f"\n{note}" if note else "")
        await answer(head + "\n\n" + "\n".join(lines), reply_markup=kb)

    async def on_setup_button(self, q: CallbackQuery):
        """Global setup from Telegram (changes ~/.claude on this Mac: allowlisted operator only)."""
        chat_id = q.message.chat.id if q.message else None
        if not self.authorized(q.from_user.id, chat_id):
            log.warning("unauthorized setup button from user=%s chat=%s", q.from_user.id, chat_id)
            await q.answer("Not authorized.", show_alert=True)
            return
        from app import global_setup

        verb = q.data[2:]
        if verb == "remove":  # destructive for every session's config: confirm first
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Yes, remove", callback_data="g:remove_yes"),
                InlineKeyboardButton(text="Keep it", callback_data="g:refresh"),
            ]])  # fmt: skip
            await q.answer()
            await q.message.answer(
                "Remove the global setup (skill, hook, permissions, channel MCP)?", reply_markup=kb
            )
            return
        await q.answer("Working…" if verb != "refresh" else "")
        note = ""
        if verb != "refresh" and env_name() != "prod":
            await self._setup_panel(
                q.message.answer, f"❌ {env_name()}: agent configs are changed from prod only"
            )
            return
        if verb == "install":
            token = (
                self.s.api_token.get_secret_value()
                if self.s.mcp_require_token and self.s.api_token
                else None
            )
            steps = await asyncio.to_thread(
                global_setup.install, True, "http://127.0.0.1:8000/mcp", token
            )
            note = "Installed / updated:\n" + "\n".join(f"• {escape(x)}" for x in steps) + \
                "\nRestart open Claude sessions to load it."  # fmt: skip
        elif verb.startswith("agenton:") or verb.startswith("agentoff:"):
            from app import agents_setup

            action, agent = verb.split(":", 1)
            if agent not in agents_setup.AGENTS:
                await q.message.answer("Unknown agent.")
                return
            fn = agents_setup.install if action == "agenton" else agents_setup.remove
            try:
                note = escape(await asyncio.to_thread(fn, agent))
            except ValueError as e:
                note = f"❌ {escape(str(e))}"
        elif verb == "remove_yes":
            steps = await asyncio.to_thread(global_setup.remove)
            note = "Removed:\n" + "\n".join(f"• {escape(x)}" for x in steps)
        await self._setup_panel(q.message.answer, note)

    def _list_text(self) -> str:
        hub = self.orch.channel
        live = hub.live()
        lines = ["🖥 <b>SESSIONS</b>"]
        lines += [session_line(x) for x in live] or [
            "none open · terminal: <code>agw session</code> (⚡ push) · any session: /loop + get_messages (🔄 pull)"
        ]
        lines += ["", "📁 <b>PROJECTS</b>"]
        for num, name in hub.projects():
            mine = [x for x in live if x.project == name]
            ready = sum(x.deliverable for x in mine)
            state = (
                f"{len(mine)} open, {ready} can take work"
                if mine
                else "no open session (work waits)"
            )
            lines.append(f"<b>p{num}</b> · {escape(name)} · {state}")
        if not hub.projects():
            lines.append("none yet (a project appears once a session opens for it)")
        lines += [
            "",
            "<code>/send s1 &lt;text&gt;</code> · <code>/send p1 &lt;text&gt;</code> · <code>/off s1</code> · "
            "<code>/on s1</code> · <code>/close s1</code> · <code>/open name</code>",
        ]
        return "\n".join(lines)

    async def on_live(self, m: TgMessage):
        if self._authorized_msg(m):
            await m.answer(self._list_text())

    def _queue_text(self) -> str:
        hub = self.orch.channel
        msgs = hub.open_messages()
        if not msgs:
            return "📥 <b>QUEUE</b>\nempty: every request was taken and answered (or cancelled)"
        label = {"pending": "⏳ waiting", "queued": "📌 assigned", "delivered": "▶️ in progress"}
        lines = ["📥 <b>QUEUE</b>"]
        for x in msgs:
            sess = hub.sessions.get(x.get("session_id") or "")
            where = f"s{sess.num}" if sess else f"p{hub.project_num(x['project'])}"
            age = int((now() - datetime.fromisoformat(x["created_at"])).total_seconds() // 60)
            request = x["text"].split("\n", 1)[-1]
            lines.append(f"<b>m{x.get('num', '?')}</b> · {label.get(x['state'], x['state'])} · {where} · "
                         f"{escape(x['project'])} · {age}m · {escape(fmt.truncate(request, 60))}")  # fmt: skip
        lines += ["", "<code>/cancel m1</code> cancels one (in-progress work is only marked)"]
        return "\n".join(lines)

    async def on_queue(self, m: TgMessage):
        if self._authorized_msg(m):
            await m.answer(self._queue_text())

    async def on_cancel(self, m: TgMessage, command: CommandObject):
        if not self._authorized_msg(m):
            return
        ref = (command.args or "").strip()
        msg = self.orch.channel.cancel(ref) if ref else None
        if msg is None:
            await m.answer("Usage: /cancel &lt;m#&gt; (open requests: /queue)")
            return
        was = msg.get("cancelled_from")
        note = ("it was already being worked on: the session is not interrupted, its answer will be "
                "marked cancelled") if was == "delivered" else "it will not be delivered"  # fmt: skip
        await m.answer(f"🗑 <b>m{msg.get('num')}</b> cancelled · {escape(note)}")

    async def on_open(self, m: TgMessage, command: CommandObject):
        """/open <folder|p#> [new]: start a Claude Code session for a project folder on this Mac.
        /open alone: buttons for the known projects."""
        if not self._authorized_msg(m):
            return
        words = (command.args or "").split()
        new = bool(words) and words[-1].lower() == "new"
        query = " ".join(words[:-1] if new else words)
        if not query:
            await self._open_menu(m.answer)
            return
        await self._open(m.answer, query, new)

    async def _open_menu(self, answer) -> None:
        hub = self.orch.channel
        rows = [(num, name, hub.project_dir(name)) for num, name in hub.projects()]
        rows = [r for r in rows if r[2]]
        if not rows:
            await answer("Usage: /open &lt;project folder&gt; [new] (e.g. <code>/open k09</code>)")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"p{num} · {Path(d).name}", callback_data=f"o:{num}")]
            for num, _name, d in rows[:12]
        ])  # fmt: skip
        await answer(
            "🖥 <b>OPEN</b> · tap a project, or <code>/open &lt;folder name&gt;</code> for any other folder",
            reply_markup=kb,
        )

    async def send_ask_escalation(self, a: dict) -> None:
        """An agent is waiting for another one that cannot or does not answer: the operator decides."""
        asks = self.orch.asks
        asker = asks._label(a.get("from_session"), a.get("from_project"))
        body = (
            f"{escape(asker)} asked <b>{escape(a['to_project'])}</b>"
            + (f" (s{target.num})" if (target := self.orch.channel.sessions.get(a.get("to_session") or "")) else "")
            + f":\n{escape(fmt.truncate(a['request'], 700))}\n\n<b>Problem:</b> {escape(a.get('reason', ''))}"
        )  # fmt: skip
        k = a["ask_id"]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Resend", callback_data=f"k:resend:{k}"),
             InlineKeyboardButton(text="➡️ Other session", callback_data=f"k:other:{k}")],
            [InlineKeyboardButton(text="🚀 Open it", callback_data=f"k:open:{k}"),
             InlineKeyboardButton(text="✍️ Answer myself", callback_data=f"k:answer:{k}"),
             InlineKeyboardButton(text="❌ Cancel", callback_data=f"k:cancel:{k}")],
        ])  # fmt: skip
        html = fmt.envelope("ask", body, project=a["to_project"], title=f"An agent is waiting ({k})", ref=k,
                            reply="Reply to this message to answer it yourself")  # fmt: skip
        msg = await self._send(self.bot.send_message, chat_id=self.s.telegram_control_chat_id, text=html,
                               reply_markup=kb)  # fmt: skip
        if msg is not None:
            self.orch.store.put(
                "ask_tg", f"c:{self.s.telegram_control_chat_id}:{msg.message_id}", {"ask_id": k}
            )

    async def on_ask_button(self, q: CallbackQuery):
        chat_id = q.message.chat.id if q.message else None
        if not self.authorized(q.from_user.id, chat_id):
            log.warning("unauthorized ask button from user=%s chat=%s", q.from_user.id, chat_id)
            await q.answer("Not authorized.", show_alert=True)
            return
        _, verb, ask_id, *rest = q.data.split(":")
        asks, hub = self.orch.asks, self.orch.channel
        a = asks.get(ask_id)
        if a is None or a["state"] in ("answered", "cancelled"):
            await q.answer(f"Already {a['state'] if a else 'gone'}.", show_alert=True)
            return
        try:
            if verb == "resend" or (verb == "to" and rest):
                target = (
                    next((x for x in hub.live() if x.num == int(rest[0])), None) if rest else None
                )
                a = asks.resend(ask_id, to_session=target.session_id if target else None)
                where = (
                    f"s{target.num}"
                    if target
                    else (a.get("delivered_to") and "the same session") or "the queue"
                )
                await q.answer(f"Resent to {where}.")
            elif verb == "other":
                options = [
                    x for x in hub.live() if x.deliverable and x.session_id != a.get("from_session")
                ]
                if not options:
                    await q.answer("No other session can take it right now.", show_alert=True)
                    return
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"s{x.num} · {x.project}"[:60], callback_data=f"k:to:{ask_id}:{x.num}")]
                    for x in options[:10]
                ])  # fmt: skip
                await q.answer()
                await q.message.answer(
                    f"Send <code>{ask_id}</code> to which session?", reply_markup=kb
                )
            elif verb == "open":
                from app.launcher import open_project

                folder = hub.project_dir(a["to_project"])
                if folder is None:
                    await q.answer(
                        "No folder recorded for that project; use /open <folder>.", show_alert=True
                    )
                    return
                root = self.s.projects_root or Path(__file__).resolve().parents[2]
                cwds = [x.cwd for x in hub.live()]
                status, _ = await asyncio.to_thread(
                    open_project, root, Path(folder).name, cwds, True, self.s.mcp_require_token
                )
                asks.resend(
                    ask_id
                )  # waits in the queue; delivered as soon as the new session connects
                await q.answer(
                    f"Opening {Path(folder).name} ({status}); the request follows when it connects."
                )
            elif verb == "answer":
                await q.answer()
                prompt = await q.message.answer(
                    f"✍️ Reply to this message with the answer for <code>{ask_id}</code>.",
                    reply_markup=ForceReply(selective=True),
                )
                self.orch.store.put(
                    "ask_tg", f"c:{chat_id}:{prompt.message_id}", {"ask_id": ask_id}
                )
                return
            elif verb == "cancel":
                asks.cancel(ask_id)
                await q.answer("Cancelled; the asking agent was told.")
            else:
                await q.answer("Unknown action.", show_alert=True)
                return
        except (ValueError, OSError, RuntimeError) as e:
            await q.answer(fmt.truncate(str(e), 190), show_alert=True)
            return
        if q.message and verb != "other":
            try:
                await q.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

    async def on_open_button(self, q: CallbackQuery):
        chat_id = q.message.chat.id if q.message else None
        if not self.authorized(q.from_user.id, chat_id):
            log.warning("unauthorized open button from user=%s chat=%s", q.from_user.id, chat_id)
            await q.answer("Not authorized.", show_alert=True)
            return
        await q.answer("Opening…")
        await self._open(q.message.answer, f"p{q.data[2:]}", False)

    async def _open(self, answer, query: str, new: bool) -> None:
        from app.launcher import open_project

        root = self.s.projects_root or Path(__file__).resolve().parents[2]
        hub = self.orch.channel
        if re.fullmatch(r"[pP]\d+", query):  # a project from /list: open the folder it was used in
            name = hub.resolve_project(query)
            folder = hub.project_dir(name) if name else None
            if folder is None:
                await answer(
                    f"Unknown project {escape(query)} (see /list), or no folder recorded for it."
                )
                return
            if Path(folder).parent != root:
                await answer(
                    f"{escape(folder)} is outside {escape(str(root))}; /open only starts folders there."
                )
                return
            query = Path(folder).name
        live = hub.live()
        try:
            status, hits = await asyncio.to_thread(
                open_project, root, query, [x.cwd for x in live], new, self.s.mcp_require_token
            )
        except Exception as e:  # osascript refused (permission), setup failed, ...
            log.warning("open %s failed: %s", query, e)
            await answer(f"❌ Could not open {escape(query)}: {escape(str(e)[:300])}")
            return
        if status == "none":
            await answer(f"No project folder matches {escape(query)} in {escape(root.name)}/.")
            return
        if status == "many":
            names = "\n".join(f"• <code>/open {escape(d.name)}</code>" for d in hits[:15])
            await answer(f"Several folders match {escape(query)}:\n{names}")
            return
        repo = hits[0]
        if status == "already_open":
            sess = [x for x in live if x.cwd.rstrip("/") == str(repo)]
            ids = ", ".join(f"s{x.num}" for x in sess)
            first = ids.split(",")[0]
            await answer(
                f"{escape(repo.name)} is already open ({ids}). Use <code>/send {first} …</code>, "
                f"or <code>/open {escape(repo.name)} new</code> for another session."
            )
            return
        body = (
            "Opening a Terminal window on the Mac with <code>agw session</code>; "
            "it starts taking work by itself."
        )
        if status == "opened_connected":
            body += "\nFirst time: connected the repo to the gateway (CLAUDE.md, .mcp.json; not committed)."
        body += "\nIt shows up in /list within about a minute. A new folder may first ask to be trusted in that window."
        await answer(fmt.envelope("open", body, project=repo.name, title=str(repo)))

    async def on_close(self, m: TgMessage, command: CommandObject):
        """/close <s#>: push -> stop its Claude Code process; pull -> stop its loop."""
        if not self._authorized_msg(m):
            return
        ref = (command.args or "").strip()
        s = self.orch.channel.find(ref) if ref else None
        if s is None:
            await m.answer("Usage: /close &lt;s#&gt; (ids from /list)")
            return
        what = await asyncio.to_thread(self.orch.channel.close, s)
        await m.answer(f"🔌 <b>s{s.num}</b> closed ({escape(s.project)}) · {escape(what)}")

    async def on_switch(self, m: TgMessage, command: CommandObject):
        """/on <session> or /off <session>: let a session take pushed/pulled work, or not."""
        if not self._authorized_msg(m):
            return
        on = (command.command or "").lower() == "on"
        ref = (command.args or "").strip()
        s = self.orch.channel.find(ref) if ref else None
        if s is None:
            await m.answer(f"Usage: /{'on' if on else 'off'} &lt;s#&gt; (ids from /list)")
            return
        self.orch.channel.set_enabled(s, on)
        await m.answer(session_line(s))

    async def on_send(self, m: TgMessage, command: CommandObject):
        """Push a request into the open Claude Code session of a project (queued if none is open)."""
        if not self._authorized_msg(m):
            return
        hub = getattr(self.orch, "channel", None)
        ref, _, text = (command.args or "").strip().partition(" ")
        if hub is None or not ref or not text.strip():
            await m.answer(
                "Usage: /send &lt;s#|p#|project&gt; &lt;text&gt; (e.g. <code>/send s1 fix the pause menu</code>; "
                "ids from /list)"
            )
            return
        session = hub.find(ref) if re.fullmatch(r"[sS]\d+", ref) else None
        project = session.project if session else hub.resolve_project(ref)
        if project is None:
            await m.answer(f"Unknown session or project {escape(ref)}.\n\n" + self._list_text())
            return
        sender = m.from_user.full_name if m.from_user else ""
        msg, target = hub.send(
            project, f"Request from {sender} (Telegram):\n{text.strip()}", sender=sender,
            session_id=session.session_id if session else None,
        )  # fmt: skip
        if target is not None:
            how = "pushed into" if target.mode == "push" else "queued for the next get_messages of"
            status = f"m{msg['num']} {how} session s{target.num} (<code>{escape(target.session_id)}</code>)"
        else:
            reason = hub.undeliverable_reason(project) or "no open session"
            status = f"m{msg['num']} queued: {escape(reason)}; delivered when a session can take it"
        await m.answer(fmt.envelope("feedback", status, project=project, ref=msg["message_id"]))

    async def on_templates(self, m: TgMessage):
        if self._authorized_msg(m):
            src = m.reply_to_message
            await m.answer(templates((src.text or src.caption or "") if src else ""))

    async def on_agents(self, m: TgMessage):
        if self._authorized_msg(m):
            await self._agents_panel(m.answer)

    async def _agents_panel(self, answer, note: str = "") -> None:
        """Every agent the operator can steer: open sessions by kind (app / CLI) and the API agents."""
        Btn = InlineKeyboardButton  # noqa: N806
        live = self.orch.channel.live()
        lines, rows = ["🤖 <b>AGENTS</b>" + (f"\n{note}" if note else "")], []
        for kind, title in (
            ("app", "🖥 <b>App</b> (desktop / IDE)"),
            ("cli", "⌨️ <b>CLI</b> (terminal)"),
        ):
            mine = [x for x in live if x.kind == kind]
            lines += ["", title] + ([session_line(x) for x in mine] or ["none open"])
            rows += [[Btn(text=f"{'⏸ Pause' if x.enabled else '▶️ Resume'} s{x.num}",
                          callback_data=f"y:{'off' if x.enabled else 'on'}:s{x.num}"),
                      Btn(text=f"✖ Close s{x.num}", callback_data=f"y:close:s{x.num}")] for x in mine]  # fmt: skip
        lines += ["", "☁️ <b>API</b> (the gateway calls them for tasks)"]
        api = [
            (c["name"], c["available"], c["on"], f"api:{c['name']}")
            for c in self.orch.router.describe()
        ]
        img = self.orch._image_provider
        if img is not None:
            api.append((f"{img.name} (images)", getattr(img, "available", True),
                        self.orch.switches.on("generate_art"), "generate_art"))  # fmt: skip
        api_buttons = []
        for name, ready, on, key in api:
            icon = ("🟢" if on else "⏸") if ready else "⚪"
            state = ("on" if on else "off") if ready else "not configured"
            lines.append(f"{icon} <b>{escape(name)}</b> · {state}")
            if ready:
                api_buttons.append(
                    Btn(text=f"{'⏸' if on else '▶️'} {name}", callback_data=f"y:sw:{key}")
                )
        rows += [api_buttons[i : i + 2] for i in range(0, len(api_buttons), 2)]
        lines += ["", "Give work: /send s# &lt;text&gt; · connect more agents: /setup"]
        await answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def on_agents_button(self, q: CallbackQuery):
        chat_id = q.message.chat.id if q.message else None
        if not self.authorized(q.from_user.id, chat_id):
            log.warning("unauthorized agents button from user=%s chat=%s", q.from_user.id, chat_id)
            await q.answer("Not authorized.", show_alert=True)
            return
        verb, _, ref = q.data[2:].partition(":")
        hub = self.orch.channel
        if verb == "sw":  # an API agent, or image generation
            known = {f"api:{c['name']}" for c in self.orch.router.describe()} | {"generate_art"}
            if ref not in known:
                await q.answer("Unknown agent.")
                return
            on = not self.orch.switches.on(ref)
            self.orch.switches.set(ref, on)
            log.info("%s switched %s by user=%s", ref, "on" if on else "off", q.from_user.id)
            await q.answer(f"{ref}: {'on' if on else 'off'}")
            await self._agents_panel(
                q.message.answer,
                f"{'▶️' if on else '⏸'} {escape(ref)} is now {'on' if on else 'off'}",
            )
            return
        s = hub.find(ref)
        if s is None:
            await q.answer("That session is no longer open.", show_alert=True)
            return
        if verb in ("on", "off"):
            hub.set_enabled(s, verb == "on")
            await q.answer(f"s{s.num}: {verb}")
            await self._agents_panel(q.message.answer)
        elif verb == "close":  # stops a terminal session's Claude process: confirm first
            await q.answer()
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"Yes, close s{s.num}", callback_data=f"y:closeyes:s{s.num}"),
                InlineKeyboardButton(text="Keep it", callback_data="y:keep:"),
            ]])  # fmt: skip
            await q.message.answer(f"Close {session_line(s)}?", reply_markup=kb)
        elif verb == "closeyes":
            what = await asyncio.to_thread(hub.close, s)
            await q.answer(f"s{s.num} closed")
            await self._agents_panel(q.message.answer, f"🔌 s{s.num} closed · {escape(what)}")

    # ---------- /keys: provider keys and models, set remotely ----------

    def _key_user(self, user_id: int | None, chat) -> bool:
        """Allowlisted operator, in the control group or in a private chat with the bot."""
        if user_id is None or user_id not in self.s.telegram_allowed_user_ids:
            return False
        return chat.id == self.s.telegram_control_chat_id or (
            chat.type == "private" and chat.id == user_id
        )

    async def on_keys(self, m: TgMessage):
        if self._key_user(m.from_user and m.from_user.id, m.chat):
            await self._keys_panel(m.answer)
        else:
            log.warning(
                "unauthorized /keys from user=%s chat=%s", m.from_user and m.from_user.id, m.chat.id
            )

    async def _keys_panel(self, answer, note: str = "") -> None:
        from app import keys
        from app.config import env_file

        Btn = InlineKeyboardButton  # noqa: N806
        head = f"🔑 <b>KEYS</b> (saved: encrypted in the database; else {escape(env_file())})"
        lines = [head + (f"\n{note}" if note else ""), ""]
        rows, saved = [], keys.saved(self.orch.store)
        for name, secret in keys.names(self.orch.gateway_cfg):
            value = os.environ.get(name)
            where = " · saved" if name in saved else (" · .env" if value else "")
            lines.append(
                f"{'🔑' if secret else '🧠'} <b>{name}</b> · {escape(keys.mask(value, secret))}{where}"
            )
            row = [Btn(text=f"✏️ {name}", callback_data=f"x:set:{name}")]
            if name in saved:
                row.append(Btn(text=f"🗑 {name}", callback_data=f"x:clear:{name}"))
            rows.append(row)
        lines += ["", "Values go only in a private chat with the bot; it deletes your message at once. "
                  "Providers reload without a restart."]  # fmt: skip
        await answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def on_keys_button(self, q: CallbackQuery):
        from app import keys
        from app.config import env_file

        if not q.message or not self._key_user(q.from_user.id, q.message.chat):
            await q.answer("Not authorized.", show_alert=True)
            return
        verb, _, name = q.data[2:].partition(":")
        if name not in dict(keys.names(self.orch.gateway_cfg)):
            await q.answer("Unknown variable.")
            return
        if verb == "set":
            self._key_wait[q.from_user.id] = (name, time.monotonic() + 300)
            me = await self.bot.get_me()
            await q.answer()
            await q.message.answer(
                f"Send the value of <b>{name}</b> to me in a private chat within 5 minutes: "
                f"https://t.me/{me.username}\nI delete your message as soon as I read it. Send /cancel there to stop."
            )
        elif verb == "clear":
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"Yes, clear {name}", callback_data=f"x:clearyes:{name}"),
                InlineKeyboardButton(text="Keep it", callback_data="x:keep:" + name),
            ]])  # fmt: skip
            await q.answer()
            await q.message.answer(
                f"Clear the saved <b>{name}</b>? Its provider falls back to .env, or stops.",
                reply_markup=kb,
            )
        elif verb == "clearyes":
            from dotenv import dotenv_values

            keys.clear(self.orch.store, name, dotenv_values(env_file()).get(name))
            await self._reload_providers()
            log.info("%s cleared by user=%s", name, q.from_user.id)
            await q.answer(f"{name} cleared")
            await self._keys_panel(q.message.answer, f"🗑 {name} cleared")
        else:
            await q.answer()

    async def on_key_value(self, m: TgMessage):
        """Private chat: the value for a pending /keys entry. Deleted at once, never logged or echoed."""
        from app import keys

        user = m.from_user.id if m.from_user else None
        wait = self._key_wait.get(user) if user is not None else None
        text = (m.text or "").strip()
        if wait is None or user not in self.s.telegram_allowed_user_ids:
            raise SkipHandler()
        name, deadline = wait
        if text.startswith("/"):
            if text.split()[0].lower() in ("/cancel", "/cancelkey"):
                self._key_wait.pop(user, None)
                await m.answer(f"Stopped: {name} unchanged.")
                return
            raise SkipHandler()
        self._key_wait.pop(user, None)
        try:
            await m.delete()
            gone = ""
        except TelegramBadRequest:
            gone = "\n⚠️ I could not delete your message: delete it yourself."
        if time.monotonic() > deadline:
            await m.answer(f"Too late for {name} (5 minutes): press ✏️ in /keys again." + gone)
            return
        try:
            value = keys.check(text)
        except ValueError as e:
            await m.answer(f"❌ {name} not saved: {escape(str(e))}." + gone)
            return
        try:
            keys.save(self.orch.store, name, value)
        except ValueError as e:  # AGW_SECRET_KEY missing or invalid
            await m.answer(f"❌ {name} not saved: {escape(str(e))}." + gone)
            return
        await self._reload_providers()
        log.info("%s set by user=%s", name, user)
        secret = dict(keys.names(self.orch.gateway_cfg)).get(name, True)
        await m.answer(f"✅ <b>{name}</b> saved: {escape(keys.mask(value, secret))}. "
                       f"{await self._provider_state(name)}" + gone)  # fmt: skip

    async def _reload_providers(self) -> None:
        from app.main import reload_providers

        reload_providers(self.orch)

    async def _provider_state(self, name: str) -> str:
        """Free health check of the providers that use this variable."""
        cfg = self.orch.gateway_cfg
        ids = (
            [p.id for p in cfg.providers.values() if name in (p.api_key_env, p.model_env)]
            if cfg
            else []
        )
        conns = [self.orch.router.connectors.get(i) for i in ids]
        img = self.orch._image_provider
        conns += [img] if img is not None and img.name in ids else []
        out = []
        for c in filter(None, conns):
            try:
                state, detail = await asyncio.wait_for(c.health_status(), 20)
            except Exception as e:  # noqa: BLE001 - shown to the operator, not raised
                state, detail = "error", type(e).__name__
            out.append(
                f"{c.name}: {escape(state)}" + (f" ({escape(str(detail)[:120])})" if detail else "")
            )
        return " · ".join(out) or "No provider uses it yet."

    async def on_roles(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        lines = [
            f"<b>{p.label}</b> ({p.role}): {escape(p.description)}"
            f" — prefers {', '.join(p.preference)}"
            for p in self.orch.profiles()
        ]
        await m.answer("\n".join(lines))

    async def on_agents_status(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        lines = []
        for c in self.orch.router.describe():
            icon = "🟢" if c["available"] else "⚪"
            lines.append(f"{icon} <b>{c['name']}</b> [{c['verification']}]")
        busy = list(self.orch._active_calls)
        lines.append(f"Running calls: {len(busy)}")
        await m.answer("\n".join(lines))

    async def on_sessions(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        sess = sorted(self.orch.sessions.values(), key=lambda s: s.created_at)[-10:]
        if not sess:
            await m.answer("No sessions yet.")
            return
        await m.answer(
            "\n".join(
                f"<code>{s.session_id}</code> {s.role} via {s.connector} (task {s.task_id})"
                for s in sess
            )
        )

    async def on_status(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        roots = [t for t in self.orch.tasks.values() if not t.parent_id][-10:]
        await m.answer("\n".join(fmt.task_line(t) for t in roots) or "No tasks yet.")

    async def _arg(self, m: TgMessage, command: CommandObject, usage: str) -> str | None:
        arg = (command.args or "").strip()
        if not arg or " " in arg:
            await m.answer(f"Usage: {escape(usage)}")
            return None
        return arg

    async def on_report(self, m: TgMessage, command: CommandObject):
        if not self._authorized_msg(m) or not (
            arg := await self._arg(m, command, "/report <task_id>")
        ):
            return
        task = self.orch.tasks.get(arg)
        if not task:
            await m.answer("Unknown task id.")
            return
        html, files = self.orch._report_message(task)
        await self._send(m.answer, text=html)
        for name, data in files:
            await m.answer_document(BufferedInputFile(data, name))

    async def on_stop(self, m: TgMessage, command: CommandObject):
        if not self._authorized_msg(m) or not (arg := await self._arg(m, command, "/stop <id>")):
            return
        try:
            stopped = await self.orch.stop(arg)
        except KeyError:
            await m.answer("Unknown task or session id.")
            return
        await m.answer("Stopped." if stopped else "Task already finished.")

    async def on_retry(self, m: TgMessage, command: CommandObject):
        if not self._authorized_msg(m) or not (
            arg := await self._arg(m, command, "/retry <task_id>")
        ):
            return
        try:
            new = await self.orch.retry(arg)
        except KeyError:
            await m.answer("Unknown task id.")
            return
        except (ValueError, ConnectorError) as e:
            await m.answer(escape(str(e)))
            return
        await m.answer(f"Retrying as <code>{new.task_id}</code>")

    async def on_steps(self, m: TgMessage, command: CommandObject):
        if not self._authorized_msg(m) or not (
            arg := await self._arg(m, command, "/steps <task_id>")
        ):
            return
        task = self.orch.tasks.get(arg)
        if task is None or not task.run_id:
            await m.answer("Unknown task or no workflow yet.")
            return
        lines = []
        for r in self.orch.load_steps(task.run_id).values():
            line = f"<code>{r.step_id}</code> {r.state} · attempts {r.attempt}"
            if r.duration_s is not None:
                line += f" · {r.duration_s}s"
            if r.error:
                line += f"\n   ↳ {escape(fmt.truncate(r.error, 200))}"
            lines.append(line)
        await self._send(m.answer, text="\n".join(lines) or "No steps.")

    async def on_answer(self, m: TgMessage, command: CommandObject):
        if not self._authorized_msg(m):
            return
        task_id, _, text = (command.args or "").strip().partition(" ")
        if not task_id or not text.strip():
            await m.answer("Usage: /answer &lt;task_id|request_id&gt; &lt;text&gt;")
            return
        try:
            if task_id.startswith("hin_"):  # answer a human-input request (e.g. from an MCP caller)
                hi = self.orch.collab.answer_human_input(task_id, text, str(m.from_user.id))
                await m.answer(f"Answer recorded for <code>{hi.request_id}</code>")
            else:
                await m.answer(await self.orch.answer(task_id, text, str(m.from_user.id)))
        except (ActionError, CollaborationError) as e:
            await m.answer(escape(str(e)))

    # ---------- report feedback loop ----------

    def _update_key(self, m: TgMessage, kwargs: dict) -> str:
        upd = kwargs.get("event_update")
        uid = getattr(upd, "update_id", None)
        return f"u:{uid}" if uid is not None else f"m:{m.chat.id}:{m.message_id}"

    async def on_discussion(self, m: TgMessage, **kwargs):
        """Messages in the linked Discussion Group: channel copies and replies to reports."""
        origin = getattr(m, "forward_origin", None)
        if (
            getattr(m, "is_automatic_forward", False)
            and origin is not None
            and getattr(origin, "chat", None)
        ):
            self.orch.collab.link_discussion_copy(
                origin.chat.id, origin.message_id, m.chat.id, m.message_id
            )
            return
        if getattr(m, "media_group_id", None):
            raise SkipHandler()  # album parts are collected by on_album_part
        text = (m.text or m.caption or "").strip()
        reply = getattr(m, "reply_to_message", None)
        if text.startswith("/") and not text.startswith("/feedback"):
            # The discussion group may also be the control group: let command handlers run.
            raise SkipHandler()
        has_image = bool(getattr(m, "photo", None)) or _image_doc(getattr(m, "document", None))
        if not (text or has_image) or (reply is None and not text.startswith("/feedback")):
            return  # ordinary discussion, not addressed to the gateway
        await self._feedback_message(m, text, reply, [m] if has_image else [], kwargs)

    async def _feedback_message(self, m: TgMessage, text: str, reply, parts: list, kwargs: dict):
        """Feedback from the discussion group: a reply to a report, or /feedback <id> <text>,
        optionally with photos (single photo or album) that become the feedback's screenshots."""
        user_id = m.from_user.id if m.from_user else None
        if user_id not in self.s.telegram_allowed_user_ids:
            log.warning("unauthorized feedback from user=%s in discussion %s", user_id, m.chat.id)
            return
        if not self.orch.collab.claim_update(self._update_key(m, kwargs)):
            return  # duplicate delivery of the same update
        try:
            images = [img for p in parts for img in await self._fetch_images(p)]
        except (AttachmentError, TimeoutError) as e:
            log.warning("feedback image rejected: %s", e or "download timed out")
            await m.reply(f"❌ Image rejected: {escape(str(e) or 'download timed out')}")
            return
        if text.startswith("/feedback"):
            ref, body = parse_feedback(text)
            if ref or reply is None:
                await self._explicit_feedback(m, text, kwargs, images)
                return
            text = body  # no id in the caption: the replied-to message says which task
        ask = self.orch.store.get("ask_tg", f"c:{reply.chat.id}:{reply.message_id}")
        if ask is not None:  # a reply to an escalation (or its "answer myself" prompt): the answer
            a = self.orch.asks.answer(ask["ask_id"], text, by="the operator")
            who = self.orch.asks._label(a.get("from_session"), a.get("from_project"))
            await m.reply(f"✅ Sent to {escape(who)} as the answer to {escape(a['ask_id'])}.")
            return
        fwd = getattr(reply, "forward_origin", None)
        report = self.orch.collab.resolve_report(
            chat_id=reply.chat.id,
            message_id=reply.message_id,
            forward_chat_id=getattr(getattr(fwd, "chat", None), "id", None),
            forward_message_id=getattr(fwd, "message_id", None),
        )
        if report is not None:
            await self._record_feedback(
                m, text, report_id=report.report_id, kwargs=kwargs, images=images
            )
            return
        # Not an indexed report post: any other gateway message (FEEDBACK confirmation, question,
        # deploy, step note) names its task in the envelope. Trusted only when the bot sent it.
        sender = getattr(getattr(reply, "from_user", None), "id", None)
        body = getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
        ids = {mt.group(1): mt.group(0) for mt in reversed(list(_ID.finditer(body)))}  # first wins
        rep_id = ids.get("rep") if self.orch.collab.get_report(ids.get("rep", "")) else None
        task_id = ids.get("tsk") if ids.get("tsk") in self.orch.tasks else None
        if sender is not None and sender == getattr(self.bot, "id", None) and (rep_id or task_id):
            await self._record_feedback(
                m, text, report_id=rep_id, task_id=None if rep_id else task_id,
                kwargs=kwargs, images=images,
            )  # fmt: skip
            return
        log.info(
            "feedback not resolved: reply_to chat=%s msg=%s sender=%s sender_chat=%s auto_fwd=%s ids=%s",
            reply.chat.id, reply.message_id, sender,
            getattr(getattr(reply, "sender_chat", None), "id", None),
            getattr(reply, "is_automatic_forward", None), sorted(ids.values()),
        )  # fmt: skip
        await m.reply(
            "I can't tell which task this refers to. Reply to a gateway message that shows a "
            "<code>#tsk_…</code> tag, or send /feedback &lt;task_id&gt; &lt;text&gt;."
        )

    async def on_feedback_command(self, m: TgMessage, command: CommandObject, **kwargs):
        if not self._authorized_msg(m):
            return
        if not self.orch.collab.claim_update(self._update_key(m, kwargs)):
            return
        await self._explicit_feedback(m, f"/feedback {command.args or ''}", kwargs)

    async def _explicit_feedback(self, m: TgMessage, text: str, kwargs: dict, images=()):
        ref, body = parse_feedback(text)
        if not ref or not (body or images):
            await m.reply(
                "Usage: /feedback &lt;task_id|report_id&gt; &lt;text&gt; (photos optional)"
            )
            return
        await self._record_feedback(
            m,
            body,
            report_id=ref if ref.startswith("rep_") else None,
            task_id=None if ref.startswith("rep_") else ref,
            kwargs=kwargs,
            images=images,
        )

    async def _record_feedback(
        self, m: TgMessage, text: str, report_id=None, task_id=None, kwargs=None, images=()
    ):
        upd = (kwargs or {}).get("event_update")
        try:
            fb = await self.orch.collab.submit_feedback(
                text=text,
                user_id=str(m.from_user.id),
                source="telegram",
                report_id=report_id,
                task_id=task_id,
                update_id=getattr(upd, "update_id", None),
                message_id=m.message_id,
                images=list(images),
            )
        except CollaborationError as e:
            log.warning("feedback rejected (%s): %s", e.code, e)
            await m.reply(f"❌ {escape(str(e))}")
            return
        if fb.route_id == "art_revision":
            body = (f"Regenerating with your changes (<code>{fb.follow_up_task_id}</code>); "
                    "the new version comes here for review")  # fmt: skip
        elif fb.route_id == "channel":
            body = (
                "Pushed into the live Claude Code session"
                if fb.delivered
                else "Queued: no session that can receive it is open; delivered when one is"
            )
        elif fb.follow_up_task_id and fb.route_id != "acknowledge":
            body = f"Follow-up <code>{fb.follow_up_task_id}</code> started ({escape(fb.route_id or '')})"
        elif fb.follow_up_task_id:
            body = "Recorded"
        else:
            body = "Recorded · no follow-up workflow is configured for this feedback"
        if fb.artifact_ids:
            body += f" · 🖼 {len(fb.artifact_ids)} screenshot(s) attached"
        task = self.orch.tasks.get(fb.task_id)
        msg = (
            self.orch.msg("feedback", task, body, ref=fb.feedback_id)
            if task
            else f"💬 <b>FEEDBACK</b> {body} · <code>{fb.feedback_id}</code>"
        )
        await m.reply(msg)

    async def on_approvals(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        pend = self.orch.pending_approvals()
        await m.answer(
            "\n".join(
                f"<code>{a['approval_id']}</code> task {a['task_id']} step {a.get('step_id')}: "
                f"{escape(fmt.truncate(a['text'], 120))}"
                for a in pend
            )
            or "No pending approvals."
        )

    async def on_tools(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        lines = [
            f"<code>{t.name}</code> [{t.risk}{', approval' if t.requires_approval else ''}] "
            f"{escape(fmt.truncate(t.description, 80))}"
            for t in self.orch.tools.list()
        ]
        await self._send(m.answer, text="\n".join(lines) or "No tools registered.")

    async def on_tool(self, m: TgMessage, command: CommandObject):
        if not self._authorized_msg(m):
            return
        name, _, raw = (command.args or "").strip().partition(" ")
        if not name:
            await m.answer("Usage: /tool &lt;name&gt; &lt;json args&gt;")
            return
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            req = TaskRequest(
                objective=f"Run tool {name}",
                user_id=str(m.from_user.id),
                tool=name,
                tool_args=args,
                mode="direct_tool",
            )
            task = await self.orch.submit_request(req)
        except (ValueError, json.JSONDecodeError) as e:
            await m.answer(f"❌ {escape(str(e))}")
            return
        await m.answer(f"Tool task <code>{task.task_id}</code> created")

    async def on_plugins(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        lines = []
        for lp in self.orch.plugins.loaded.values():
            if lp.manifest:
                ops = ", ".join(o.task_type for o in lp.manifest.operations)
                lines.append(
                    f"{'🟢' if lp.active else '⚪'} <b>{lp.manifest.id}</b> {lp.manifest.version}: {ops}"
                )
            else:
                lines.append(f"🔴 {escape(lp.module)}: {escape(lp.error or '')}")
        await m.answer("\n".join(lines) or "No plugins.")

    async def on_capabilities(self, m: TgMessage):
        if not self._authorized_msg(m):
            return
        icon = {"operational": "🟢", "validated": "🟡", "degraded": "🟠"}
        lines = [
            f"{icon.get(c.status, '⚪')} <code>{c.id}</code> {c.status}"
            for c in self.orch.caps.list()
            if c.kind != "tool"
        ]
        tools = self.orch.caps.list("tool")
        lines.append(
            f"Tools: {sum(c.status == 'operational' for c in tools)} operational / {len(tools)} registered"
        )
        await self._send(m.answer, text="\n".join(lines))

    async def on_stray_media(self, m: TgMessage):
        if self._authorized_msg(m):
            await m.answer("Attachment not used: send media with a <code>/task …</code> caption.")

    async def on_album_part(self, m: TgMessage):
        in_discussion = m.chat.id == self.discussion_chat_id and m.from_user is not None
        if not (
            self._authorized_msg(m)
            or (in_discussion and m.from_user.id in self.s.telegram_allowed_user_ids)
        ):
            return
        gid = m.media_group_id
        self._albums.setdefault(gid, []).append(m)
        if timer := self._album_timers.get(gid):
            timer.cancel()
        self._album_timers[gid] = asyncio.create_task(self._flush_album(gid))

    async def _flush_album(self, gid: str):
        await asyncio.sleep(ALBUM_WAIT_S)
        parts = self._albums.pop(gid, [])
        self._album_timers.pop(gid, None)
        lead = next((p for p in parts if (p.caption or "").startswith("/task")), None)
        fb = next((p for p in parts if (p.caption or "").startswith("/feedback")), None) or next(
            (p for p in parts if getattr(p, "reply_to_message", None)), None
        )
        if lead is None and fb is not None and fb.chat.id == self.discussion_chat_id:
            caption = next((p.caption for p in parts if p.caption), "") or ""
            await self._feedback_message(fb, caption.strip(), fb.reply_to_message, parts, {})
            return
        if lead is None:
            await parts[0].answer(
                f"Album of {len(parts)} items not used: caption it <code>/task …</code>, "
                "or send it as a reply to a report."
            )
            return
        await self._create_draft(lead, lead.caption.partition(" ")[2], parts)

    async def on_task(self, m: TgMessage, command: CommandObject):
        if self._authorized_msg(m):
            await self._create_draft(m, command.args or "", [m])

    async def _fetch_images(self, m: TgMessage) -> list[tuple[str, bytes]]:
        """Photos (and image documents) of one message as (name, bytes); other media are ignored."""
        items = []
        if getattr(m, "photo", None):
            p = m.photo[-1]  # largest size; Telegram re-encodes photos as JPEG
            items.append((p.file_id, p.file_size, f"photo_{m.message_id}.jpg"))
        d = getattr(m, "document", None)
        if _image_doc(d):
            items.append((d.file_id, d.file_size, d.file_name or f"image_{m.message_id}"))
        out = []
        for file_id, size, name in items:
            if size and size > self.s.max_attachment_bytes:
                raise AttachmentError(f"{name}: {size} bytes exceeds limit")
            buf = await asyncio.wait_for(self.bot.download(file_id, destination=io.BytesIO()), 60)
            out.append((name, buf.getvalue()))
        return out

    async def _download(self, m: TgMessage) -> list[str]:
        items = []
        if m.photo:
            p = m.photo[-1]  # Telegram re-encodes photos as JPEG
            items.append((p.file_id, p.file_size, f"photo_{m.message_id}.jpg", "image/jpeg"))
        if m.document:
            d = m.document
            items.append((d.file_id, d.file_size, d.file_name or "file", d.mime_type or ""))
        if m.video or m.audio or m.voice:
            raise AttachmentError("audio/video attachments are not supported yet")
        ids = []
        for file_id, size, name, mime in items:
            if size and size > self.s.max_attachment_bytes:
                raise AttachmentError(f"{name}: {size} bytes exceeds limit")
            buf = await asyncio.wait_for(self.bot.download(file_id, destination=io.BytesIO()), 60)
            att = self.attachments.save(buf.getvalue(), name, mime, "telegram")
            ids.append(att.attachment_id)
        return ids

    async def _create_draft(self, m: TgMessage, text: str, parts: list[TgMessage]):
        text = text.strip()
        if not text:
            await m.answer("Usage: /task &lt;description&gt; (attachments optional)")
            return
        try:
            att_ids = [a for p in parts for a in await self._download(p)]
        except (AttachmentError, TimeoutError) as e:
            await m.answer(f"❌ Attachment rejected: {escape(str(e) or 'download timed out')}")
            return
        group = f"draft_{m.message_id}"
        base = {"text": text, "attachments": att_ids}
        acts = [
            self.actions.create(
                "draft",
                "draft",
                label,
                {TaskState.created},
                group,
                payload={**base, "domain": dom, "task_type": ttype},
            )
            for dom, ttype, label in DOMAIN_CHOICES
            if dom == "auto" or self.orch.plugins.get(dom)
        ] + [
            self.actions.create(
                "draft",
                "draft",
                label,
                {TaskState.created},
                group,
                payload={**base, "workflow": wf},
            )
            for wf, label in WORKFLOW_LABELS.items()
        ]
        await m.answer(
            f"Choose how to handle:\n<i>{escape(fmt.truncate(text, 300))}</i>"
            + (f"\n📎 {len(att_ids)} attachment(s)" if att_ids else ""),
            reply_markup=keyboard(acts),
        )

    async def on_callback(self, q: CallbackQuery):
        token = q.data[2:]
        user_id = q.from_user.id
        chat_id = q.message.chat.id if q.message else None
        allowed_chats = {self.s.telegram_control_chat_id, self.s.telegram_report_channel_id}
        if user_id not in self.s.telegram_allowed_user_ids or chat_id not in allowed_chats:
            log.warning("unauthorized callback from user=%s chat=%s", user_id, chat_id)
            await q.answer("Not authorized.", show_alert=True)
            return
        try:
            action = self.actions.get(token)
            if action and action.kind == "draft":
                self.actions.consume(token, user_id, TaskState.created)
                p = action.payload
                if "workflow" in p:
                    task = await self.orch.submit(
                        p["text"], p["workflow"], str(user_id), attachment_ids=p["attachments"]
                    )
                else:
                    req = TaskRequest(
                        objective=p["text"],
                        user_id=str(user_id),
                        attachment_ids=p["attachments"],
                        domain=None if p["domain"] == "auto" else p["domain"],
                        task_type=p.get("task_type"),
                    )
                    task = await self.orch.submit_request(req)
                text = f"Task {task.task_id} created"
            else:
                text = await self.orch.apply_action(token, user_id)
        except (ActionError, ValueError, KeyError, ConnectorError, AttachmentError) as e:
            await q.answer(fmt.truncate(str(e), 190), show_alert=True)
            return
        await q.answer(fmt.truncate(text, 190))
        if q.message:
            try:
                await q.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
