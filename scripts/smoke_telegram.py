"""Real Telegram smoke test (reads .env; never prints the token).

  uv run python -m scripts.smoke_telegram --discover   # list chats/users the bot has seen (getUpdates)
  uv run python -m scripts.smoke_telegram              # validate + run a real tool task with Telegram reports

The full command -> result flow (a human sending /task in the control group) is verified by
running the gateway (`uv run uvicorn app.main:app`) and sending `/tool workspace.list_files {}`.
"""

import argparse
import asyncio
import json
import sys

import httpx

from app.config import Settings
from app.intake import TaskRequest
from app.main import build


async def api(client, base, method, **params):
    r = await client.get(f"{base}/{method}", params=params)
    return r.json()


async def main(discover: bool) -> int:
    s = Settings()
    if not s.telegram_bot_token:
        print("NOT CONFIGURED: TELEGRAM_BOT_TOKEN is blank")
        return 2
    base = f"https://api.telegram.org/bot{s.telegram_bot_token.get_secret_value()}"
    async with httpx.AsyncClient(timeout=20) as c:
        me = await api(c, base, "getMe")
        if not me.get("ok"):
            print(f"FAILED: getMe -> {me.get('description')}")
            return 1
        print(
            f"bot ok: @{me['result']['username']} (privacy mode {'off' if me['result'].get('can_read_all_group_messages') else 'on'})"
        )
        hook = (await api(c, base, "getWebhookInfo"))["result"]
        print(f"webhook configured: {bool(hook.get('url'))}")
        if discover:
            if hook.get("url"):
                print("BLOCKED: getUpdates is unavailable while a webhook is set")
                return 1
            ups = (await api(c, base, "getUpdates", timeout=0)).get("result", [])
            seen = {}
            for u in ups:
                for k in ("message", "channel_post", "my_chat_member", "edited_message"):
                    m = u.get(k)
                    if m and m.get("chat"):
                        seen[m["chat"]["id"]] = (
                            m["chat"].get("type"),
                            m["chat"].get("title"),
                            (m.get("from") or {}).get("id"),
                        )
            if not seen:
                print(
                    "no updates yet: add the bot to the group/channel and send a message in each, then rerun"
                )
            for cid, (typ, title, uid) in seen.items():
                print(f"chat {cid} type={typ} title={title!r} from_user={uid}")
            return 0
        ok = True
        for label, chat in (
            ("control", s.telegram_control_chat_id),
            ("report", s.telegram_report_channel_id),
        ):
            r = await api(c, base, "getChat", chat_id=chat)
            if not r.get("ok"):
                print(f"BLOCKED: {label} chat {chat}: {r.get('description')}")
                ok = False
            else:
                m = await api(c, base, "getChatMember", chat_id=chat, user_id=me["result"]["id"])
                print(
                    f"{label}: {r['result']['type']} {r['result'].get('title')!r}, bot is {m.get('result', {}).get('status')}"
                )
        if not ok:
            print(
                "Fix: add the bot (channel: as admin with 'Post messages'), then `--discover` to get the IDs."
            )
            return 1
    from app.telegram_bot import TelegramGateway

    o = build(s)
    gw = TelegramGateway(s, o, o.attachments, o.actions)
    sent = []
    orig = gw.bot.send_message

    async def spy(**kw):
        msg = await orig(**kw)
        sent.append((kw["chat_id"], msg.message_id))
        return msg

    gw.bot.send_message = spy
    o.notifier = gw
    await o.start()
    t = await o.submit_request(
        TaskRequest(
            objective="telegram smoke", tool="workspace.list_files", tool_args={}, user_id="smoke"
        )
    )
    await asyncio.wait_for(asyncio.shield(o._running[t.task_id]), 60)
    print(
        json.dumps(
            {
                "task": t.task_id,
                "state": t.state,
                "delivery_failures": t.delivery_failures,
                "messages": sent,
            },
            default=str,
        )
    )
    await gw.bot.session.close()
    return 0 if t.state == "succeeded" and sent and not t.delivery_failures else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().discover)))
