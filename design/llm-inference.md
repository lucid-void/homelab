# Local LLM inference — design proposal

**Date:** 2026-08-28, superseded in part 2026-08-30
**Status:** **RATIONALE ONLY — the stack is deployed; this document is not the current
state.** Read [design/llm-deployment.md](llm-deployment.md) for what actually runs. Kept
because the reasoning and the rejected options are still worth having; the numbers in it
are pre-deployment estimates unless marked otherwise.
**Constraint:** CPU + RAM only, on the existing MS-02 Ultra. No GPU.
**Settled sizing:** `cp-1/2/3` at **30 GB** each, `llm-1` worker at **70 GB** (was 64 in
this document's original sizing).
**Goal:** Serve a local LLM to Open WebUI, bots and tooling, with routing, observability
and SSO consistent with the rest of the homelab.

> **Implementation plan: [design/llm-deployment.md](llm-deployment.md).**
> This document is the *rationale* — what fits, what was rejected and why. The
> deployment spec holds the manifests, placement, ordering and verification.

> ### Superseded by deployment — read this before trusting any number below
>
> | This document says | Actual, measured 2026-08-30 |
> |---|---|
> | `qwen-smart` ~5–8 tok/s | **8.4–8.9 tok/s decode** — better than estimated |
> | `qwen-vision` as a second model | **dropped.** Qwen3.6-35B-A3B is itself multimodal (`image-text-to-text`, ships `mmproj-F16.gguf`), so a separate 27B bought nothing |
> | `qwen-fast` as a second, smaller model | **replaced** by `local-fast` — the *same* weights with thinking disabled, no extra memory |
> | `--cache-reuse` helps follow-up turns | **it never runs.** `cache_reuse is not supported by this context` — gated on `!llama_memory_can_shift(...)` for this model's KV implementation |
> | 64 GB, three models co-resident | **70 GB, one model.** Budget also missed llama-server's 8 GiB default prompt cache |
> | `qwen-smart` filename `UD-Q8_0` | no such file; it is `Qwen3.6-35B-A3B-Q8_0.gguf` |
>
> The largest *unmeasured* claim below is prefill (~50–80 tok/s). It is still unmeasured on
> a realistic prompt and is the number the agentic-coding verdict rests on.

---

## 1. What 64 GB actually buys

**Qwen3.8-Flash-Next does not fit *in RAM*.** Its smallest quantization is 72.5 GB
(UD-IQ1_S) and the smallest one worth running is 82 GB (UD-IQ3_XXS). A 64 GB node has
roughly **60 GB usable** after Talos, kubelet and Cilium.

Note the precise claim. It does not fit *resident* — but llama.cpp mmaps its weights, so
a model larger than RAM still **runs**, page-faulting the overflow from disk on every
token. At 82 GB against ~60 GB usable that is only ~22 GB of overflow, which is a far
gentler ratio than the disk-streaming designs discussed in §8. It will be slow and it
will hammer the NVMe, but "impossible" is the wrong word and earlier revisions of this
document overstated it. **Benchmark it in Phase 4** rather than assuming either way.

What follows is the recommended build, which keeps the model resident.

That is not a loss. At 64 GB you can do something better than a huge model at a damaging
3-bit quant: run a strong model at a **near-lossless** one.

### The model set

All sizes are real GGUF file sizes, not estimates.

| llama-swap entry | Model | Quant | Size | Est. decode | Role |
|---|---|---|---|---|---|
| **`qwen-smart`** | Qwen3.6-35B-A3B | **Q8_0** | **36.9 GB** | **~5–8 tok/s** | default — effectively lossless |
| `qwen-fast` | Qwen3.6-35B-A3B | UD-Q4_K_XL | 22.1 GB | ~10–15 tok/s | interactive chat, autocomplete |
| `qwen-vision` | Qwen3.8-27B | UD-Q4_K_XL + mmproj | 18.8 GB | ~1.5–2.5 tok/s | Immich / Paperless, batch only |

Optional fourth entry to benchmark, not to assume: **Qwen3-Next-80B-A3B** at Q4_K_M
(51 GB, 3B active, ~4–6 tok/s). It is a bigger model but a full generation older than
Qwen3.6, so it is unlikely to beat `qwen-smart` — test it in Phase 4 rather than
planning around it.

### Why Q8_0 is the right default here

You said you would rather have a slow smart model than a fast stupid one. At 64 GB the
way to honour that is **quantization headroom**, not parameter count.

Qwen3.6-35B-A3B scores **86.0 GPQA Diamond** — and that is the BF16 number. At Q8_0 you
are running essentially that model: Q8 is the point where quantization loss stops being
measurable. Contrast the alternative from the previous revision, Flash-Next at IQ3_XXS:
nominally 91.7 GPQA, but a 6B-active MoE at 3 bits is the worst case for quantization
damage, so the real delivered quality was an open question. **Q8_0 removes that risk
entirely** — you know exactly what you are getting.

Sizes for the same model, if you want to trade back: Q4_K_M 22.1 GB, Q5_K_M 26.5 GB,
Q6_K 29.3 GB, Q8_0 36.9 GB. **Q6_K is the honest alternative** — visually
indistinguishable from Q8 in quality and ~25% faster, because decode speed scales with
bytes read per token. Start at Q8_0, drop to Q6_K if the speed bothers you.

### Memory budget for `llm-1`

**Live as of 2026-08-28:** the control-plane resize is **done** (29.3 GiB allocatable
each) and `llm-1` is **built and joined** (62.3 GiB allocatable). That is 154 GB of the
**160 GB** available.

| Item | at 64 GB | at 70 GB |
|---|---|---|
| Talos + kubelet + Cilium | 2.5 | 2.5 |
| llama-swap proxy | 0.1 | 0.1 |
| `qwen-smart` Q8_0 | 36.9 | 36.9 |
| `local-embed` | 0.3 | 0.3 |
| `qwen-vision` | *swaps in* | 18.8 |
| KV cache, 32K ctx at `q8_0` k/v | ~6 | ~6 |
| **Peak** | **~45.8** | **~64.6** |
| **Allocatable** | 62.3 | ~68.3 |

**Spend the remaining 6 GB — take `llm-1` to 70 GB.** At 64 GB `qwen-vision` has to swap
in, which evicts the 36.9 GB chat model on every Paperless or Immich call and reloads it
afterwards. At 70 GB all three stay resident and nothing swaps. The node has to be
drained and rebooted to fix its taint anyway (see
[llm-deployment.md](llm-deployment.md) §3), so the resize is effectively free.

---

## 2. The stack

Four layers. Only the first is new infrastructure; the rest follow patterns already in
this repo.

```
  CONSUMERS                  ROUTER (ai ns)            INFERENCE (llm-1, tainted)
  ─────────                  ──────────────            ──────────────────────────

  Open WebUI ──┐             ┌──────────────┐          ┌─────────────────────────┐
  chat.…cc     │             │   LiteLLM    │          │      llama-swap         │
               │             │              │          │   one :8080 /v1 surface │
  bots · RSS ──┼────────────►│ virtual keys │─────────►│                         │
  hermes       │  llm.…cc    │ budgets      │          │  ┌── qwen-smart  Q8_0   │
               │  OpenAI API │ fallbacks    │          │  ├── qwen-fast   Q4_K_XL│
  Paperless ───┤             │ /metrics     │          │  └── qwen-vision + proj │
  Immich       │             └──────┬───────┘          │                         │
               │                    │                  │  loads on demand, TTL   │
  opencode ────┘                    │                  │  unload, one at a time  │
  (→ cloud)                         │                  └─────────────────────────┘
                            ┌───────┴────────┐                     │
                            ▼                ▼                     ▼
                     CNPG Postgres    VictoriaMetrics ◄──── llamacpp:* metrics
                     request log      → Grafana → Gotify
```

### 2.1 Inference — llama-swap wrapping llama.cpp

**Image:** `ghcr.io/mostlygeek/llama-swap:cpu` (multi-platform CPU build; images track
llama.cpp upstream and are tagged `v<ver>-cpu-b<llama-build>` — pin the full tag, per the
repo's image-pinning rule).

llama-swap is a single Go binary that presents **one OpenAI-compatible `/v1` surface**
and starts/stops `llama-server` processes behind it on demand. It is the right choice
here for three specific reasons:

- **One port, many models, one RAM budget.** Running three `llama-server` Deployments
  would need three RAM reservations. llama-swap loads on demand and unloads on TTL, so
  64 GB hosts a 37 GB model *and* two others without ever holding them simultaneously.
- **Config is a single YAML file** — lives in a ConfigMap, so the model set is in git and
  changes reconcile through Flux like everything else.
- **It exposes `/metrics` in Prometheus format**, so it fits the existing
  VictoriaMetrics path with no exporter sidecar.

It also serves `/v1/models`, `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`, plus a `/ui` for manual model control and `/logs/stream`.

**Why not the alternatives.** *vLLM* is built for GPU batching; its CPU backend is weak
and it cannot swap models. *Ollama* keeps its own model store and hides llama.cpp flags,
which fights both GitOps and the tuning this hardware needs. Worth a look in Phase 4:
**`ik_llama.cpp`**, a fork with materially better CPU MoE performance — llama.cpp issue
[#19480](https://github.com/ggml-org/llama.cpp/issues/19480) points at it as the
workaround for exactly the sparse-MoE inefficiency that caps our throughput.

**ConfigMap** — `kubernetes/apps/ai/llama-swap/app/config-configmap.yml`:

```yaml
models:
  qwen-smart:
    aliases: [default]
    ttl: 3600
    cmd: >
      /app/llama-server --host 0.0.0.0 --port ${PORT}
      --model /models/Qwen3.6-35B-A3B-Q8_0.gguf
      --threads 6 --ctx-size 32768
      --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn
      --cache-reuse 256 --metrics

  qwen-fast:
    ttl: 900
    cmd: >
      /app/llama-server --host 0.0.0.0 --port ${PORT}
      --model /models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
      --threads 6 --ctx-size 32768
      --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn
      --cache-reuse 256 --metrics

  qwen-vision:
    ttl: 600
    cmd: >
      /app/llama-server --host 0.0.0.0 --port ${PORT}
      --model /models/Qwen3.8-27B-UD-Q4_K_XL.gguf
      --mmproj /models/Qwen3.8-27B-mmproj-F16.gguf
      --threads 6 --ctx-size 16384
      --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn
      --metrics

  # Embeddings for Open WebUI's RAG. MUST live in its own group so it stays
  # resident alongside the chat model -- in the default group every embedding
  # call would evict 36.9 GB and reload it afterwards.
  local-embed:
    ttl: 0
    cmd: >
      /app/llama-server --host 0.0.0.0 --port ${PORT}
      --model /models/nomic-embed-text-v1.5.Q8_0.gguf
      --embeddings --threads 2 --ctx-size 8192 --metrics
```

Group the embedding model separately from the chat models so llama-swap keeps both
loaded rather than swapping between them — verify the `groups`/`matrix` key name against
the pinned image's docs, since that part of the schema has moved between versions.

Flags that are load-bearing, not decoration:

| Flag | Why |
|---|---|
| `--cache-reuse 256` | keeps the KV cache for a shared prompt prefix and reprocesses only the new suffix. The single biggest win for bots and templated prompts. **A timestamp in a system prompt defeats it entirely.** |
| `--metrics` | off by default. Without it there is no `/metrics` and no monitoring. |
| `--threads 6` | match the host's **6 P-cores**, not the 8 vCPU. E-cores contribute little and measurably *hurt* — the reference benchmark degraded from 3.54 to 2.84 tok/s going 12 → 24 threads. |
| `--cache-type-k/v q8_0` | halves KV footprint at negligible quality cost. |
| `--ctx-size` | KV for the whole window is pre-allocated **at startup**. 32K is honest for chat; do not set 262144 "just in case". |

`macros:` exists in the config schema for DRY-ing the repeated flags — verify the
substitution syntax against the pinned image's docs before relying on it.

**Model weights** live on an `openebs-hostpath` PVC (150 GB) on `llm-1` — never NFS; a
37 GB mmap over NFS is a slow cold start and a page-cache hostage. Weights are fetched by
an idempotent `model-fetch` **Job** (`huggingface-cli download`, skip if present with
matching size) that runs before the Deployment. They are **not backed up** — they are
re-downloadable, which matches the repo's existing backup policy.

**Placement:** `nodeSelector: {node-role: llm}` + a toleration for
`workload=llm:NoSchedule`. Nothing else schedules on `llm-1`.

### 2.2 Router — LiteLLM

`ai` namespace. Every consumer talks to LiteLLM and nothing else, which is what makes the
model set swappable without touching a client.

- **Postgres** via the shared CNPG cluster: add a `litellm` managed role plus a
  `litellm-role-secret` SealedSecret mirrored by Reflector, per `design/docs/secrets.md`.
  **Expect the CNPG managed-role race** documented in `.claude/CLAUDE.md` on first deploy
  — nudge the Cluster object, do not just `flux reconcile`.
- **Virtual keys** — one per consumer (Open WebUI, each bot, Paperless, Immich), with
  budgets. Mirrors the existing one-token-per-consumer convention.
- **Model entries** map friendly names onto llama-swap entries, and can pin sampling
  parameters per entry, so callers pick a quality/speed point explicitly:
  `local-smart` → `qwen-smart`, `local-fast` → `qwen-fast`,
  `local-vision` → `qwen-vision`, plus an optional `cloud-*` fallback target for agentic
  coding.
- **Fallback chain:** `qwen-smart` → `qwen-fast` → cloud. A model still loading returns
  slowly rather than erroring.
- `HTTPRoute` → shared Gateway, `llm.blackcats.cc`. No `Ingress` objects.
- Add `litellm` to the `postgres-backup` `databases.yml` list.

### 2.3 UI — Open WebUI

- `chat.blackcats.cc`, `DATABASE_URL` → CNPG (SQLite will not survive on NFS).
- OIDC → Zitadel via the Terraform bootstrap, like every other service. Redirect URI is
  `https://chat.blackcats.cc/oauth/oidc/callback`.
- **Set `ENABLE_OAUTH_PERSISTENT_CONFIG=false`.** It defaults to *true*, which copies
  OAuth settings into the database on first boot and then **ignores the environment** —
  git stops being the source of truth and later credential rotations silently do nothing.
  Same class of trap as the Gitea `redis-cluster.enabled` no-op.

### 2.4 Observability

llama-swap and `llama-server` both expose `/metrics`. Useful series:
`llamacpp:prompt_tokens_total`, `llamacpp:prompt_seconds_total`,
`llamacpp:tokens_predicted_total`, `llamacpp:predicted_tokens_seconds`.

**Scrape-label gotcha:** the `VMServiceScrape` must select on
`monitoring.blackcats.cc/scrape: llama-swap` (and `: litellm`), **never**
`app.kubernetes.io/name` — Flux `commonMetadata` rewrites that label across the whole
Kustomization, so the selector would match the wrong Services or none.

Alerts worth having from day one, routed to Gotify through the existing path: backend
unreachable, decode rate collapsed below a floor, model-load failure, `llm-1` memory
pressure, PVC nearly full.

**Langfuse: defer.** v3 needs langfuse-web, langfuse-worker, Postgres, ClickHouse, Redis
*and* an S3-compatible store — roughly 9 vCPU / 25 GiB, comparable to the entire rest of
the homelab, for prompt tracing. LiteLLM's Postgres request log covers the need for now.

---

## 3. Service inventory — what runs where

Six deployable units across two namespaces. Only one of them touches `llm-1`.

| Service | Namespace | Node | Replicas | CPU req/lim | Mem req/lim | Storage | Exposed as |
|---|---|---|---|---|---|---|---|
| **llama-swap** | `ai` | **`llm-1` only** | 1 | 4 / 6 | 46Gi / **none** | `llama-models` 150Gi `openebs-hostpath` | ClusterIP `llama-swap:8080` |
| **model-fetch** | `ai` | `llm-1` (Job) | — | 500m / 2 | 256Mi / 512Mi | mounts the same PVC | — |
| **litellm** | `ai` | any CP | 1 | 200m / 1 | 512Mi / 1Gi | none (CNPG) | `llm.blackcats.cc` |
| **open-webui** | `ai` | any CP | 1 | 200m / 2 | 1Gi / 3Gi | `open-webui-data` 20Gi `nfs-client` | `chat.blackcats.cc` |
| *ai-database* | `postgres` | — | — | — | — | — | two SealedSecrets + managed roles |
| *ai-monitoring* | `monitoring` | — | — | — | — | — | scrape + rules + dashboard |

**Added to the control planes: 400m CPU and 1.5Gi memory of requests, total.** The serving
layer is genuinely small; everything expensive is on `llm-1` behind a taint.

The `ai` namespace takes **no PSA label** — cluster-default `baseline` is sufficient,
nothing here needs privileged. (See the `--mlock` note below for the one thing that would
change that, and why we are not doing it.)

### 3.1 `llama-swap` — the only workload on `llm-1`

```yaml
nodeSelector:
  homelab.blackcats.cc/workload: llm
tolerations:
  - key: workload
    operator: Equal
    value: llm
    effect: NoSchedule
```

Three deployment details that are not obvious:

**`strategy: Recreate`, not RollingUpdate.** The PVC is `openebs-hostpath` (node-local,
RWO) and the model is 36.9 GB. A rolling update would try to schedule a second pod that
cannot bind the volume, and would attempt to load a second 37 GB model on a 64 GB node
if it could. Recreate is correct here.

**No memory limit — deliberately, and for a different reason than cilium's.** llama.cpp
**mmaps** the GGUF, and mmap'd file pages count toward the cgroup's `memory.current` as
*reclaimable page cache*. A limit near the working set therefore does not produce a clean
OOMKill; it produces continuous reclaim-and-refault against the NVMe, which presents as
"the model got mysteriously slow" with no event, no restart and nothing in the logs. The
node is dedicated and tainted, so there is nothing to protect it from. Set the **request**
to reserve the node and omit the limit.

**Do not reach for `--mlock`.** It would pin the weights and sidestep the above, but it
needs the `IPC_LOCK` capability, which PSA `baseline` does not permit — so it would force
the whole `ai` namespace to `privileged` to solve a problem a dedicated node does not
have.

Also: `reloader.stakater.com/auto: "true"` goes on the **Deployment's own
`metadata.annotations`**, not `spec.template.metadata.annotations`. On the pod template it
is a silent no-op, and a config change would simply never take effect.

Probes go on `/v1/models`. llama-swap is a proxy — it answers immediately while models
load lazily on first request — so ordinary probe timings work and the pod does not sit
unready for minutes waiting on a 37 GB load.

Service carries `monitoring.blackcats.cc/scrape: llama-swap`; `/metrics` is on the same
port, so one named port `http` serves both.

### 3.2 `model-fetch` — a Job, not a service

Runs on `llm-1` because `openebs-hostpath` is node-local: the Job must land on the same
node as the Deployment to write the same directory.

Use **`ghcr.io/lucid-void/backup-tools`** and plain `curl` against
`https://huggingface.co/<repo>/resolve/main/<file>` — it already has bash and curl, so
there is **no runtime `apk add`** and none of the unbounded-install hazard documented in
`.claude/CLAUDE.md`. Idempotent: skip any file already present at the expected size.

Carries `ttlSecondsAfterFinished: 86400` plus
`ignore-check.kube-linter.io/job-ttl-seconds-after-finished` on **top-level** metadata,
matching every other CronJob/Job in this repo. **Job specs are immutable** — editing this
manifest while the previous Job still exists makes the Flux dry-run fail with "field is
immutable"; delete the Job by hand and let Flux recreate it.

### 3.3 `litellm`

Model list maps friendly names onto llama-swap entries over the cluster Service:

```yaml
model_list:
  - model_name: local-smart
    litellm_params:
      model: openai/qwen-smart
      api_base: http://llama-swap.ai.svc.cluster.local:8080/v1
      api_key: none
```

Secrets: `litellm-secret` (sealed — `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`) and
`litellm-role-secret`, mirrored from the `postgres` namespace by Reflector. One virtual
key per consumer.

**Verify the metrics endpoint before wiring the scrape.** LiteLLM has moved Prometheus
metrics behind its enterprise tier in some releases. If it is gated on the pinned version,
drop the `litellm` VMServiceScrape and rely on llama-swap's `/metrics` plus LiteLLM's
Postgres request log — do not leave a scrape pointing at a 404.

### 3.4 `open-webui`

**Turn off the built-in RAG embedder.** By default Open WebUI downloads a
sentence-transformers model at first boot and runs embeddings **on the CPU of whichever
control plane it lands on** — a silent, recurring tax on nodes that are also running
etcd. Point it at the cluster instead:

```yaml
RAG_EMBEDDING_ENGINE: openai
RAG_OPENAI_API_BASE_URL: http://litellm.ai.svc.cluster.local:4000/v1
RAG_EMBEDDING_MODEL: local-embed
```

That requires a **fourth llama-swap entry** — a small GGUF embedding model
(`nomic-embed-text-v1.5`, ~250 MB) — and it must sit in a **separate llama-swap group**
so it stays resident concurrently. Left in the default group, every embedding call would
evict the 36.9 GB chat model and reload it afterwards.

Other required env: `DATABASE_URL` → CNPG, `OPENAI_API_BASE_URL` → LiteLLM, and
`ENABLE_OAUTH_PERSISTENT_CONFIG=false` (see §2.3 — the default silently freezes OAuth
config into the database and ignores git thereafter).

### 3.5 `ai-database` and `ai-monitoring`

`ai-database` holds only the two SealedSecrets carrying the Reflector annotations; the
**managed roles themselves belong in `apps/postgres/cluster/app/cluster.yml`**, since
that is where the CNPG `Cluster` object lives.

`ai-monitoring` follows the `minecraft-monitoring` precedent exactly: the CRs live in the
`monitoring` namespace and reach across with `namespaceSelector`.

```yaml
apiVersion: operator.victoriametrics.com/v1beta1
kind: VMServiceScrape
metadata:
  name: llama-swap
  namespace: monitoring
spec:
  namespaceSelector:
    matchNames: [ai]          # required — without it the selector silently matches nothing
  selector:
    matchLabels:
      monitoring.blackcats.cc/scrape: llama-swap
  endpoints:
    - port: http
      path: /metrics
      interval: 60s
```

Alerts: backend unreachable, `llama-swap` pod not ready, decode rate below floor, model
load failure, `llm-1` memory pressure, `llama-models` PVC nearly full. Note the repo's
standing caveat that `kubelet_volume_stats_*` reports the **backing filesystem** for
`openebs-hostpath`, so a PVC-full alert here is really an `llm-1` `/var` alert — size it
against the node disk, not the 150Gi claim.

### 3.6 Explicitly not deployed

| Not deploying | Why |
|---|---|
| Langfuse | v3 is six services, ~9 vCPU / 25 GiB — see §2.4 |
| A separate embeddings service | a llama-swap group does it for ~250 MB |
| `qwen-fast` / `qwen-vision` at first | Phase 3 ships `qwen-smart` alone; add once measured |
| GPT-OSS-120B | 71.5 GPQA — bigger, slower and less capable than the chosen model |
| Any HTTPRoute for llama-swap | LiteLLM is its only client; the `/ui` stays cluster-internal |

---

## 4. Repo layout

Follows the existing `apps/<ns>/<app>/{ks.yml,app/}` convention exactly.

```
kubernetes/apps/ai/
├── kustomization.yml              # namespace + the four ks.yml
├── namespace.yml
├── database/                      # CNPG roles + Database CRs for litellm, open-webui
│   ├── ks.yml
│   └── app/{kustomization.yml,litellm-role-sealed.yml,openwebui-role-sealed.yml}
├── llama-swap/
│   ├── ks.yml                     # dependsOn: openebs
│   └── app/
│       ├── kustomization.yml
│       ├── helmrelease.yml        # bjw-s app-template
│       ├── config-configmap.yml   # the YAML in §2.1
│       ├── pvc.yml                # openebs-hostpath, 150Gi
│       ├── model-fetch-job.yml
│       └── vmservicescrape.yml
├── litellm/
│   ├── ks.yml                     # dependsOn: llama-swap, postgres-cluster, sealed-secrets
│   └── app/
│       ├── kustomization.yml
│       ├── helmrelease.yml
│       ├── config-sealed.yml      # virtual keys, master key
│       ├── route.yml              # llm.blackcats.cc
│       └── vmservicescrape.yml
└── open-webui/
    ├── ks.yml                     # dependsOn: litellm, zitadel-bootstrap
    └── app/{kustomization.yml,helmrelease.yml,pvc.yml,route.yml}
```

**app-template naming trap:** a single controller named `app` produces a Deployment and
Service named `{release-name}` with **no suffix**. So the Service is `llama-swap`, not
`llama-swap-app`. Verify with `kubectl get deploy -n ai` before writing any
`backendRef` or RBAC `resourceNames`.

Also touched:

| File | Change |
|---|---|
| `infra/terraform/kubernetes.tf` | add `llm-1` (vm_id 2023, ip_last 14, 64 GB, 250 GB disk); reduce cp-1/2/3 memory to 30720 |
| `kubernetes/talos/talconfig.yaml` | add `llm-1` node with `controlPlane: false` + the `workload=llm:NoSchedule` taint and `node-role: llm` label |
| `kubernetes/apps/postgres/cluster/app/cluster.yml` | add `litellm` and `openwebui` managed roles |
| `kubernetes/apps/postgres/backup/app/...` | add `litellm`, `openwebui` to `databases.yml` |
| `infra/terraform/` (Zitadel) | register the Open WebUI OIDC app |
| `kubernetes/flux/.../apps.yaml` | nothing — `apps/ai/` is picked up by the existing tree |

---

## 5. Host-level changes

**vCPU is already oversubscribed.** Three VMs × 8 vCPU = 24 on 14 physical cores.
Adding `llm-1` at 8 makes it 32 on 14 — a 2.3× commit, and the LLM is the workload most
hurt by scheduler contention.

Recommended alongside the RAM change:

- **Reduce cp-1/2/3 to 4 vCPU each.** Their real load is light; 12 + 8 = 20 vCPU on 14
  cores is a much healthier ratio.
- **Pin `llm-1` to the 6 P-cores** via Proxmox CPU affinity. Arrow Lake-HX is 6 P + 8 E,
  and `--threads 6` above assumes the LLM actually gets those P-cores.
- **Verify memory clock and XMP.** The board is 4 × DDR5 SODIMM but Arrow Lake-HX is
  **dual-channel** regardless of slot count, rated 4800. If four slots are populated it
  likely downclocks to 4000–4400. Since decode is bandwidth-bound, that is roughly 30% of
  your token rate — and XMP left off is another silent ~20%. **Check this first; it is
  free.**

---

## 6. Expectations

Rewritten 2026-08-30 against the deployed stack. The original table assumed three models
(`qwen-fast`, `qwen-vision`) that were never deployed.

| Use case | Model | Workable? |
|---|---|---|
| Considered questions where the answer matters | `local-smart` | **yes** — 86.0 GPQA at Q8, near-lossless |
| Open WebUI chat | `local-smart` | **yes** — 8.4–8.9 tok/s is around reading speed, and thinking is visible as `reasoning_content` |
| Bots, notifications | either | **yes** — async, latency irrelevant |
| Summarisation, RSS triage, tagging | `local-fast` | **yes** — batch, and thinking is pure overhead for these |
| Code chat, focused small-context work | `local-fast` | **yes** |
| Vision (Paperless / Immich) | — | **not deployed.** Available for ~0.8 GiB via `--mmproj` if ever wanted |
| Code autocomplete | `local-fast` | **marginal** — needs tight context |
| opencode / large-context agentic loops | — | **no.** See below |

**Thinking, not prefill, is the cost that was underestimated.** Qwen3.6 is a reasoning
model. Measured on *"Reply with exactly: ok"*: `local-smart` emitted 572 characters of
`reasoning_content` and 152 output tokens (~18 s at 8.4 tok/s); `local-fast`, the same
weights with `enable_thinking: false`, emitted 0 and 2 (~0.2 s). That overhead is **per
turn**, so it compounds across a tool-calling loop far more than across a chat reply.

**Agentic coding on large contexts still does not work here, and one reason is worse than
this document assumed.** Agent workloads are prefill-dominated, and prefill is
compute-bound — exactly where the missing AVX-512/AMX hurts. The original text claimed
`--cache-reuse` "helps follow-up turns enormously"; **it does not run at all** with this
model, so there is no mid-prompt cache recovery. What remains is llama-server's
cross-request prompt cache (`--cache-ram`, 8 GiB by default), which only helps an *exact*
shared prefix.

The practical split: `local-fast` for scoped, small-context agent work; treat a refactor
that re-reads a large codebase each turn as the case this hardware does not serve. There
is deliberately no cloud fallback configured — see llm-deployment.md §7.

---

## 7. Rollout

**Phase 1 — verify the host. No deployment.** DIMM count, actual memory clock, XMP state,
free space on the LVM-thin pool for a 250 GB disk. Everything below scales off the first
three, and the fourth is a hard prerequisite.

**Phase 2 — control plane, no cluster changes.** LiteLLM + Open WebUI + Zitadel OIDC +
monitoring, pointed at a cloud backend. Proves routing, SSO, metrics and the HTTPRoute
end to end for ~1.2 vCPU / 3.5 GiB, before any node work.

**Phase 3 — `llm-1`.** New VM via OpenTofu, `controlPlane: false`, tainted. Rebalance
cp-1/2/3 to 30 GB / 4 vCPU. Deploy llama-swap with **`qwen-fast` only** and confirm the
path works end to end.

**Phase 4 — the model set, and three benchmarks.** Add `qwen-smart` (Q8_0) and
`qwen-vision`, then measure real tok/s for each — every rate in this document is an
estimate scaled from published benchmarks on comparable hardware, not a measurement on
this box. Three things worth benchmarking while the harness is set up, keeping only what
wins:

- **`ik_llama.cpp`** — the fork with materially better CPU MoE performance.
- **Qwen3-Next-80B-A3B** at Q4_K_M (51 GB) — fits resident, but a generation older.
- **Qwen3.8-Flash-Next at UD-IQ3_XXS (82 GB) via mmap overflow**, per the correction in
  §1. ~22 GB of paging against NVMe. If it lands above ~1 tok/s it is a viable overnight
  batch tier; if it thrashes, the question is closed with data rather than assumption.

**Phase 5 — tune.** `--cache-reuse` validation, thread count against measured P-core
availability, `--ctx-size` sized to real usage, Q6_K vs Q8_0 if Q8 feels too slow.

---

## 8. Considered: disk-streaming a frontier model

Prompted by [FareedKhan-dev/kimi-k3-in-c](https://github.com/FareedKhan-dev/kimi-k3-in-c),
which is worth understanding because it attacks the same constraint from the opposite
direction — and because it reframes what "fits" means.

**What it is.** A portable C99 implementation (no BLAS, no framework, no GPU) that runs
**Kimi K3** — Moonshot's 2.8T-parameter MoE, open-weighted 2026-07-27 under a Modified
MIT licence, scoring **60 on the Artificial Analysis Intelligence Index, within three
points of Claude Opus 5** — on consumer hardware, by streaming weights from disk instead
of holding them in RAM.

**How.** ~113 GB is always resident (93 dense trunk layers, 2 shared experts, embeddings,
attention projections) while **1.45 TB of routed experts is streamed** from a 1.56 TB
checkpoint, never dequantized, multiplied directly from packed MXFP4 nibbles. The trunk
cycles through a **pinned-prefix + ring buffer** rather than an LRU — deliberately,
because LRU on sequential layer access has a pathology where layer 0 re-enters as
least-recently-used every cycle. Sustained I/O is measured at **5.4–6.1 GB/s using
`O_DIRECT`**.

**Its own benchmark table, against our exact sizing:**

| RAM | s/token | tok/s | I/O share |
|---|---|---|---|
| 8 GB | 26.5 | 0.038 | 80% |
| 32 GB | 24.2 | 0.041 | ~70% |
| **64 GB — our worker** | **19.8** | **0.051** | ~65% |
| 128 GB+ | 5.6 | 0.179 | ~40% |

**Why it is not the build.** At our 64 GB it is **19.8 seconds per token** — roughly
**100–160× slower** than the ~5–8 tok/s of `qwen-smart`. A 500-token answer takes
**2 h 45 m**. Beyond speed: it needs **~1.7 TB of fast local NVMe** (5–6 GB/s sustained;
this cannot run from the Synology over NFS, which is ~50× too slow), and it is a
**standalone single-user CLI binary with no HTTP server and no API** — so it cannot sit
behind LiteLLM, cannot serve Open WebUI, and has no concurrency. The author is explicit
that it is educational, not production.

**What is worth taking from it.**

1. **Model size is bounded by disk, not RAM; RAM buys speed.** Its output is
   *byte-identical* across every memory tier — only the clock changes. That is the
   principle behind the correction in §1: our residency ceiling is a performance
   decision, not a feasibility one.
2. **Asymmetric quantization is right.** 4-bit experts against an unquantized trunk is
   the same instinct as Unsloth's dynamic UD-* quants keeping embeddings and output
   weights at Q8 while compressing the rest — which is exactly why §1 specifies UD
   quants rather than plain `Q4_K_M`.
3. **Fixed-size recurrent state.** Kimi Delta Attention holds a 626 MB state
   *independent of sequence length*, the same family of idea as the GDN hybrid attention
   in Qwen3.6/3.8 — and the reason the KV budget in §1 is only 4–6 GB rather than tens.
4. **Pinned prefix beats LRU for sequential layer access.** Worth remembering if we ever
   tune `madvise`/mmap behaviour for an oversized model.

**If Kimi K3 is genuinely wanted**, the real path is not this repo — it is **vLLM**,
which shipped production KDA support at release. That needs roughly 1.4 TB of accelerator
memory for the MXFP4 checkpoint, which is far outside this homelab. Route it through
LiteLLM as a cloud endpoint instead; the fallback chain already supports exactly that.

---

## 9. Open questions

1. **`llm-1` at 70 GB or 64?** 160 GB is available and 154 is assigned. 70 keeps all
   three models co-resident; 64 makes `qwen-vision` swap. Recommended: 70.
2. **Free space on the Proxmox LVM-thin pool** for a 250 GB `llm-1` disk. Not verifiable
   from here — SSH to `172.16.20.3` was refused.
3. **Is reducing cp-1/2/3 to 4 vCPU acceptable?** It is the right call for LLM
   throughput, but it is a change to working control planes and worth an explicit yes.
4. **"hermes bot" — which one?** Interpreted throughout as a chat bridge (Telegram /
   Matrix / Discord) onto LiteLLM. If it means a Nous Hermes *model*, note Hermes
   releases are typically **dense**, which is the wrong architecture here — a 27B dense
   model is slower than a 120B sparse one on this box. If it is a bot, follow the
   `media/minecraft-events` pattern: optional mounted secret re-read per use, not
   `envFrom`, so it self-heals without a restart.
5. **"deepseek harness"** — Open WebUI is specified above because its native OIDC fits
   the Zitadel model. Confirm whether a specific alternative harness is wanted.
6. **`.claude/CLAUDE.md:184` says the host is an MS-A2.** It is an **MS-02 Ultra** with
   an Intel Core Ultra 5 235HX; MS-A2 is the AMD Ryzen 9 9955HX line. Worth correcting —
   the CPU identity underpins every performance number here.

---

## Sources

- [llama-swap — config schema, CPU image, endpoints](https://github.com/mostlygeek/llama-swap)
- [llama-server manual — `--metrics`, `--cache-reuse`](https://manpages.debian.org/testing/llama.cpp-tools/llama-server.1.en.html)
- [llama.cpp issue #19480 — CPU MoE inefficiency, `ik_llama.cpp`](https://github.com/ggml-org/llama.cpp/issues/19480)
- [unsloth/Qwen3.6-35B-A3B-GGUF — quant sizes](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF)
- [Qwen/Qwen3.8-27B — dense vision-language, Apache-2.0](https://huggingface.co/Qwen/Qwen3.8-27B)
- [unsloth/Qwen3.8-Flash-Next-GGUF — quant sizes (why it does not fit)](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF)
- [Open WebUI SSO / OAuth configuration](https://docs.openwebui.com/features/authentication-access/auth/sso/)
- [FareedKhan-dev/kimi-k3-in-c — disk-streaming CPU inference in C99](https://github.com/FareedKhan-dev/kimi-k3-in-c)
- [Kimi K3 — 2.8T open-weight MoE, self-hosting requirements](https://northflank.com/blog/what-is-kimi-k3-self-hosting)
- [MINISFORUM MS-02 Ultra — 4 × DDR5 SODIMM, dual-channel](https://store.minisforum.com/products/minisforum-ms-02-ultra-workstation)
