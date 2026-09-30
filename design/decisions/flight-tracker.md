# Flight tracker

**Read before editing:** `kubernetes/apps/monitoring/flight-tracker/`

## Current state

Single-user Telegram bot in `monitoring`. You send `/track LH123 2026-10-05`; it polls
AeroDataBox (RapidAPI free plan, 400 units/month at 2 units per call) and messages gate,
terminal, time and status changes. `/list`, `/untrack`, and `/fetch` (tappable list of
tracked flights, or `/fetch LH123`) complete the interface. One stdlib-only Python file,
delivered by `configMapGenerator` `files:`; state is a JSON file on an `nfs-client` PVC.

It reuses the existing bot (`telegram-secret`, owned by the `gotify-telegram` Kustomization)
and answers only `TELEGRAM_CHAT_ID`. Flight numbers are normalised, so `sk0486`, `SK 486`
and `SK486` are the same flight.

## Rules

- **It is the only `getUpdates` consumer of the shared bot token.** `gotify-telegram` only
  sends. A second poller, or a webhook, on the same token makes Telegram answer `409` and
  the receiver loop will back off and log `getUpdates failed`.
- **Do not prune `gotify-telegram`** without sealing a separate `telegram-secret` for this
  app: `flight-tracker` `dependsOn` it for that Secret.
- **Polling spends a small free quota (about 200 calls a month).** The schedule (48 h →
  12 h → 6 h → 60 min → 15 min in the last 2 h → 120 min in flight → 15 min after
  arrival, about 27 calls a flight) and the 80%/95% budget guard exist for that reason.
  The counter is a local estimate by calendar month, not RapidAPI's billing cycle; `/list`
  shows it, and the `x-ratelimit-api-units-remaining` response header is the truth.
  Raising polling frequency needs the arithmetic redone.
- **Send a custom `User-Agent` on every AeroDataBox call.** Cloudflare in front of RapidAPI
  answers Python's default agent with `403 error code 1010`, which looks like a bad key.
- **The `configMapGenerator` must set `namespace: monitoring`**, as `gotify-bootstrap`
  does. Without it kustomize leaves the Deployment's volume pointing at the un-hashed
  ConfigMap name and the pod sticks in `ContainerCreating`.
- **Time changes under 5 minutes are ignored** against a stored baseline, so drift still
  alerts once it passes 5 minutes from the last alerted time.
- **Never log request headers or Telegram/RapidAPI exception text** — both embed the
  credential. Log the exception type only.
- **Gate data is airport-dependent** and often appears only 1–3 h before departure; a
  missing gate is not a bug. The API can also return a `revisedTime` and a different
  `predictedTime` for the same arrival; the tracker follows `revisedTime`.
- **A failed API call is one message per streak** (`Failed to fetch API`); `/track` and
  `/fetch` always answer it. `/track` stores a flight only after a successful fetch.
- **The API key is sealed in `aerodatabox-sealed.yml`.** To rotate it, regenerate on
  RapidAPI, write a plaintext `aerodatabox-secret.yml` (gitignored) and re-seal it with the
  repo's `kubeseal` command.

## Verify

```bash
mise exec -- kubectl get deploy,pvc -n monitoring | grep flight-tracker
mise exec -- kubectl logs -n monitoring deploy/flight-tracker
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests
```

Then send `/list` to the bot.
