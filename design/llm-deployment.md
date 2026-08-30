# Local LLM — deployment spec

**Date:** 2026-08-30
**Status:** **DEPLOYED and serving.** All four Kustomizations Ready, both hostnames
answering through the Gateway, weights on disk, model loaded and generating. Deployed
2026-08-30 over commits `7a9cc83`…`90147fc`.
**Rationale and alternatives considered:** [design/llm-inference.md](llm-inference.md).

This document holds decisions and traps. It deliberately does **not** reproduce the
manifests — those live under `kubernetes/apps/ai/` and are the source of truth.

---

## 1. Locked decisions

| | |
|---|---|
| Model | **Qwen3.6-35B-A3B, Q8_0, 34.4 GiB** — effectively lossless, 86.0 GPQA |
| Served as | **`local-smart`** (thinking) and **`local-fast`** (thinking off) — one process, one copy of the weights |
| Embeddings | **nomic-embed-text-v1.5 Q8_0**, 139 MiB |
| Vision | **None.** Usage is text and coding. See §2. |
| Inference server | llama.cpp behind **llama-swap** |
| Router | **LiteLLM**, `llm.blackcats.cc` |
| UI | **Open WebUI**, `chat.blackcats.cc` |
| Node | **`llm-1`** — built, joined, tainted, drained |
| `llm-1` memory | **70 GB** (`71680`), spent on KV cache |
| Context window | **Target 128K**, staged — must be measured first (§5) |
| Control planes | **30 GB, 8 vCPU** — vCPU reduction deferred pending benchmark |
| Namespace | **`ai`** — no PSA label, cluster-default `baseline` |
| Coding agents | **Workstation clients of the endpoint, not cluster services** (§7) |
| Cloud fallback | **None — local only.** Deliberate; see §7 for what it costs |
| Deferred | Langfuse, `qwen-fast`, GPT-OSS-120B |

### RAM: 160 GB total

3 × 30 GB control planes + 70 GB on `llm-1` = 160 GB, the full amount available. The
earlier "154 is 4 GB over the 150 budget" caveat is resolved — 150 was the conservative
figure, 160 is the real one.

| | |
|---|---|
| Talos + kubelet + Cilium | 2.5 |
| `qwen-smart` Q8_0 | 34.4 |
| `local-embed` | 0.14 |
| llama-server prompt cache (`--cache-ram` default) | **8.0** |
| KV cache @ 128K, q8_0 | ~24 |
| **Total** | **~69 GiB** against 68.2 GiB allocatable — **does not fit** |

**That total is why `--ctx-size` is still 32768.** The 8 GiB prompt cache was
missed in the original budget: `cache_ram_mib` defaults to 8192 MiB and is on
without being asked for. At 128K the numbers no longer close, so the context
target needs re-deriving from a measured KV figure rather than the linear scaling
the first revision assumed — see §5.

---

## 2. Corrections applied during implementation

Every pinned fact in the previous revision was re-checked against upstream. Five had
drifted, and each would have failed at apply time or later.

| Previous revision said | Reality |
|---|---|
| `Qwen3.6-35B-A3B-UD-Q8_0.gguf` | **No such file.** It is `Qwen3.6-35B-A3B-Q8_0.gguf` (34.4 GiB, = the 36.9 GB decimal figure). The unsloth-dynamic variant at that tier is `UD-Q8_K_XL.gguf`, 35.8 GiB. |
| llama-swap `v166-cpu-b6795` | 85 releases stale; current `v251-cpu-b10680` |
| `--flash-attn` as a bare flag | It now **takes a value** (`[on\|off\|auto]`). Bare, it swallows the next argument and llama-server dies on `unknown value for --flash-attn: '--cache-reuse'`. |
| Zitadel Terraform in `infra/terraform/` | It is `kubernetes/apps/auth/bootstrap/app/tofu/main.tf`, applied by an in-cluster Job |
| `ENABLE_OAUTH_PERSISTENT_CONFIG` defaults `true` | It defaults **`false`**. The one defaulting `true` is `ENABLE_PERSISTENT_CONFIG`. The trap was real, pinned to the wrong variable — itself a silent no-op. |

**Vision was dropped, and the reasoning is worth keeping.** `Qwen3.6-35B-A3B` is *itself*
multimodal (`image-text-to-text`; the unsloth GGUF repo ships an 0.8 GiB
`mmproj-F16.gguf`). The earlier plan paired it with a separate Qwen3.8-27B — 17 GiB of
resident memory for a capability the chat model already has. That pairing was the **only**
argument for taking `llm-1` past 64 GB. With text/coding as the actual use case, vision is
out entirely; if Paperless or Immich ever wants it, it is one extra file and one `--mmproj`
flag, not another model.

### Still to verify on the host

| Check | How | Why it matters |
|---|---|---|
| Installed DIMMs + actual clock | Proxmox BIOS / `dmidecode -t memory` | 4 × 48 GB likely downclocks to 4000–4400 on a dual-channel controller — **~30% of token rate** |
| XMP enabled | BIOS | JEDEC default is another silent ~20% |

Do these **before** the Phase D benchmark; they move every number it produces.

---

## 3. Node `llm-1` — done, and what it cost

Built, joined, tainted, drained. This records the trap, because it is invisible and will
recur on any future worker.

### 3.1 `machine.nodeTaints` does nothing on a worker

The obvious spelling — `nodeLabels` / `nodeTaints` in `talconfig.yaml` — was accepted by
`talhelper genconfig`, applied cleanly, and appeared in the machine config on the running
node. The Node object had **neither**.

**Cause: the NodeRestriction admission controller.** Talos applies those using the *node's
own* credentials, and NodeRestriction forbids a worker from tainting or labelling its own
Node object. Talos cannot disable it and should not. The rejection is **silent** — no
event, no error, no degraded status. The node looks healthy while 20 foreign pods schedule
onto it.

### 3.2 The fix

```yaml
machine:
  kubelet:
    extraConfig:
      registerWithTaints:
        - key: workload
          value: llm
          effect: NoSchedule
```

The kubelet applies this at **registration**, before NodeRestriction has an opinion.

> **It only fires when the Node object is created.** Adding it to an already-registered
> node does nothing — a reboot is not enough, because kubelet *updates* an existing Node
> rather than registering a new one. Either delete the Node object to force
> re-registration, or apply the taint once with `kubectl taint nodes`.

**The label was dropped, not fixed.** Everything targets `kubernetes.io/hostname: llm-1` —
kubelet-set, always present, never restricted. Only reintroduce a `workload=llm` label if
a second inference node appears.

Result: 20 pods → **5, all DaemonSets**, 135m CPU / 1008Mi requests.

### 3.3 What the taint broke: DaemonSet coverage

A `NoSchedule` taint silently excludes every DaemonSet that does not tolerate it, and a
DaemonSet at `DESIRED 3` on a 4-node cluster **looks completely healthy**. The only tell is
comparing against the node count.

| DaemonSet | Action |
|---|---|
| cilium, cilium-envoy, kube-proxy, spegel, node-exporter | none — they tolerate everything |
| **falco** | **toleration added** (`ea78cbd`), live at `DESIRED 4` |
| **democratic-csi-nfs-node** | **left off deliberately** |

Falco matters because `llm-1` is the node that pulls multi-GB weights off the internet.
Note **Helm replaces lists rather than merging them**, so the chart's two default
control-plane tolerations had to be repeated or they would have been silently dropped.

**democratic-csi-nfs-node is deliberately absent.** It only provides `nfs-client` mounts,
and everything on this node uses `openebs-hostpath`. The consequence: **no `nfs-client` PVC
can ever schedule on `llm-1`** until a toleration is added.

### 3.4 Verified: `openebs-hostpath` provisions on a tainted node

A live risk, not a theoretical one — the class is `WaitForFirstConsumer` and its
provisioner runs a **helper pod on the target node**. If that helper could not tolerate the
taint, `llama-models` would never bind and `llama-swap` would never start.

Tested with a throwaway PVC plus a tolerating pod: **bound in ~10 s,
`ProvisioningSucceeded`, pod Running.** No `Tolerations` entry in `cas.openebs.io/config`
is needed and the StorageClass is unchanged.

---

## 4. What is deployed where

| Service | Namespace | Node | CPU req | Mem req / lim | Storage | Exposed |
|---|---|---|---|---|---|---|
| `llama-swap` | `ai` | **`llm-1`** | 4 | 60Gi / **none** | `llama-models` 150Gi `openebs-hostpath` | ClusterIP `:8080` |
| `model-fetch` (Job) | `ai` | `llm-1` | 500m | 256Mi / 512Mi | same PVC | — |
| `litellm` | `ai` | any CP | 200m | 512Mi / 1Gi | none (CNPG) | `llm.blackcats.cc` |
| `open-webui` | `ai` | any CP | 200m | 1Gi / 3Gi | `open-webui-data` 20Gi `nfs-client` | `chat.blackcats.cc` |

Added to the control planes: **400m CPU, 1.5Gi memory** of requests, total.

CPU limits are omitted throughout, matching house style — a CPU limit is throttling, and on
`llama-swap` throttling is directly a token-rate cut.

```
kubernetes/apps/ai/
├── kustomization.yml, namespace.yml
├── database/      2 SealedSecrets w/ Reflector annotations + 2 CNPG Database CRs
├── llama-swap/    helmrelease, config-configmap, pvc, model-fetch job, fetch-models.sh
├── litellm/       helmrelease, config-configmap, sealed secret, route
└── open-webui/    helmrelease, pvc, sealed secret, route

kubernetes/apps/monitoring/ai-monitoring/   scrapes, vmrules, dashboard
```

```
sealed-secrets ─┬─► ai-database ──► postgres-cluster (roles)
                │
openebs ────────┴─► llama-swap ──► litellm ──► open-webui
                                      ▲            ▲
                            postgres-cluster   zitadel-bootstrap
```

---

## 5. Traps encoded in the manifests

Each of these is commented at the point it matters; this is the index.

**Measured on first load (2026-08-30):** decode **8.4–8.9 tok/s**, above the
5–8 estimate. Prefill on an 11-token prompt read 23 tok/s, which is dominated by
fixed overhead and is *not* a usable prefill number — a large-prompt measurement
is still owed. Model load is ~10 s, because llama.cpp mmaps and pages in lazily
rather than reading 34 GiB up front. Runtime confirms `n_threads = 6`,
`n_slots = 4`, `n_ctx_slot = 32768`, `kv_unified = true`.

**`--cache-reuse` does not work with this model and was removed.** llama-server
logs `cache_reuse is not supported by this context, it will be disabled` — the
guard is `!llama_memory_can_shift(...)`, a property of the model's KV
implementation that no flag turns on. The live prefix-caching mechanism is the
cross-request prompt cache (`--cache-ram`), which is not gated by that check and
defaults to 8192 MiB. Budget for it: that is 8 GiB of real memory, and missing it
is what broke the 128K arithmetic above.

**`logToStdout` must be `both`.** It defaults to `proxy`, which forwards only
llama-swap's request log and swallows llama-server's entirely — not visible in
`kubectl logs`, `GET /logs`, or `/logs/stream/upstream`. That hides model-load
failures completely: a bad GGUF or an OOM abort surfaces as a request that never
returns, while the proxy keeps answering `/v1/models` and the pod stays Ready.
It is also the only place the `cache_reuse` warning above appears.

**`--ctx-size` pre-allocates the whole KV cache at startup**, and `llama-swap` runs with no
memory limit — so a bad value takes the **node**, not the pod. The config ships at
`32768`. Before raising it: boot, read `KV self size` from the startup log, scale ×4 for
128K, add 34.4 GiB weights + 2.5 GiB overhead, and confirm it clears ~68.3 GiB.

**No memory limit is deliberate.** llama.cpp mmaps the GGUF, so those pages count toward
the cgroup as reclaimable page cache. A limit near the working set does not OOMKill
cleanly — it causes continuous reclaim-and-refault, which presents as "the model got
mysteriously slow" with no event and nothing in the logs.

**llama-swap group defaults are `swap: true, exclusive: true`.** In the single default
group, every embedding call from Open WebUI's RAG would unload the 34 GiB chat model and
reload it. Both models are therefore in explicit groups with `exclusive: false`. A newer
canonical `routing:` block does the same job — **a config must not use both styles**.

**`/metrics` on llama-swap serves only `llamaswap_*` system metrics** — CPU, RAM, load,
network. It does **not** expose llama.cpp's `llamacpp:*` token counters; those live on the
llama-server child process behind the `/upstream/{model}/` passthrough, which must not be
scraped because hitting it on an unloaded model triggers a 34 GiB load. Consequently there
is **no token-rate alert** — a rule against a series that is never ingested is
indistinguishable from one that never fires.

**app-template strips `optional` from an `envFrom` secretRef** (verified by rendering — no
error, the key simply never reaches the manifest), and `optional` on a map-form
`secretKeyRef` fails the chart's values schema outright. So Open WebUI genuinely requires
`openwebui-oidc-secret` and sits in `CreateContainerConfigError` without it, exactly like
Kavita. `dependsOn: zitadel-bootstrap` is what makes that safe.

**`ENABLE_OAUTH_SIGNUP` defaults to `false`.** OIDC is configured, the button renders, and
the first login fails — there is no account and nothing will create one.

**Open WebUI does not URL-encode the DB password** before interpolating it, and only builds
a URL when **all five** `DATABASE_*` vars are set — miss one and it silently falls back to
SQLite on the PVC. The generated role passwords are deliberately alphanumeric. Note
LiteLLM uses `DATABASE_USERNAME` while Open WebUI uses `DATABASE_USER`.

**`--mlock` is not used.** It would pin the weights and sidestep the page-cache behaviour,
but it needs `IPC_LOCK`, which PSA `baseline` forbids — forcing the whole namespace to
`privileged` to solve a problem a dedicated node does not have.

**Expect the CNPG managed-role race.** New role and its SealedSecret reconcile
near-simultaneously; if CNPG evaluates the role first it records
`cannotReconcile: failed to get password secret … not found` and **never retries**.
`flux reconcile` does not help — the `Cluster` already matches git, so no watch event
fires. Nudge it:
`kubectl annotate cluster postgres -n postgres reconcile-nudge="$(date +%s)" --overwrite`.

**`kubelet_volume_stats_*` reports the backing filesystem, not the PVC.** For
`openebs-hostpath` every claim reports `llm-1`'s `/var`, so a "model PVC almost full" alert
would be a node-disk alert wearing the wrong name. `NodeFilesystemAlmostFull` already
covers it; the dashboard shows `/var` free directly.

---

## 6. What deploying it actually cost

Everything below was found by deploying, not by review. Each is fixed in git.

| Problem | Cause | Fix |
|---|---|---|
| **LiteLLM OOMKilled**, exit 137, dead in 11 s, **no log output at all** | Dies inside the Prisma migration before the proxy logs anything. Only evidence is `lastState.terminated.reason`. Steady state measured 941 MiB — just under the 1Gi limit, leaving the startup spike nowhere to go. | limit → 4Gi (`7529ad4`) |
| **LiteLLM `/metrics` 404** | Prometheus metrics are behind LiteLLM's enterprise tier. The scrape sat at `up=0`. | scrape deleted, not left red (`c2cefad`) |
| **`KV self size` unobtainable** | `logToStdout` defaults to `proxy` and swallows llama-server's log entirely — invisible in `kubectl logs`, `GET /logs` *and* `/logs/stream/upstream`. | `logToStdout: both` (`9c8b106`) |
| **`--cache-reuse 256` never ran** | `cache_reuse is not supported by this context, it will be disabled`. Guard is `!llama_memory_can_shift(...)` — a property of the model's KV implementation, not a flag. | removed (`796ee73`) |
| **128K budget was wrong** | `cache_ram_mib` defaults to **8192 MiB** and is on unasked. Adding it, 128K no longer fits. | `--ctx-size` stays 32768 (`796ee73`) |
| **CNPG managed-role race** | Exactly as documented — CNPG evaluated the roles before Sealed Secrets decrypted, then never retried. | annotate-nudge |

The pattern worth keeping: **four of the six were silent.** No error, no event — a flag
discarded at startup, a metrics endpoint quietly 404ing, a log stream that was never
forwarded, and a default that consumed 8 GiB without appearing anywhere in the config. The
one that surfaced loudly (the OOM) still produced zero log output.

### Order it was brought up in

`ai-database` → `llama-swap` (PVC → `model-fetch` → HelmRelease) → `zitadel-bootstrap`
re-run → `litellm` → `open-webui` → `ai-monitoring`. The node resize came first.

`model-fetch` took ~36 minutes for 35 GiB (~16 MB/s) and both files landed at exactly
their pinned byte sizes.

---

## 7. Coding agents are clients, not services

Both tools from the original brief consume `llm.blackcats.cc` over the OpenAI protocol and
belong on the workstation:

- **opencode** — `anomalyco/opencode`. Note the old Go repo `opencode-ai/opencode` is
  **archived** since Sep 2025 and most search results still point at it.
- **DeepSeek Harness** — `deepseek-ai/deepseek-harness`, npm `@deepseek-ai/dsh`. Provider
  config in `$DSH_HOME/settings.yaml` (`api: openai-completions`, `baseURL`, `apiKeyEnv`).

**Neither is deployed into the cluster, and DeepSeek Harness specifically should not be.**
It is at `0.1.1-rc.2` and its README states verbatim: *"DeepSeek Harness is in developer
preview and iterating rapidly. **THERE WILL BE COMPATIBILITY-BREAKING CHANGES.**"* Its web
UI binds `127.0.0.1:3080` and opens a local browser — a personal dev tool, not a shared
service. Revisit at a stable minor.

### Working opencode config

`~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "homelab": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Homelab",
      "options": {
        "baseURL": "https://llm.blackcats.cc/v1",
        "apiKey": "sk-…"                       // LiteLLM virtual key, alias `opencode`
      },
      "models": {
        "local-fast":  { "name": "Qwen3.6-35B (fast)",  "limit": { "context": 32768, "output": 8192 } },
        "local-smart": { "name": "Qwen3.6-35B (think)", "limit": { "context": 32768, "output": 8192 } }
      }
    }
  }
}
```

**Declare `context: 32768`, not 128000.** The server truncates silently otherwise.

### Virtual keys

One per client, minted against the master key, so usage is attributable and each is
revocable alone:

```bash
curl -sX POST http://litellm.ai.svc.cluster.local:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"key_alias":"opencode","models":["local-smart","local-fast","local-embed"]}'
```

**The `models` list is enforced and is a real trap.** A key minted before `local-fast`
existed rejects it with `key not allowed to access model` — and `/v1/models` is filtered
per key, so the model simply does not appear rather than erroring usefully. Adding a model
to LiteLLM means updating existing keys via `/key/update`.

### Thinking is the dominant cost, not prefill

Qwen3.6 is a reasoning model, and llama-server splits `reasoning_content` from `content`.
Measured end to end on *"Reply with exactly: ok"*:

| | reasoning | output tokens | ≈ time @ 8.4 tok/s |
|---|---|---|---|
| `local-smart` | 572 chars | 152 | ~18 s |
| `local-fast` | 0 | 2 | ~0.2 s |

`local-fast` is the **same process and the same resident weights** with
`enable_thinking: false` pushed into the chat template via `extra_body` — not a second
model, no extra memory. The overhead is **per turn**, so it compounds across a tool-calling
loop in a way it never does for a single chat reply. **Point coding agents at
`local-fast`.**

**The honest expectation: local serves focused, small-context work well and
large-context agentic loops badly.** Prefill is still unmeasured on a realistic prompt,
and it is worse than first estimated because `--cache-reuse` does not work with this model
at all (§6) — there is no mid-prompt cache recovery, only llama-server's cross-request
prompt cache for exact shared prefixes.

> **There is no cloud fallback, by choice.** `router_settings.fallbacks` in the LiteLLM
> ConfigMap is commented out and no cloud model is registered. Agentic requests therefore
> take as long as they take rather than being routed away. Adding one later is small — a
> key in `litellm-secret`, one `model_list` entry, uncomment the block — and no client
> changes, because they all ask for the same model names either way.

---

## 8. Verification

```bash
# memory actually assigned (~68.3 GiB after the resize; 65322688Ki before)
kubectl get node llm-1 -o jsonpath='{.status.allocatable.memory}{"\n"}'

# only DaemonSets plus the LLM stack should be here
kubectl get pods -A --field-selector spec.nodeName=llm-1

# app-template naming is what the manifests assumed (no -app suffix)
kubectl get deploy,svc -n ai

# CNPG roles reconciled — watch for the managed-role race
kubectl get cluster postgres -n postgres -o jsonpath='{.status.managedRolesStatus}'

# KV cache size, BEFORE raising --ctx-size to 131072
kubectl -n ai logs deploy/llama-swap | grep -i 'KV self size'

# real memory, not the estimate in §1
kubectl -n ai exec deploy/llama-swap -- cat /sys/fs/cgroup/memory.current

# the model actually loaded (first call is slow by design)
kubectl -n ai exec deploy/llama-swap -- curl -s localhost:8080/v1/models

# end to end through the router
curl -s https://llm.blackcats.cc/v1/chat/completions \
  -H "Authorization: Bearer <virtual-key>" \
  -d '{"model":"local-smart","messages":[{"role":"user","content":"hi"}]}'

# does LiteLLM's metrics endpoint exist on this version? if not, delete its scrape
kubectl -n ai exec deploy/litellm -- curl -s localhost:4000/metrics | head
```

Open WebUI by hand: log in via Zitadel (proves `ENABLE_OAUTH_SIGNUP`), send a message
(proves LiteLLM), upload a document and ask about it (proves `local-embed` routing and that
the groups prevent chat-model eviction).

**Measure and record real tok/s.** Every rate in the rationale document is an estimate
scaled from published benchmarks on comparable hardware — none is a measurement on this
box. Record decode, **prefill**, and cold TTFT on a 20K context before deciding whether
Phase 4 (`qwen-fast`, `ik_llama.cpp`, Flash-Next via mmap overflow) is worth it. Try
`--threads-batch 8` as a prefill experiment: batch processing is compute-bound and may use
llm-1's E-cores profitably where decode does not.

---

## 9. Rollback

| Failure | Rollback |
|---|---|
| 70 GB resize unstable | `tofu apply` back to `65536` and reboot — `llm-1` runs nothing critical |
| Serving layer misbehaves | delete `kubernetes/apps/ai/{litellm,open-webui}` — nothing depends on them |
| Model too slow to be useful | keep the node, repoint LiteLLM at a cloud entry; the stack still works end to end |
| Need the node back entirely | `kubectl taint node llm-1 workload-` restores the status quo exactly |

Nothing here is destructive to existing services, and the control planes are not touched.

---

## 10. Open questions

1. **Prefill on a realistic prompt is still unmeasured.** This is the one number that
   decides whether agentic coding is viable here, and the only reason it is still open is
   that every measurement so far used a trivial prompt where fixed overhead dominates
   (11–15 tokens reading 23–27 tok/s, which means nothing). Measure with a ~5–20K prompt
   before drawing any conclusion about opencode.
2. **`--ctx-size` is 32768 and the path to more is not linear.** The 128K target died on
   the 8 GiB prompt cache (§6). Raising it needs a measured KV figure, and `KV self size`
   does not appear in this build's log even at verbosity 3 — so the honest method is to
   raise it one step, watch `node_memory_MemAvailable_bytes` on llm-1, and back off. With
   no memory limit a bad value takes the node.
3. **Control planes to 4 vCPU** — deferred, not closed. 3 × 8 + 8 = 32 vCPU on 14 physical
   cores is a 2.3× commit and inference is the workload most hurt by it. Decide from the
   prefill numbers, not from the ratio.
4. **Open WebUI's virtual key is still a placeholder.** `open-webui-secret.yml` holds
   `REPLACE_WITH_LITELLM_VIRTUAL_KEY`; opencode's key was minted, Open WebUI's was not. It
   can reach the router but cannot authenticate to it.
5. **Host BIOS: DIMM count, clock, XMP.** Still unchecked, and worth ~50% of the token rate
   between them (§2). Decode measured 8.4–8.9 tok/s *without* knowing whether the memory is
   running at its rated speed.

*Resolved this revision:* the stack is deployed and serving; vision (dropped); `llm-1`
memory (70 GB, verified 68.2 GiB allocatable); model filename and quant; cloud fallback
(none, deliberate); thinking cost quantified and `local-fast` added; `--cache-reuse` proven
dead; the 8 GiB prompt cache found and budgeted; LiteLLM sizing measured; **MS-02 Ultra**
corrected in `.claude/CLAUDE.md`.
