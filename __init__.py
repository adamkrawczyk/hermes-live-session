"""live-session plugin: `hermes live {ls,steer,queue,attach,continue}`. See live.py for the why."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _live():
    # Hyphenated plugin dir is not an importable package name: load the sibling by path.
    name = "hermes_live_session_live"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("live.py"))
        if spec is None or spec.loader is None:
            raise ImportError("live-session: cannot load live.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _setup(parser):
    _live().setup_parser(parser)


def _handle(args):
    return _live().handle(args)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="live",
        help="See, attach to, steer, queue into or keep running a session another terminal owns",
        setup_fn=_setup,
        handler_fn=_handle,
        description=(
            "Drive a running session from any terminal (e.g. a second SSH login) without the\n"
            "'open in another Hermes window/terminal' refusal and without interrupting it.\n\n"
            "  hermes live ls                      what runs, who views it, from which tty / SSH IP\n"
            "  hermes live steer <id> \"text\"       inject after the next tool batch\n"
            "  hermes live queue <id> \"text\"       run as the next turn, print the reply\n"
            "  hermes live attach <id>             live view; /detach leaves the view\n"
            "  hermes live continue [<id>]         keep a /goal running after its terminal quit\n\n"
            "<id> = full session id, a unique prefix (e.g. 7d3a9), or the exact title."
        ),
    )
