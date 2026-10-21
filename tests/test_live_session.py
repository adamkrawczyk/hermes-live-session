"""Hermetic tests for live-session (no gateway, no network, no real registry).

Run: python3 tests/test_live_session.py  (or via ~/.hermes/plugins/run_tests.sh)
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("live_under_test", HERE / "live.py")
assert spec and spec.loader
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)

A = "local-7d3a9c41e0b25f6d8a17c3e9b04f2d6a1c8e5b3f7a9d2c4e6b8f1a3c5e7d9b2f"  # headless goal run
B = "local-7d3b1111111111111111111111111111111111111111111111111111111111"
C = "local-4f81d2a6c3e9b7051f2d8a4c6e0b3f9d7a1c5e8b2d4f6a0c3e5b7d9f1a2c4e6b"  # fresh CLI (temp mint)
E = "local-b52e0f7c9a1d3e5f7b9c1a3e5d7f9b2c4a6e8d0f2b4c6a8e0d2f4b6c8a0e2d4f"  # resumed CLI
NO_TITLES = lambda _db, _t: []  # noqa: E731
DB = Path("/nonexistent/state.db")
TTYS = {13: "pts/1", 14: "pts/2", 2: "?"}

GW = [
    {"id": A, "title": "Ship the release checklist", "status": "working", "message_count": 369},
    {"id": B, "title": "", "status": "waiting", "message_count": 12},
    {"id": "local-ffff", "title": "", "status": "idle", "message_count": 0},
    {"id": "local-eeee", "title": "old chat", "status": "idle", "message_count": 40},
    {"id": C, "title": "Hermes session takeover", "status": "working", "message_count": 204},
]
LEASES = [
    {"session_id": "20260101_090000_c0ffee", "pid": 13, "surface": "cli", "started_at": 1000.0,
     "metadata": {"live_session_id": "20260101_090000_c0ffee"}},
    {"session_id": E, "pid": 14, "surface": "cli", "started_at": 2000.0, "metadata": {"live_session_id": E}},
    {"session_id": "agent:main:local:dm:" + E, "pid": 2, "surface": "gateway:local", "started_at": 1.0},
]
CRUMBS = {"pts/1": (C, 1001.0), "pts/0": (A, 500.0)}


def model():
    return live.build_model(GW, LEASES, CRUMBS, tty_lookup=lambda pid: TTYS[pid])


def expect_error(fn, code):
    try:
        fn()
    except live.LiveError as exc:
        assert exc.code == code, (exc.code, code, str(exc))
        return str(exc)
    raise AssertionError("expected LiveError")


def test_fresh_cli_temp_mint_maps_via_breadcrumb():
    m = model()
    assert [t["pid"] for t in m[C]["terminals"]] == [13]  # not a phantom '20261020_…' record
    assert "20260101_090000_c0ffee" not in m


def test_stale_breadcrumb_is_ignored():
    lease = {"session_id": "20261020_000000_aaaa", "pid": 13, "surface": "cli", "started_at": 5000.0}
    assert live.canonical_id(lease, "pts/1", CRUMBS) == "20261020_000000_aaaa"  # crumb ts 1001 < 5000


def test_headless_goal_is_listed_as_detached():
    m = model()
    assert m[A]["status"] == "working" and m[A]["terminals"] == []
    lines = live.render([m[A]], {}, {}, now=2000.0)
    assert any("no terminal attached" in line for line in lines)


def test_visible_filters_idle_noise_and_orders_by_urgency():
    ids = [r["id"] for r in live.visible(model(), show_all=False)]
    assert ids[0] == B  # waiting (needs approval) first
    assert set(ids) == {A, B, C, E}  # E idle but a terminal is on it; empty idle runtimes hidden
    assert "local-ffff" not in ids and "local-eeee" not in ids
    assert "local-eeee" in [r["id"] for r in live.visible(model(), show_all=True)]
    assert "local-ffff" not in [r["id"] for r in live.visible(model(), show_all=True)]


def test_gateway_lease_is_not_a_viewer():
    assert [t["surface"] for t in model()[E]["terminals"]] == ["cli"]


def test_gateway_unreachable_degrades_to_leases():
    m = live.build_model(None, LEASES, CRUMBS, tty_lookup=lambda pid: TTYS[pid])
    assert set(m) == {C, E} and m[E]["status"] == "?"
    assert {r["id"] for r in live.visible(m, False)} == {C, E}


def test_waiting_is_flagged_loudly():
    lines = live.render([model()[B]], {}, {}, now=0.0)
    assert "[WAITING]" in lines[0] and "needs you" in lines[0]


def test_render_names_ssh_source():
    m = model()
    lines = live.render([m[C]], {C: {"last_activity_at": 990.0, "last_activity_description": "tool x"}},
                        {"pts/1": "203.0.113.52"}, now=1000.0)
    assert any("pts/1 from 203.0.113.52" in line for line in lines)
    assert any("last activity 10s ago: tool x" in line for line in lines)


def test_resolve_prefix_title_ambiguity():
    ids = list(model().keys())
    assert live.resolve_session(A, ids, DB, NO_TITLES) == A
    assert live.resolve_session("4f81d", ids, DB, NO_TITLES) == C
    assert live.resolve_session("local-b52e0", ids, DB, NO_TITLES) == E
    msg = expect_error(lambda: live.resolve_session("7d3", ids, DB, NO_TITLES), live.EXIT_USAGE)
    assert "2 sessions" in msg
    titles = lambda _db, t: [A] if t == "Ship the release checklist" else []  # noqa: E731
    assert live.resolve_session("Ship the release checklist", ids, DB, titles) == A
    expect_error(lambda: live.resolve_session("nope", ids, DB, NO_TITLES), live.EXIT_USAGE)
    expect_error(lambda: live.resolve_session("  ", ids, DB, NO_TITLES), live.EXIT_USAGE)


def test_parse_who():
    who = ("alice    pts/0        2026-10-20 09:38 (203.0.113.7)\n"
           "alice    tty2         2026-10-20 08:00\n"
           "alice    pts/3        2026-10-20 20:17 (203.0.113.52)\n")
    assert live.parse_who(who) == {"pts/0": "203.0.113.7", "pts/3": "203.0.113.52"}


def test_read_breadcrumbs_maps_filename_to_tty():
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d) / "terminal-sessions"
        folder.mkdir()
        (folder / "tty-dev-pts-12").write_text(json.dumps({"session_id": C, "ts": 7.0}))
        (folder / "tty-dev-pts-13").write_text("not json")
        assert live.read_breadcrumbs(Path(d)) == {"pts/12": (C, 7.0)}


class FakeClient:
    def __init__(self, snap, steer_result=None):
        self.snap, self.steer_result, self.calls = snap, steer_result, []

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "session.resume":
            return self.snap
        if method == "session.steer":
            return self.steer_result
        raise AssertionError(method)


def test_steer_fences_to_execution_generation_and_live_id():
    client = FakeClient({"running": True, "execution_generation": 4, "session_id": C},
                        {"status": "queued", "text": "hi", "execution_generation": 4})
    assert asyncio.run(live.steer(client, C, "hi"))["status"] == "queued"
    assert client.calls[1] == ("session.steer", {"session_id": C, "text": "hi", "execution_generation": 4})


def test_steer_idle_session_sends_nothing():
    client = FakeClient({"running": False, "execution_generation": 4, "session_id": C})
    expect_error(lambda: asyncio.run(live.steer(client, C, "hi")), live.EXIT_IDLE)
    assert [m for m, _ in client.calls] == ["session.resume"]


def test_steer_without_generation_refuses():
    client = FakeClient({"running": True, "session_id": C})
    expect_error(lambda: asyncio.run(live.steer(client, C, "hi")), live.EXIT_ERROR)
    assert [m for m, _ in client.calls] == ["session.resume"]


def test_parser_wiring():
    import argparse
    p = argparse.ArgumentParser()
    live.setup_parser(p)
    a = p.parse_args(["steer", "7d3a9", "include", "PINEAPPLE"])
    assert a.live_cmd == "steer" and a.session == "7d3a9" and live._text(a) == "include PINEAPPLE"
    assert p.parse_args(["attach", "b52e0"]).live_cmd == "attach"
    ls = p.parse_args(["ls", "--all", "--full"])
    assert ls.all is True and ls.full is True
    assert p.parse_args(["continue"]).session is None
    assert p.parse_args(["continue", "d9e2b"]).session == "d9e2b"


# ------------------------------------------------------------------ goals: stall / pause / continue

G_ACTIVE = {"status": "active", "turns_used": 1, "max_turns": 40, "goal": "g"}
G_PAUSED = {"status": "paused", "turns_used": 0, "max_turns": 40, "goal": "g",
            "paused_reason": "user-interrupted (Ctrl+C)"}
G_WAITING = {"status": "active", "turns_used": 2, "max_turns": 40, "goal": "g", "waiting_on_pid": 4242}
S1 = "local-c17f3a5e9d2b4f6a8c0e2d4b6f8a0c2e4d6b8f0a2c4e6d8b0f2a4c6e8d0b2f4a"  # /quit at boundary
S2 = "local-d9e2b4f6a8c0e1d3b5f7a9c1e3d5b7f9a1c3e5d7b9f1a3c5e7d9b1f3a5c7e9d1"  # window closed


def goal_model(goals):
    return live.build_model(GW, LEASES, CRUMBS, tty_lookup=lambda pid: TTYS[pid], goals=goals)


def test_quit_at_boundary_goal_is_stalled_and_listed_first():
    m = goal_model({S1: G_ACTIVE})
    assert live.goal_stalled(m[S1])
    assert live.visible(m, False)[0]["id"] == S1
    text = "\n".join(live.render([m[S1]], {}, {}, now=0.0))
    assert "GOAL STALLED" in text and "hermes live continue c17f3a5e9d" in text


def test_goal_with_viewer_running_or_waiting_is_not_stalled():
    assert not live.goal_stalled(goal_model({A: G_ACTIVE})[A])  # A is working
    assert not live.goal_stalled(goal_model({E: G_ACTIVE})[E])  # E has a terminal on it
    m = goal_model({S1: G_WAITING})
    assert not live.goal_stalled(m[S1])
    assert "waiting on pid 4242" in "\n".join(live.render([m[S1]], {}, {}, now=0.0))


def test_window_close_paused_goal_points_at_continue():
    m = goal_model({S2: G_PAUSED})
    text = "\n".join(live.render([m[S2]], {}, {}, now=0.0))
    assert "GOAL PAUSED" in text and "user-interrupted" in text and "hermes live continue d9e2b4f6a8" in text
    assert not live.goal_stalled(m[S2])


def test_active_goals_reads_state_meta_readonly_and_filters():
    import sqlite3
    import time as _t
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
        now = _t.time()
        rows = {f"goal:{S1}": {**G_ACTIVE, "last_turn_at": now - 60},
                f"goal:{S2}": {**G_PAUSED, "last_turn_at": now - 60},
                "goal:local-done": {"status": "done", "last_turn_at": now},
                "goal:local-old": {**G_ACTIVE, "last_turn_at": now - 30 * 86400}}
        conn.executemany("INSERT INTO state_meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in rows.items()])
        conn.execute("INSERT INTO state_meta VALUES ('goal:local-bad', 'not json')")
        conn.commit()
        conn.close()
        assert set(live.active_goals(db, now=now)) == {S1, S2}


def test_default_goal_session_prefers_this_terminal_then_unique():
    m = goal_model({S1: G_ACTIVE, S2: G_PAUSED})
    crumbs = {"pts/9": (S2, 1.0)}
    assert live.default_goal_session(m, crumbs, "pts/9") == S2
    msg = expect_error(lambda: live.default_goal_session(m, crumbs, "pts/77"), live.EXIT_USAGE)
    assert "2 goals" in msg
    only = goal_model({S1: G_ACTIVE})
    assert live.default_goal_session(only, {}, None) == S1
    expect_error(lambda: live.default_goal_session(goal_model({}), {}, None), live.EXIT_USAGE)


class GoalClient:
    def __init__(self, running, state, resume_error=None):
        self.running, self.state, self.resume_error, self.calls = running, state, resume_error, []

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "session.resume":
            return {"running": self.running, "session_id": S2, "execution_generation": 3}
        if method == "session.automation" and params["call"] == "state":
            return {"state": self.state, "value": None}
        if method == "session.automation" and params["call"] == "resume":
            return {"error": {"message": self.resume_error}} if self.resume_error else {"state": {}}
        if method == "prompt.submit":
            return {"admission_id": "adm-1"}
        raise AssertionError(method)


def test_continue_paused_idle_resumes_with_budget_kept_and_admits_one_turn():
    c = GoalClient(False, G_PAUSED)
    res = asyncio.run(live.continue_goal(c, S2))
    methods = [m for m, _ in c.calls]
    assert methods == ["session.resume", "session.automation", "session.automation", "prompt.submit"]
    assert c.calls[2][1]["kwargs"] == {"reset_budget": False}
    assert c.calls[3][1]["text"] == live.CONTINUE_TEXT and c.calls[3][1]["input_id"]
    assert len(res["actions"]) == 2


def test_continue_stalled_active_admits_without_resume():
    c = GoalClient(False, G_ACTIVE)
    asyncio.run(live.continue_goal(c, S1))
    assert [m for m, _ in c.calls] == ["session.resume", "session.automation", "prompt.submit"]


def test_continue_running_turn_admits_nothing():
    c = GoalClient(True, G_ACTIVE)
    res = asyncio.run(live.continue_goal(c, S1))
    assert "prompt.submit" not in [m for m, _ in c.calls]
    assert "when it ends" in res["actions"][0]


def test_continue_refuses_done_or_missing_goal_and_failed_resume():
    for state in ({"status": "done"}, {}, None):
        c = GoalClient(False, state)
        expect_error(lambda c=c: asyncio.run(live.continue_goal(c, S1)), live.EXIT_IDLE)
        assert "prompt.submit" not in [m for m, _ in c.calls]
    c = GoalClient(False, G_PAUSED, resume_error="boom")
    expect_error(lambda: asyncio.run(live.continue_goal(c, S2)), live.EXIT_ERROR)
    assert "prompt.submit" not in [m for m, _ in c.calls]


def test_resolve_falls_back_to_stored_prefix():
    stored = lambda _db, p: [S1] if S1.startswith("local-" + p) else []  # noqa: E731
    assert live.resolve_session("c17f3a", [], DB, NO_TITLES, stored) == S1
    two = lambda _db, _p: [S1, S2]  # noqa: E731
    assert "2 stored sessions" in expect_error(lambda: live.resolve_session("zz999", [], DB, NO_TITLES, two),
                                               live.EXIT_USAGE)


def test_stored_prefix_matches_rejects_short_and_unsafe_prefixes():
    assert live.stored_prefix_matches(DB, "ab") == []
    with tempfile.TemporaryDirectory() as d:
        import sqlite3
        db = Path(d) / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sessions (id TEXT, source TEXT, started_at REAL)")
        conn.executemany("INSERT INTO sessions VALUES (?, ?, ?)",
                         [(S1, "cli", 1.0), (S2, "cli", 2.0), ("local-c17fsub", "subagent", 3.0)])
        conn.commit()
        conn.close()
        assert live.stored_prefix_matches(db, "c17f3") == [S1]
        assert live.stored_prefix_matches(db, "local-d9e2b") == [S2]
        assert live.stored_prefix_matches(db, "c17f%") == []  # LIKE wildcard injection refused
        assert live.stored_prefix_matches(db, "c17fs") == []  # subagent rows excluded


# ------------------------------------------------------------------ old-build guard

def _handle_exit_code(missing: list[str]) -> tuple[int, list[str], str]:
    """Run ``live.handle(ls)`` with ``missing_core`` stubbed; returns (exit code, ran, stderr)."""
    import argparse
    import contextlib
    import io
    ran: list[str] = []
    mod: Any = live  # module attributes are rebound for the test, then restored
    real_missing, real_ls = mod.missing_core, mod.cmd_ls
    mod.missing_core = lambda: missing
    mod.cmd_ls = lambda _args: ran.append("ls") or 0
    err = io.StringIO()
    code = -1
    try:
        with contextlib.redirect_stderr(err):
            try:
                live.handle(argparse.Namespace(live_cmd="ls"))
            except SystemExit as exc:
                code = int(exc.code or 0)
    finally:
        mod.missing_core, mod.cmd_ls = real_missing, real_ls
    return code, ran, err.getvalue()


def test_missing_core_reports_absent_modules_and_attrs():
    assert live.missing_core((("json", "dumps"),)) == []
    assert live.missing_core((("json", "no_such_attr"),)) == ["json.no_such_attr"]
    assert live.missing_core((("no_such_module_xyz", "f"),)) == ["no_such_module_xyz.f"]


def test_handle_on_old_build_exits_1_with_update_hint_and_runs_nothing():
    code, ran, err = _handle_exit_code(["hermes_cli.gateway_client.connect_gateway"])
    assert code == live.EXIT_ERROR and ran == []
    assert "hermes update" in err and "connect_gateway" in err


def test_handle_on_supported_build_dispatches():
    code, ran, _err = _handle_exit_code([])
    assert code == live.EXIT_OK and ran == ["ls"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
