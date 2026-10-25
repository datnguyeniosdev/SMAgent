"""Results reach the agent that asked (an "answer" message that wakes it) instead of being polled:
delegated tasks, art review decisions, review_project runs. Art waits for its review."""

from app.connectors.fal import FalImageProvider
from app.intake import TaskRequest
from tests.test_fal import action, fal_api

P = "2048 Shooter (K05)"


def answers(orch, sid):
    s, msgs, _ = orch.channel.pull(P, sid, "/w/k05", 20, "claude_code")
    return [m for m in msgs if m.get("kind") == "answer"]


async def test_delegated_task_end_is_pushed_to_the_caller(settings, cfg_dir):
    from tests.conftest import wait_done
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    sid = orch.channel.pull(P, None, "/w/k05", 0, "claude_code")[0].session_id
    task = await orch.submit_request(TaskRequest(
        objective="summarise the repo", source="mcp", data={"project": P, "session_id": sid}))  # fmt: skip
    await wait_done(orch, task)
    (got,) = answers(orch, sid)
    assert task.task_id in got["text"] and task.state.value in got["text"]
    other = await orch.submit_request(TaskRequest(objective="no caller", data={"project": P}))
    await wait_done(orch, other)
    assert answers(orch, sid) == []  # no session given: nothing pushed


async def test_art_decisions_are_pushed_not_its_generation(settings, cfg_dir):
    from app.gateway_config import load_config
    from tests.conftest import build_orch, wait_done

    client, _ = fal_api()
    orch, _ = build_orch(settings, [], image=FalImageProvider("k", client=client, poll_s=0),
                         gateway_cfg=load_config(cfg_dir))  # fmt: skip
    sid = orch.channel.pull(P, None, "/w/k05", 0, "claude_code")[0].session_id
    data = {"prompt": "gold coin\n\nPurpose: icon", "width": 64, "height": 32, "transparent": False,
            "project": P, "session_id": sid}  # fmt: skip
    first = await orch.submit_request(
        TaskRequest(objective="coin", task_type="design_image", data=data, source="mcp")
    )
    await wait_done(orch, first)
    assert answers(orch, sid) == []  # generated: waits for the operator's review, no message yet
    report = orch.collab.reports_for(first.task_id)[-1]
    fb = await orch.collab.submit_feedback(text="thicker outline", user_id="11", source="telegram",
                                           report_id=report.report_id)  # fmt: skip
    second = orch.tasks[fb.follow_up_task_id]
    (changes,) = answers(orch, sid)
    assert "asked for changes" in changes["text"] and second.task_id in changes["text"]
    await wait_done(orch, second)
    await orch.apply_action(action(orch, "art_accept", second.task_id), "11")
    (ok,) = answers(orch, sid)  # the revision, started by the operator, still reaches the agent
    assert "accepted" in ok["text"] and ".png" in ok["text"]
