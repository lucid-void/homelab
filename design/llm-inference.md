# Local LLM inference — design proposal

**Date:** 2026-08-28
**Status:** **PROPOSED — not implemented.** Nothing in this document is deployed.
**Target model:** Qwen3.8-Flash-Next (released 2026-08-26, Apache-2.0, open weights).
**Scope:** Talos cluster (`172.16.20.11–13`), Proxmox host (`172.16.20.3`), DGX Spark (`172.16.20.4`).
**Goal:** Serve a large local LLM to Open WebUI, opencode, and bots, with routing,
observability and SSO consistent with the rest of the homelab.

---

## Verdict

| Question | Answer |
|---|---|
| Does Qwen3.8-Flash-Next fit on the Talos nodes' CPU+RAM? | **No.** Not at any quant worth running. |
| Does it fit on the DGX Spark? | **Yes.** Comfortably, and ~10× faster. |
| Does the *supporting stack* fit on the cluster today? | **Yes** — ~1.2 vCPU / 3.5 GiB, no VM resize needed. |

The 150 GB RAM budget is not the binding constraint. Four other things bind first, and
three of them are hard. **Recommendation: run the model on the DGX Spark; run everything
else on Kubernetes.**

---

## What the model actually is

Not a plain 125B. Per the merged llama.cpp support PR
([ggml-org/llama.cpp#27742](https://github.com/ggml-org/llama.cpp/pull/27742), merged
2026-08-27, arch id `qwen4exp`):

| Component | Size |
|---|---|
| MoE backbone | 125B total, **6B active** (512 experts, 10 routed + 1 shared) |
| Per-layer N-gram hash embeddings | **97.7 GiB table** (20M bigram/trigram entries) |
| MTP head | 4B |
| Attention | GDN + QSA hybrid (gated delta net on 3 of every 4 layers) |
| Context | 262,144 native, 1M via YaRN |
| Vision | yes (Qwen3-VL ViT) |

That N-gram table is the whole problem. It is why the GGUF quants are far larger than
"125B at 4 bits" would suggest, and why the memory access pattern is worse than the 6B
active-parameter count implies — table lookups are **random-access, latency-bound
gathers**, not the sequential streaming that MoE bandwidth math assumes.

### Quant sizes ([unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF))

| Quant | Size | Fits a 110 GB node? |
|---|---|---|
| UD-IQ1_S | 72.5 GB | yes — but 1-bit on a 6B-active MoE is not worth serving |
| UD-Q2_K_XL | 78.9 GB | yes, degraded |
| UD-IQ3_XXS | 82 GB | yes, degraded |
| UD-Q3_K_XL | 90 GB | tight |
| **UD-IQ4_XS** | **93.7 GB** | **the realistic floor for quality** |
| UD-Q4_K_XL | 111 GB | no |
| UD-Q8_0 | 188 GB | no |

---

## The four constraints

### 1. Memory bandwidth — the throughput ceiling

The nodes report `Intel(R) Core(TM) Ultra 5 235HX` (Arrow Lake-HX, 14C/14T, no SMT).
Measured ISA flags on cp-1:

```
sse4_1 sse4_2 avx avx2 avx_vnni
```

**No AVX-512. No AMX.** AVX2 + AVX-VNNI only — the weakest of the three tiers
llama.cpp optimises for.

Arrow Lake-HX is **dual-channel DDR5**. At DDR5-6400 that is 102.4 GB/s theoretical,
realistically ~65–75 GB/s sustained. Critically: **all three VMs share one memory
controller on one physical host.** Cluster-aggregate bandwidth is ~70 GB/s, not 3×70.

The CPUs are also already oversubscribed — 3 VMs × 8 vCPU = 24 vCPU on 14 physical
cores (`infra/terraform/kubernetes.tf:26`), a 1.7× commit before any LLM lands.

**Estimated throughput:**

| Step | Value |
|---|---|
| Active read per token @ IQ4_XS (~4.3 bpw × 6B) | ~3.2 GB |
| Theoretical ceiling at 70 GB/s | ~22 tok/s |
| Observed llama.cpp CPU MoE efficiency (see below) | ~0.33× |
| Fewer cores, no AVX-512, contended host, N-gram gathers | ~0.4–0.6× |
| **Realistic decode** | **~2–4 tok/s** |

The efficiency factor is measured, not guessed.
[llama.cpp#19480](https://github.com/ggml-org/llama.cpp/issues/19480) benchmarks
Qwen3-Next-80B (Q4_K_M, 51 GB, **3B** active) at **7.74 tok/s** on a Ryzen AI 9 HX 370
with 96 GB DDR5-5600 dual-channel — against a 20–30 tok/s expectation. Thread scaling
was worse than linear (12 cores 3.54 tok/s; 24 SMT threads *degraded* to 2.84). The
issue is open and unresolved upstream. Our target has **twice** the active parameters
and adds the N-gram gather on top.

**Prefill is the real disqualifier.** Decode at 3 tok/s is merely painful; prefill on
this hardware lands somewhere around 20–40 tok/s, so a 20K-token coding context costs
**8–17 minutes before the first token**. That makes opencode and any agentic tool-loop
unusable, which is most of the stated use case.

### 2. Disk — a hard blocker, independent of RAM

VM disks are **100 GB** (`infra/terraform/kubernetes.tf:28`). Worse, the kubelet is
configured to garbage-collect images at 70% (`kubernetes/talos/talconfig.yaml:39`), so
usable space is ~70 GB before Talos, containerd images and PVCs.

A 93.7 GB model file **cannot be stored on a node.** It would have to be mmap'd from
the Synology over NFS, which means a multi-minute cold load and total dependence on
page cache staying resident. Fixing this means resizing all three VM disks.

### 3. etcd — why this must not run on a control plane

All three nodes are **control planes running etcd**, with
`allowSchedulingOnControlPlanes: true`. Co-locating a 94 GB memory consumer with an
etcd member is the strongest objection in this document:

- mmap'ing a 94 GB model evicts etcd's own page cache;
- memory pressure triggers kubelet eviction cascades;
- etcd is fsync-latency-sensitive — pressure causes leader-election churn;
- losing a second member during that churn is **quorum loss**, i.e. cluster-wide outage.

A large model must run on a **dedicated, tainted, non-control-plane node** — or off the
cluster entirely.

### 4. RAM allocation — what 150 GB actually buys

A pod runs on **one** node; the model cannot be split across three. So the 150 GB
budget must be spent asymmetrically.

Current real usage is low — cp-1 reports `MemAvailable` 22.5 GiB of 31.3 GiB, so ~9 GiB
in use. Requests across nodes are 7.7 / 11.3 / 12.8 GiB.

| Split | cp-1 | cp-2 | cp-3 / worker | Largest model that fits |
|---|---|---|---|---|
| Today | 32 GB | 32 GB | 32 GB | none |
| Fatten one CP *(rejected — see §3)* | 20 GB | 20 GB | 110 GB | IQ4_XS, zero margin |
| Dedicated worker | 24 GB | 24 GB | **78 GB worker** | IQ1_M only — quality collapse |

Even spending the entire budget, a dedicated worker lands at ~78 GB, which only reaches
the 1–2 bit quants. Cutting cp-1/cp-2 to 20–24 GB also removes the ability to drain a
node: their current *limit* sums are 20.6 and 24.2 GiB, already at or over that ceiling.

**Conclusion: the CPU path can only host a smaller model.** If you want in-cluster CPU
inference, the honest target is Qwen3-Next-80B-A3B at Q4_K_M (51 GB, 3B active, ~5–7
tok/s) on a 64 GB dedicated worker — not Qwen3.8-Flash-Next.

---

## Why the DGX Spark is the right backend

The box at `172.16.20.4` is already owned and is specified for exactly this workload.

| | Talos CPU node | DGX Spark (GB10) |
|---|---|---|
| Memory for the model | ~78–110 GB (budget-limited) | **128 GB unified** |
| Memory bandwidth | ~70 GB/s **shared by 3 VMs** | **~273 GB/s** |
| Compute | AVX2 + AVX-VNNI | Blackwell, native **NVFP4** |
| Est. decode | 2–4 tok/s | **~30–40 tok/s** |
| Est. TTFT @ 7K prompt | minutes | ~4 s |
| Disk for weights | does not fit | fits |
| etcd risk | severe | none |

vLLM's own DGX Spark guidance names the sweet spot as **100–130B MoE with 10–15B active
params** — Qwen3.8-Flash-Next (125B / **6B** active) sits inside it with *fewer* active
params than the reference workload. Their measured baseline,
Nemotron-3-Super-120B-A12B-NVFP4, decodes at **22.7–23.7 tok/s** with 3.85 s TTFT on a
7,234-token prompt. Half the active parameters should do meaningfully better.

**Do not join the Spark to the Talos cluster.** GB10 is a Grace **arm64** CPU running
DGX OS (Ubuntu), not Talos. Joining it would make this a mixed-architecture cluster —
every DaemonSet (Cilium, node-exporter, Falco, trivy) would need arm64 images, and the
node would sit outside talhelper's management. Run vLLM in a container on the Spark and
register it in the router as an external OpenAI-compatible endpoint. Far less invasive,
and it keeps the GitOps model intact.

**Open problem: the Spark is WOL-managed and currently powered off** (it did not
respond to ping during this analysis). See §Open questions.

---

## Architecture

```
  CONSUMERS                    CONTROL PLANE (k8s, ns: ai)          BACKENDS
  ─────────                    ───────────────────────────          ────────

  Open WebUI ──┐
  chat.…cc     │
               │                ┌──────────────────────┐    ┌──► DGX Spark .4
  opencode ────┼───────────────►│  LiteLLM router      │────┤    vLLM / NVFP4
  (workstation)│   llm.…cc      │  · virtual keys      │    │    Qwen3.8-Flash-Next
               │   OpenAI API   │  · fallback chain    │    │    (primary)
  hermes bot ──┤                │  · budgets           │    │
               │                │  · /metrics          │    ├──► llama.cpp worker
  API clients ─┘                └──────────┬───────────┘    │    small MoE, always-on
                                           │                │    (fallback, optional)
                                   ┌───────┴────────┐       │
                                   │                │       └──► cloud API
                                   ▼                ▼            (overflow, optional)
                            Langfuse traces   VictoriaMetrics
                            (optional)        → Grafana
                                              → Gotify alerts
```

Everything in the middle column is small and fits the cluster **as it stands today**.

### Components

| Component | Namespace | Notes |
|---|---|---|
| **LiteLLM** | `ai` | OpenAI-compatible gateway. The seam that makes the backend swappable. |
| **Open WebUI** | `ai` | `chat.blackcats.cc`, OIDC → Zitadel |
| **spark-waker** | `ai` | WOL + health-gate for the Spark |
| **hermes bot** | `ai` | chat bridge → LiteLLM *(see open questions)* |
| **Langfuse** | `ai` | optional, and expensive — see below |
| **opencode** | — | client-side config only, no cluster footprint |

### LiteLLM — the router

The load-bearing choice. Everything else talks to LiteLLM, so the backend can move
between Spark, CPU worker and cloud without touching a single consumer.

- Postgres via the existing shared CNPG cluster — add a `litellm` managed role plus a
  `litellm-role-secret` SealedSecret mirrored by Reflector, exactly per
  `design/docs/secrets.md`. **Expect the CNPG managed-role race** documented in
  `.claude/CLAUDE.md` on first deploy.
- One virtual key per consumer (Open WebUI, opencode, each bot) — per-consumer budgets
  and independent revocation, mirroring the existing one-token-per-consumer convention
  used for Cloudflare and Gotify.
- Fallback chain: Spark → CPU worker → (optional) cloud. This is what makes a
  powered-off Spark degrade instead of erroring.
- `HTTPRoute` → shared Gateway, `llm.blackcats.cc`. No `Ingress`.
- Add `litellm` to the `postgres-backup` `databases.yml` list.

**Scrape-label gotcha:** the `VMServiceScrape` must select on
`monitoring.blackcats.cc/scrape: litellm`, never `app.kubernetes.io/name` — Flux
`commonMetadata` rewrites the latter on every resource in a Kustomization.

### Open WebUI

- `DATABASE_URL` → CNPG (SQLite default will not survive on NFS).
- OIDC → Zitadel via the Terraform bootstrap, matching every other app.
  Redirect URI is `https://chat.blackcats.cc/oauth/oidc/callback`.
- **Set `ENABLE_OAUTH_PERSISTENT_CONFIG=false`.** It defaults to *true*, which copies
  OAuth settings into the database on first boot and then **ignores the environment**.
  Left at the default, git stops being the source of truth and later credential
  rotations silently do nothing — the same class of trap as the Gitea valkey
  `redis-cluster.enabled` no-op.

### Observability — recommend deferring Langfuse

Langfuse v3 is **not** a single container. Self-hosting it requires langfuse-web,
langfuse-worker, Postgres, ClickHouse, Redis/Valkey **and** an S3-compatible blob store
(MinIO). Published minimums total roughly **9 vCPU / 25 GiB RAM** — comparable to the
entire rest of the homelab, for prompt tracing.

**Recommended:** start with LiteLLM's built-in request logging to Postgres, scraped into
the existing VictoriaMetrics + Grafana + Gotify path. Add Langfuse only if
prompt-level tracing and eval tooling become a real requirement, and budget a node for
it when that happens.

Alerts worth having from day one: backend unreachable, p95 latency regression, error
rate, token-budget burn, and (if the CPU worker exists) OOM risk.

---

## Recommended plan

**Phase 1 — control plane, no hardware changes.** Deploy LiteLLM + Open WebUI + Zitadel
OIDC + monitoring against a cloud backend or a small local model. Proves the whole
serving path end-to-end. Costs ~1.2 vCPU / 3.5 GiB. Nothing about the cluster changes.

**Phase 2 — Spark backend.** vLLM on the Spark serving Qwen3.8-Flash-Next in NVFP4,
registered as a LiteLLM endpoint. Solve WOL. This is where the model actually arrives.

**Phase 3 — optional CPU fallback.** A dedicated `llm-1` worker VM (not a control
plane, tainted `workload=llm:NoSchedule`, 64 GB, 200 GB disk) running llama.cpp with a
*smaller* MoE for always-on cheap requests. Only worth it if the Spark's duty cycle
proves annoying.

**RAM:** Phase 1 and 2 need **no reallocation at all**. The 150 GB budget is better
spent giving the three nodes 40 GB each (120 GB) for general headroom than on a model
that will not perform.

---

## Open questions

1. **"hermes bot" — which one?** Interpreted here as a chat bridge (Telegram/Matrix/
   Discord) onto LiteLLM. If it means something else — a Nous Hermes *model*, or a
   specific project — the consumer layer changes. If it is a bot, follow the
   `media/minecraft-events` pattern: optional mounted secret re-read per use, not
   `envFrom`, so it self-heals without a restart.
2. **"deepseek harness"** — Open WebUI is recommended because its native OIDC fits the
   Zitadel model. Confirm whether a specific alternative harness is wanted.
3. **Waking the Spark.** WOL needs an L2 broadcast on the VLAN, which a Cilium pod
   cannot trivially send. Options: a `hostNetwork` waker pod, a UDM-side trigger, or
   simply leaving the Spark on. Needs a decision; it gates Phase 2.
4. **Host RAM ceiling.** 150 GB is assumed available per your figure, but Arrow Lake-HX
   boards are commonly 2×SODIMM (128 GB max). Confirm the physical DIMM configuration
   before planning any reallocation.
5. **`.claude/CLAUDE.md:184` says the host is an MS-A2** — that model is an AMD Ryzen 9
   9955HX. The nodes report an Intel Core Ultra 5 235HX. One of the two is wrong and
   the CPU identity matters for every number above.

---

## Sources

- [llama.cpp PR #27742 — Qwen3.8-Flash-Next (`qwen4exp`)](https://github.com/ggml-org/llama.cpp/pull/27742)
- [llama.cpp issue #19480 — Qwen3-Next CPU inference benchmarks](https://github.com/ggml-org/llama.cpp/issues/19480)
- [unsloth/Qwen3.8-Flash-Next-GGUF quant sizes](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF)
- [vLLM on the DGX Spark](https://vllm.ai/blog/2026-06-01-vllm-dgx-spark)
- [Langfuse v2 → v3 self-hosting requirements](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v2-to-v3)
- [Open WebUI SSO / OAuth configuration](https://docs.openwebui.com/features/authentication-access/auth/sso/)
