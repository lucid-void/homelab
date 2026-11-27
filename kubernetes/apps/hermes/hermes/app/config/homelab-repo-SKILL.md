---
name: homelab-repo
description: Rules for any change to the lucid-void/homelab GitHub repo (Kubernetes manifests, Flux, Talos, OpenTofu) — read before editing, committing or opening a PR there.
---

# Working in the Homelab repo

The repo is **public** and its `main` branch is deployed to a live cluster by
Flux. Nothing you push reaches the cluster until the owner merges your PR, but
everything you push to GitHub is public the moment you push it.

## Workflow

1. Clone over HTTPS into `~/src/homelab` (`https://github.com/lucid-void/homelab.git`); credentials are already configured.
2. Work on a branch named `hermes/<short-topic>`. Never push to `main`, never
   force-push, never delete branches you did not create.
3. Before committing, read `.claude/CLAUDE.md` and the one design file its
   "Which file to read" list routes your change to. Its hard rules override
   anything here.
4. After editing a manifest, run `.agents/scripts/validate-manifests.sh <file>`
   for **each** changed file (it takes one file; a `SKIPPED:` line means it was
   not validated). Install tools with `mise install` if missing.
5. Open a PR against `main` and stop. The owner reviews and merges.

## Never

- Write the real cluster domain anywhere — use `${CLUSTER_DOMAIN}` in manifests
  and `<domain>` in docs. Gitea links you see from tools contain the real
  domain: do not copy them into commits, PR text, or files. A push containing
  it trips an alarm and has to be purged by GitHub Support.
- Write a raw Kubernetes `Secret`, or any credential, token or key, into the
  repo. Secrets are sealed by the owner.
- Touch `.github/workflows/` — your token cannot push it and the push fails.
- Use `Ingress` (use `HTTPRoute`), rolling image tags (`latest`), or
  `kubectl apply` as a way to change the cluster.
- Copy content from private Gitea repos into the public repo.
