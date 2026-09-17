# Image pinning

**Read before editing:** `.github/renovate.json` (read-only — do not modify it here), any manifest carrying an image tag

## Current state

Images are pinned to a minor semver or an exact tag (e.g. `traefik:v3.1`) — never a
rolling major tag (`latest`, `3`), and never a digest pin.

**linuxserver.io (lscr) images are the exception and must use the full tag**, e.g.
`plex:1.43.3.10896-cb3ebc72d-ls321`, `sabnzbd:5.1.2-ls270`. lscr's short `X.Y.Z` tag is
mutable — it is re-pointed at every rebuild, several times a month, without the
upstream version changing, which makes it a rolling tag wearing a version number.
Renovate tracks the `ls` revision via three regex-versioning rules in
`.github/renovate.json`, grouped into one `linuxserver` PR (per-image PRs would exhaust
`prConcurrentLimit: 6`). Three regexes because the tag shapes differ: plex carries a git
hash, sonarr/radarr/prowlarr/kavita are 4-part (kavita with a leading `v`), sabnzbd is
3-part.

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
