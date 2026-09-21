#!/usr/bin/env bash
# Map a repo path to the design doc(s) that govern editing it.
# Usage: .agents/scripts/route-context.sh <path>
# Prints matching design/ file paths, one per line. Prints nothing (exit 0)
# on a missing argument, a path no rule covers, or a path outside kubernetes/.
#
# The mapping below is a machine-readable subset of AGENTS.md's routing
# table, restricted to paths under kubernetes/ (the scope this hook cares
# about). It is derived from each design/decisions/*.md file's own
# "Read before editing:" line — the most reliable signal available, since
# every decision file carries one and hand-typed globs would rot the moment
# a service moves. A few "Read before editing" clauses describe *content*
# rather than a path (e.g. helm-charts.md's "any HelmRelease", images.md's
# "any manifest carrying an image tag") and can't be matched by path alone
# without parsing YAML; those are approximated by filename convention where
# the repo's naming is consistent (helmrelease*.yml, *job.yml) and otherwise
# left uncovered — see task-10-report.md for the full list of exclusions.
set -euo pipefail

P="${1:-}"
[[ -z "$P" ]] && exit 0

# Only kubernetes/ paths are in scope for this hook. Normalize an absolute
# (or otherwise prefixed) path down to its kubernetes/... tail so every rule
# below only has to spell the relative form once.
case "$P" in
  */kubernetes/*) P="kubernetes/${P#*/kubernetes/}" ;;
  kubernetes/*) ;;
  *) exit 0 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
declare -A EMITTED=()
emit() {
  [[ -n "${EMITTED[$1]:-}" ]] && return 0
  [[ -f "$REPO_ROOT/$1" ]] || return 0
  echo "$1"
  EMITTED["$1"]=1
}

# Each arm uses ;;& so multiple decision files can fire for one path (a
# service-specific file plus a repo-wide one, e.g. immich/backup/ matches
# both immich.md and backups.md) — matching how the decision files'
# "Read before editing" lines genuinely overlap in this repo.
# media-stack.md explicitly excludes plex/, romm/, and the minecraft family
# (they keep their own decision files) — checked separately from the big
# case below because extglob's !(...) negation isn't segment-bounded: it
# would also swallow "plex/app/foo.yml" as "not literally plex" and wrongly
# fire (caught in testing; see task-10-report.md).
#
# This exclusion list is spelled out to the exact same directory names
# claimed by name further down (minecraft.md's three, plus minecraft-events/
# which minecraft-monitoring.md claims) rather than a "minecraft*" wildcard.
# A wildcard here would be broader than any arm actually covers, so a brand
# new minecraft-family directory (e.g. minecraft-creative/) would be
# excluded from media-stack.md *and* unclaimed by any specific arm — routing
# to nothing, a silent gap. With the exact list, an unrecognized new
# directory instead falls through to media-stack.md's catch-all below — the
# same default every other new, ordinarily-named media/ app already gets —
# until a human gives it its own arm here, exactly like every other carve-out
# in this file already requires.
case "$P" in
  kubernetes/apps/media/plex/*|kubernetes/apps/media/romm/*|\
kubernetes/apps/media/minecraft/*|kubernetes/apps/media/minecraft-proxy/*|\
kubernetes/apps/media/minecraft-backup/*|kubernetes/apps/media/minecraft-events/*) ;;
  kubernetes/apps/media/*) emit design/decisions/media-stack.md ;;
esac

case "$P" in
  # --- media stack: per-service decision files ---
  kubernetes/apps/media/plex/*)          emit design/decisions/plex.md ;;&
  kubernetes/apps/media/romm/*)          emit design/decisions/romm.md ;;&
  kubernetes/apps/media/minecraft/*|kubernetes/apps/media/minecraft-proxy/*|kubernetes/apps/media/minecraft-backup/*)
                                          emit design/decisions/minecraft.md ;;&
  kubernetes/apps/media/minecraft/app/world-size-configmap.yml|kubernetes/apps/media/minecraft-events/*)
                                          emit design/decisions/minecraft-monitoring.md ;;&

  # --- single-service app namespaces ---
  kubernetes/apps/gitea/*)               emit design/decisions/gitea.md ;;&
  kubernetes/apps/immich/*)              emit design/decisions/immich.md ;;&
  kubernetes/images/postgres-cnpg-immich/*) emit design/decisions/immich.md ;;&
  kubernetes/apps/homebox/*)             emit design/decisions/homebox.md ;;&
  kubernetes/apps/obsidian/*)            emit design/decisions/obsidian-livesync.md ;;&
  kubernetes/apps/changedetection/*)     emit design/decisions/changedetection.md ;;&

  # --- keycloak: the OIDC clients path also governs Proxmox, which is bare metal ---
  kubernetes/apps/keycloak/*)            emit design/decisions/keycloak.md ;;&
  kubernetes/apps/keycloak/clients/*)    emit design/decisions/proxmox-oidc.md ;;&

  # --- OIDC apps / paperless (protonmail-bridge also reads paperless/paperless) ---
  kubernetes/apps/freshrss/*)            emit design/decisions/oidc-apps.md ;;&
  kubernetes/apps/paperless/paperless/*)
    emit design/decisions/oidc-apps.md
    emit design/decisions/protonmail-bridge.md ;;&
  kubernetes/apps/paperless/protonmail-bridge/*) emit design/decisions/protonmail-bridge.md ;;&

  # --- postgres / cnpg (cluster + any per-app database/ dir + reflector) ---
  kubernetes/apps/postgres/*)            emit design/decisions/cnpg.md ;;&
  kubernetes/apps/*/database/*)          emit design/decisions/cnpg.md ;;&
  kubernetes/apps/kube-system/reflector/*) emit design/decisions/cnpg.md ;;&

  # --- llm / ai stack ---
  kubernetes/apps/ai/*)                  emit design/decisions/llm.md ;;&
  kubernetes/apps/monitoring/ai-monitoring/*) emit design/decisions/llm.md ;;&

  # --- minecraft-monitoring's own namesake dir under monitoring/ (its other
  #     two header paths, minecraft-events/ and world-size-configmap.yml,
  #     are wired above) ---
  kubernetes/apps/monitoring/minecraft-monitoring/*) emit design/decisions/minecraft-monitoring.md ;;&

  # --- security tooling ---
  kubernetes/apps/security/*)            emit design/decisions/security-tooling.md ;;&
  kubernetes/apps/kube-system/k8s-cleaner*/*) emit design/decisions/security-tooling.md ;;&
  kubernetes/apps/kube-system/descheduler/*) emit design/decisions/security-tooling.md ;;&

  # --- helm-charts.md's second header path (its first, "any HelmRelease
  #     under kubernetes/apps/", is the filename rule further below) ---
  kubernetes/apps/kube-system/reloader/*) emit design/decisions/helm-charts.md ;;&

  # --- gotify family (bootstrap.sh is also load-bearing for changedetection.md) ---
  kubernetes/apps/monitoring/gotify-bootstrap/app/bootstrap.sh) emit design/decisions/changedetection.md ;;&
  kubernetes/apps/monitoring/gotify/*|kubernetes/apps/monitoring/gotify-bootstrap/*|kubernetes/apps/monitoring/gotify-telegram/*)
                                          emit design/decisions/gotify.md ;;&

  # --- monitoring (repo-wide claim; more specific monitoring subdirs above
  #     still add their own file via the shared ;;& fallthrough) ---
  kubernetes/apps/monitoring/*)          emit design/decisions/monitoring.md ;;&
  kubernetes/apps/goldilocks/*)          emit design/decisions/monitoring.md ;;&

  # --- cilium / gateway ---
  kubernetes/apps/kube-system/cilium/*)  emit design/decisions/cilium-gateway.md ;;&
  kubernetes/apps/gateway/*)             emit design/decisions/cilium-gateway.md ;;&
  kubernetes/talos/talconfig.yaml)
    emit design/decisions/cilium-gateway.md
    emit design/architecture.md ;;&

  # --- flux ---
  kubernetes/flux/*)                     emit design/decisions/flux.md ;;&
  kubernetes/apps/*/*/ks.yml)            emit design/decisions/flux.md ;;&
  kubernetes/apps/monitoring/flux-notifications/*) emit design/decisions/flux.md ;;&
  kubernetes/bootstrap/flux/*)           emit design/decisions/flux.md ;;&
esac

# Content-shaped rules approximated by filename convention (see header note):
# helm-charts.md claims "any HelmRelease under kubernetes/apps/"; jobs-and-
# scripts.md claims "any Job, CronJob or initContainer under kubernetes/apps/".
# initContainers can't be spotted from a path and are left uncovered.
case "$P" in
  kubernetes/apps/*/app/helmrelease*.yml) emit design/decisions/helm-charts.md ;;
esac
case "$P" in
  kubernetes/apps/*job.yml)               emit design/decisions/jobs-and-scripts.md ;;
esac

# Backups: any app's backup/ dir, the etcd snapshot job, and the shared
# backup-tools image, wherever they're touched from.
case "$P" in
  kubernetes/apps/*/backup/*|kubernetes/apps/kube-system/etcd-snapshot/*|kubernetes/images/backup-tools/*)
    emit design/decisions/backups.md ;;
esac

exit 0
