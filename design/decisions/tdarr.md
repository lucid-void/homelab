# Tdarr

**Read before editing:** `kubernetes/apps/media/tdarr/` and `kubernetes/apps/monitoring/tdarr/`

## Current state

Re-encodes H.264 to HEVC to shrink `/volume2/Media`. Status: manifests built; the libraries,
the flow, the test encode and the pilot are runtime work still to do (`design/TODO.md`).

- **Server** in `media` (`bjw-s/app-template`): UI on 8265 behind `tdarr.blackcats.cc`,
  node port 8266 cluster-internal, config on a `tdarr-config` `nfs-client` PVC. No Keycloak
  gate — Tdarr has no OIDC, so reachability is the Gateway only.
- **One node** pinned to **llm-1** (tolerates `workload=llm`), CPU x265 only, **starts paused**
  (`startPaused`), one CPU encode worker, CPU request `500m` and limit `6`. llm-1 requests 6 of
  its 8 CPUs already (llama-swap 4, Clef 2), so the low request lets them win contention. The
  node was chosen because llm-1 averaged 1.2% CPU (p95 3.7%, peak 9%) over 7 days.
- **Mounts:** server and node both mount `media-nfs` read-write, like sonarr/radarr. The encode
  cache, `/app/configs` and `/app/logs` on the node are `emptyDir`s.
- **`tdarr-swap.sh`** (ConfigMap `tdarr-scripts`, mounted at `/scripts` on the node) verifies an
  encode and swaps it in. Exit codes: 0 swapped, 10 verify failed, 20 swap refused, 2 usage.
  Checks: HEVC video, duration within 1 s, same audio and subtitle stream counts and languages,
  a clean full decode (`-xerror`), output smaller than the source. Playability is checked
  automatically, so nobody has to watch outputs: per-stream packet counts within 0.1% of the
  source (a dropped frame is a skip), no more timestamp gaps over 0.5 s than the source, audio
  and video start offset unchanged (A/V sync), HEVC Main or Main 10 4:2:0 at level 5.1 or lower
  (what the TV and iPad direct-play), and SSIM of at least 0.95 on five 10 s segments spread
  across the file, seeking into both files. Real encodes measure SSIM 0.98-0.99, with identical
  packet counts and zero gaps, so the floor catches broken output rather than taste.
  `VERIFY_ONLY=1 tdarr-swap.sh <orig> <out>` runs the checks and moves nothing, which audits a
  swapped file against its trashed original. The original is renamed into
  `/Media/.tdarr-trash/<relative path>` and touched; the output takes its path (`.mkv`).
- **`tdarr-trash-purge`** CronJob (daily 04:23) deletes trash files older than 14 days by mtime, and
  stale files (older than a day) in `/Media/.tdarr-staging`, where a flow puts a finished encode
  before the swap. A crash or power cut can leave a half-written file there.
- **Exporter** (`homeylab/tdarr-exporter`, Docker Hub) in `monitoring`, scraped every 60 s.
  Availability alerts exist; queue and error alerts are written after the live series names
  are known.

## Scope (from a 10,622-file census, 2026-11-07)

Target: H.264, 720p/1080p, video bitrate 6 Mbps and up, over 5 minutes, in `Series/`,
`Series_done/` and `Movies/`, over-15-Mbps files first. Skipped: HEVC/AV1/VP9, DV and HDR,
2160p, under 6 Mbps, and everything outside those three trees. The H.264 pool is about
6 TB; the savings estimate (1.8–2.4 TB at 30–40%) is an assumption until the test encode
replaces it. Target codec is HEVC: no evidence the Living Room TV decodes AV1.

## Test results (2026-11-08)

Five 5-minute clips at CRF 21, 10-bit, `slow`: 56-83% smaller, SSIM 0.97-0.99 (the 21 Mbps
Blu-ray movie scored lowest, 0.967, so over-15 Mbps sources are the risk; consider a lower CRF
there). Speed was 0.4-1.0x realtime on the node's 6 CPUs, so the 6-9 Mbps band alone is
roughly two months of continuous encoding. The flow was then run end to end on two copies in a
throwaway library: 365 MB -> 63 MB and 273 MB -> 95 MB, originals parked in the trash.

## Rules

- **Never use Tdarr's "replace original" step.** `/volume2/Media` has no backup; the swap
  script is the only thing allowed to remove an original, and it moves rather than deletes.
- **Never put the trash inside `Series/`, `Series_done/` or `Movies/`.** It sits at the share
  root so Plex, Jellyfin and Tdarr never index it. Keep it on the same share and NFS mount,
  or the swap stops being an atomic rename (the script refuses a cross-filesystem swap).
- **Never raise the node's CPU limit above 6.** llama.cpp and Clef run on the same 8 vCPU.
- **`startPaused` stays `true` until the pilot has passed.** A restart must not resume an
  untested policy unattended. For the full run (weeks of encoding) set it to `false` so the queue
  resumes by itself after a clean UPS shutdown and reboot; an encode in flight restarts from zero.
- **The trash purge ages files by mtime**, so the swap must `touch` the original when it moves
  it (rename keeps the old mtime). Removing the `touch` makes the purge delete originals at
  once.
- **Pin both Tdarr images to the same tag**, full version, never `latest`.
- **Libraries and the flow live in Tdarr's own database**, not git. The flow is exported as
  `design/decisions/tdarr-flow.json` (13 nodes). To rebuild it after losing the config PVC, POST it
  to `/api/v2/cruddb` with `{"data":{"collection":"FlowsJSONDB","mode":"insert","docID":"h264-to-hevc-slow","obj":<file>}}`
  and the node's `apiKey` in an `x-api-key` header. Libraries are created in the UI (three:
  `/Media/Series`, `/Media/Series_done`, `/Media/Movies`, cache `/temp`, folder watch off, flow
  selected). The flow is: codec is h264 -> resolution 720p or 1080p -> video bitrate 6000 kbps and up
  -> begin command -> 10-bit -> `libx265` preset `slow` CRF 21, hardware encoding off -> cap x265
  pools at 6 -> mkv -> execute -> copy to `/Media/.tdarr-staging` (keeping the relative path, as the
  working file) -> `Run CLI` `/scripts/tdarr-swap.sh "<original>" "<staged>"` -> working file back to
  the original. A non-zero exit from the script fails the item as a transcode error.
- **Tdarr's `/api/v2/poll-worker-limits` is a long-poll.** The node logging `Worker limit polling
  failed ... ETIMEDOUT` every ~30 s is normal, not a network fault.
- **Directories Tdarr's worker (uid 2202) cannot write make the swap refuse.** Never create a
  library or test folder as root (`kubectl exec` runs as root); chown it to `2202:2200`. A refused
  swap leaves the original untouched and shows as `Transcode: Error` in Tdarr.
- **The node's ffmpeg has no libvmaf.** Judge quality with SSIM/PSNR (`ssim` and `psnr` filters), or
  by eye, not VMAF.
- After a swap, Sonarr/Radarr need a manual rescan for items whose source was `.mp4` (the
  output is `.mkv`), or they mark the episode missing.

## Verify

```bash
mise exec -- kubectl get deploy -n media tdarr-server tdarr-node
mise exec -- kubectl get deploy -n media tdarr-node -o jsonpath='{.spec.template.spec.nodeName}{" "}{.spec.template.spec.containers[0].resources}{"\n"}'
mise exec -- kubectl exec -n media deploy/tdarr-node -- ls /Media/.tdarr-trash
mise exec -- kubectl get cronjob -n media tdarr-trash-purge
```
