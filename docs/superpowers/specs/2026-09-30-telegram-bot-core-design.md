# Telegram bot core + modules (step 1 of 3)

Date: 2026-09-30. Status: awaiting review.

## Goal

Turn the single-purpose `flight-tracker` bot into a small platform where a new Telegram
feature is one new file plus one registration line. Step 1 only extracts the core and moves
the flight tracker onto it. **No user-visible behavior change**: flight data is in live use.

Later steps, each with its own spec:

- Step 2: `alerts` module (`/alerts` mute toggle, Gotify → Telegram forwarding), retiring the
  `gotify-telegram` bridge. Open question for that spec: what "off" does to alerts that arrive
  while muted.
- Step 3: `status` module (`/status`, cluster health summary).

## Constraints (from the repo)

- `flight-tracker` is the only `getUpdates` consumer of the bot token; a second poller gets `409`.
- Stdlib-only Python, read-only root filesystem, no `pip` at start. The core must stay stdlib-only.
- Code ships through `configMapGenerator` `files:` (flat mount at `/scripts`).
- Do **not** rename the app, Kustomization, Deployment or PVC in this step. A rename changes the
  Flux Kustomization name, and `prune: true` would delete the PVC holding live flight state.
- `gotify-telegram` still owns `telegram-secret`; `flight-tracker` `dependsOn` it. Untouched.
- Deployment strategy is already `Recreate` (one poller at a time, RWO PVC). Keep it.

## Layout

All in `kubernetes/apps/monitoring/flight-tracker/app/`, all listed under `files:` in
`kustomization.yml`:

| File | Role |
|------|------|
| `core.py` | Telegram client, owner check, offset, command/callback registry, receive loop |
| `flights.py` | Today's `tracker.py` logic, moved with `git mv` so history follows. Keeps its own lock and `state.json` |
| `main.py` | Reads env, builds the `Bot`, registers modules, starts them, runs the loop |

`tracker.py` no longer exists. The Deployment command becomes `python -u /scripts/main.py`.
Python puts the script directory on `sys.path`, so `import core` / `import flights` resolve.

## Module interface

A module is an object with:

```
name: str                       # state file: /data/<name>.json, log prefix
help: list[str]                 # lines shown by the combined help
commands: dict[str, fn(args)]   # "track" -> handler; args is the list after the command
callbacks: dict[str, fn(parts)] # inline-button prefix -> handler; parts = data.split(":")[1:]
start() -> None                 # optional; spawn background threads here
```

Modules are constructed with a `Ctx` from the core, so handlers need no bot reference:

```
ctx.send(text, buttons=None)    # to the owner chat
ctx.log(msg)
ctx.state_path                  # /data/<name>.json (module owns the format)
```

Rules the core enforces:

- Registration fails at startup on a duplicate command name or callback prefix.
- Dispatch is a dict lookup on the parsed command. Unknown command → combined help text of
  every module, in registration order.
- A handler that raises is logged as the **exception type only** (existing rule: exception
  text embeds credentials) and the loop continues.
- Anything not from `TELEGRAM_CHAT_ID` is ignored, for messages and callbacks, as today.

Inline-button data keeps today's format `f:NUMBER:DAY`, so buttons in old chat messages still
work. The flights module registers callback prefix `f`.

## Flights module

`flights.py` keeps `Tracker`, the parsers, the schedule and the budget guard unchanged.
Changes are limited to:

- `Tracker.handle_message` / `handle_callback` are removed; a thin `Flights` module object wraps
  a `Tracker` and maps `track`, `untrack`, `list`, `fetch` and callback `f` to its methods.
- `parse_command`, `HELP`, `Telegram`, `dispatch`, `receiver_loop` and `run` move to `core.py`
  or `main.py`. Flight-specific help lines become `Flights.help`.
- `Flights.start()` launches the existing `scheduler_loop` thread.
- `Tracker` no longer owns the Telegram offset.

## State and the offset

Flights keeps `/data/state.json` with the same shape. The core keeps the offset in
`/data/core.json`. On first start, if `core.json` does not exist, the core seeds its offset from
the legacy `state.json["offset"]`. Without this, Telegram redelivers unconfirmed updates and
old `/track` commands could run again. The legacy key is left in place and ignored.

Rollback is safe: Telegram drops updates below the highest confirmed offset, so an older image
reading a stale `state.json` offset gets no replay.

## Rollout

Single in-place change to the existing Deployment: same PVC, same secrets, same token. Flux
rolls it out with `Recreate`. The bridge is not touched, so Gotify notifications keep working.
Expected downtime: the few seconds of the pod swap.

Manual smoke test after Flux reconciles, in the owner chat:

1. `/list`: same tracked flights as before the change.
2. `/fetch`: buttons appear; tapping one refreshes (uses one API unit pair, budget is small).
3. An unknown command such as `/x`: help text lists all flight commands.
4. `kubectl logs` shows `flight-tracker started` and no `getUpdates failed` / `409`.

## Testing

- `tests/test_tracker.py` becomes `tests/test_flights.py`. Parser, diff, schedule, budget and
  state tests stay as they are. The ~25 call sites of `tracker.handle_message(...)` /
  `handle_callback(...)` change mechanically to go through a test helper that dispatches via
  the real core registry, so the wiring is exercised end to end. This is more than an import
  change, but no assertion changes.
- New `tests/test_core.py`:
  - command dispatch, including `@botname` suffix and case
  - callback prefix routing
  - unknown command returns combined help
  - duplicate command / prefix registration raises
  - a raising handler is logged (type only) and the next update still runs
  - update from a foreign chat id is ignored
  - offset seeded from legacy `state.json`, and persisted to `core.json` afterwards
- Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`.
- `.agents/scripts/validate-manifests.sh` on the changed `kustomization.yml` / `deployment.yml`.

## Docs

Update `design/decisions/flight-tracker.md`: file layout, the module interface, and the
"no rename without a state copy" rule. Update the routing row in `.claude/CLAUDE.md` if the
description text no longer fits. No deployed versions are recorded.

## Out of scope

Renaming the app or PVC, the `alerts` and `status` modules, changing the bridge, any change to
polling schedule, budget or message formats.
