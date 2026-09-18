# Local LLM

**Read before editing:** `kubernetes/apps/ai/`, `kubernetes/apps/monitoring/ai-monitoring/`

## Current state

`Qwen3.6-35B-A3B` Q8_0 (**34.4 GiB**, effectively lossless) served by `llama.cpp` behind
`llama-swap`, on the dedicated worker `llm-1`, tainted `workload=llm:NoSchedule`. Runs
with `--threads 6`, matching the host's 6 P-cores. `LiteLLM` routes requests at
`llm.blackcats.cc`; `Open WebUI` is the chat UI at `chat.blackcats.cc`. Namespace `ai`,
no PSA label (cluster-default `baseline`).

Served as two LiteLLM model names on one process, one copy of the resident weights:
`local-smart` (thinking on) and `local-fast` (`enable_thinking: false` pushed into the
chat template via `extra_body`). Neither is a separate model or uses extra memory.

Measured decode: **8.4–8.9 tok/s**. Thinking cost on the same trivial prompt: `local-smart`
572 chars / 152 reasoning tokens (~18s) vs `local-fast` 0 / 2 (~0.2s) — the overhead is
per turn, so it compounds across a tool-calling loop. **Coding agents use `local-fast`.**

Vision is not deployed. The model is itself multimodal, so adding it later is one
`--mmproj` flag, not another model.

`--ctx-size` is `32768`. Embeddings model is `nomic-embed-text-v1.5` Q8_0, served as
`local-embed`. Client configs (e.g. opencode) must declare `context: 32768`, not the
128K target — the server truncates silently past what it was actually started with.

Open WebUI reads OIDC config from `openwebui-oidc-secret`; its Kustomization has
`dependsOn: zitadel-bootstrap`.

## Rules

- **Never raise `--ctx-size` without reading `KV self size` from a fresh boot log
  first** — it pre-allocates the entire KV cache at startup and `llama-swap` runs with
  no memory limit, so an oversized value takes the *node*, not the pod.
- **No memory limit on `llama-swap` is deliberate** — `llama.cpp` mmaps the GGUF, so
  those pages count as reclaimable page cache; a limit near the working set does not
  OOMKill cleanly, it causes continuous reclaim-and-refault that presents as "the model
  got mysteriously slow" with nothing in the logs.
- **Both models need explicit `llama-swap` groups with `exclusive: false`** — the
  default group settings are `swap/exclusive: true`, so in a single default group every
  embedding call would unload and reload the 34 GiB chat model. A newer canonical
  `routing:` block does the same job; a config must not mix both styles.
- **Set `logToStdout: both`** — it defaults to `proxy`, which forwards only
  `llama-swap`'s own request log and swallows `llama-server`'s entirely: not visible in
  `kubectl logs`, `GET /logs`, or `/logs/stream/upstream`. That hides model-load
  failures completely — a bad GGUF or an OOM abort surfaces as a request that never
  returns, while the proxy keeps answering `/v1/models` and the pod stays Ready.
- **`--cache-reuse` does not work with this model** — `llama-server` logs
  `cache_reuse is not supported by this context`, gated by `!llama_memory_can_shift(...)`,
  a property of the model's KV implementation that no flag overrides. The only live
  prefix caching is the cross-request prompt cache, which defaults to **8192 MiB** and
  is on unasked — budget 8 GiB of real memory for it before sizing context.
- **LiteLLM needs a `4Gi` memory limit** — at `1Gi` it dies inside the Prisma migration
  in ~11s with exit **137** and **zero log output**; `lastState.terminated.reason` was
  the only evidence. Steady state runs just under 1Gi, so the startup spike is the
  entire problem.
- **LiteLLM's `/metrics` is 404 on the OSS tier** — Prometheus metrics are gated behind
  LiteLLM's enterprise tier, so the endpoint doesn't exist here. The scrape was deleted
  rather than left permanently red.
- **`ENABLE_OAUTH_SIGNUP` defaults to `false`** on Open WebUI — OIDC renders and the
  first login fails outright because there is no account and nothing will create one.
- **`ENABLE_PERSISTENT_CONFIG` defaults to `true`** on Open WebUI — it copies env-based
  config into its database on first read, then ignores the env var on every subsequent
  boot. A config change requires editing the DB-backed value, not just the manifest.
- **`app-template` strips `optional` from an `envFrom` secretRef** (verified by
  rendering — no error, the key just never reaches the manifest), so Open WebUI
  genuinely requires `openwebui-oidc-secret` and sits in `CreateContainerConfigError`
  without it, same as Kavita.
- **LiteLLM and Open WebUI use different env var names for the same DB credential** —
  `DATABASE_USERNAME` for LiteLLM, `DATABASE_USER` for Open WebUI. Open WebUI also only
  builds a Postgres URL when **all five** `DATABASE_*` vars are set — miss one and it
  falls back to SQLite on the PVC silently, and it does not URL-encode the password
  before interpolating it, so the generated role passwords are kept alphanumeric.
- **`--mlock` is not used** — it would need `IPC_LOCK`, which PSA `baseline` forbids,
  forcing the whole namespace to `privileged` to solve a problem the dedicated,
  otherwise-idle node doesn't have.
- **`kubelet_volume_stats_*` on the `llama-models` PVC reports `llm-1`'s node
  filesystem, not the PVC** — it's `openebs-hostpath`, so a "model PVC almost full"
  alert would just be a node-disk alert under the wrong name; `NodeFilesystemAlmostFull`
  already covers it.
- **A LiteLLM virtual key's `models` list is enforced, and `/v1/models` is filtered per
  key** — a key minted before a model existed rejects it with an unhelpful
  key-not-allowed-to-access-model error rather than erroring usefully; adding a model
  means updating existing keys via `/key/update`.
- **Expect the CNPG managed-role race on first deploy** — the app's role and its
  SealedSecret reconcile near-simultaneously; if CNPG evaluates the role first it
  records `cannotReconcile: failed to get password secret … not found` and never
  retries. `flux reconcile` does not help since the `Cluster` object already matches
  git; nudge it with an annotation instead (see `cnpg.md`).
- **There is no cloud fallback, by choice** — `router_settings.fallbacks` is commented
  out in the LiteLLM config and no cloud model is registered, so agentic requests take
  as long as they take rather than being routed away.

## Verify

```bash
# real memory assigned to llm-1
mise exec -- kubectl get node llm-1 -o jsonpath='{.status.allocatable.memory}{"\n"}'

# only DaemonSets plus the LLM stack should land here
mise exec -- kubectl get pods -A --field-selector spec.nodeName=llm-1

# KV cache size, before ever raising --ctx-size
mise exec -- kubectl -n ai logs deploy/llama-swap | grep -i 'KV self size'

# model actually loaded (first call after a swap is slow by design)
mise exec -- kubectl -n ai exec deploy/llama-swap -- curl -s localhost:8080/v1/models

# end to end through the router
curl -s https://llm.blackcats.cc/v1/chat/completions \
  -H "Authorization: Bearer <virtual-key>" \
  -d '{"model":"local-smart","messages":[{"role":"user","content":"hi"}]}'
```
