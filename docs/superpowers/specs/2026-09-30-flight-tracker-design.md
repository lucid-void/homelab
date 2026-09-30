# Flight tracker — design

**Date:** 2026-09-30
**Status:** approved

## Purpose

Personal flight tracker. The owner sends a flight number and date to a Telegram bot and
gets push messages when gate, terminal, time or status changes. Single user, a handful of
flights at a time. Free API tier only.

## Decisions taken

| Topic | Decision | Reason |
|---|---|---|
| Users | One: the chat in `TELEGRAM_CHAT_ID` | Owner said "just for me"; no allowlist or per-user caps needed |
| Bot | Reuse the existing bot (`telegram-secret` in `monitoring`) | Owner's choice. The Gotify bridge only *sends*; the tracker *receives* via `getUpdates`, so there is no conflict |
| Data source | AeroDataBox, free Basic plan via RapidAPI (400 units/month, 2 units per call) | Best free quota that includes gate/terminal fields; gate data is "sometimes" available, airport-dependent |
| Namespace | `monitoring`, app `flight-tracker` | `telegram-secret` is namespaced there, so it is reused without re-sealing |
| Runtime | `python:3.14-alpine`, stdlib only, script in a ConfigMap | No `pip`/`apk` at startup, so none of the repo's startup-stall traps apply |
| State | JSON file on an `nfs-client` PVC | Tracked flights survive restarts |

## Commands

Only messages from `TELEGRAM_CHAT_ID` are handled; everything else is ignored silently.

| Command | Behaviour |
|---|---|
| `/track LH123 2026-10-05` | Validates format, fetches immediately, stores the flight **only if the fetch succeeded**, replies with the full snapshot |
| `/list` | Tracked flights with next scheduled departure and API units used this month |
| `/untrack LH123` | Stops tracking. Also drops the flight automatically once landed |
| `/fetch` | Shows the tracked flights as tappable buttons (`LH123 · 2026-10-05`, soonest first). Tapping one hard-fetches it now, ignoring the schedule. With exactly one tracked flight it fetches that one directly, with no list. With none, replies `No tracked flights` |
| `/fetch LH123` | Shortcut: hard-fetches that tracked flight without the list. An untracked flight is refused with `Not tracked: LH123`; `/fetch` never adds a flight |

A hard fetch replies with the current snapshot even when nothing changed, and applies any
change to stored state. Buttons are Telegram inline-keyboard callbacks, handled by the same
`getUpdates` loop; a tap from any chat other than `TELEGRAM_CHAT_ID` is ignored. A button
for a flight that was untracked or landed since the list was sent answers `Not tracked`.

## Polling schedule

Every tracked flight is fetched on `/track`, then by time to scheduled departure:

| Window | Interval |
|---|---|
| > 72 h | 48 h |
| 72 h – 24 h | 12 h |
| 24 h – 6 h | 6 h |
| 6 h – 2 h | 60 min |
| 2 h – departure (and late departures) | 15 min |
| departure – expected arrival | 120 min |
| after expected arrival, and after landing | 15 min |

That is roughly 27 calls (54 units) per flight, chosen so a handful of flights fits the free quota.

Stops after the flight is landed, once arrival data (terminal, baggage belt) has been
reported or 1 h has passed, on cancellation, or 6 h after the expected arrival if the API
never reports it landed.

**Budget guard.** A persisted per-month counter of API calls. At 80% of 400 units the bot
sends one warning; at 95% scheduled polling pauses and `/fetch` keeps working. Units per
call vary by endpoint, so the counter records units, not requests (cost verified against a
real response during implementation). It counts by calendar month, which approximates
RapidAPI's billing cycle.

## Messages

- **Change:** `✈ LH123 · 2026-10-05` then one line per changed field, e.g. `Gate B22 → B24`.
  Fields watched: departure gate and terminal, arrival gate, terminal and baggage belt,
  scheduled/revised/predicted times, status (delayed, cancelled, diverted, departed,
  landed).
- **Fetch failure:** `Failed to fetch API`. Covers HTTP errors, timeouts, and a 200 with no
  data for the flight. Scheduled polls send it once per failure streak (re-armed by the
  next success) so an outage does not spam every 15 minutes. `/track` and `/fetch` always
  reply with it, since the owner asked for a result.
- **Unchanged poll:** silent. Expected-time changes under 5 minutes are ignored against the
  last alerted time, so jitter is silent but real drift still alerts.

## Components

One Python file (`tracker.py`), one process, two loops:

- **Receiver:** long-polls `getUpdates` (messages and button callbacks), parses commands, answers.
- **Scheduler:** wakes every minute, polls whichever flights are due.

Pure functions, unit-testable without the network: `parse_command`, `next_poll_at`,
`diff_snapshots`, `format_message`. Impure edges: `fetch_flight` (AeroDataBox HTTP),
`send` (Telegram HTTP), state load/save (atomic write via temp file + rename).

## Deployment

`kubernetes/apps/monitoring/flight-tracker/{ks.yml,app/}`: Deployment (1 replica,
`Recreate` strategy, non-root, read-only rootfs with `/tmp` emptyDir), PVC, ConfigMap via
`configMapGenerator`, `aerodatabox-sealed.yml`. `envFrom` the existing `telegram-secret`
and the new key secret. Reloader annotation for secret changes. No HTTPRoute: nothing is
exposed; the bot makes outbound calls only.

The plaintext key lives in a gitignored `aerodatabox-secret.yml` and is sealed with the
repo's `kubeseal` command. It is never committed in plaintext.

## Testing

- Unit tests for the pure functions with recorded AeroDataBox response fixtures
  (including a no-gate response, a multi-leg response and an empty response).
- One real call with the owner's key to record the response shape and unit cost before the
  parser is finalized.
- Live check after Flux applies: `/track` today's flight, `/list`, `/fetch`, then an
  induced failure (bad key in a scratch run) to see `Failed to fetch API`.
- `.agents/scripts/validate-manifests.sh` on the app directory before any push.

## Docs to update on implementation

`design/decisions/flight-tracker.md` (new), a routing row in `.claude/CLAUDE.md`, and the
inventory line in `design/docs/services.md`.

## Out of scope

Friends or multi-user, web UI, Signal, airport-wide watching, ADS-B/live map, provider push
alerts, a second API fallback.

## Known limits

- Gate data is airport-dependent and often only published 1–3 h before departure.
- Free plan: 400 units/month (about 200 calls); a paused budget stops scheduled alerts until the month rolls.
- The API key was pasted into the chat session; regenerate it on RapidAPI once the tracker
  is live.
