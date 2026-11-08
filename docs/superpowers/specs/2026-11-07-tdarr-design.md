# Tdarr library transcoding — design

Date: 2026-11-07 · Status: approved design, not built

## Intent

Save disk space on the Synology `Media` share by re-encoding H.264 video to HEVC. Compatibility
is a side benefit, not a goal. `design/TODO.md` records the motive: `/volume2/Media` growth is
uncapped and the share has no backup.

- **Goals:** reclaim roughly 1.8–2.4 TB · never lose an original until its replacement is proven ·
  no impact on etcd, Plex, Jellyfin or the LLM stack.
- **Non-goals:** AV1 · re-encoding HEVC, DV/HDR or 2160p · YouTube, Manga, Photos, Downloads ·
  a Keycloak gate on the UI · changing any client's codec settings.
- **Anti-goals:** an original deleted before verification · a run that starves llama.cpp or
  Clef · a Sonarr/Radarr library that goes "missing" after a swap.

## Evidence (full ffprobe census, 2026-11-07, 10,622 of 10,662 video files)

| Tree | Size | H.264 | HEVC |
|---|---|---|---|
| Movies | 2.4 TB | 276 GB | 2080 GB (66 are 2160p, 39 DV) |
| Series | 4.4 TB | 2910 GB | 1447 GB |
| Series_done | 6.4 TB | 3189 GB | 3116 GB |
| YouTube | 93 GB | 0.7 GB | AV1 78 GB |

- **Library:** 21 TB PVC, 14 TB used, 7.9 TB free. `Series_done/` is finished, ended series, still
  in Jellyfin's library, and is in scope.
- **Target:** H.264 1080p, 5.97 TB. Median bitrate is about 8 Mbps (WEB-DL). Bands: 6–9 Mbps
  3.8 TB · over 15 Mbps 780 GB · 3–6 Mbps 714 GB · under 3 Mbps 190 GB.
- **Subtitles:** 1,391 files carry PGS image subtitles, 95 carry VobSub. Both must be copied.
- **Client evidence is thin.** Jellyfin has no playback history. Plex logs cover 8.5 h, with one
  real transcode. Streamed bytes by device: Living Room TV (Android) 2.3 TB, iPad 1.9 TB, Pixel
  297 GB, Chrome 190 GB. HEVC is the safe baseline because 4,727 HEVC files already exist.
- **Savings figures (30–40% for HEVC) are assumptions**, not measured. The test phase replaces them.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Target codec | HEVC (10-bit, mkv) | Proven baseline in this library. No evidence the TV decodes AV1. |
| Encoder | CPU x265, `slow`, CRF about 21 | Best quality per byte. QSV needs roughly 20–30% more bitrate for the same quality (general experience, not measured here). |
| Placement | llm-1 | 1.2% average CPU, 3.7% p95, 9% peak, 61 of 64 GB free over 7 days. No etcd. |
| Originals | Kept in a trash dir for 14 days | The share has no backup. |
| Swap gate | Automatic verify, then a manual client check for the first files | A decode pass proves validity, not that the TV plays it. |

## Components

- **Tdarr server** in `media`, `bjw-s/app-template`. Config on an `nfs-client` PVC. HTTPRoute with
  `parentRefs: [{name: shared, namespace: gateway}]`. Full image tag pinned. No OIDC and no
  Keycloak gate (Tdarr has none), so it is reachable only through the Gateway, like the
  other no-auth media apps.
- **Tdarr node** on llm-1. It tolerates the `workload=llm` taint. `media-nfs` is mounted
  **read-write** and is the only read-write consumer (Plex and Jellyfin stay `readOnly`).
  - CPU request `500m`, limit `6`. The node already requests 6 CPU (llama-swap 4, Clef 2), so the
    low request keeps llama.cpp and Clef ahead of Tdarr under contention.
  - Encode cache is an `emptyDir`, never the config PVC.
  - One worker at a time to start.
- **Purge CronJob** deletes trash entries older than 14 days.

## File selection

- **Include:** H.264, 720p or 1080p, video bitrate 6 Mbps and above, files over 5 minutes, in
  `Series/`, `Series_done/` and `Movies/`.
- **Skip:** HEVC, AV1, VP9, DV, HDR, 2160p, under 6 Mbps, everything outside the three trees,
  and the trash directory.
- **Order:** over 15 Mbps first, then 6–9 Mbps.
- **Not decided:** the 3–6 Mbps band (714 GB) stays out of v1.

## Per-file pipeline

1. Encode with x265 `slow`, CRF about 21, 10-bit, mkv. Copy all audio unchanged. Copy all
   subtitle streams, including PGS and VobSub.
2. Write the output to a staging directory on the same Synology volume, so the swap is a rename.
3. Verify the output:
   - `ffprobe`: video is HEVC, duration within about 1 s of the source.
   - Audio and subtitle stream counts and languages match.
   - A full decode pass (`ffmpeg -v error -i out -f null -`) with zero errors.
   - Output is smaller than the source.
4. On pass: rename the original into `.tdarr-trash/` at the root of the `Media` share, outside `Series/`, `Series_done/` and `Movies/`, then rename the output
   into the original's path (same stem). On any failure: discard the output, leave the original
   untouched, and flag the file.

## Safety and rollback

- Both swap steps are atomic renames, so no window with a missing file.
- Rollback is a rename from the trash directory.
- Source is mp4: the output is mkv, so the extension changes. Sonarr and Radarr need a manual
  rescan or they mark the item missing. Not automated; decided not to.
- Plex and Jellyfin pick up the change on their next scan. The trash directory sits outside every library path (`Series/`, `Series_done/`, `Movies/`), so neither server indexes it. It stays on the same share and the same NFS mount, so the swap remains a rename.
- The decode pass proves the file is valid. Before the pilot, play 3–5 outputs on the Living Room
  TV and the iPad.

## Rollout

1. **Test:** 3–5 representative files copied to a throwaway path, never the live library. Record
   real size and VMAF, then play them on the TV and iPad. This replaces the assumed 30–50%.
2. **Pilot:** about 100 live files with the 14-day trash.
3. **Full run:** the rest.
4. Each phase ends with your approval before the next starts.

## Monitoring

- Tdarr has no native Prometheus endpoint. The third-party `homeylab/tdarr-exporter`
  (default port 9090, path `/metrics`) provides queue and library metrics. Not verified on
  this cluster.
- Alert through Gotify on a failed verify or a stalled queue. Alert rules are a VMRule on the
  exporter's series, so they depend on deploying it as a small second pod beside the server.

## Docs to update when built

- New `design/decisions/tdarr.md`, plus a row in `.claude/CLAUDE.md`'s table.
- Close the Tdarr entry in `design/TODO.md`. It currently says "no Talos node has a GPU", which is
  stale (cp-2 has the iGPU Jellyfin uses). Fix that line either way.
- `design/docs/storage.md`: note that `media-nfs` now has a read-write consumer.

## Open items

- Confirm `homeylab/tdarr-exporter` works against the pinned Tdarr version and which series it
  exposes, before writing the VMRule.
- `Series_done/` is indexed by Plex as well as Jellyfin, so both servers rescan it after swaps.
- Exclude the trash directory from Tdarr's own library scan (it sits outside the three trees, so
  adding only those three as Tdarr libraries is enough).
