"""`hermes live` — see, attach to, steer and queue into a session another terminal owns.

Every local session already runs inside the one gateway (unified runtime, upstream #106742);
the terminal that started it is a viewer, and tool processes are children of the gateway.
The classic CLI still refuses a second `hermes --resume` ("This chat is open in another Hermes
window/terminal") because of its per-terminal lease. These commands talk to the gateway
directly, the way the shipped gateway client does, so they never take or need that lease and
never interrupt the running turn:

    ls        what is running, who is viewing it (terminal leases), from which SSH IP, and any
              goal that is paused or stalled
    steer     session.steer, delivered after the agent's next tool batch, no interrupt
    queue     prompt.submit, waits behind the running turn, prints the reply
    attach    the gateway's interactive viewer (GatewayChatView): /detach, /stop, /approve
    continue  hand a goal to the gateway after its terminal left (resume + one admitted turn)

Superseded when upstream ships `hermes steer` / `hermes queue` (PR #132798), cooperative
attach (PR #113486) and a classic-CLI detach (issue #11347).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterable

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_IDLE = 3  # steer needs a running turn; nothing was sent

# Gateway LiveSessionStatus values, most urgent first. `waiting` = blocked on an approval/clarify.
STATUS_ORDER = {"waiting": 0, "working": 1, "streaming": 1, "starting": 2, "resuming": 3, "idle": 4}
BUSY = {"waiting", "working", "streaming", "starting", "resuming"}


class LiveError(Exception):
    """A user-facing failure; the message is printed as-is."""

    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- data sources

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def registry_entries() -> list[dict[str, Any]]:
    """Terminal leases from the official registry API (it prunes dead pids itself)."""
    from hermes_cli.active_sessions import active_session_registry_snapshot
    return active_session_registry_snapshot()


def gateway_sessions() -> list[dict[str, Any]] | None:
    """The gateway's own live-session list (read-only RPC); None when it cannot be reached."""
    try:
        res = _run(lambda client: client.rpc("session.active_list"))
    except Exception:  # noqa: BLE001 - ls degrades to leases-only, never fails
        return None
    return list((res or {}).get("sessions") or [])


def _ro(db_path: Path) -> sqlite3.Connection:
    # Read-only URI: a read-write open of state.db can lock a running session out.
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)


def session_rows(db_path: Path, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids or not db_path.exists():
        return {}
    conn = _ro(db_path)
    try:
        marks = ",".join("?" * len(ids))
        cur = conn.execute(
            "SELECT id, title, message_count, last_activity_at, last_activity_description, source "
            f"FROM sessions WHERE id IN ({marks})", ids)
        cols = [c[0] for c in cur.description]
        return {row[0]: dict(zip(cols, row, strict=False)) for row in cur.fetchall()}
    finally:
        conn.close()


def title_matches(db_path: Path, title: str) -> list[str]:
    if not db_path.exists():
        return []
    conn = _ro(db_path)
    try:
        cur = conn.execute("SELECT id FROM sessions WHERE title = ? ORDER BY started_at DESC", (title,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def stored_prefix_matches(db_path: Path, prefix: str) -> list[str]:
    """Stored (not necessarily live) sessions whose id starts with ``prefix`` (``local-`` optional).

    A goal whose terminal quit at a turn boundary is in no live list and holds no lease, yet it is
    exactly the session a user wants to nudge, so prefixes must resolve against state.db too."""
    if not db_path.exists() or len(prefix) < 5:
        return []
    bare = prefix[len("local-"):] if prefix.startswith("local-") else prefix
    if not all(c.isalnum() or c in "_-" for c in bare):
        return []
    conn = _ro(db_path)
    try:
        cur = conn.execute(
            "SELECT id FROM sessions WHERE (id LIKE ? OR id LIKE ?) AND source != 'subagent' "
            "ORDER BY started_at DESC LIMIT 6", (bare + "%", "local-" + bare + "%"))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def active_goals(db_path: Path, max_age_s: float = 2 * 86400, now: float | None = None) -> dict[str, dict]:
    """``{session_id: goal_state}`` for recent goals still ``active`` or ``paused`` (``goal:<sid>``)."""
    if not db_path.exists():
        return {}
    now = time.time() if now is None else now
    conn = _ro(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM state_meta WHERE key LIKE 'goal:%'").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for key, value in rows:
        try:
            state = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("status") not in ("active", "paused"):
            continue
        last = float(state.get("last_turn_at") or state.get("created_at") or 0)
        if now - last <= max_age_s:
            out[key[len("goal:"):]] = state
    return out


def goal_waiting(state: dict) -> str:
    """Non-empty when the goal legitimately sleeps on something that will wake the gateway."""
    if state.get("waiting_on_pid"):
        return f"waiting on pid {state['waiting_on_pid']}"
    if state.get("waiting_on_session"):
        return f"waiting on session {_short(str(state['waiting_on_session']))}"
    if int(state.get("waiting_on_delegations") or 0):
        return f"waiting on {state['waiting_on_delegations']} delegation(s)"
    if float(state.get("waiting_until") or 0) > time.time():
        return "waiting on a timer"
    return ""


def read_breadcrumbs(home: Path) -> dict[str, tuple[str, float]]:
    """``{'pts/1': (stored_session_id, ts)}`` from the CLI's per-terminal breadcrumbs."""
    out: dict[str, tuple[str, float]] = {}
    folder = home / "terminal-sessions"
    if not folder.is_dir():
        return out
    for f in folder.glob("tty-dev-*"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tty = f.name[len("tty-dev-"):].replace("-", "/", 1)  # tty-dev-pts-1 -> pts/1
        sid = str(data.get("session_id") or "")
        if sid:
            out[tty] = (sid, float(data.get("ts") or 0))
    return out


def tty_of(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "?"
    return out or "?"


def ssh_sources() -> dict[str, str]:
    try:
        out = subprocess.run(["who"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_who(out)


def parse_who(text: str) -> dict[str, str]:
    """tty -> remote host for logins that have one, e.g. {'pts/0': '203.0.113.7'}."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "(" in line and line.rstrip().endswith(")"):
            result[parts[1]] = line[line.rindex("(") + 1:-1]
    return result


# ------------------------------------------------------------------------------- model

def canonical_id(entry: dict[str, Any], tty: str, crumbs: dict[str, tuple[str, float]]) -> str:
    """The stored id the gateway knows for a terminal lease.

    A resumed CLI leases the stored ``local-`` id. A FRESH CLI leases its temporary mint and never
    re-keys (upstream #117946); its terminal breadcrumb records the stored id it adopted, so use
    that when the breadcrumb was written after the lease started (older = a previous chat)."""
    sid = str(entry.get("session_id") or "")
    live = str((entry.get("metadata") or {}).get("live_session_id") or "")
    for cand in (sid, live):
        if cand.startswith("local-"):
            return cand
    crumb = crumbs.get(tty)
    if crumb and crumb[1] >= float(entry.get("started_at") or 0) - 5:
        return crumb[0]
    return sid or live


def _is_terminal(entry: dict[str, Any]) -> bool:
    return not str(entry.get("surface") or "").startswith("gateway")


def build_model(gw: list[dict[str, Any]] | None, entries: list[dict[str, Any]],
                crumbs: dict[str, tuple[str, float]],
                tty_lookup: Callable[[int], str] = tty_of,
                goals: dict[str, dict] | None = None) -> dict[str, dict[str, Any]]:
    """One record per session: gateway status/title, attached terminals, and any active goal."""
    model: dict[str, dict[str, Any]] = {}

    def blank(sid: str) -> dict[str, Any]:
        return {"id": sid, "status": "?" if gw is None else "idle", "title": "", "messages": 0,
                "terminals": [], "goal": None}

    for s in gw or []:
        sid = str(s.get("id") or "")
        if sid:
            model[sid] = {"id": sid, "status": str(s.get("status") or "?"),
                          "title": str(s.get("title") or ""), "messages": int(s.get("message_count") or 0),
                          "terminals": [], "goal": None}
    for e in entries:
        if not _is_terminal(e):
            continue
        pid = int(e.get("pid") or 0)
        tty = tty_lookup(pid) if pid else "?"
        sid = canonical_id(e, tty, crumbs)
        if not sid:
            continue
        rec = model.setdefault(sid, blank(sid))
        rec["terminals"].append({"surface": str(e.get("surface") or "?"), "pid": pid, "tty": tty,
                                 "started_at": float(e.get("started_at") or 0)})
    for sid, state in (goals or {}).items():
        model.setdefault(sid, blank(sid))["goal"] = state
    return model


def goal_stalled(rec: dict[str, Any]) -> bool:
    """Active goal, nothing running, no viewer, not waiting on anything: no one will run its next
    turn. Happens when a classic CLI quits at a turn boundary (the CLI drives goal continuation
    while attached; the gateway's post-turn hook only fires when a turn ends with no viewer)."""
    goal = rec.get("goal")
    return bool(goal and goal.get("status") == "active" and rec["status"] == "idle"
                and not rec["terminals"] and not goal_waiting(goal))


def visible(model: dict[str, dict[str, Any]], show_all: bool) -> list[dict[str, Any]]:
    """Busy sessions, sessions a terminal is on, and sessions with an active goal; ``--all`` adds
    idle ones with history."""
    keep = [r for r in model.values()
            if r["status"] in BUSY or r["terminals"] or r["status"] == "?" or r.get("goal")
            or (show_all and r["messages"] > 0)]

    def order(r):
        rank = 0 if goal_stalled(r) else STATUS_ORDER.get(r["status"], 5) + 1
        return (rank, r["id"])
    return sorted(keep, key=order)


def resolve_session(ref: str, candidates: Iterable[str], db_path: Path,
                    title_lookup: Callable[[Path, str], list[str]] = title_matches,
                    prefix_lookup: Callable[[Path, str], list[str]] = stored_prefix_matches) -> str:
    """Exact id, unique prefix (``local-`` optional) of a live or stored session, or exact title."""
    ref = (ref or "").strip()
    if not ref:
        raise LiveError("Give a session id, a unique prefix of one, or its exact title. "
                        "See `hermes live ls`.", EXIT_USAGE)
    ids = list(dict.fromkeys(c for c in candidates if c))
    if ref in ids or (ref.startswith("local-") and len(ref) > 40):
        return ref
    bare = ref[len("local-"):] if ref.startswith("local-") else ref
    hits = [i for i in ids if i.startswith(ref) or i.startswith("local-" + bare)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise LiveError(f"'{ref}' matches {len(hits)} sessions: "
                        + ", ".join(_short(h) for h in hits[:6]) + ". Use a longer prefix.", EXIT_USAGE)
    by_title = title_lookup(db_path, ref)
    if by_title:
        return by_title[0]
    stored = prefix_lookup(db_path, ref)
    if len(stored) == 1:
        return stored[0]
    if len(stored) > 1:
        raise LiveError(f"'{ref}' matches {len(stored)} stored sessions: "
                        + ", ".join(_short(h) for h in stored) + ". Use a longer prefix.", EXIT_USAGE)
    if len(ref) >= 20:
        return ref  # an exact id we could not see; the gateway decides
    raise LiveError(f"No session matches '{ref}'. See `hermes live ls` or `hermes sessions list`.",
                    EXIT_USAGE)


def _short(sid: str) -> str:
    return sid[len("local-"):][:10] if sid.startswith("local-") else sid[:24]


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def render(records: list[dict[str, Any]], rows: dict[str, dict[str, Any]], who: dict[str, str],
           now: float, full: bool = False) -> list[str]:
    lines: list[str] = []
    for r in records:
        row = rows.get(r["id"], {})
        title = (r["title"] or row.get("title") or "(untitled)")[:48]
        status = r["status"].upper() if r["status"] == "waiting" else r["status"]
        flag = "  <- needs you (approval/question)" if r["status"] == "waiting" else ""
        lines.append(f"{_short(r['id'])}  [{status}]  {title}{flag}")
        goal = r.get("goal")
        if goal:
            used, cap = goal.get("turns_used", "?"), goal.get("max_turns", "?")
            if goal.get("status") == "paused":
                lines.append(f"    GOAL PAUSED ({used}/{cap} turns, {goal.get('paused_reason') or 'paused'}). "
                             f"Keep it running headless: hermes live continue {_short(r['id'])}")
            elif goal_stalled(r):
                lines.append(f"    GOAL STALLED ({used}/{cap} turns): active, but nothing will run its "
                             f"next turn. Keep it running headless: hermes live continue {_short(r['id'])}")
            else:
                why = goal_waiting(goal)
                lines.append(f"    goal active ({used}/{cap} turns)" + (f", {why}" if why else ""))
        if r["terminals"]:
            for t in r["terminals"]:
                where = f"{t['tty']} from {who[t['tty']]}" if t["tty"] in who else t["tty"]
                lines.append(f"    viewed by {t['surface']} pid {t['pid']} on {where}, "
                             f"opened {_age(now - t['started_at'])} ago")
        elif r["status"] in BUSY:
            lines.append("    no terminal attached (detached; still runs in the gateway)")
        else:
            lines.append("    no terminal attached")
        last = row.get("last_activity_at")
        if last:
            doing = str(row.get("last_activity_description") or "")[:60]
            lines.append(f"    last activity {_age(now - float(last))} ago" + (f": {doing}" if doing else ""))
        if full:
            lines.append(f"    id: {r['id']}")
    return lines


# ------------------------------------------------------------------- gateway actions

def _run(coro_fn):
    from hermes_cli.gateway_client import connect_gateway

    async def main():
        async with connect_gateway() as client:
            return await coro_fn(client)
    return asyncio.run(main())


async def steer(client, session_id: str, text: str) -> dict[str, Any]:
    """Attach as a viewer, then steer the RUNNING turn, fenced to its execution generation."""
    snap = await client.rpc("session.resume", session_id=session_id)
    if not isinstance(snap, dict):
        raise LiveError("Gateway returned no session snapshot.")
    if not snap.get("running"):
        raise LiveError("That session is idle: nothing to steer, nothing was sent. "
                        "Use `hermes live queue` to start a new turn.", EXIT_IDLE)
    generation = snap.get("execution_generation")
    if type(generation) is not int:
        raise LiveError("Gateway snapshot has no execution generation; refusing to steer blind.")
    live_id = snap.get("session_id") or session_id
    return await client.rpc("session.steer", session_id=live_id, text=text,
                            execution_generation=generation)


CONTINUE_TEXT = ("[Resumed from another terminal] Continue working toward the standing goal "
                 "from where it stopped.")


async def continue_goal(client, session_id: str) -> dict[str, Any]:
    """Hand a session's goal to the gateway so it keeps running with NO terminal attached.

    The classic CLI drives goal continuation while it is attached; once it leaves, only the
    gateway's post-turn hook continues a goal, and that hook fires only when a turn ENDS with no
    viewer. So: resume the goal if a terminal close paused it (budget kept), and if nothing is
    running admit one turn; its end hands the goal to the gateway. Closing this connection clears
    our automation registration (``viewer_driven`` keys off live subscriptions)."""
    snap = await client.rpc("session.resume", session_id=session_id)
    if not isinstance(snap, dict):
        raise LiveError("Gateway returned no session snapshot.")
    live_id = snap.get("session_id") or session_id
    got = await client.rpc("session.automation", session_id=live_id, kind="goal", call="state")
    state = ((got or {}).get("state") or {}) if isinstance(got, dict) else {}
    if isinstance(state, str):
        state = json.loads(state)
    status = state.get("status")
    if status not in ("active", "paused"):
        raise LiveError(f"No goal to continue (goal status: {status or 'none'}). Use `hermes live queue` "
                        "to send a normal message.", EXIT_IDLE)
    actions = []
    if status == "paused":
        res = await client.rpc("session.automation", session_id=live_id, kind="goal", call="resume",
                               kwargs={"reset_budget": False})
        if isinstance(res, dict) and res.get("error"):
            raise LiveError(f"Goal resume failed: {res['error'].get('message')}")
        actions.append(f"resumed paused goal ({state.get('paused_reason') or 'paused'}), turn budget kept")
    if snap.get("running"):
        actions.append("a turn is running; the gateway continues the goal when it ends")
    else:
        receipt = await client.rpc("prompt.submit", session_id=live_id, input_id=uuid.uuid4().hex,
                                   text=CONTINUE_TEXT)
        if not (isinstance(receipt, dict) and receipt.get("admission_id")):
            raise LiveError(f"Gateway did not admit the continuation turn: {receipt}")
        actions.append("admitted one continuation turn; the gateway drives the goal from its end")
    return {"session_id": live_id, "goal": state.get("goal", ""), "turns_used": state.get("turns_used"),
            "max_turns": state.get("max_turns"), "actions": actions}


def _gateway_launch(options: dict[str, Any]) -> int:
    from hermes_cli.gateway_chat import launch_from_kwargs
    return launch_from_kwargs(options)


# ------------------------------------------------------------------------------ commands

def _discover(gw_needed: bool = True):
    home = _hermes_home()
    gw = gateway_sessions() if gw_needed else None
    model = build_model(gw, registry_entries(), read_breadcrumbs(home),
                        goals=active_goals(home / "state.db"))
    return home, gw, model


def _resolve(ref: str) -> str:
    home, _gw, model = _discover()
    return resolve_session(ref, model.keys(), home / "state.db")


def _text(args) -> str:
    text = " ".join(args.text).strip() if isinstance(args.text, list) else str(args.text or "").strip()
    if text == "-":
        text = sys.stdin.read().strip()
    if not text:
        raise LiveError("Nothing to send.", EXIT_USAGE)
    return text


def cmd_ls(args) -> int:
    home, gw, model = _discover()
    records = visible(model, getattr(args, "all", False))
    if gw is None:
        print("(gateway unreachable: showing terminal leases only, status unknown)", file=sys.stderr)
    if not records:
        print("Nothing running and no terminal attached to any session."
              + ("" if getattr(args, "all", False) else " (`--all` lists idle sessions too)"))
        return EXIT_OK
    rows = session_rows(home / "state.db", [r["id"] for r in records])
    for line in render(records, rows, ssh_sources(), time.time(), getattr(args, "full", False)):
        print(line)
    me = os.environ.get("SSH_CONNECTION", "").split()[:1]
    if me:
        print(f"\n(you are connected from {me[0]})")
    print("\nhermes live steer <id> \"text\"   inject now, no interrupt"
          "\nhermes live queue <id> \"text\"   next turn, prints the reply"
          "\nhermes live attach <id>          live view here (/detach to leave)")
    return EXIT_OK


def cmd_steer(args) -> int:
    sid = _resolve(args.session)
    text = _text(args)
    res = _run(lambda client: steer(client, sid, text))
    status = (res or {}).get("status")
    if status == "queued":
        print(f"Steer accepted for {_short(sid)}: delivered after the agent's next tool batch.")
        return EXIT_OK
    if status == "rejected":
        print("Steer rejected by the agent (its turn was finishing). Nothing queued; retry, or use "
              "`hermes live queue`.", file=sys.stderr)
        return EXIT_ERROR
    print(f"Steer result: {res}")
    return EXIT_OK


def cmd_queue(args) -> int:
    sid = _resolve(args.session)
    text = _text(args)
    print(f"Queued into {_short(sid)}; waiting for the reply (Ctrl+C detaches, the turn still runs).",
          file=sys.stderr)
    return _gateway_launch({"resume": sid, "query": text, "quiet": True})


def cmd_attach(args) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LiveError("attach needs an interactive terminal.", EXIT_USAGE)
    sid = _resolve(args.session)
    print(f"Attaching to {_short(sid)}. Typing queues a turn; /detach (or Ctrl+C) leaves it running; "
          "/stop interrupts it.", file=sys.stderr)
    return _gateway_launch({"resume": sid})


def default_goal_session(model: dict[str, dict[str, Any]], crumbs: dict[str, tuple[str, float]],
                         my_tty: str | None) -> str:
    """The session `hermes live continue` means when given no id: the one THIS terminal just left
    (its breadcrumb) if that has a resumable goal, else the only paused/stalled goal on the box."""
    def resumable(rec):
        goal = rec.get("goal") or {}
        return goal.get("status") == "paused" or goal_stalled(rec)
    if my_tty and my_tty in crumbs:
        rec = model.get(crumbs[my_tty][0])
        if rec and resumable(rec):
            return rec["id"]
    cands = [r["id"] for r in model.values() if resumable(r)]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise LiveError("No paused or stalled goal found. `hermes live ls` shows every goal.", EXIT_USAGE)
    raise LiveError(f"{len(cands)} goals could be continued: " + ", ".join(_short(c) for c in cands)
                    + ". Name one: hermes live continue <id>", EXIT_USAGE)


def _my_tty() -> str | None:
    for fd in (0, 1):
        try:
            name = os.ttyname(fd)
        except OSError:
            continue
        if name.startswith("/dev/"):
            return name[len("/dev/"):]
    return None


def cmd_continue(args) -> int:
    if args.session:
        sid = _resolve(args.session)
    else:
        home, _gw, model = _discover()
        sid = default_goal_session(model, read_breadcrumbs(home), _my_tty())
    res = _run(lambda client: continue_goal(client, sid))
    print(f"Goal on {_short(res['session_id'])} now runs in the gateway, no terminal needed "
          f"({res['turns_used']}/{res['max_turns']} turns used):")
    for action in res["actions"]:
        print(f"  - {action}")
    print(f"Watch: hermes live ls  |  view: hermes live attach {_short(res['session_id'])}")
    return EXIT_OK


# ----------------------------------------------------------------------------- argparse

def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="live_cmd", metavar="{ls,steer,queue,attach,continue}")
    p = sub.add_parser("ls", help="What is running, who is viewing it, from which SSH IP (read-only)")
    p.add_argument("--all", action="store_true", help="also list idle sessions with history")
    p.add_argument("--full", action="store_true", help="print full session ids")
    for name, helptext in (("steer", "Inject text into the RUNNING turn without interrupting it"),
                           ("queue", "Submit a message as the next turn and print the reply")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("session", help="session id, unique prefix, or exact title")
        p.add_argument("text", nargs="+", help="message text ('-' reads stdin)")
    p = sub.add_parser("attach", help="Interactive live view of a session another terminal owns")
    p.add_argument("session", help="session id, unique prefix, or exact title")
    p = sub.add_parser("continue", help="Keep a session's goal running in the gateway after its "
                                        "terminal left (un-pauses / un-stalls it)")
    p.add_argument("session", nargs="?", default=None,
                   help="session id/prefix/title; default: the session this terminal just left")


UPDATE_HINT = ("this Hermes build predates the unified gateway runtime that `hermes live` drives "
               "(missing: {missing}). Update Hermes (`hermes update`); the first release that "
               "carries it is the one after v0.22.2.")

# Core entry points this plugin uses; all are lazy so `hermes --help` never pays for them.
REQUIRED_CORE = (
    ("hermes_cli.gateway_client", "connect_gateway"),
    ("hermes_cli.gateway_chat", "launch_from_kwargs"),
    ("hermes_cli.active_sessions", "active_session_registry_snapshot"),
)


def missing_core(required=REQUIRED_CORE) -> list[str]:
    """``module.attr`` names this Hermes build lacks (empty = supported build)."""
    import importlib
    missing = []
    for module, attr in required:
        try:
            ok = hasattr(importlib.import_module(module), attr)
        except ImportError:
            ok = False
        if not ok:
            missing.append(f"{module}.{attr}")
    return missing


def handle(args) -> int:
    commands = {"ls": cmd_ls, "steer": cmd_steer, "queue": cmd_queue, "attach": cmd_attach,
                "continue": cmd_continue}
    fn = commands.get(getattr(args, "live_cmd", None) or "ls", cmd_ls)
    try:
        gaps = missing_core()
        if gaps:
            raise LiveError(UPDATE_HINT.format(missing=", ".join(gaps)), EXIT_ERROR)
        code = fn(args)
    except LiveError as exc:
        print(f"hermes live: {exc}", file=sys.stderr)
        code = exc.code
    except KeyboardInterrupt:
        print("Detached; accepted work continues at the gateway.", file=sys.stderr)
        code = 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary; gateway RPC errors carry a machine reason as text
        print(f"hermes live: gateway refused: {exc}", file=sys.stderr)
        code = EXIT_ERROR
    sys.exit(code)
