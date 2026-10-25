"""`agw wait`: wakes Claude only for new queued work, waits out a down gateway, newest waiter wins."""

import json
import os

from app import waiter as w


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def feed(monkeypatch, states):
    """Peek answers in order; an OSError entry is a gateway that is down."""
    it = iter(states)

    def urlopen(url, timeout):
        s = next(it)
        if isinstance(s, Exception):
            raise s
        return Resp(s)

    monkeypatch.setattr(w.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)


def test_wakes_once_per_new_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(w, "WAITERS", tmp_path)
    idle, m1 = {"queued": False}, {"queued": True, "next": "chm_1"}
    feed(monkeypatch, [OSError("down"), idle, m1])  # down and idle: no wake-up, no output
    assert w.wait("Coin & Co", "/w/coin", "app", 8000) == 0
    out = capsys.readouterr().out
    assert "chm_1" in out and "session_id 'pull_coinco_claudecode_" in out and "'/w/coin'" in out
    assert "run_in_background" in out and "wait --project 'Coin & Co' --cwd /w/coin" in out

    # restarted while chm_1 is still queued (e.g. busy): no new wake-up until chm_2
    feed(monkeypatch, [m1, m1, {"queued": True, "next": "chm_2"}])
    assert w.wait("Coin & Co", "/w/coin", "app", 8000) == 0
    assert "chm_2" in capsys.readouterr().out


def test_closed_session_and_newer_waiter_stop_quietly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(w, "WAITERS", tmp_path)
    feed(monkeypatch, [{"closing": True}])
    assert w.wait("P", "/w/p", "", 8000) == 0
    assert "do not restart" in capsys.readouterr().out

    def replaced(url, timeout):  # another waiter took over this session meanwhile
        (path,) = tmp_path.glob("*.json")
        path.write_text(json.dumps({"pid": os.getpid() + 1}))
        return Resp({"queued": False})

    monkeypatch.setattr(w.urllib.request, "urlopen", replaced)
    assert w.wait("P", "/w/p", "", 8000) == 0
    assert "newer waiter" in capsys.readouterr().out
