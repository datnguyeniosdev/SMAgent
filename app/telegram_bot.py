"""Telegram control interface (control group) and report delivery (channel), aiogram 3.

Long polling only: no public webhook endpoint is exposed. Polling refuses to start
if a webhook is already configured for the bot (it would conflict).
"""

import asyncio
import io
import logging
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.types import Message as TgMessage

from app import formatting as fmt
from app.actions import Action, ActionError, ActionRegistry
from app.config import Settings
from app.connectors.base import ConnectorError
from app.models import TaskState, Workflow
from app.orchestrator import Orchestrator
from app.storage import AttachmentError, AttachmentStore

log = logging.getLogger(__name__)
PHOTO_LIMIT = 10 * 1024 * 1024
ALBUM_WAIT_S = 1.5

HELP = """<b>Agent Gateway</b>
/task &lt;description&gt; — new task; attach images/files with the command as
caption, then pick a workflow
/status — recent tasks
/report &lt;task_id&gt; — full task report
/stop &lt;task_id|session_id&gt; — cancel a task
/retry &lt;task_id&gt; — retry a failed/cancelled task
/agents — agent roles and connectors
/agents_status — connector availability and running calls
/sessions — recent agent sessions
/help — this message"""

WORKFLOW_LABELS = {
    Workflow.ask: "💬 Ask",
    Workflow.code: "🛠 Code",
    Workflow.delegate_review: "🔍 Code + Review",
    Workflow.parallel: "⚖️ Parallel",
    Workflow.design_implement_review: "🎨 Design→Code→Review",
}


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

    async def run_polling(self) -> None:
        info = await self.bot.get_webhook_info()
        if info.url:
            log.error("A webhook is configured for this bot; refusing to start long polling.")
            return
        log.info("telegram destinations: %s", await self.check_destinations())
        dp = Dispatcher()
        dp.include_router(self.router)
        await dp.start_polling(
            self.bot, allowed_updates=["message", "callback_query"], handle_signals=False
        )

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
        r.message(F.media_group_id)(self.on_album_part)
        r.message(Command("task"))(self.on_task)
        r.message(F.photo | F.document | F.video | F.audio | F.voice)(self.on_stray_media)
        r.callback_query(F.data.startswith("a:"))(self.on_callback)

    async def on_help(self, m: TgMessage):
        if self._authorized_msg(m):
            await m.answer(HELP)

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

    async def on_stray_media(self, m: TgMessage):
        if self._authorized_msg(m):
            await m.answer("Attachment not used: send media with a <code>/task …</code> caption.")

    async def on_album_part(self, m: TgMessage):
        if not self._authorized_msg(m):
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
        if lead is None:
            await parts[0].answer(
                f"Album of {len(parts)} items not used: put <code>/task …</code> in the caption."
            )
            return
        await self._create_draft(lead, lead.caption.partition(" ")[2], parts)

    async def on_task(self, m: TgMessage, command: CommandObject):
        if self._authorized_msg(m):
            await self._create_draft(m, command.args or "", [m])

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
        acts = [
            self.actions.create(
                "draft",
                "draft",
                label,
                {TaskState.created},
                group,
                payload={"workflow": wf, "text": text, "attachments": att_ids},
            )
            for wf, label in WORKFLOW_LABELS.items()
        ]
        await m.answer(
            f"Choose a workflow for:\n<i>{escape(fmt.truncate(text, 300))}</i>"
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
                task = await self.orch.submit(
                    p["text"], p["workflow"], str(user_id), attachment_ids=p["attachments"]
                )
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
