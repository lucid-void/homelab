# Flight tracker

**Read before editing:** `kubernetes/apps/monitoring/flight-tracker/`

## Current state

Single-user Telegram bot in `monitoring`. You send `/track LH123 2026-10-05`; it polls
AeroDataBox (RapidAPI free plan, 400 units/month at 2 units per call) and messages gate,
terminal, time and status changes. `/list`, `/untrack`, and `/fetch` (tappable list of
tracked flights, or `/fetch LH123`) complete the flight interface; `/alerts` mutes Gotify forwarding, `/status` summarises cluster health. `alerts.py` runs Clef triage on Gotify messages below priority 8, and `news.py` sends a twice-daily FreshRSS digest of articles matching `/topics`. Nine
stdlib-only Python files (`core.py`, `flights.py`, `alerts.py`, `status.py`, `clef.py`, `triage.py`, `freshrss.py`, `news.py`, `main.py`) delivered by one `configMapGenerator` `files:`; state is
JSON on an `nfs-client` PVC.

It reuses the existing bot (`telegram-secret`, owned by the `gotify-telegram` Kustomization),
also loads `gotify-client-secret` (provisioned by `gotify-bootstrap`) for the alerts module,
and answers only `TELEGRAM_CHAT_ID`. Flight numbers are normalised, so `sk0486`, `SK 486`
and `SK486` are the same flight.

## Layout and modules

`core.py` owns the Telegram client, the owner-only check, the update offset (`/data/core.json`),
and a registry of commands and inline-button prefixes. `flights.py` is the flight tracker as a
module. `alerts.py` forwards Gotify messages to Telegram and owns `/alerts`. `status.py` answers `/status` from VictoriaMetrics. `main.py` builds the bot and registers modules. A module is any object with `name`,
`help` (lines), `commands` (`name -> fn(args)`), `callbacks` (`prefix -> fn(parts)`) and an
optional `start()`; the core hands it a `Ctx` with `send`, `log` and its own state path
`/data/<name>.json`. Flights is the exception: it keeps its historical `/data/state.json`.

To add a feature: write `<feature>.py` with a module class, add it to `files:` in
`app/kustomization.yml`, add one `bot.register(...)` line in `main.py`, and put its help lines
on the class. The core needs no change.

## Rules

- **It is the only `getUpdates` consumer of the shared bot token.** `gotify-telegram` only
  sends. A second poller, or a webhook, on the same token makes Telegram answer `409` and
  the receiver loop will back off and log `getUpdates failed`.
- **Do not rename the app, Kustomization, Deployment or `flight-tracker-data` PVC without
  copying the state first.** A rename changes the Flux Kustomization name and `prune: true`
  deletes the PVC holding the live flight state.
- **`core.json` is authoritative for the update offset; `state.json["offset"]` is only the
  first-start seed.** On the first start after the split the core reads the old offset from
  `state.json`, so Telegram does not replay old commands.
- **Callback data keeps the `f:NUMBER:DAY` format**, so buttons in old chat messages still work.
- **Duplicate command names or callback prefixes across modules fail at startup.** That is
  intentional.
- **`/alerts off` drops Gotify messages from Telegram** (they stay in the Gotify UI) and never
  mutes flight messages. `/alerts off 4h` expires by itself; no duration lasts until `/alerts on`.
- **Missing or corrupt `alerts.json` means alerts ON**, never muted.
- **First start with no `alerts.json` forwards nothing** and records the newest Gotify id, so
  old history is not replayed into Telegram.
- **`/status` relies on `ALERTS`, `kube_pod_status_phase`, `kube_deployment_status_replicas_unavailable`
  and `kube_cronjob_*` from vmsingle (env `VM_URL`).** A check whose data is missing is red
  `unavailable`, never green: with kube-state-metrics down the pod and backup series vanish,
  and reading that as zero would report a healthy cluster.
- **The `/status` backup line matches CronJobs by name (`*-backup`, `etcd-snapshot`)**, so a new
  backup job with another name is not covered until the pattern in `status.py` changes. A
  matching CronJob that never succeeded shows as `never`; it has no `last_successful_time`.
- **`/status` has no Flux line.** The Flux controllers emit no per-object Ready metric
  (`gotk_resource_info` is absent); it needs kube-state-metrics custom resource state in the
  `vm-stack` HelmRelease first. Flux events still reach Telegram through the alerts module.
- **Do not prune `gotify-telegram`** without sealing a separate `telegram-secret` for this
  app: `flight-tracker` `dependsOn` it for that Secret.
- **Polling spends a small free quota (about 200 calls a month).** The schedule (48 h →
  12 h → 6 h → 60 min → 15 min in the last 2 h → up to 120 min in flight, aiming at the
  expected arrival → 15 min after arrival, about 27 calls a flight) and the 80%/95%
  budget guard exist for that reason. **Each interval is cut at the next window boundary**
  — without that, a poll made 7 h out lands 30 min before departure and the 6 h–2 h
  window, when gates are published, is never polled. The counter is a local estimate by
  calendar month, not RapidAPI's billing cycle; `/list` shows it, and the
  `x-ratelimit-api-units-remaining` response header is the truth. Raising polling
  frequency needs the arithmetic redone.
- **Failed scheduled polls back off** (15 min, doubling, capped at the flight's normal
  interval, reset by a success). Flat 15-minute retries on a flight the API no longer
  knows would reach the 95% pause in about two days and silence every other flight.
  Requests that never reached RapidAPI (DNS, refused, timeout) are not billed to the
  counter; an HTTP error or an unreadable body is.
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
- **Triage never drops priority >= 8, a message with no usable priority, or anything when Clef
  fails** (timeout 15 s, bad answer, service down; `clef.py` turns every transport error into
  `ClefError`, so `judge` never raises). Dropped messages stay in the Gotify UI; the last 20 are
  kept in `alerts.json` and listed by `/alerts dropped`, and corrupt entries are filtered on
  load. Triage runs outside the alerts lock; do not move it inside, it would freeze `/alerts`
  for the length of a burst.
- **The news module needs the sealed `freshrss-api-secret`** (`FRESHRSS_USER`,
  `FRESHRSS_API_PASSWORD` = FreshRSS's API password, which bypasses OIDC/2FA). Without it the
  module is simply not registered and the log says so.
- **The first news poll marks the whole unread backlog as seen without scoring it**, and a poll
  with no topics marks articles seen too; only articles that arrive while topics exist can match.
  The FreshRSS client raises on a response whose `items` is not a list, so a malformed first
  response cannot seed an empty seen-list.
- **A poll scores at most 40 articles** (about 7 s each); the rest wait for the next poll. A Clef
  failure leaves the unscored articles unseen so nothing is lost.
- **Digest times are 08:00 and 18:00 in `DIGEST_TZ`**; if the zone database is missing the bot
  logs it and uses UTC. The first digest check only starts the schedule.
- **A failed digest send is retried, not lost.** `Telegram.send` returns True/False;
  `maybe_digest` puts the pending matches and the previous slot back when it returns False, so
  the next loop pass (about 30 s) tries again.
- **Article text is untrusted.** A crafted article can fool the classifier into a false match; the
  result is a wrong line in a digest, nothing more.
- **Topics are stored per chat id** in `/data/news.json`, but the core still answers only
  `TELEGRAM_CHAT_ID`, so there is one list today. Multi-user needs the core to pass the chat id to
  commands.

## Verify

```bash
mise exec -- kubectl get deploy,pvc -n monitoring | grep flight-tracker
mise exec -- kubectl logs -n monitoring deploy/flight-tracker
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests
```

Then send `/list` to the bot.
