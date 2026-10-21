"""Telegram control interface (control group) and report delivery (channel), aiogram 3.

Long polling only: no public webhook endpoint is exposed. Polling refuses to start
if a webhook is already configured for the bot (it would conflict).
"""

import asyncio
import io
import json
import logging
import re
from html import escape

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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
)
from aiogram.types import Message as TgMessage

from app import formatting as fmt
from app.actions import Action, ActionError, ActionRegistry
from app.collaboration import CollaborationError
from app.config import Settings
from app.connectors.base import ConnectorError
from app.intake import TaskRequest
from app.models import TaskState, Workflow
from app.orchestrator import Orchestrator
from app.storage import AttachmentError, AttachmentStore

log = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"
ALLOWED_UPDATES = ["message", "callback_query"]
PHOTO_LIMIT = 10 * 1024 * 1024
ALBUM_WAIT_S = 1.5

HELP = (
    """<b>Agent Gateway</b>
/task &lt;description&gt; — new task; attach images/files with the command as
caption, then pick a workflow
/status — recent tasks
/report &lt;task_id&gt; — full task report
/stop &lt;task_id|session_id&gt; — cancel a task
/retry &lt;task_id&gt; — retry a failed/cancelled task
/agents — agent roles and connectors
/agents_status — connector availability and running calls
/sessions — recent agent sessions
/steps &lt;task_id&gt; — workflow steps, attempts and failures
/answer &lt;task_id|request_id&gt; &lt;text&gt; — answer a clarification or input request
/feedback &lt;task_id|report_id&gt; &lt;text&gt; — feedback (or reply to a gateway message); photos = screenshots
/approvals — pending approvals
/templates — copy-ready commands (reply to a gateway message to prefill its ids)
/tools — registered tools · /tool &lt;name&gt; &lt;json args&gt; — run one tool
/plugins — domain plugins · /capabilities — capability status
/help — this message

<b>Message prefixes</b>
"""
    + " · ".join(f"{i} {k}" for i, k in fmt.KINDS.values())
    + """
First line: <code>PREFIX · #project · #task_id</code> (tap a hashtag to find every related message).
Last lines: project, session and ref ids, then the reply template."""
)

MENU = [
    ("task", "New task"),
    ("status", "Recent tasks"),
    ("templates", "Copy-ready commands (reply to a message to prefill)"),
    ("report", "Full task report"),
    ("steps", "Workflow steps of a task"),
    ("answer", "Answer a question"),
    ("feedback", "Feedback on a task or report"),
    ("approvals", "Pending approvals"),
    ("retry", "Retry a task"),
    ("stop", "Cancel a task"),
    ("help", "Commands and message prefixes"),
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
        r.message(Command("start", "help"))(self.on_help)
        r.message(Command("agents"))(self.on_agents)
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
        r.message(Command("tools"))(self.on_tools)
        r.message(Command("tool"))(self.on_tool)
        r.message(Command("plugins"))(self.on_plugins)
        r.message(Command("capabilities"))(self.on_capabilities)
        r.message(Command("task"))(self.on_task)
        r.message(F.photo | F.document | F.video | F.audio | F.voice)(self.on_stray_media)
        r.callback_query(F.data.startswith("a:"))(self.on_callback)

    async def on_help(self, m: TgMessage):
        if self._authorized_msg(m):
            name = escape(m.from_user.full_name) if m.from_user else ""
            mode = "webhook" if self.s.telegram_mode == "webhook" else "polling"
            await m.answer(f"👋 Xin chào {name}! Bot đã sẵn sàng lắng nghe ({mode}).\n\n{HELP}")

    async def on_templates(self, m: TgMessage):
        if self._authorized_msg(m):
            src = m.reply_to_message
            await m.answer(templates((src.text or src.caption or "") if src else ""))

    async def on_agents(self, m: TgMessage):
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
        if fb.follow_up_task_id and fb.route_id != "acknowledge":
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
