# Clef classifier: Gotify triage and FreshRSS news digest

Date: 2026-10-06. Status: design approved, plan not written.

## Goal

Deploy Cloudflare's open `clef-flash` decision model (9B, Apache 2.0) on `llm-1` and use it for two jobs in the Telegram bot:

1. **Triage** Gotify messages so only needed ones reach Telegram.
2. **News digest** from FreshRSS: the user keeps a topic list, the bot sends only matching articles.

Success: fewer low-value Telegram messages with zero missed critical alerts; a twice-daily digest containing only topic matches.

## Decisions (made with the user)

- Model: `clef-flash` deployed first, as its own service (not a reuse of `local-fast`, not a `llama-swap` model).
- No cloud: Workers AI is rejected; `design/decisions/llm.md` "no cloud fallback" stays.
- Delivery of news: Telegram digest (not per-article, not tagged back into FreshRSS).
- Triage scope: only Gotify priority < 8 is judged. Priority >= 8 always forwards.
- Fail-open: classifier down, slow, or returning an unusable answer means the message is forwarded.
- Topics are managed by Telegram commands, stored per chat ID (multi-user later).
- FreshRSS is read through its API key, which bypasses OIDC and 2FA.

## Components

### 1. Model service (`kubernetes/apps/ai/clef/`)

**Not `llama-swap`.** Clef is a Qwen 9B backbone plus a trained `joint_head.safetensors` and custom code (`joint_schema_model.py`); it answers with one probability per option in a single forward pass. The community GGUFs contain only the backbone, so `llama-server` cannot run it. (Found 2026-10-06; see Spike result.)

- A small separate Deployment `clef` in `ai`, pinned to `llm-1` with the `workload=llm` toleration, running the official `Cloudflare/clef-flash` weights on CPU in bf16 via `torch` + `transformers 5.10.2` (+ `accelerate`, `torchvision`, `pillow`).
- A thin HTTP wrapper around `joint_schema_model.systemone(model, processor, body)` exposing `POST /v1/systemone` (SystemOne request/response shape) and `/healthz`. Inside the cluster only; no HTTPRoute.
- Weights (19 GB) live on a PVC, fetched by a Job like `model-fetch`; the custom image bakes only the Python dependencies. Pin the Hugging Face revision (spike used `17f0b0ad64efb65d273590632833508766b2aae6`).
- Memory: the pod needs about 20 GiB. `llama-swap` already *requests* 60 Gi of the node's ~68 Gi, so the plan must lower the `llama-swap` request (its real use is lower) or size Clef's request and limit explicitly; scheduling fails otherwise.
- Not routed through LiteLLM: LiteLLM speaks chat completions, not SystemOne.

### 2. Triage (`triage.py` in the flight-tracker app)

- `alerts.py` calls triage only for messages with priority < 8.
- Input: title, message and priority. One `noul` question ("does this need the owner's attention?"); forward when the probability is at least 0.5.
- Timeout 15 s (a call takes about 4.3 s on CPU). Triage runs on a worker thread so a slow call never delays priority >= 8 forwarding. Any timeout, HTTP error, or output outside the two labels forwards the message.
- Dropped messages stay in the Gotify UI. The last 20 drops are kept in state and reported by `/alerts`; every decision is logged.
- `/alerts off` mute behaviour is unchanged and applies before triage.

### 3. News digest (`news.py`)

- Polls the FreshRSS API every 30 min for unread articles, skipping IDs already seen (state on the PVC).
- Each new article is scored against the chat's topics in one call, with one `noul` question per topic (about 6.7 s per call for 3 topics, so a poll of 100 articles takes about 11 min; fine for a background job). Matches are queued.
- A digest is sent at 08:00 and 18:00 (configurable) with title, link and matched topic. An empty digest sends nothing.
- Commands: `/topics` (list), `/topics add <text>`, `/topics rm <text>`. Topics are stored per chat ID in `/data/news.json`.
- The core still answers only `TELEGRAM_CHAT_ID`. Per-chat storage keeps multi-user possible without a data migration; lifting the owner-only check is out of scope.
- The API key is a new SealedSecret in `monitoring`, loaded as an env var. The bot reaches FreshRSS in-cluster, not through the public route.

### 4. Docs and monitoring

- Update `design/decisions/llm.md` (Clef service, `local-classify`) and `design/decisions/flight-tracker.md` (triage and news modules, new rules).
- Optional `/status` line for the classifier if it is cheap.

## Error handling

- Classifier unreachable: triage forwards; news skips the scoring cycle and retries next poll (articles stay unseen).
- FreshRSS unreachable: log once, back off, no digest sent for that cycle.
- Corrupt or missing state: triage and alerts behave as "forward everything"; news starts with an empty topic list and sends nothing.

## Risks to resolve in the plan

1. Memory headroom on `llm-1`: Clef peaked at 20.8 GiB (includes weight page cache) while the 35B stayed loaded; confirm `llama-swap` stays fast with both resident.
2. Accuracy on harder data: the spike set was easy (see Spike result); keep the `/alerts` drop log and review it for a week.
3. Latency: about 4.3 s per call on 6 CPU threads; revisit if a burst of messages queues up.
4. The FreshRSS API key may exist only in the FreshRSS UI; sealing it is a manual step for the user.

## Out of scope

- Hosted Workers AI, multi-user command access, image classification, fine-tuning.

## Spike result (2026-10-06)

Run on `llm-1` in a throwaway pod (deleted afterwards; a one-time exception to the Flux-only rule, approved by the user). Official weights, CPU bf16, 6 threads, torch 2.14.1+cpu, transformers 5.10.2.

- Load 3.4 s. Per-call latency: median 4.26 s, max 6.46 s for the Gotify question; 6.74 s for 3 topic questions in one call.
- Memory: cgroup peak 20.8 GiB (includes page cache of the safetensors).
- Gotify triage: 100 real messages (all priority 5-7), labelled by rule (routine backup success, test, plugin-installed = noise; everything else needed). 0 needed-but-dropped and 0 noise-forwarded at thresholds 0.3, 0.5 and 0.7.
- News: 20 synthetic headlines, 3 topics: 60/60 correct at 0.5.
- Caveat: both sets are easy (the Gotify set is mostly repeats of 3 message kinds, labelled by me). Treat the accuracy as "works", not "proven". Also the FreshRSS sample is synthetic.
- Go decision: latency is under the 5 s bar for triage and acceptable for batch news.
