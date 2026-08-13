# .claude/ TODO — agent context slimming

Work plan for making agent context load lazily instead of eagerly. Scoped to `.claude/`
and the `design/decisions/` tree it points at. Infrastructure gaps live in
[design/TODO.md](../design/TODO.md) — do not mix the two.

## Why

`.claude/CLAUDE.md` is loaded in full into every session, before the user types anything,
and again into every subagent that spawns. Measured 2026-08-13:

| | |
|---|---|
| Size | 67 KB ≈ 17k tokens |
| Growth | 14 KB (2026-04-08) → 53 KB (2026-08-01) → 67 KB (2026-08-13) |
| Rate | ~1.2 KB/day, 39 commits, no pruning path |
| Service-specific + k8s-stack tables | 55 KB = **82% of the file** |
| Largest single row (Minecraft) | 9,209 chars ≈ 2,300 tokens |

Three costs, in increasing order of importance:

1. **Tokens.** Prompt caching makes this cheap in dollars after the first call — this is
   the weakest argument and should not be the one we optimise for.
2. **Context window.** Cache does not give back window space. 17 KB competes with
   manifests, `kubectl` output, and diffs, and is re-paid per subagent (3 parallel
   subagents = 51k tokens of identical boilerplate).
3. **Attention dilution.** The real cost. A rule stated once in a 4 KB file is followed
   more reliably than the same rule buried in row 47 of a 60-row table. Today the
   load-bearing invariants ("never `Ingress`", "never `kubectl apply` for config") are
   diluted by 55 KB of per-service incident archaeology.

The file already says *"Read only the file(s) relevant to your current task"* and ships
two routing tables — then inlines everything the tables point at. The routing tables do
zero work because the payload is already loaded. **Fixing that is the whole plan.**

## Contract

**Zero information loss.** Every gotcha in the tables was paid for in debugging hours.
Extraction means *relocating text verbatim* into a file one Read away, never summarising
it. If a fact would be lost, the extraction is wrong.

**Target: 67 KB → ~8 KB.** What survives in `CLAUDE.md`: what the repo is, one routing
table, universal invariants, the IP map, the IaC stack, and one pointer line per service.

---

## Tasks

### 1. Extract service-specific rows → `design/decisions/*.md`
Section `### Service-specific` (~16.5 KB, 8 rows: Immich ×3, Plex, Proxmox OIDC, RomM,
Minecraft, Joplin). Move each row body verbatim into `design/decisions/<topic>.md`;
leave a trigger line naming the paths that should send a reader there.

Shape of a trigger line:
```markdown
| Minecraft | Two Paper servers + Velocity proxy in `media`. Non-obvious constraints
(world storage, quiesced backups, proxy IP pinning, memory sizing, plugin traps) →
design/decisions/minecraft.md — read before touching kubernetes/apps/media/minecraft*. |
```
~250 chars replacing 9,209. Expected saving on this section alone: **~15 KB.**

### 2. Extract per-app rows from `### Kubernetes stack`
~38 KB, 48 rows, mixed. Sort each row into one of three buckets first:
- **Universal invariant** → keep, and feed into task 3
- **Per-app detail** (Gitea valkey, Gotify, Kavita, backups, monitoring…) → extract
- **Stale/duplicated** → delete outright

### 3. Promote universal invariants into a `## Never` block
The ~12 repo-wide rules currently buried inside prose rows are the highest-value tokens
in the file and deserve to be unmissable near the top. `design/CLAUDE.md` lines 127–136
already has this list written correctly — that block belongs in the root file.

### 4. Merge the two routing tables, trim the sync policy
- "Design specs" (L19–30) and "Which design file to read" (L34–43) route to the same 10
  files → one table.
- "Keeping design in sync" (L45–65, 1.6 KB) restates `commands/update-docs.md`. It is
  end-of-task process paid at the start of every session → cut to a pointer.

### 5. `PreToolUse` hook to inject decision docs on demand
`hooks/validate-manifests.sh` already demonstrates parsing `file_path` from stdin JSON.
A `PreToolUse` hook matching e.g. `kubernetes/apps/media/minecraft*` can print the
matching `design/decisions/*.md` into context *only* when that path is touched — true
on-demand loading, zero cost when irrelevant, and more reliable than hoping the agent
follows a pointer.

### 6. Add a growth ratchet to `commands/update-docs.md`
Step 4 currently only ever tells the agent to *add* to the key-decisions table. Add:
> New gotchas go to `design/decisions/<topic>.md`. Only add a `CLAUDE.md` row if the rule
> applies repo-wide. If a row exceeds ~200 chars, move its body out and leave a trigger.

Without this, whatever we trim grows back by October.

### 7. Delete memory files duplicating `CLAUDE.md`
9 of 20 files in the memory dir restate a `CLAUDE.md` row nearly verbatim (targetNamespace,
nfsvers, rclone filen, YAML block scalar, app-template naming, allauth callback,
`PAPERLESS_APPS`, backup-tools/jq, mise exec). `design/CLAUDE.md` "What NOT to Do" is a
third copy of several. Three copies = three update paths = guaranteed drift, which is the
exact failure `CLAUDE.md` L58 warns about for version numbers. Repo-specific technical
rules belong in the committed file only; memory keeps cross-project preferences.

### 8. Quiet `hooks/validate-manifests.sh`
Echoes `kubeconform: <path>` and `OK` into the transcript on every Write/Edit. Silent on
success sharpens the signal when it fails. Also check the path *before* spawning `python3`
— currently it parses JSON even for files outside `kubernetes/`.

---

## Out of scope / user action

- **Rotate the GitHub PAT.** `.claude/settings.local.json` holds a live token in
  plaintext. Correctly gitignored (`~/.config/git/ignore`), so never committed — but it
  is readable on disk by any tool call, and `defaultMode: bypassPermissions` + `Bash(*)`
  means anything can read it. Source it from an env var outside the repo.
- **The `no versions` rule (L58) is not holding.** The "recorded incident" carve-out is
  broad enough that everything qualifies, which is why the file grows 1.2 KB/day.
  Incident narratives are exactly what `design/decisions/` is for; tasks 1–2 and 6 fix
  this structurally rather than by tightening the rule's wording.
