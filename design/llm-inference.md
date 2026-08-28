# Local LLM inference — design proposal

**Date:** 2026-08-28
**Status:** **PROPOSED — not implemented.** Nothing in this document is deployed.
**Constraint:** CPU + RAM only, on the existing MS-02 Ultra. No GPU, no DGX Spark.
**Budget:** 150 GB RAM across the Kubernetes VMs.
**Goal:** Serve a useful local LLM to Open WebUI, bots and tooling, with routing,
observability and SSO consistent with the rest of the homelab.

---

## Verdict

| Question | Answer |
|---|---|
| Qwen3.8-Flash-Next on CPU? | **No.** Even the 1-bit quant is a bad trade — see §Why not. |
| Any other Qwen3.8 model? | **One.** Qwen3.8-27B, as a batch *vision* model — not for chat. |
| Is a *genuinely useful* local LLM possible on this box? | **Yes.** ~10–15 tok/s, good quality. |
| Interactive chat, bots, summarisation, RSS triage? | **Yes** — comfortably. |
| opencode / agentic coding? | **No.** Prefill-bound; minutes to first token. |
| Does the serving stack fit the cluster today? | **Yes** — ~1.2 vCPU / 3.5 GiB. |

**The headline:** the best model for this hardware is about **22 GB**, not 150 GB. Your
binding constraint is memory *bandwidth*, not memory *capacity* — so the interesting
question is not "how do I spend 150 GB" but "how do I stop wasting bandwidth".

---

## The one number that governs everything

Decode speed on CPU is memory-bandwidth-bound. Every token requires reading all active
weights from RAM, so:

```
tokens/sec  ≈  usable_bandwidth  ÷  (active_params × bits_per_weight ÷ 8)
```

The MS-02 Ultra has **4 × DDR5 SODIMM slots and takes 256 GB** — but Arrow Lake-HX has
a **dual-channel** memory controller regardless of slot count. Four slots means two
DIMMs per channel, not four channels. Minisforum rates the board at **DDR5-4800**.

| | Theoretical | Realistically usable |
|---|---|---|
| DDR5-4800, dual channel | 76.8 GB/s | ~50–60 GB/s |
| DDR5-5600, dual channel (2 DIMMs only) | 89.6 GB/s | ~60–70 GB/s |
| 4 DIMMs populated, likely downclock to 4000–4400 | 64–70 GB/s | ~45–55 GB/s |

Two things make it worse than the raw figure:

1. **All three VMs share one memory controller on one host.** Cluster-aggregate
   bandwidth is ~55 GB/s, not 3 × 55. Splitting a model across nodes with llama.cpp RPC
   gains nothing and adds network latency — **distributed inference is pointless here.**
2. **No AVX-512, no AMX.** Measured flags on cp-1 are `avx avx2 avx_vnni` only. This
   barely affects decode (bandwidth-bound) but **hits prefill hard** (compute-bound).

### The cheapest win available: check your DIMM configuration

If the 150 GB figure comes from **4 × 48 GB**, the board is very likely clocking memory
down to 4000–4400 MT/s, costing roughly **30% of your token rate** — permanently, on
every request.

Because the model worth running is ~22 GB, **capacity is not the scarce resource**.
A 2 × 64 GB configuration at 5600 would give 128 GB total (~100 GB assignable to VMs,
still plenty) at meaningfully higher bandwidth. Verify before doing anything else:

- Confirm the installed DIMM count and actual clock in BIOS.
- **Confirm XMP/EXPO is enabled** — running at JEDEC default is a common silent
  ~20% loss, and showed up as a measurable difference in published GPT-OSS benchmarks.

This is a free or cheap change that improves every model equally. Do it first.

---

## What to actually run

Pick by **active parameters**, not total size. MoE models with ~3B active are the
sweet spot: they read little per token but carry the knowledge of a much larger model.

| Model | Total / active | Q4 size | Est. decode | Verdict |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** | 35B / 3B | **~22 GB** | **~10–15 tok/s** | **primary — best fit by a wide margin** |
| Qwen3.6-27B (dense) | 27B / 27B | ~18 GB | ~1.5–2 tok/s | avoid — dense means *all* params read per token |
| GPT-OSS-120B | 120B / 5.1B | ~63 GB (MXFP4) | ~5–7 tok/s | optional second tier, strong quality |
| Qwen3-Next-80B-A3B | 80B / 3B | ~51 GB | ~4–6 tok/s | exotic arch, poorly optimised on CPU |
| **Qwen3.8-27B** | 28B / 28B **dense**, vision | ~17.9 GB | ~1.5–2.5 tok/s | **yes, but only for batch vision** |
| Qwen3.8-Flash-Next | 125B / 6B | ≥72.5 GB | ~2–3 tok/s | **no** — see below |
| Qwen3.8-2.4T-A95B ("Max") | 2.4T / 95B | ~1.2 TB | <1 tok/s | **no** — fails twice over |

Qwen3.6-35B-A3B is Apache-2.0, 262K context, and widely reported as the best
general-purpose option on modest hardware. At ~22 GB it leaves enormous headroom for
KV cache and a draft model.

The dense row is there to make the point concrete: a 27B **dense** model is *slower*
than a 120B **sparse** one on this box, despite being a quarter the size on disk. Total
parameters tell you almost nothing about CPU speed.

### Why not Qwen3.8-Flash-Next

It is not a plain 125B. Per the [merged llama.cpp PR](https://github.com/ggml-org/llama.cpp/pull/27742)
(arch `qwen4exp`, 2026-08-27) it is a 125B/6B-active MoE **plus a 97.7 GiB per-layer
N-gram hash embedding table** plus a 4B MTP head. Consequences:

- Smallest quant is **72.5 GB** (IQ1_S); the smallest *quality* quant is **93.7 GB**.
- The N-gram table is random-access gathers, which is latency-bound — worse on CPU than
  the sequential streaming that MoE bandwidth math assumes.
- 6B active is **double** Qwen3.6-35B-A3B's, so it is inherently ~2× slower per token.
- llama.cpp issue [#19480](https://github.com/ggml-org/llama.cpp/issues/19480) measures
  the closely-related Qwen3-Next-80B at **7.74 tok/s** on *better* hardware (Zen 5 with
  AVX-512, DDR5-5600) against a 20–30 expectation. The issue is open and unresolved.

At IQ1_S you would spend 72.5 GB of your 150 GB to get ~2–3 tok/s from a model degraded
to 1-bit weights — worse in every dimension than Qwen3.6-35B-A3B at 22 GB. Revisit only
if a CPU-side MoE optimisation lands upstream.

### The rest of the Qwen3.8 family

Three Qwen3.8 checkpoints have open weights. Only one fits, and it is not the obvious one.

**Qwen3.8-2.4T-A95B ("Max")** — open-weighted 2026-08-12, the first Max-class Qwen you
can download. It is out by roughly **8× on capacity alone**: 2.4T parameters is about
1.2 TB at Q4 against your 150 GB. It would not help if it fit — 95B active parameters
means ~48 GB read per token, i.e. **under 1 tok/s** on this memory bus. Genuinely
impressive, entirely irrelevant to this machine.

**Qwen3.8-27B** is the interesting one. Apache-2.0, 262K native context, and a *native
vision-language model* covering images and hour-scale video. Unsloth's UD-Q4_K_XL is
**17.9 GB**, plus a ~0.9 GB `mmproj` file for the vision tower — so it fits with room to
spare, unlike everything else in the 3.8 line.

The catch is that it is **dense**: all 28B parameters are read for every token. Put the
two recommendations side by side:

| | On disk | Active per token | Est. decode |
|---|---|---|---|
| Qwen3.6-35B-A3B | 22 GB | 3B | **~10–15 tok/s** |
| Qwen3.8-27B | 18 GB | 28B | ~1.5–2.5 tok/s |

The **smaller** model is roughly **six times slower**. This is the sparse/dense lesson
made concrete on two models that look interchangeable in a download list.

**So run it — but not as the chat model.** Vision work in this homelab is batch and
latency-tolerant, and the outputs are short: Immich photo tagging and semantic search,
Paperless document understanding, screenshot/diagram OCR. A 50-token caption at 2 tok/s
is 25 seconds, which is fine for an overnight job and useless for a chat window.

Budget extra time for **image prefill**, not just decode. An image expands to hundreds
or thousands of tokens through the vision tower, and prefill is exactly where the
missing AVX-512 hurts most. Measure a representative image before sizing any batch job.

---

## Where it runs — a dedicated worker, not a control plane

Two constraints from the existing cluster are unchanged and both are hard:

**etcd.** All three nodes are control planes with `allowSchedulingOnControlPlanes: true`.
A large mmap'd model evicts etcd's page cache; memory pressure triggers kubelet eviction
cascades; etcd is fsync-latency-sensitive, so pressure causes leader-election churn, and
losing a second member during that churn is quorum loss. **Model inference must not run
on an etcd member.**

**Disk.** Node disks are 100 GB (`infra/terraform/kubernetes.tf:28`) and the kubelet
garbage-collects images at 70% (`kubernetes/talos/talconfig.yaml:39`), leaving ~70 GB
usable. Fine for a 22 GB model, not for a 63 GB one alongside Talos and containerd.

So: **add a fourth VM.** Not a control plane, not an etcd member, tainted so nothing
else lands on it.

```
llm-1   172.16.20.14   8 vCPU   see RAM plan   250 GB disk
        controlPlane: false
        taint: workload=llm:NoSchedule
```

This also isolates the blast radius: an OOM on `llm-1` costs you the LLM, not the
cluster.

### RAM plan

| | cp-1 | cp-2 | cp-3 | llm-1 | Total | Runs |
|---|---|---|---|---|---|---|
| Today | 32 | 32 | 32 | — | 96 | nothing |
| **A — recommended** | **28** | **28** | **28** | **64** | **148** | 35B-A3B + Qwen3.8-27B vision, both resident |
| B — minimal change | 32 | 32 | 32 | 48 | 144 | either model, one at a time |
| C — maximal | 24 | 24 | 24 | 76 | 148 | adds GPT-OSS-120B as a third, quality tier |
| D — fast RAM (2 DIMMs) | 22 | 22 | 22 | 56 | 122 | both, ~30% faster than A |

Current real usage is ~9–13 GiB per node (cp-1 reports 22.5 GiB available of 31.3 GiB),
so 24 GB control planes are comfortable. Note their current *limit* sums are 20.6 /
24.2 / 35.1 GiB — cutting below 24 GB starts to erode the ability to drain a node.

**Option A is now the recommendation** rather than B: holding Qwen3.6-35B-A3B (22 GB)
and Qwen3.8-27B + mmproj (18.8 GB) resident together is ~41 GB of weights, and a 64 GB
worker covers that plus KV caches and the OS with headroom. Keeping both loaded matters
because a cold reload of either is tens of seconds off local disk. Option B still works
if you would rather swap models on demand via `llama-swap`.

---

## Architecture

```
  CONSUMERS                    CONTROL PLANE (k8s, ns: ai)         BACKEND (llm-1, tainted)
  ─────────                    ───────────────────────────         ────────────────────────

  Open WebUI ──┐
  chat.…cc     │                ┌──────────────────────┐    ┌──► llama.cpp · "fast"
               │                │  LiteLLM router      │────┤    Qwen3.6-35B-A3B Q4
  bots / RSS ──┼───────────────►│  · virtual keys      │    │    + draft model
  hermes bot   │   llm.…cc      │  · budgets           │    │    ~10–15 tok/s
               │   OpenAI API   │  · model tiers       │    │
  API clients ─┤                │  · fallback chain    │    ├──► llama.cpp · "quality"
               │                │  · /metrics          │    │    GPT-OSS-120B MXFP4
  opencode ────┘                └──────────┬───────────┘    │    ~5–7 tok/s  (optional)
  (degraded — see below)                   │                │
                                   ┌───────┴────────┐       └──► cloud API
                                   ▼                ▼            (optional overflow)
                            LiteLLM Postgres   VictoriaMetrics
                            request log        → Grafana → Gotify
```

Everything in the middle column is small and fits the cluster **as it stands today** —
roughly 1.2 vCPU / 3.5 GiB. LiteLLM is the seam: because every consumer talks to it and
nothing else, models can be swapped, added or retired without touching a single client.

### Components

| Component | Namespace | Notes |
|---|---|---|
| **llama.cpp server** | `ai` | on `llm-1`, `nodeSelector` + toleration. One Deployment per resident model. |
| **LiteLLM** | `ai` | router, `llm.blackcats.cc`, CNPG-backed |
| **Open WebUI** | `ai` | `chat.blackcats.cc`, OIDC → Zitadel |
| **hermes bot** | `ai` | chat bridge → LiteLLM *(see open questions)* |
| **model weights** | — | `openebs-hostpath` PVC on `llm-1`, **not** NFS |

Model weights must be on local disk. A 22 GB mmap over NFS is a slow cold start and
leaves you dependent on page cache staying resident; `openebs-hostpath` already has the
`extraMounts` patch in `talconfig.yaml`.

### Integration notes specific to this repo

- **LiteLLM Postgres** via the shared CNPG cluster — add a `litellm` managed role plus a
  `litellm-role-secret` SealedSecret mirrored by Reflector, per `design/docs/secrets.md`.
  Expect the **CNPG managed-role race** documented in `.claude/CLAUDE.md` on first deploy.
- **Scrape label:** the `VMServiceScrape` must select on
  `monitoring.blackcats.cc/scrape: litellm`, never `app.kubernetes.io/name` — Flux
  `commonMetadata` rewrites the latter across a whole Kustomization.
- **Open WebUI:** set `ENABLE_OAUTH_PERSISTENT_CONFIG=false`. It defaults to *true*,
  which copies OAuth settings into the database on first boot and then **ignores the
  environment** — git stops being the source of truth and later credential rotations
  silently do nothing. Same class of trap as the Gitea `redis-cluster.enabled` no-op.
- **Backups:** add `litellm` to the `postgres-backup` `databases.yml` list. Model weights
  are re-downloadable and should not be backed up.
- **Langfuse: defer it.** v3 needs langfuse-web, langfuse-worker, Postgres, ClickHouse,
  Redis *and* an S3-compatible store — roughly **9 vCPU / 25 GiB**, comparable to the
  entire rest of the homelab, for prompt tracing. Start with LiteLLM's Postgres request
  log scraped into the existing VictoriaMetrics → Grafana → Gotify path.

---

## Getting more out of the hardware

Ordered by payoff. The first three are free.

1. **Enable XMP and verify the memory clock.** See §The cheapest win. Up to ~30%.
2. **Prompt cache reuse** (`--cache-reuse`). The single biggest lever for anything with
   a stable prefix — bots, templated prompts, repeated system messages. llama.cpp keeps
   the KV cache for a shared prefix and processes only the new suffix. Keep the prompt
   front byte-identical: **a timestamp in a system prompt invalidates everything
   downstream.**
3. **Pin the LLM VM to the P-cores.** Arrow Lake-HX is 6 P + 8 E. The three existing VMs
   already claim 24 vCPU on 14 physical cores — a 1.7× commit before any LLM lands.
   Adding `llm-1` makes it worse unless you set CPU affinity in Proxmox and reduce the
   control planes' vCPU count. E-cores contribute little and can actively hurt: the
   reference benchmark measured 12 cores at 3.54 tok/s *degrading* to 2.84 with 24
   threads.
4. **Speculative decoding** (`-md <draft> --draft-max 16 --draft-min 4`). A small draft
   model proposes tokens, the main model verifies in one pass — **1.5–3× on decode with
   bit-identical output**. Works best on predictable text (code, boilerplate). The draft
   model must share the target's tokenizer. Cheap in RAM, and you have plenty.
5. **8-bit KV cache and flash attention.** Halves KV footprint, speeds long-context
   prefill.
6. **Size `--ctx-size` honestly.** KV cache for the whole window is pre-allocated at
   startup. Allocating 128K when you use 8K wastes RAM you could spend on a better quant.

---

## Honest verdict per use case

| Use case | Workable? | Why |
|---|---|---|
| Open WebUI chat | **yes** | 10–15 tok/s is above comfortable reading speed |
| Bots, notifications, hermes | **yes** | async; latency is irrelevant |
| RSS/document summarisation, tagging | **yes** | batch work, runs overnight |
| Paperless / Immich enrichment | **yes** | Qwen3.8-27B vision, batch — short outputs hide the low tok/s |
| Code autocomplete (short context) | **marginal** | needs a small draft-class model and tight context |
| **opencode / agentic coding** | **no** | see below |

**Why agentic coding does not work here.** Agent workloads are *prefill-dominated* — the
prompt is enormous and the reply is often a short tool call. Prefill is compute-bound,
which is exactly where the missing AVX-512/AMX hurts most. Expect roughly 50–80 tok/s
prefill, so a cold 20K-token context costs **4–7 minutes before the first token**, on
every context that is not a cache hit. Cache reuse helps enormously on follow-up turns
but does nothing for the first one, and agent loops break cache constantly through
nondeterministic tool ordering and compaction.

If agentic coding is a real requirement, the honest options are a cloud model routed
through the same LiteLLM (which the architecture already supports as a fallback target),
or a GPU. Worth knowing: the MS-02 Ultra has a **PCIe 5.0 x16 slot with dual-slot GPU
support**, and llama.cpp's `--n-cpu-moe` keeps expert weights in system RAM while
putting attention and KV cache on the GPU — so even a modest used 16–24 GB card
transforms prefill without needing to fit the whole model in VRAM. That is a purchase,
and explicitly out of scope here, but it is the one upgrade that changes the verdict.

---

## Rollout

**Phase 1 — verify memory, no deployment.** Check DIMM count, actual clock, XMP state.
Everything downstream scales off this number, and it may change the RAM plan.

**Phase 2 — control plane.** LiteLLM + Open WebUI + Zitadel OIDC + monitoring, pointed
initially at a cloud backend. Proves the serving path end-to-end. ~1.2 vCPU / 3.5 GiB,
no cluster changes.

**Phase 3 — `llm-1` worker.** New VM via OpenTofu, `controlPlane: false`, tainted.
llama.cpp serving Qwen3.6-35B-A3B Q4 with a draft model. Register in LiteLLM. Rebalance
RAM per the chosen option.

**Phase 4 — tune, then decide on a second tier.** Cache reuse, speculative decoding,
CPU pinning. Only add GPT-OSS-120B once you know the measured token rate is worth the
extra 63 GB.

---

## Open questions

1. **DIMM configuration and actual clock.** Gates the whole RAM plan. 4 × 48 GB at a
   downclocked 4000–4400 is a materially different machine from 2 × 64 GB at 5600.
2. **Free space on the Proxmox LVM-thin pool** for a 250 GB `llm-1` disk. Not verifiable
   from here — SSH to `172.16.20.3` was refused.
3. **"hermes bot" — which one?** Interpreted here as a chat bridge (Telegram / Matrix /
   Discord) onto LiteLLM. If it means a Nous Hermes *model*, note that Hermes releases
   are typically dense — which is the wrong architecture for this hardware, per the
   dense/sparse row above. If it is a bot, follow the `media/minecraft-events` pattern:
   optional mounted secret re-read per use, not `envFrom`, so it self-heals without a
   restart.
4. **"deepseek harness"** — Open WebUI is recommended because its native OIDC fits the
   Zitadel model already used by every other service. Confirm if a specific alternative
   is wanted.
5. **`.claude/CLAUDE.md:184` says the host is an MS-A2.** It is an **MS-02 Ultra** —
   different vendor line, Intel rather than AMD. Worth correcting; the CPU identity
   underpins every number here.

---

## Sources

- [llama.cpp PR #27742 — Qwen3.8-Flash-Next (`qwen4exp`)](https://github.com/ggml-org/llama.cpp/pull/27742)
- [llama.cpp issue #19480 — Qwen3-Next CPU inference benchmarks](https://github.com/ggml-org/llama.cpp/issues/19480)
- [unsloth/Qwen3.8-Flash-Next-GGUF quant sizes](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF)
- [Qwen3.6-35B-A3B — vLLM recipes](https://recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B)
- [Qwen/Qwen3.8-27B — 28B dense vision-language, Apache-2.0](https://huggingface.co/Qwen/Qwen3.8-27B)
- [Qwen3.8-27B GGUF sizes and the vision mmproj file](https://unsloth.ai/docs/models/qwen3.8)
- [Speeding up a local coding agent — prefill, prompt cache, speculative decoding](https://llmconfigurator.com/en/guides/coding-agents/speed-up-local-coding-agent)
- [MINISFORUM MS-02 Ultra — 4 × DDR5 SODIMM, 256 GB](https://store.minisforum.com/products/minisforum-ms-02-ultra-workstation)
- [Open WebUI SSO / OAuth configuration](https://docs.openwebui.com/features/authentication-access/auth/sso/)
