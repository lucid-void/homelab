#!/usr/bin/env python3
"""One-time migration: mark Trakt watch history as watched in Plex.

Trakt's API apps went VIP-only on 2026-07-30, so there is no ongoing Trakt
integration in this cluster (see design/decisions/watch-sync.md). This script
is how the owner's Trakt history gets into Plex ONE TIME before leaving
Trakt for good: download the archive from trakt.tv/settings/data
("Export now" -> a ZIP of JSON files), then run this script against it.
CrossWatch's existing Plex <-> Jellyfin pair propagates the marks to
Jellyfin afterwards, so this script only ever talks to Plex.

This is NOT repo tooling meant to be run again and again — it is a
throwaway migration aid kept in git for the record of how the import was
done. Run it once, confirm the results, then forget about it.

SAFETY: Plex's config lives on an openebs-hostpath PVC with no backup
CronJob (design/docs/storage.md). There is no restore path if this script
marks the wrong items watched. For that reason:

  * Dry-run is the DEFAULT. Nothing is written to Plex unless --apply is
    passed explicitly.
  * Matching NEVER falls back to title/year text matching. A title match on
    the wrong item would silently corrupt watch state with no way back — so
    matching is strictly by external id (Plex's own metadata guid, or
    imdb/tmdb/tvdb), and an entry that can't be matched by id is reported
    as unmatched and left alone.
  * The only write this script performs is marking an item watched
    (the Plex "scrobble" endpoint). It never deletes or unmarks anything.

The Trakt export format is not publicly documented, so this script does not
hardcode which files to read by name. It walks every *.json file inside the
export (zip or already-unzipped directory) and classifies each entry by
shape. In practice the export (trakt.tv/settings/data) contains, among other
things: watched-history-N.json (one entry PER PLAY, both movies and
episodes — this is the only file with per-episode granularity, so it is the
only source used for matching), watched-movies.json and watched-shows-N.json
(deduplicated summaries with no per-episode detail — not usable for marking
episodes watched, so their entries are parsed and shown by --inspect but
never fed into matching), and lists-watchlist-N.json (ignored entirely).
A play entry is recognised by shape, not filename: it is any entry with a
"watched_at" field and a movie/episode payload — which is exactly what
watched-history-N.json contains and the summary files do not.

Usage:

    # Always look first — inspect what the export actually contains.
    python3 trakt-export-to-plex.py --inspect ~/Downloads/trakt-export.zip

    # Dry run against Plex (default — nothing is written).
    PLEX_TOKEN=... python3 trakt-export-to-plex.py ~/Downloads/trakt-export.zip

    # Only once the dry-run summary looks right:
    PLEX_TOKEN=... python3 trakt-export-to-plex.py ~/Downloads/trakt-export.zip --apply
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

DEFAULT_PLEX_URL = "http://172.16.20.51:32400"
REQUEST_TIMEOUT = 15  # seconds
SAMPLE_SIZE = 20

# Plex item types, used to fetch episodes directly rather than walking each
# show's leaves one at a time (see build_plex_index).
PLEX_TYPE_EPISODE = 4

# Match order for every item: Plex's own metadata guid first (present on
# essentially every entry in a modern Trakt export as ids.plex.guid, and it
# is exactly the id Plex's own Guid list carries as "plex://<kind>/<id>"),
# then the third-party ids as a fallback for anything missing a plex guid.
ID_TYPES_IN_ORDER = ("plex", "imdb", "tmdb", "tvdb")


# --------------------------------------------------------------------------
# Reading the export (zip or directory) and finding every *.json file in it
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

    A file may be a bare top-level list (including the empty list — many
    export files are literally "[]"), or an object that wraps the list under
    some key. We take the first list we find; if none exists the file has
    nothing recognisable in it.
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


def classify_entry(entry: dict) -> str:
    """Classify one entry for display/inspection purposes.

    Real Trakt export entries (e.g. everything in watched-history-N.json)
    carry an explicit top-level "type" field ("movie" / "episode" / ...) —
    when present we trust it outright rather than inferring from which keys
    exist. Some export files (the deduplicated watched-movies.json /
    watched-shows-N.json summaries, ratings files, etc) don't carry a "type"
    field at all, so for those we fall back to a shape guess: a "movie" key
    means movie, "episode" + "show" means episode, "show" alone means a
    show-level entry with no episode detail.
    """
    explicit_type = entry.get("type")
    if isinstance(explicit_type, str) and explicit_type:
        return explicit_type
    if "movie" in entry:
        return "movie"
    if "episode" in entry and "show" in entry:
        return "episode"
    if "show" in entry:
        return "show"
    return "unknown"


def is_play_entry(entry: dict, kind: str) -> bool:
    """A "play" is one watch event — exactly what watched-history-N.json
    contains, one entry per play, movies and episodes both. Detected by
    shape (a "watched_at" timestamp alongside a movie/episode payload), not
    by filename, so this also correctly excludes the deduplicated summary
    files (watched-movies.json / watched-shows-N.json), which carry
    "last_watched_at"/"plays" counters instead of a "watched_at" per-play
    timestamp and are not usable for marking individual episodes watched.
    """
    return kind in ("movie", "episode") and "watched_at" in entry


# --------------------------------------------------------------------------
# Trakt entry data
# --------------------------------------------------------------------------


@dataclass
class MovieEntry:
    title: Optional[str]
    year: Optional[int]
    ids: Dict[str, object]
    source_file: str


@dataclass
class EpisodeEntry:
    show_title: Optional[str]
    show_year: Optional[int]
    ids: Dict[str, object]  # the EPISODE's own ids (imdb/tmdb/tvdb/plex/trakt)
    season: Optional[int]
    number: Optional[int]
    episode_title: Optional[str]
    source_file: str


@dataclass
class ParseStats:
    files_seen: int = 0
    files_skipped: int = 0
    total_entries: int = 0
    movie_entries: int = 0
    episode_entries: int = 0
    show_only_entries: int = 0
    unknown_entries: int = 0
    play_entries: int = 0
    shape_samples: Dict[str, dict] = field(default_factory=dict)
    file_reports: List[Tuple[str, int, str]] = field(default_factory=list)


def load_export(
    export_path: str,
) -> Tuple[List[MovieEntry], List[EpisodeEntry], ParseStats]:
    """Walk the whole export, report on everything found, and return only
    the play entries (movies + episodes) that matching actually uses.

    Never lets one bad file abort the run — a file that fails to parse as
    JSON, or that parses but has nothing recognisable in it, is counted as
    skipped and we move on.
    """
    stats = ParseStats()
    movies: List[MovieEntry] = []
    episodes: List[EpisodeEntry] = []

    for name, raw in iter_export_files(export_path):
        stats.files_seen += 1
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            stats.files_skipped += 1
            stats.file_reports.append((name, 0, "unparseable JSON"))
            continue

        entries = extract_entry_list(parsed)
        if not entries:
            stats.files_skipped += 1
            stats.file_reports.append((name, 0, "empty / no recognisable entries"))
            continue

        kinds_in_file: Counter = Counter()
        for entry in entries:
            stats.total_entries += 1
            kind = classify_entry(entry)
            kinds_in_file[kind] += 1
            stats.shape_samples.setdefault(kind, entry)

            if kind == "movie":
                stats.movie_entries += 1
            elif kind == "episode":
                stats.episode_entries += 1
            elif kind == "show":
                stats.show_only_entries += 1
            else:
                stats.unknown_entries += 1

            if is_play_entry(entry, kind):
                stats.play_entries += 1
                if kind == "movie":
                    movie = entry.get("movie") or {}
                    movies.append(
                        MovieEntry(
                            title=movie.get("title"),
                            year=movie.get("year"),
                            ids=movie.get("ids") or {},
                            source_file=name,
                        )
                    )
                else:  # kind == "episode"
                    show = entry.get("show") or {}
                    ep = entry.get("episode") or {}
                    episodes.append(
                        EpisodeEntry(
                            show_title=show.get("title"),
                            show_year=show.get("year"),
                            ids=ep.get("ids") or {},
                            season=ep.get("season"),
                            number=ep.get("number"),
                            episode_title=ep.get("title"),
                            source_file=name,
                        )
                    )

        if kinds_in_file:
            if len(kinds_in_file) == 1:
                kind_desc = next(iter(kinds_in_file))
            else:
                kind_desc = "mixed(" + ",".join(sorted(kinds_in_file)) + ")"
        else:
            kind_desc = "empty"
        stats.file_reports.append((name, len(entries), kind_desc))

    return movies, episodes, stats


# --------------------------------------------------------------------------
# Deduplication — Trakt history has one entry per PLAY, so a rewatched
# episode/movie appears many times. We dedupe on Trakt's own id for the item
# (stable regardless of whether Plex matching later succeeds), not on the
# Plex ratingKey, so "unique items after dedup" reflects the source data
# and is comparable to "total plays parsed" even for items that don't match.
# --------------------------------------------------------------------------


def _dedup_key_movie(m: MovieEntry) -> Tuple[str, object]:
    trakt_id = m.ids.get("trakt")
    return ("trakt", trakt_id) if trakt_id is not None else ("label", label_movie(m))


def _dedup_key_episode(e: EpisodeEntry) -> Tuple[str, object]:
    trakt_id = e.ids.get("trakt")
    return ("trakt", trakt_id) if trakt_id is not None else ("label", label_episode(e))


def dedup_movies(movies: List[MovieEntry]) -> List[MovieEntry]:
    seen: Dict[Tuple[str, object], MovieEntry] = {}
    for m in movies:
        seen.setdefault(_dedup_key_movie(m), m)
    return list(seen.values())


def dedup_episodes(episodes: List[EpisodeEntry]) -> List[EpisodeEntry]:
    seen: Dict[Tuple[str, object], EpisodeEntry] = {}
    for e in episodes:
        seen.setdefault(_dedup_key_episode(e), e)
    return list(seen.values())


# --------------------------------------------------------------------------
# --inspect mode — no Plex contact at all
# --------------------------------------------------------------------------


def run_inspect(export_path: str) -> None:
    _movies, _episodes, stats = load_export(export_path)

    print(f"Export: {export_path}")
    print(f"JSON files found: {stats.files_seen} ({stats.files_skipped} skipped)")
    print()
    print("Per file:")
    for name, count, kind in stats.file_reports:
        print(f"  {name}: {count} entries, kind={kind}")
    print()
    print(
        "Totals across all files: "
        f"{stats.total_entries} entries "
        f"({stats.movie_entries} movie, {stats.episode_entries} episode, "
        f"{stats.show_only_entries} show-only, {stats.unknown_entries} unknown)"
    )
    print(
        f"Of those, {stats.play_entries} are play entries (watched_at present) "
        "— the only ones this script's matching step will use."
    )
    print()
    print("Sample entry per distinct shape:")
    for kind, sample in stats.shape_samples.items():
        print(f"  --- shape: {kind} ---")
        print(indent(json.dumps(sample, indent=2, sort_keys=True)))


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# --------------------------------------------------------------------------
# Plex API (stdlib urllib only)
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
    base_url: str, token: str, path: str, params: Optional[dict] = None
) -> urlrequest.Request:
    """Build the urllib Request for a Plex call, with auth/headers applied.
    Shared by every request path so token/header construction is defined
    exactly once."""
    query = dict(params or {})
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urlparse.urlencode(query)
    return urlrequest.Request(
        url,
        headers={"X-Plex-Token": token, "Accept": "application/json"},
    )


def plex_get(base_url: str, token: str, path: str, params: Optional[dict] = None) -> dict:
    req = _build_plex_request(base_url, token, path, params)
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


def plex_write(base_url: str, token: str, path: str, params: Optional[dict] = None) -> None:
    """Perform a write-only Plex request: checks only the HTTP status and
    never parses the response body. Some write endpoints (e.g. /:/scrobble)
    answer HTTP 200 with an XML or empty body — routing those through
    plex_get's JSON decode would turn a successful write into a reported
    failure, so writes use this instead. Any 2xx status counts as success."""
    req = _build_plex_request(base_url, token, path, params)
    try:
        with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            status = resp.status
    except PLEX_REQUEST_ERRORS as exc:
        raise PlexError(f"request {path} failed: {exc}") from exc
    if not (200 <= status < 300):
        raise PlexError(f"request {path} returned HTTP {status}")


def parse_guid(guid: str) -> Optional[Tuple[str, str]]:
    """Split a Plex Guid string into (type, id).

    Third-party guids look like 'imdb://tt1234567?lang=en' -> ('imdb',
    'tt1234567'). Plex's own metadata guid looks like
    'plex://episode/6957a88cd279b962529673e1' -> ('plex',
    '6957a88cd279b962529673e1') — note the extra "<kind>/" path segment,
    which we strip by always taking the LAST "/"-separated piece; that's a
    no-op for the third-party guids (which have none) and correct for the
    plex:// form.
    """
    if "://" not in guid:
        return None
    gtype, _, rest = guid.partition("://")
    gid = rest.split("?", 1)[0]
    gid = gid.rsplit("/", 1)[-1]
    if not gtype or not gid:
        return None
    return gtype, gid


def get_match_candidates(ids: Dict[str, object]) -> List[Tuple[str, str]]:
    """Turn a Trakt ids object into (type, id) candidates, in match order.

    ids.plex is nested ({"guid": "...", "slug": "..."}) rather than a bare
    scalar like the others, so it needs its own extraction before the
    uniform imdb/tmdb/tvdb loop.
    """
    candidates: List[Tuple[str, str]] = []
    plex_obj = ids.get("plex")
    plex_guid = None
    if isinstance(plex_obj, dict):
        plex_guid = plex_obj.get("guid")
    elif isinstance(plex_obj, str):
        plex_guid = plex_obj
    if plex_guid:
        candidates.append(("plex", str(plex_guid)))
    for id_type in ("imdb", "tmdb", "tvdb"):
        value = ids.get(id_type)
        if value is not None:
            candidates.append((id_type, str(value)))
    return candidates


@dataclass
class PlexIndex:
    # (guid_type, guid_id) -> ratingKey, built once up front, so matching a
    # Trakt entry is a dict lookup and never a Plex query.
    movie_guids: Dict[Tuple[str, str], str] = field(default_factory=dict)
    episode_guids: Dict[Tuple[str, str], str] = field(default_factory=dict)


def build_plex_index(base_url: str, token: str) -> PlexIndex:
    idx = PlexIndex()
    sections = plex_get(base_url, token, "/library/sections")
    directories = sections.get("MediaContainer", {}).get("Directory", [])

    for section in directories:
        section_type = section.get("type")
        section_key = section.get("key")
        if section_type not in ("movie", "show") or section_key is None:
            continue

        if section_type == "movie":
            listing = plex_get(
                base_url,
                token,
                f"/library/sections/{section_key}/all",
                params={"includeGuids": 1},
            )
            target = idx.movie_guids
        else:
            # Fetch every episode in the section directly (type=4) instead
            # of walking each show's allLeaves one at a time — one request
            # covers the whole library.
            listing = plex_get(
                base_url,
                token,
                f"/library/sections/{section_key}/all",
                params={"type": PLEX_TYPE_EPISODE, "includeGuids": 1},
            )
            target = idx.episode_guids

        items = listing.get("MediaContainer", {}).get("Metadata", [])
        for item in items:
            rating_key = item.get("ratingKey")
            if rating_key is None:
                continue
            for guid_entry in item.get("Guid", []) or []:
                parsed = parse_guid(guid_entry.get("id", ""))
                if parsed:
                    target[parsed] = rating_key

    return idx


def scrobble(base_url: str, token: str, rating_key: str) -> None:
    """Mark one Plex item watched. This is the ONLY write this script makes
    — it never unmarks or deletes anything. Uses plex_write, not plex_get:
    the scrobble endpoint can return a non-JSON body on a successful write,
    and this call only needs to know whether the write succeeded."""
    plex_write(
        base_url,
        token,
        "/:/scrobble",
        params={"key": rating_key, "identifier": "com.plexapp.plugins.library"},
    )


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def match_ids(
    ids: Dict[str, object], guid_index: Dict[Tuple[str, str], str]
) -> Tuple[Optional[str], Optional[str]]:
    """Try a Trakt ids object against a Plex guid index, plex -> imdb ->
    tmdb -> tvdb (ID_TYPES_IN_ORDER). Returns (ratingKey, id_type_matched)
    or (None, None). Never falls back to any kind of title/year text match
    — see the module docstring for why: a wrong title match would silently
    corrupt watch state with no backup to recover from."""
    for id_type, id_value in get_match_candidates(ids):
        rating_key = guid_index.get((id_type, id_value))
        if rating_key:
            return rating_key, id_type
    return None, None


def label_movie(m: MovieEntry) -> str:
    year = m.year if m.year is not None else "?"
    return f"{m.title or '(untitled)'} ({year})"


def label_episode(e: EpisodeEntry) -> str:
    season = e.season if e.season is not None else "?"
    number = e.number if e.number is not None else "?"
    try:
        se = f"S{int(season):02d}E{int(number):02d}"
    except (TypeError, ValueError):
        se = f"S{season}E{number}"
    return f"{e.show_title or '(untitled show)'} {se}"


@dataclass
class MatchResult:
    matched_movie_keys: Dict[str, str] = field(default_factory=dict)  # ratingKey -> label
    matched_episode_keys: Dict[str, str] = field(default_factory=dict)  # ratingKey -> label
    matched_id_types: Dict[str, str] = field(default_factory=dict)  # ratingKey -> id type used
    unmatched_movies: List[str] = field(default_factory=list)
    unmatched_episodes: List[str] = field(default_factory=list)


def match_all(
    unique_movies: List[MovieEntry],
    unique_episodes: List[EpisodeEntry],
    idx: PlexIndex,
) -> MatchResult:
    result = MatchResult()

    for m in unique_movies:
        rating_key, id_type = match_ids(m.ids, idx.movie_guids)
        label = label_movie(m)
        if rating_key:
            result.matched_movie_keys.setdefault(rating_key, label)
            result.matched_id_types.setdefault(rating_key, id_type)
        else:
            result.unmatched_movies.append(label)

    for e in unique_episodes:
        rating_key, id_type = match_ids(e.ids, idx.episode_guids)
        label = label_episode(e)
        if rating_key:
            result.matched_episode_keys.setdefault(rating_key, label)
            result.matched_id_types.setdefault(rating_key, id_type)
        else:
            result.unmatched_episodes.append(label)

    return result


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_summary(
    stats: ParseStats,
    total_plays: int,
    unique_count: int,
    result: MatchResult,
) -> None:
    unique_matched = len(result.matched_movie_keys) + len(result.matched_episode_keys)
    unique_unmatched = len(result.unmatched_movies) + len(result.unmatched_episodes)

    print(f"JSON files: {stats.files_seen} ({stats.files_skipped} skipped)")
    print(f"Total plays parsed (watched-history entries): {total_plays}")
    print(f"Unique items after dedup: {unique_count}")
    print(
        f"Matched: {unique_matched} unique Plex items "
        f"({len(result.matched_movie_keys)} movies, {len(result.matched_episode_keys)} episodes)"
    )
    print(f"Unmatched: {unique_unmatched} unique items")
    print(
        "  (matched counts unique Plex items, unmatched counts unique Trakt "
        "entries — these need not sum to the unique-items-after-dedup total "
        "above, e.g. when two distinct Trakt episodes map to one Plex guid)"
    )
    print()

    id_type_counts = Counter(result.matched_id_types.values())
    print("Matched by id type (tells you whether matching is actually working):")
    for id_type in ID_TYPES_IN_ORDER:
        print(f"  {id_type}: {id_type_counts.get(id_type, 0)}")
    print()

    all_matched_labels = list(result.matched_movie_keys.values()) + list(
        result.matched_episode_keys.values()
    )
    print(f"Sample of matched items (up to {SAMPLE_SIZE}):")
    for label in all_matched_labels[:SAMPLE_SIZE]:
        print(f"  MATCH    {label}")
    print()

    all_unmatched = result.unmatched_movies + result.unmatched_episodes
    print(f"Sample of unmatched items (up to {SAMPLE_SIZE}):")
    for label in all_unmatched[:SAMPLE_SIZE]:
        print(f"  NO MATCH {label}")
    print()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-time migration: mark Trakt watch history as watched in Plex. "
            "Dry-run by default — pass --apply to actually write anything."
        ),
    )
    parser.add_argument(
        "export_path",
        help="Path to a Trakt data export — either the downloaded .zip, or an already-unzipped directory",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print what the export contains (files, entry counts, shapes) and exit. Never contacts Plex.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually mark matched items watched in Plex. Without this flag the run is a dry-run (default).",
    )
    parser.add_argument(
        "--plex-url",
        default=DEFAULT_PLEX_URL,
        help=f"Plex base URL (default: {DEFAULT_PLEX_URL})",
    )
    parser.add_argument(
        "--plex-token",
        default=None,
        help="Plex auth token. Falls back to the PLEX_TOKEN environment variable.",
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

    movies, episodes, stats = load_export(args.export_path)
    total_plays = len(movies) + len(episodes)
    if total_plays == 0:
        print("error: no play entries found in export — nothing to do", file=sys.stderr)
        return 1

    unique_movies = dedup_movies(movies)
    unique_episodes = dedup_episodes(episodes)
    unique_count = len(unique_movies) + len(unique_episodes)

    print("Building Plex guid index (movies + episodes)...")
    try:
        idx = build_plex_index(args.plex_url, token)
    except PlexError as exc:
        print(f"error: could not talk to Plex: {exc}", file=sys.stderr)
        return 2
    print(
        f"Indexed {len(idx.movie_guids)} movie guids, {len(idx.episode_guids)} episode guids."
    )
    print()

    result = match_all(unique_movies, unique_episodes, idx)
    print_summary(stats, total_plays, unique_count, result)

    unique_matched_keys = list(result.matched_movie_keys.keys()) + list(
        result.matched_episode_keys.keys()
    )
    if not unique_matched_keys:
        print(
            "error: nothing matched at all — this almost always means the "
            "Plex token is wrong or the matching logic needs a look, not "
            "that the whole library is genuinely absent from Plex.",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print("Dry run only — no changes were made. Re-run with --apply to write these marks to Plex.")
        return 0

    print(f"Applying: marking {len(unique_matched_keys)} Plex items watched...")
    succeeded = 0
    failed = 0
    for rating_key in unique_matched_keys:
        try:
            scrobble(args.plex_url, token, rating_key)
            succeeded += 1
        except PlexError as exc:
            failed += 1
            print(f"  failed to mark ratingKey={rating_key} watched: {exc}", file=sys.stderr)

    print(f"Done: {succeeded} marked watched, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
