# YouTube queue (ytdl-sub → Jellyfin)

**Read before editing:** `kubernetes/apps/media/youtube-queue/`, the `youtube` persistence
entry in `kubernetes/apps/media/jellyfin/app/helmrelease.yml`

## Why it exists

Videos saved to a dedicated YouTube playlist get downloaded automatically and show up
in Jellyfin (watched on the iPad through Swiftfin/Infuse). The playlist acts as an
inbox; Jellyfin is where things get watched and deleted.

## Current state

- **Hourly CronJob `youtube-queue`** in `media` (`:17` past the hour) runs
  `ytdl-sub sub` on the `ghcr.io/jmbannon/ytdl-sub` image. The image's s6 `/init` is
  bypassed: the CronJob does the scheduling and `securityContext` sets uid 2202 /
  gid 2200.
- **Source:** one public/unlisted playlist. No cookies, no Google account, no OAuth.
  The playlist ID is in the SealedSecret `youtube-queue-secret` (`PLAYLIST_ID`) and
  gets swapped into `subscriptions.yaml` with `sed` at runtime. The repo is public, and
  an unlisted playlist's ID is its only access control.
- **Layout:** the prebuilt `Jellyfin TV Show by Date | Max 1080p` preset. That's one show
  called `YouTube` with NFO + thumbnails, seasons by upload year, episodes by upload date.
  Files land at `/Media/YouTube/YouTube/Season YYYY/…` on the `media-nfs` share.
- **Jellyfin** mounts `media-nfs` subPath `YouTube` read-write at `/YouTube`. It's a
  *Shows* library pointed at `/YouTube`. The library, and the per-user "Allow media
  deletion" permission, are set in the Jellyfin UI and aren't in git.
- **Retention:** manual. You delete a watched video in Jellyfin. Automating that is in
  `design/TODO.md`.
- **Scratch:** downloads go to a 20Gi `emptyDir` and move to the share only once finished.

## Rules

- **Never use ytdl-sub's own retention (`Only Recent`, `date_range`, `keep_files_*`)
  here.** It goes by *upload* date, not download date, so a 3-year-old video saved today
  is out of range straight away and gets skipped or deleted.
- **Never enable `sync_with_source`.** It deletes local files for anything no longer
  in the playlist, which turns tidying the playlist into deleting unwatched downloads.
- **Deleting a file doesn't re-download it.** The download archive
  (`.ytdl-sub-YouTube-download-archive.json` in the show folder) keeps the entry after
  the file is gone. Don't delete the archive: every video still in the playlist would
  come back. To re-fetch one video, remove its entry from the archive.
- **Pin a plain date tag (`YYYY.MM.DD`), not a `.postN` tag.** Renovate's docker
  versioning can't parse `.postN`, so the image would stop updating without anyone
  noticing. ytdl-sub pins the yt-dlp it ships with, so this tag is also what keeps
  yt-dlp current. That matters because YouTube breaks old yt-dlp versions regularly.
- **The PO token provider goes in as a Deployment + Service, never a sidecar.** A
  plain sidecar never exits, so the Job never completes. It isn't deployed; see
  `design/TODO.md`.
- **No YouTube Data API.** Clearing the playlist automatically would need OAuth,
  refresh-token care and quota. That was rejected: the download archive already stops
  re-downloads, so the playlist can grow and be cleaned up by hand. The playlist caps
  out at 5,000 items.

## Verify

```bash
mise exec -- kubectl get cronjob,job -n media -l app.kubernetes.io/name=youtube-queue
mise exec -- kubectl logs -n media job/<latest-job> | tail -20
mise exec -- kubectl exec -n media deploy/jellyfin -- ls /YouTube/YouTube
```

A healthy run ends in `Download Summary:` with `Success`. **`Success` alone doesn't prove
anything downloaded:** a yt-dlp error is logged but still reported as `Success`, so
check for `ERROR:` lines too.
