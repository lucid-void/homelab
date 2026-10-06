# Clef classifier: Gotify triage and FreshRSS news digest

Date: 2026-10-06. Status: design approved, plan not written.

## Goal

Deploy Cloudflare's open `clef-flash` decision model (9B, Apache 2.0) on `llm-1` and use it for two jobs in the Telegram bot:

1. **Triage** Gotify messages so only needed ones reach Telegram.
2. **News digest** from FreshRSS: the user keeps a topic list, the bot sends only matching articles.

Success: fewer low-value Telegram messages with zero missed critical alerts; a twice-daily digest containing only topic matches.

## Decisions (made with the user)

- Model: `clef-flash` deployed first, as its own model (not a reuse of `local-fast`).
- No cloud: Workers AI is rejected; `design/decisions/llm.md` "no cloud fallback" stays.
- Delivery of news: Telegram digest (not per-article, not tagged back into FreshRSS).
- Triage scope: only Gotify priority < 8 is judged. Priority >= 8 always forwards.
- Fail-open: classifier down, slow, or returning an unusable answer means the message is forwarded.
- Topics are managed by Telegram commands, stored per chat ID (multi-user later).
- FreshRSS is read through its API key, which bypasses OIDC and 2FA.

## Components

### 1. Model service (`kubernetes/apps/ai/`)

- `llama-swap` gets a `clef-flash` model and a `classify` group with `swap: false`, `exclusive: false`, so it never unloads the 35B model (same rule as embeddings).
- Budget: `--threads 2 --ctx-size 8192`. `llm-1` has about 68 GiB allocatable; the existing stack uses roughly 45-48 GiB; Clef-flash Q8 is about 9-10 GB.
- `fetch-models.sh` downloads one pinned community GGUF (candidate: `bartowski/Cloudflare_clef-flash-GGUF`) with a recorded checksum. The GGUF is not published by Cloudflare, so the checksum is the provenance record.
- LiteLLM registers `local-classify` and a dedicated virtual key limited to that model (key `models` lists are enforced; see `llm.md`).

### 2. Triage (`triage.py` in the flight-tracker app)

- `alerts.py` calls triage only for messages with priority < 8.
- Input: title and message. Output: exactly `needed` or `noise`, requested with a constrained schema.
- Timeout 3 s. Any timeout, HTTP error, or output outside the two labels forwards the message.
- Dropped messages stay in the Gotify UI. The last 20 drops are kept in state and reported by `/alerts`; every decision is logged.
- `/alerts off` mute behaviour is unchanged and applies before triage.

### 3. News digest (`news.py`)

- Polls the FreshRSS API every 30 min for unread articles, skipping IDs already seen (state on the PVC).
- Each new article is scored against the chat's topics; matches are queued.
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

1. GGUF file size and checksum are unverified; confirm before download.
2. Real memory headroom on `llm-1` after loading (check `KV self size` and node memory).
3. Prompt and schema quality: test on real Gotify and FreshRSS samples before wiring into the bot.
4. The FreshRSS API key may exist only in the FreshRSS UI; sealing it is a manual step for the user.

## Out of scope

- Hosted Workers AI, multi-user command access, image classification, fine-tuning.
