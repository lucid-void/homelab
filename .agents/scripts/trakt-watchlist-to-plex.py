#!/usr/bin/env python3
"""One-time migration: seed a Trakt watchlist export into the owner's Plex
watchlist.

Trakt's API apps went VIP-only on 2026-07-30, so there is no ongoing Trakt
integration in this cluster (see design/decisions/watch-sync.md). The owner's
watch HISTORY was already carried over by the sibling script,
trakt-export-to-plex.py. This script is the second and final piece: it puts
the Trakt WATCHLIST (not history) onto the owner's Plex watchlist, one time,
before leaving Trakt for good. Once items are on the Plex watchlist, Seerr's
native Plex-watchlist auto-request feature turns them into Sonarr/Radarr
requests at its own pace — this script's only job is getting them onto that
watchlist, nothing downstream.

This is NOT repo tooling meant to be run again and again — it is a
throwaway migration aid kept in git for the record of how the import was
done. Run it once, confirm the results, then forget about it.

SAFETY / design notes:

  * Dry-run is the DEFAULT. Nothing is written to Plex unless --apply is
    passed explicitly.
  * This script only ever ADDS to the watchlist. It never removes or clears
    anything — there is no delete/remove code path at all.
  * The Plex ratingKey this script needs is taken directly from the export's
    ids.plex.guid field. No id translation, and no title/year fallback: an
    entry without ids.plex.guid is reported as unaddable and left alone, the
    same "never guess" stance as the history script's id-only matching.
  * --limit exists because dumping ~175 new watchlist adds into Seerr in one
    shot would fire that many auto-requests at once. --limit N adds only the
    first N new items (ordered by the export's rank, falling back to
    listed_at), so the owner can seed the watchlist in deliberate batches
    instead of flooding Seerr.

This talks to Plex's HOSTED account API (discover.provider.plex.tv), not the
local Plex server the sibling script uses — the watchlist lives on the
plex.tv account, not on any one server. It therefore needs the plex.tv
ACCOUNT token, which is the same PLEX_TOKEN the sibling script (and its
design/decisions/watch-sync.md section) already uses.

Usage:

    # Always look first — inspect what the export actually contains.
    python3 trakt-watchlist-to-plex.py --inspect ~/Downloads/trakt-export.zip

    # Dry run against Plex (default — nothing is written).
    PLEX_TOKEN=... python3 trakt-watchlist-to-plex.py ~/Downloads/trakt-export.zip

    # Only once the dry-run summary looks right, seed a first batch:
    PLEX_TOKEN=... python3 trakt-watchlist-to-plex.py ~/Downloads/trakt-export.zip \\
        --apply --limit 25
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Set, Tuple
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# Plex's hosted account API — the watchlist lives on the plex.tv account, not
# on any one server, so (unlike the sibling script) there is no --plex-url
# flag here: these two endpoints are fixed and verified, not discovered.
DISCOVER_BASE_URL = "https://discover.provider.plex.tv"
WATCHLIST_PATH = "/library/sections/watchlist/all"
ADD_TO_WATCHLIST_PATH = "/actions/addToWatchlist"

REQUEST_TIMEOUT = 15  # seconds
SAMPLE_SIZE = 20
WATCHLIST_PAGE_SIZE = 100
DEFAULT_DELAY = 0.5  # seconds between writes — this hits Plex's hosted API

# Required on every request to discover.provider.plex.tv (verified working;
# X-Plex-Product/X-Plex-Client-Identifier are not optional against this host
# the way they can be against a local server).
PLEX_PRODUCT = "Plex Web"
PLEX_CLIENT_IDENTIFIER = "homelab-migrate-1"

# Only files whose name contains this are treated as watchlist entries — the
# export also contains watched-history-N.json etc, which happen to carry
# "movie"/"show" keys too and must not be mistaken for watchlist entries.
WATCHLIST_FILE_MARKER = "watchlist"


# --------------------------------------------------------------------------
# Reading the export (zip or directory) and finding every *.json file in it
# — same approach as trakt-export-to-plex.py, duplicated here rather than
# imported since each migration script is meant to stand alone.
# --------------------------------------------------------------------------


def iter_export_files(export_path: str) -> Iterator[Tuple[str, bytes]]:
    """Yield (display_name, raw_bytes) for every *.json file in the export.

    Handles both a raw ZIP file and an already-unzipped directory, since the
    owner may hand us either. Nothing about file naming is assumed beyond
    the .json extension.
    """
    if os.path.isdir(export_path):
        for root, _dirs, files in os.walk(export_path):
            for name in files:
                if name.lower().endswith(".json"):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, export_path)
                    with open(full, "rb") as fh:
                        yield rel, fh.read()
    elif zipfile.is_zipfile(export_path):
        with zipfile.ZipFile(export_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if info.filename.lower().endswith(".json"):
                    yield info.filename, zf.read(info.filename)
    else:
        raise SystemExit(
            f"error: {export_path!r} is neither a directory nor a zip file"
        )


def extract_entry_list(parsed: object) -> Optional[List[dict]]:
    """Pull the list of entries out of one parsed JSON file's top level.

    A file may be a bare top-level list (including the empty list), or an
    object that wraps the list under some key. We take the first list we
    find; if none exists the file has nothing recognisable in it.
    """
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict)]
    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list) and (
                not value or isinstance(value[0], dict)
            ):
                return [e for e in value if isinstance(e, dict)]
    return None


# --------------------------------------------------------------------------
# Trakt watchlist entry data
# --------------------------------------------------------------------------


@dataclass
class WatchlistEntry:
    kind: str  # "movie" or "show"
    title: Optional[str]
    year: Optional[int]
    plex_guid: Optional[str]  # ids.plex.guid — used AS the Plex ratingKey directly
    rank: Optional[int]
    listed_at: Optional[str]
    source_file: str


@dataclass
class LoadStats:
    files_seen: int = 0
    files_skipped: int = 0
    total_entries: int = 0
    movie_entries: int = 0
    show_entries: int = 0
    unrecognised_entries: int = 0
    file_reports: List[Tuple[str, int, str]] = field(default_factory=list)


def get_plex_guid(ids: Dict[str, object]) -> Optional[str]:
    """Extract ids.plex.guid. Unlike the history script's imdb/tmdb/tvdb
    fallback matching, this script has exactly one usable id: the Plex guid
    IS the Plex ratingKey (see the module docstring's "key insight"). ids.plex
    is nested ({"guid": ..., "slug": ...}) in every export seen, but a bare
    string is also accepted defensively in case an older export shape used
    one — either way, no id translation happens, just extraction.
    """
    plex_obj = ids.get("plex")
    if isinstance(plex_obj, dict):
        guid = plex_obj.get("guid")
        return str(guid) if guid else None
    if isinstance(plex_obj, str) and plex_obj:
        return plex_obj
    return None


def load_watchlist_export(export_path: str) -> Tuple[List[WatchlistEntry], LoadStats]:
    """Walk the export, but only the lists-watchlist-*.json files (matched by
    name, not shape — see WATCHLIST_FILE_MARKER) — everything else in the
    export (watched-history-N.json etc.) also contains "movie"/"show"
    payloads and would be wrongly counted as watchlist entries if we didn't
    filter by filename first.

    Never lets one bad file abort the run — a file that fails to parse as
    JSON, or that parses but has nothing recognisable in it, is counted as
    skipped and we move on.
    """
    stats = LoadStats()
    entries: List[WatchlistEntry] = []

    for name, raw in iter_export_files(export_path):
        if WATCHLIST_FILE_MARKER not in os.path.basename(name).lower():
            continue
        stats.files_seen += 1
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            stats.files_skipped += 1
            stats.file_reports.append((name, 0, "unparseable JSON"))
            continue

        parsed_list = extract_entry_list(parsed)
        if not parsed_list:
            stats.files_skipped += 1
            stats.file_reports.append((name, 0, "empty / no recognisable entries"))
            continue

        file_movie = 0
        file_show = 0
        file_unrecognised = 0
        for entry in parsed_list:
            stats.total_entries += 1
            kind = entry.get("type")
            if kind not in ("movie", "show"):
                stats.unrecognised_entries += 1
                file_unrecognised += 1
                continue

            payload = entry.get(kind) or {}
            ids = payload.get("ids") or {}
            entries.append(
                WatchlistEntry(
                    kind=kind,
                    title=payload.get("title"),
                    year=payload.get("year"),
                    plex_guid=get_plex_guid(ids),
                    rank=entry.get("rank"),
                    listed_at=entry.get("listed_at"),
                    source_file=name,
                )
            )
            if kind == "movie":
                stats.movie_entries += 1
                file_movie += 1
            else:
                stats.show_entries += 1
                file_show += 1

        kind_desc = f"{file_movie} movie, {file_show} show, {file_unrecognised} unrecognised"
        stats.file_reports.append((name, len(parsed_list), kind_desc))

    return entries, stats


def label(e: WatchlistEntry) -> str:
    year = e.year if e.year is not None else "?"
    return f"{e.title or '(untitled)'} ({year}) [{e.kind}]"


# --------------------------------------------------------------------------
# --inspect mode — no Plex contact at all
# --------------------------------------------------------------------------


def run_inspect(export_path: str) -> None:
    entries, stats = load_watchlist_export(export_path)
    with_guid = [e for e in entries if e.plex_guid]
    without_guid = [e for e in entries if not e.plex_guid]

    print(f"Export: {export_path}")
    print(f"Watchlist JSON files found: {stats.files_seen} ({stats.files_skipped} skipped)")
    print()
    print("Per file:")
    for name, count, kind in stats.file_reports:
        print(f"  {name}: {count} entries ({kind})")
    print()
    print(
        f"Total watchlist entries: {stats.total_entries} "
        f"({stats.movie_entries} movie, {stats.show_entries} show, "
        f"{stats.unrecognised_entries} unrecognised)"
    )
    print(f"With ids.plex.guid (addable): {len(with_guid)}")
    print(f"Without ids.plex.guid (unaddable): {len(without_guid)}")
    if without_guid:
        print()
        print("Unaddable entries (no ids.plex.guid in the export):")
        for e in without_guid:
            print(f"  NO PLEX GUID  {label(e)}")


# --------------------------------------------------------------------------
# Plex hosted account API (stdlib urllib only)
# --------------------------------------------------------------------------


class PlexError(Exception):
    pass


# Exceptions raised by a single Plex HTTP call that should be treated as a
# per-item failure (counted and skipped) rather than aborting the whole run.
# http.client.HTTPException (e.g. IncompleteRead, raised out of resp.read())
# is NOT an OSError, so it has to be listed explicitly alongside the usual
# urllib/socket errors — every per-item call site below uses this same tuple.
PLEX_REQUEST_ERRORS = (
    urlerror.URLError,
    urlerror.HTTPError,
    TimeoutError,
    OSError,
    http.client.HTTPException,
)


def _build_plex_request(
    token: str,
    path: str,
    params: Optional[dict] = None,
    method: str = "GET",
    extra_headers: Optional[dict] = None,
) -> urlrequest.Request:
    """Build the urllib Request for a discover.provider.plex.tv call, with
    auth/headers applied. Shared by every request path so token/header
    construction is defined exactly once."""
    query = dict(params or {})
    url = DISCOVER_BASE_URL.rstrip("/") + path
    if query:
        url += "?" + urlparse.urlencode(query)
    headers = {
        "X-Plex-Token": token,
        "Accept": "application/json",
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Client-Identifier": PLEX_CLIENT_IDENTIFIER,
    }
    if extra_headers:
        headers.update(extra_headers)
    return urlrequest.Request(url, headers=headers, method=method)


def plex_get(
    token: str,
    path: str,
    params: Optional[dict] = None,
    extra_headers: Optional[dict] = None,
) -> dict:
    req = _build_plex_request(token, path, params, method="GET", extra_headers=extra_headers)
    try:
        with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read()
    except PLEX_REQUEST_ERRORS as exc:
        raise PlexError(f"GET {path} failed: {exc}") from exc
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise PlexError(f"GET {path} returned unparseable JSON: {exc}") from exc


def plex_put(token: str, path: str, params: Optional[dict] = None) -> None:
    """Perform a write-only Plex request: checks only the HTTP status and
    never parses the response body. addToWatchlist answers 200 with
    {"MediaContainer":{"size":0}} on success, but routing every write
    through a strict JSON-shape check would be fragile for no benefit here
    — any 2xx status counts as success, same as the sibling script's
    plex_write."""
    req = _build_plex_request(token, path, params, method="PUT")
    try:
        with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            status = resp.status
    except PLEX_REQUEST_ERRORS as exc:
        raise PlexError(f"request {path} failed: {exc}") from exc
    if not (200 <= status < 300):
        raise PlexError(f"request {path} returned HTTP {status}")


def fetch_watchlist_rating_keys(token: str) -> Tuple[Set[str], int]:
    """Fetch every item currently on the account's Plex watchlist, paged via
    X-Plex-Container-Size/X-Plex-Container-Start, and return
    (set-of-ratingKeys, reported-total-size)."""
    rating_keys: Set[str] = set()
    start = 0
    total = 0
    while True:
        data = plex_get(
            token,
            WATCHLIST_PATH,
            extra_headers={
                "X-Plex-Container-Size": str(WATCHLIST_PAGE_SIZE),
                "X-Plex-Container-Start": str(start),
            },
        )
        container = data.get("MediaContainer", {})
        total = container.get("totalSize", total)
        items = container.get("Metadata", []) or []
        if not items:
            break
        for item in items:
            rating_key = item.get("ratingKey")
            if rating_key:
                rating_keys.add(str(rating_key))
        start += len(items)
        if start >= total:
            break
    return rating_keys, total


def add_to_watchlist(token: str, rating_key: str) -> None:
    """Add one item to the Plex watchlist. This is the ONLY write this
    script makes — it never removes anything. Idempotent on Plex's side: an
    item already on the watchlist is a no-op, not a duplicate, so re-running
    this against an already-added item is harmless."""
    plex_put(token, ADD_TO_WATCHLIST_PATH, params={"ratingKey": rating_key})


# --------------------------------------------------------------------------
# Ordering — deterministic so successive --limit batches make progress
# instead of re-adding the same items each time.
# --------------------------------------------------------------------------


def sort_key(indexed: Tuple[int, WatchlistEntry]) -> Tuple[int, object, int]:
    """Rank first (Trakt's own watchlist order) when present, else
    listed_at (ISO8601 strings sort correctly as plain strings), else
    original export order — all entries with a rank sort before all entries
    without one, which is fine since real Trakt watchlist exports carry rank
    on every entry."""
    idx, entry = indexed
    if entry.rank is not None:
        return (0, entry.rank, idx)
    return (1, entry.listed_at or "", idx)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_summary(
    stats: LoadStats,
    with_guid_count: int,
    without_guid: List[WatchlistEntry],
    already_on_watchlist_count: int,
    to_add: List[WatchlistEntry],
    existing_watchlist_total: int,
) -> None:
    print(f"Watchlist JSON files: {stats.files_seen} ({stats.files_skipped} skipped)")
    print(f"Total export entries: {stats.total_entries}")
    print(f"  with ids.plex.guid (addable): {with_guid_count}")
    print(f"  without ids.plex.guid (unaddable): {len(without_guid)}")
    # Printed even when zero: without it the three lines above cannot be
    # reconciled against the total, and a reader checking the arithmetic
    # before an irreversible-ish write reads the gap as a bug.
    print(
        f"  not a movie or show (season/episode entries, skipped): "
        f"{stats.unrecognised_entries}"
    )
    print(f"Current Plex watchlist size (per Plex): {existing_watchlist_total}")
    print(f"Already on the Plex watchlist (unique, skipped): {already_on_watchlist_count}")
    print(f"Would be added (unique, new): {len(to_add)}")
    print()

    print(f"Sample of to-be-added titles (up to {SAMPLE_SIZE}, in apply order):")
    for e in to_add[:SAMPLE_SIZE]:
        print(f"  ADD      {label(e)}")
    print()

    print(f"Unaddable entries — no ids.plex.guid in the export ({len(without_guid)}):")
    for e in without_guid:
        print(f"  NO GUID  {label(e)}")
    print()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-time migration: seed a Trakt watchlist export into the owner's "
            "Plex watchlist. Dry-run by default — pass --apply to actually write "
            "anything."
        ),
    )
    parser.add_argument(
        "export_path",
        help="Path to a Trakt data export — either the downloaded .zip, or an already-unzipped directory",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print what the watchlist files in the export contain and exit. Never contacts Plex.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually add items to the Plex watchlist. Without this flag the run is a dry-run (default).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Add only the first N new items (ordered by rank, else listed_at). "
            "The point of this flag is to seed the watchlist in deliberate "
            "batches instead of dumping ~175 new items into Seerr's "
            "watchlist-auto-request at once."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        metavar="SECONDS",
        help=f"Delay between writes, in seconds (default: {DEFAULT_DELAY}) — this hits Plex's hosted API, not the local server.",
    )
    parser.add_argument(
        "--plex-token",
        default=None,
        help="Plex ACCOUNT (plex.tv) auth token — the same token the sibling history script uses. Falls back to the PLEX_TOKEN environment variable.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.inspect:
        run_inspect(args.export_path)
        return 0

    token = args.plex_token or os.environ.get("PLEX_TOKEN")
    if not token:
        print(
            "error: no Plex token given — pass --plex-token or set PLEX_TOKEN",
            file=sys.stderr,
        )
        return 2

    entries, stats = load_watchlist_export(args.export_path)
    if stats.total_entries == 0:
        print("error: no watchlist entries found in export — nothing to do", file=sys.stderr)
        return 1

    with_guid = [(i, e) for i, e in enumerate(entries) if e.plex_guid]
    without_guid = [e for e in entries if not e.plex_guid]

    print("Fetching current Plex watchlist...")
    try:
        existing_keys, existing_total = fetch_watchlist_rating_keys(token)
    except PlexError as exc:
        print(f"error: could not talk to Plex: {exc}", file=sys.stderr)
        return 2
    print(f"Current Plex watchlist: {len(existing_keys)} items fetched (reported total: {existing_total}).")
    print()

    # Dedup by plex guid, first occurrence (in original export order) wins —
    # a duplicate entry in the export should not be added or counted twice.
    unique_by_guid: Dict[str, Tuple[int, WatchlistEntry]] = {}
    for idx, e in with_guid:
        unique_by_guid.setdefault(e.plex_guid, (idx, e))

    already_on_watchlist = [
        e for guid, (_, e) in unique_by_guid.items() if guid in existing_keys
    ]
    to_add_candidates = [
        (idx, e) for guid, (idx, e) in unique_by_guid.items() if guid not in existing_keys
    ]
    to_add = [e for _, e in sorted(to_add_candidates, key=sort_key)]

    print_summary(
        stats=stats,
        with_guid_count=len(with_guid),
        without_guid=without_guid,
        already_on_watchlist_count=len(already_on_watchlist),
        to_add=to_add,
        existing_watchlist_total=existing_total,
    )

    if not to_add:
        print(
            "error: nothing addable at all — either everything is already on "
            "the watchlist, or nothing in the export carries ids.plex.guid. "
            "This is worth a second look before assuming the run is simply done.",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print("Dry run only — no changes were made. Re-run with --apply to write these adds to Plex.")
        if args.limit is not None:
            print(f"(--limit {args.limit} would apply to the {len(to_add)} shown above.)")
        return 0

    targets = to_add[: args.limit] if args.limit is not None else to_add
    print(f"Applying: adding {len(targets)} of {len(to_add)} new items to the Plex watchlist...")
    succeeded = 0
    failed = 0
    for i, e in enumerate(targets):
        try:
            add_to_watchlist(token, e.plex_guid)
            succeeded += 1
            print(f"  ADDED  {label(e)}")
        except PlexError as exc:
            failed += 1
            print(f"  failed to add {label(e)} (ratingKey={e.plex_guid}): {exc}", file=sys.stderr)
        if args.delay and i < len(targets) - 1:
            time.sleep(args.delay)

    print(f"Done: {succeeded} added, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
