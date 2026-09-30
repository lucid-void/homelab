# Telegram bot: alerts module (step 2 of 3)

Date: 2026-09-30. Status: awaiting review. Builds on
`2026-09-30-telegram-bot-core-design.md` (step 1, live).

## Goal

Move Gotify → Telegram forwarding into the bot as an `alerts` module and add a mute toggle:
`/alerts`, `/alerts on`, `/alerts off [1h|4h|24h]`. Retire the separate `gotify-telegram`
bridge Deployment.

## Decisions (from the brainstorm)

- Muted alerts are **dropped** from Telegram (still visible in the Gotify web UI). No digest,
  no priority exception. Revisit later if a mute ever hides something that mattered.
- Gotify intake is **polling**, not WebSocket: stdlib only, no `pip install` at boot.
- Flight messages are never muted; only Gotify forwarding is.

## Constraints

- Bot core and modules stay stdlib-only, read-only rootfs.
- `flight-tracker` stays the only `getUpdates` consumer; do not rename anything (PVC safety).
- `telegram-secret` is still owned by the `gotify-telegram` Kustomization, and `flight-tracker`
  `dependsOn` it. Step 2 keeps that Kustomization and removes only its Deployment and script.
- `gotify-client-secret` (key `CLIENT_TOKEN`, provisioned by `gotify-bootstrap` in
  `monitoring`) is loaded by the bot with an added `envFrom`. Because `gotify-telegram`
  depends on `gotify-bootstrap` and `flight-tracker` on `gotify-telegram`, the secret exists
  before the pod starts. No new sealed secret.
- Never log the client token or exception text (type only).

## Module

`alerts.py`, one class `Alerts`, registered in `main.py` with one line, files list gains
`alerts.py`. State in `/data/alerts.json`.

Commands:

| Command | Effect |
|---------|--------|
| `/alerts` | Reply `Alerts: ON` or `Alerts: OFF until 14:02 UTC` or `Alerts: OFF (until you turn them on)` |
| `/alerts on` | Unmute, reply `Alerts: ON` |
| `/alerts off` | Mute indefinitely |
| `/alerts off 1h` / `4h` / `24h` | Mute until now + duration |
| `/alerts <anything else>` | Usage line |

Duration grammar is exactly `<int>h` or `<int>m` with a positive int, capped at 7 days;
`1h`, `4h`, `24h` are examples, not a whitelist. Anything else gets the usage line.

State shape: `{"last_id": int, "muted": false, "until": null | ISO-8601 UTC}`.
Mute is active when `muted` is true and (`until` is null or now < `until`). Expiry is
evaluated lazily on each poll and each `/alerts`, so no timer is needed and a restart during a
timed mute keeps it.

Fail-open rule: a missing, unreadable or wrong-shape `alerts.json` means alerts are ON and
`last_id` is seeded from the server (below). It must never mean muted.

## Forwarder

A daemon thread started by `Alerts.start()` loops every 10 seconds:

1. `GET {GOTIFY_HOST}/message?limit=100` with header `X-Gotify-Key: <CLIENT_TOKEN>`, timeout 10 s.
   `GOTIFY_HOST` defaults to `http://gotify.monitoring.svc.cluster.local`.
2. Response `messages` are newest first. Take those with `id > last_id`, oldest first.
3. If not muted, `ctx.send` each as `<emoji> <title>\n<message>` (emoji by priority: ≥ 8 red,
   ≥ 5 yellow, else green, same as the bridge). If muted, drop them.
4. Set `last_id` to the highest id seen and save, in both cases, so a muted message is never
   replayed on unmute.

Edge cases, each with a test:

- **First start, no state:** seed `last_id` with the current highest id and forward nothing, so
  the existing Gotify history is not replayed into Telegram. Empty server: `last_id = 0`.
- **Gotify DB reset** (highest id below `last_id`): reset `last_id` to 0, log once, and
  forward what the server now holds. Everything on the server is post-reset, and skipping it
  would swallow the priority-8 `DRIFT` message that `gotify-bootstrap` posts right after a
  reset. Without the rewind every alert after a reset would be skipped forever.
- **More than 100 new messages in one interval:** only the newest 100 are seen; documented,
  not handled (a 10 s window with 100 alerts is an incident, and the Gotify UI has them).
- **Poll failure** (network, HTTP error, bad JSON): log the exception type, keep `last_id`,
  retry next tick; one log line per streak, not one per tick.
- **Send failure** is already swallowed by `Telegram.send`; the id still advances (same as the
  bridge, which also did not retry).
- **A message missing `title`, `message` or `priority`:** use empty strings and priority 5.

The forwarder and the `/alerts` handler run on different threads, so they share one lock around
reading and writing the state file.

## Cutover

One PR:

1. Add `alerts.py`, wire it in `main.py`, add to `files:`.
2. `deployment.yml`: add `envFrom` `gotify-client-secret`. Nothing else.
3. Delete `gotify-telegram/app/deployment.yml` and `script-configmap.yml`; the app's
   `kustomization.yml` keeps only `telegram-sealed.yml`. The Kustomization stays.
4. Overlap and gaps during the swap: the bot forwards only messages newer than the newest id
   at its first start, and the bridge forwards live stream messages until it is pruned. While
   both run, a message can arrive twice. A message created after the bridge stops and before
   the bot's first poll is missed. Both windows are seconds long and the message stays in
   Gotify. Accepted.

Rollback: revert the PR; the bridge Deployment returns.

## Docs

- `design/decisions/flight-tracker.md`: add alerts to the module list, the fail-open and
  drop-when-muted rules, and the "seed `last_id` on first start" rule.
- `design/decisions/gotify.md`: replace the `gotify-telegram bridge` paragraph with the alerts
  module description, drop the `python -u` rule (no longer applicable), keep the note that
  `gotify-telegram` now exists only to own `telegram-secret`.
- Routing table row for Gotify already mentions the Telegram bridge: reword to "the Telegram
  alerts module".

## Testing

New `tests/test_alerts.py`, stdlib only, with an injected fake Gotify client and clock:

- mute state machine: on, off, off with duration, expiry, restart mid-mute, bad durations
- fail-open on missing, corrupt and wrong-shape state
- forwarding order (oldest first), formatting, priority emoji
- muted messages dropped and `last_id` still advances
- first-start seeding, empty server, DB-reset rewind, missing fields
- poll failure keeps `last_id` and logs type only, one line per streak
- `/alerts` output in each state, usage on bad input

Plus `MainWiringTests` gains the `alerts` command and no command clashes with flights.

Manual smoke test after deploy:

1. `/alerts` shows ON.
2. Send a test message to Gotify (`curl` with an app token from a debug shell, or the Gotify
   web UI); it arrives in Telegram within about 10 s.
3. `/alerts off 1h`, send another; nothing arrives; `/alerts` shows the expiry.
4. `/alerts on`; the next message arrives, and the muted one does not.
5. `kubectl get deploy -n monitoring` no longer lists `gotify-telegram`.

## Out of scope

Priority exceptions or digests while muted, moving `telegram-secret` out of `gotify-telegram`,
the `/status` module (step 3), WebSocket intake.
