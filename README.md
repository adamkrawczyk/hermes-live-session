# hermes-live-session

`hermes live` for [Hermes Agent](https://github.com/NousResearch/hermes-agent): see, steer,
queue into, watch and keep running a session that **another terminal owns**, for example the
SSH login on your other PC. It works without the classic CLI's

    This chat is open in another Hermes window/terminal. Use it there, or start a new chat here.

refusal, and without interrupting the turn that is running.

    hermes live ls [--all] [--full]     what is running, who views it, from which SSH IP,
                                        and any /goal that is PAUSED or STALLED
    hermes live steer <id> "text"       inject into the RUNNING turn after its next tool batch
    hermes live queue <id> "text"       run as the next turn and print the reply
    hermes live attach <id>             interactive live view: Enter steers a running turn,
                                        or runs the next turn when idle (/queue, /steer,
                                        /stop, /detach; /approve and /answer resolve prompts)
    hermes live continue [<id>]         keep a /goal running in the gateway after its terminal
                                        left (default: the session this terminal just left)

`<id>` is a full session id, a unique prefix as printed by `ls` (for example `7d3a9`), or the
exact session title.

## Install

    hermes plugins install adamkrawczyk/hermes-live-session --enable
    hermes live --help

Requires a Hermes build with the unified gateway runtime (main after v0.22.2; the first release
after v0.22.2 carries it). On an older build every command exits 1 with an "update Hermes"
message and does nothing else. Linux and macOS.

## Typical use

You started a long `/goal` over SSH from PC A and are now on PC B:

    ssh my-hermes-host
    hermes live ls                           # find it: 7d3a9  [working]  Ship the release ...
    hermes live steer 7d3a9 "also update the changelog"
    hermes live attach 7d3a9                 # watch it; type to steer; /detach to leave the view

### Leaving a classic-CLI session so its goal keeps running

    /quit                    inside the CLI (typed mid-turn it queues; the CLI exits after the turn)
    hermes live continue     at the shell prompt right after, in the same terminal

Why two steps: while a classic CLI is attached, the CLI itself judges and continues the goal.
After it leaves, only the gateway's post-turn hook continues a goal, and that hook fires only
when a turn ends with no viewer attached. Measured on Hermes main (October 2026):

| How you leave                     | Running step            | Goal afterwards     | Tool processes |
|-----------------------------------|-------------------------|---------------------|----------------|
| `/quit` (runs at a turn boundary) | finishes                | active but stalled  | survive        |
| window closed / SSH drop (SIGHUP) | interrupted (exit 130)  | paused              | survive        |
| Ctrl+C                            | interrupted             | paused              | survive        |

`hermes live continue` fixes every row: it resumes a paused goal with the turn budget kept,
admits one continuation turn, and from the end of that turn the gateway drives the goal to
completion with no terminal attached. Prefer `/quit` to closing the window, because `/quit`
never kills the running step.

## How it works

Every local session runs inside the one Hermes gateway (the unified runtime, upstream
[#106742](https://github.com/NousResearch/hermes-agent/pull/106742)). A terminal is a viewer,
and tool processes are children of the gateway. The classic CLI still holds a per-terminal lease
and refuses a second `hermes --resume`. These commands speak the gateway protocol directly
(`session.active_list`, `session.resume`, `session.steer`, `prompt.submit`,
`session.automation`, and the shipped `GatewayChatView`), which never takes that lease.

| Command    | Effect |
|------------|--------|
| `steer`    | Accepted as `queued` (fenced by `execution_generation`); arrives as an out-of-band user message after the agent's next tool batch. On an idle session: exit 3, nothing sent. |
| `queue`    | Waits behind the running turn, then runs as the next turn. Ctrl+C detaches; the turn still runs. |
| `attach`   | Streams the session live: tool calls, replies, approvals. A line typed while a turn runs is a `steer` (shown as `↪ steered`); typed while idle, or if the turn ends first (`rejected`), it runs as the next turn. `/steer <text>` never queues, `/queue <text>` never steers. |
| `continue` | Exit 3 when there is no active or paused goal. Never resets the turn budget. |
| `ls`       | Read-only. `[WAITING]` means blocked on an approval or a question. |

### What `attach` is, and is not

`attach` is the gateway's own live view (`GatewayChatView`, the screen `hermes chat` uses on the
unified runtime), not a second copy of the classic CLI window. It is the same session, with the
same history, tools and approvals, on a simpler screen with fewer commands (`/help` lists them).
The bottom bar says `⚡ live attach` and whether a turn is running. A full classic-CLI attach needs
the owning terminal to share its runtime, which is upstream
[#113486](https://github.com/NousResearch/hermes-agent/pull/113486).

As shipped, that view sends every typed line as a new turn, which waits behind the running one,
and refuses `/steer`. `attach` therefore routes typed lines the way core's own client does
(`hermes_cli.gateway_agent._correct`): `session.steer` fenced by `execution_generation` first,
then the same line as the next turn when nothing is running.

## What it touches (disclosure)

- **Reads, read-only:** `$HERMES_HOME/state.db` (session titles, message counts and `/goal`
  state; opened with `mode=ro`), the terminal-lease registry through
  `hermes_cli.active_sessions`, and the `$HERMES_HOME/terminal-sessions/` breadcrumbs.
- **Runs locally:** `ps -o tty= -p <pid>` and `who`, to show which tty and SSH IP view a
  session.
- **Connects to:** only the local Hermes gateway, through `hermes_cli.gateway_client`. No other
  network calls, no telemetry, no credentials read or stored.
- **Writes:** `steer`, `queue` and `continue` submit messages to the session you name, and
  `continue` resumes a paused goal. Nothing else is written.
- **Core imports:** `hermes_cli.gateway_client`, `hermes_cli.gateway_chat` and
  `hermes_cli.active_sessions` are not documented plugin APIs yet, so `hermes plugins validate`
  warns about them. Nothing in core is replaced or rebound: `attach` constructs its own subclass
  of the shipped `GatewayChatView` and runs it on the resume path of core's
  `run_gateway_chat` (`session.resume` → print history → `view.run`), entirely inside the
  `hermes live attach` process. The gateway and other terminals are untouched. On a build whose
  viewer lacks the hooks it needs, `attach` says so and uses the stock view.

## Retire when

Upstream ships `hermes steer` / `hermes queue`
([#132798](https://github.com/NousResearch/hermes-agent/pull/132798)), cooperative attach
([#113486](https://github.com/NousResearch/hermes-agent/pull/113486)) and a classic-CLI detach
([#11347](https://github.com/NousResearch/hermes-agent/issues/11347)). Then run
`hermes plugins remove live-session`.

## Development

    python3 tests/test_live_session.py       # hermetic: no gateway, no network, no Hermes needed
    ruff check .
    hermes plugins validate . --install-deps # the catalog CI gate

## License

MIT
