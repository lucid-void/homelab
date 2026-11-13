# Image pinning

**Read before editing:** `.github/renovate.json` (read-only — do not modify it here), `.github/workflows/image-scan.yml`, `.github/scripts/`, any manifest carrying an image tag

## Current state

Images are pinned to a minor semver or an exact tag (e.g. `traefik:v3.1`) — never a
rolling major tag (`latest`, `3`), and never a digest pin.

**linuxserver.io (lscr) images are the exception and must use the full tag**, e.g.
`plex:1.43.3.10896-cb3ebc72d-ls321`, `sabnzbd:5.1.2-ls270`. lscr's short `X.Y.Z` tag is
mutable — it is re-pointed at every rebuild, several times a month, without the
upstream version changing, which makes it a rolling tag wearing a version number.
Renovate tracks the `ls` revision via four regex-versioning rules in
`.github/renovate.json`, grouped into one `linuxserver` PR (per-image PRs would exhaust
`prConcurrentLimit: 6`). Four regexes because the tag shapes differ — table under *Mutable upstream tags* below.

## Rules

- **Never pin an lscr image to the short `X.Y.Z` tag** — it defeats both gates at once.
  Renovate reports *no update available* (the string in git is already the newest
  `X.Y.Z`, so there is genuinely nothing to bump), and `image-scan` never sees the new
  image because no file changed. `imagePullPolicy` defaults to `IfNotPresent` (the tag
  is not `:latest`), so the node keeps serving the cached digest and a pod restart does
  not re-pull — the only escape is the tag string itself changing.
- **Re-validate the `linuxserver` Renovate regex rules whenever an lscr image changes
  its tag shape** — a regex that fails to parse a tag silently returns to "no update
  available", the same failure as running a short tag.

## Verify

```bash
grep -n 'linuxserver' .github/renovate.json
grep -rn 'lscr.io' kubernetes/apps/media/
```

## Mutable upstream tags: how to confirm, and the Renovate rules

**linuxserver.io is the case that bit us.** `lscr.io/linuxserver/plex:1.43.3`
is mutable — lscr rebuilds the image every few days (base-image and mod
refreshes) and moves the short tag each time, while the upstream Plex version
stays `1.43.3`. Two gates fail together, both silently:

- **Renovate reports no update.** The string in git is already the newest
  `X.Y.Z`, so there is genuinely nothing to bump. This is correct behaviour —
  do not go looking for a broken Renovate config.
- **`image-scan` never runs.** No file changes, so no PR, so no CVE diff.

And the cluster does not move either: `imagePullPolicy` defaults to
`IfNotPresent` (the tag is not `:latest`), so the node serves the digest it
cached the first time. **Restarting the pod does not re-pull.** Only the tag
string changing forces a new pull.

A short tag and `latest` resolving to the same index digest is how you confirm the
short tag is mutable:

```bash
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:linuxserver/plex:pull&service=ghcr.io" | jq -r .token)
curl -sI -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://ghcr.io/v2/linuxserver/plex/manifests/1.43.3 | grep -i docker-content-digest
```

Compare that against what is actually running — the resolved digest, not the tag:

```bash
kubectl -n media get pod -l app.kubernetes.io/name=plex \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
```

The fix is to pin the **full** lscr tag, which is immutable, and teach Renovate
to order those tags. `.github/renovate.json` carries four `regex:` versioning
rules because the shapes differ:

| Images | Shape | Example |
|---|---|---|
| plex | 4-part + upstream git hash + `ls` | `1.43.3.10896-cb3ebc72d-ls321` |
| sonarr, radarr, prowlarr, kavita | 4-part + `ls` (kavita has a leading `v`) | `4.0.19.2979-ls322`, `v0.9.0.2-ls121` |
| sabnzbd | 3-part + `ls` | `5.1.2-ls270` |
| jellyfin | 2-part + Ubuntu base + `ls`, no separator | `12.1ubu2604-ls50` |

The jellyfin rule matches the `ubu####` segment but does not capture it — it tracks the
Ubuntu base, not Jellyfin, so capturing it would read a jump to `ubu2610` as an upstream
release.

All four share `groupName: linuxserver` so the churn arrives as one PR — lscr
rebuilds often enough that six separate PR streams would exhaust
`prConcurrentLimit: 6` and crowd out everything else.

**A regex that fails to parse a tag puts you straight back to "no update
available", with no error anywhere.** If lscr changes a tag shape, or a new lscr
image is added without a matching rule, the silence looks identical to "already
current". Re-validate by checking that every pinned tag still parses and that the
rule selects the newest tag in the registry.

## Release age

`minimumReleaseAge: "3 days"` holds every update until the release is three days old, so
a yanked or hot-fixed release never reaches a PR. `minimumReleaseAgeBehaviour:
timestamp-optional` goes with it: many registries publish no release timestamp, and the
default (`timestamp-required`) would hold those updates forever with no error — the same
silence as "already current".

## Image vulnerability scanning (CI)

`.github/workflows/image-scan.yml` scans every container image a PR changes, so
Renovate bumps carry a CVE diff. Required status check: `Image Scans Passed`.

**Gate:** with a baseline (a tag bump), a PR fails only if the *net* Critical or
High count **increases**. An upgrade that introduces some CVEs while fixing more
passes — the introduced ones are still listed in the PR comment. With no baseline
(new image, or the old tag no longer pulls) any Critical fails.

Both scanners run on each image: **grype** (Anchore) and **osv-scanner** (Google).
Results are posted as a single PR comment with before/after/delta tables and
collapsible introduced/fixed CVE lists. Raw scanner JSON is uploaded as a
30-day artifact.

Logic lives in `.github/scripts/`, not inline in the workflow:

| Script | Role |
|---|---|
| `extract-images.py` | Pairs old/new image refs across the diff |
| `scan-report.py` | Normalises both scanners, computes the delta, renders the comment |

Two things make this repo-specific and are easy to get wrong if the workflow is
ever rewritten:

- **Renovate usually does not change a whole image reference.** The bjw-s
  app-template and Helm-values form splits the image across two lines, so a bump
  shows up as a lone `tag:` line with `repository:` sitting on an unchanged
  context line. Grepping the diff's `+`/`-` hunks for `image:` misses these
  entirely. `extract-images.py` instead extracts the full image set from the base
  and head versions of each changed file and diffs those sets.
- **osv-scanner v2 removed severity labels.** v1 exposed
  `database_specific.severity` as `CRITICAL`/`HIGH`; in v2 that field is always
  null and severity is only available as a CVSS base score on
  `groups[].max_severity`. Any filter keyed off the v1 field silently counts zero
  for every image — a dead gate that still reports green. `scan-report.py` bands
  the CVSS score instead. grype's `matches[].vulnerability.severity` is unaffected.

Helm chart coordinates (`chart:` + `version:`, OCIRepository `url:`) are
deliberately not scanned — they are chart artifacts, not runnable images.

Test the extractor against a real PR without pushing:

```bash
git fetch origin <renovate-branch>
python3 .github/scripts/extract-images.py --base origin/main --head origin/<renovate-branch>
```
