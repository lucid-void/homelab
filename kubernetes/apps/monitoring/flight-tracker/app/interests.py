#!/usr/bin/env python3
"""Interest-profile scoring for the news module: profile loading, group matching and scoring.

Pure apart from load_profile(); the Clef client is passed to assess(), so nothing here does I/O.
(Not called profile.py: that name shadows Python's own `profile` module.)
"""
import json
import re
from urllib.parse import urlsplit

TIER_KEYS = ("1", "2", "3")
ADJUST_KEYS = ("marketing", "substantive", "major", "paywalled", "build_noise")
QUESTION_KEYS = ("marketing", "substantive", "major", "time_sensitive", "spoiler")


class ProfileError(Exception):
    """The profile file is missing or malformed, so the news module stays off."""


def _num(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where} must be a number")
    return float(value)


def _strings(value, where, required=True):
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise ProfileError(f"{where} must be a list of non-empty strings")
    if required and not value:
        raise ProfileError(f"{where} must not be empty")
    return value


def _words(terms):
    """One whole-word, case-insensitive regex for the terms; None when there are none."""
    if not terms:
        return None
    alternatives = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])", re.IGNORECASE)


def _regex(pattern, where):
    if not isinstance(pattern, str) or not pattern:
        raise ProfileError(f"{where} must be a regular expression string")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise ProfileError(f"{where} is not a valid regular expression: {e}") from e


def _section(raw, key):
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ProfileError(f"{key} must be an object")
    return value


def load_profile(path):
    try:
        with open(path) as f:
            raw = json.load(f)
    except OSError as e:
        raise ProfileError(f"cannot read {path}: {type(e).__name__}") from e
    except ValueError as e:
        raise ProfileError(f"{path} is not valid JSON") from e
    return compile_profile(raw)


def compile_profile(raw):
    if not isinstance(raw, dict):
        raise ProfileError("the profile must be a JSON object")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProfileError("version must be an integer")
    tiers_raw = _section(raw, "tiers")
    tiers = {}
    for key in TIER_KEYS:
        if key not in tiers_raw:
            raise ProfileError(f"tiers.{key} is missing")
        tiers[int(key)] = _num(tiers_raw[key], f"tiers.{key}")
    adjust_raw = _section(raw, "adjust")
    adjust = {}
    for key in ADJUST_KEYS:
        if key not in adjust_raw:
            raise ProfileError(f"adjust.{key} is missing")
        adjust[key] = _num(adjust_raw[key], f"adjust.{key}")
    questions_raw = _section(raw, "questions")
    questions = {}
    for key in QUESTION_KEYS:
        text = questions_raw.get(key)
        if not isinstance(text, str) or not text.strip():
            raise ProfileError(f"questions.{key} must be a non-empty string")
        questions[key] = text
    groups_raw = _section(raw, "groups")
    if not groups_raw:
        raise ProfileError("groups must not be empty")
    groups = []
    for key, g in groups_raw.items():
        if not isinstance(g, dict):
            raise ProfileError(f"groups.{key} must be an object")
        title = g.get("title")
        tier = g.get("tier")
        if not isinstance(title, str) or not title.strip():
            raise ProfileError(f"groups.{key}.title must be a non-empty string")
        if isinstance(tier, bool) or tier not in (1, 2, 3):
            raise ProfileError(f"groups.{key}.tier must be 1, 2 or 3")
        keywords = _strings(g.get("keywords", []), f"groups.{key}.keywords", required=False)
        scoped = _strings(g.get("scoped_keywords", []), f"groups.{key}.scoped_keywords", required=False)
        categories = _strings(g.get("categories"), f"groups.{key}.categories")
        groups.append({"key": key, "title": title, "tier": tier, "categories": set(categories),
                       "kw_rx": _words(keywords), "scoped_rx": _words(scoped)})
    spoilers = _section(raw, "spoilers")
    noise = _section(raw, "build_noise")
    return {
        "version": version,
        "tiers": tiers,
        "deliver_min": _num(raw.get("deliver_min"), "deliver_min"),
        "yes_at": _num(raw.get("yes_at"), "yes_at"),
        "adjust": adjust,
        "questions": questions,
        "pure_rx": _regex(raw.get("pure_feed_pattern"), "pure_feed_pattern"),
        "groups": groups,
        "ruled_rx": _words(_strings(raw.get("ruled_out", []), "ruled_out", required=False)),
        "spoiler_rx": _words(_strings(spoilers.get("topics", []), "spoilers.topics", required=False)),
        "allow_rx": _words(_strings(spoilers.get("allow", []), "spoilers.allow", required=False)),
        "paywalled": tuple(d.lower() for d in _strings(raw.get("paywalled_domains", []),
                                                       "paywalled_domains", required=False)),
        "noise_rx": _regex(noise.get("title_regex"), "build_noise.title_regex"),
        "noise_unless_rx": _words(_strings(noise.get("unless_keywords", []),
                                           "build_noise.unless_keywords", required=False)),
    }


def group_lines(profile):
    """[(title, tier)] in profile order, for the /topics listing."""
    return [(g["title"], g["tier"]) for g in profile["groups"]]


def missing_categories(profile, labels):
    """Profile categories that no subscribed feed uses (a renamed category silently stops matching)."""
    wanted = set().union(*(g["categories"] for g in profile["groups"]))
    return sorted(wanted - set(labels))


def _text(article):
    return f"{article['title']} {article['summary']}"


def match_group(profile, article):
    """(group, pure) for the best matching group, or None. Lowest tier number wins; ties go to the first listed.
    `pure` means a release feed whose category is the group's: the feed itself is the topic."""
    cats = set(article.get("categories") or [])
    is_release = bool(profile["pure_rx"].search(article.get("site") or ""))
    text = _text(article)
    best = None
    for g in profile["groups"]:
        in_category = bool(cats & g["categories"])
        hit = ((g["kw_rx"] is not None and g["kw_rx"].search(text) is not None)
               or (in_category and g["scoped_rx"] is not None and g["scoped_rx"].search(text) is not None)
               or (is_release and in_category))
        if hit and (best is None or g["tier"] < best[0]["tier"]):
            best = (g, is_release and in_category)
    return best


def _verdict(group, score=0.0, deliver=False, alert=False, reason=""):
    return {"deliver": deliver, "alert": alert, "score": round(score, 3), "reason": reason,
            "tier": group["tier"] if group else None, "group": group["title"] if group else None}


def _host_matches(url, domains):
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def assess(profile, article, clef):
    """Score one article. ClefError raised by `clef.noul` propagates (the caller retries later)."""
    match = match_group(profile, article)
    if match is None:
        return _verdict(None, reason="no match")
    group, pure = match
    if profile["ruled_rx"] is not None and profile["ruled_rx"].search(article["title"]):
        return _verdict(group, reason="ruled out")
    tier, adjust, yes_at, q = group["tier"], profile["adjust"], profile["yes_at"], profile["questions"]
    score = profile["tiers"][tier]
    text = _text(article)
    state = {"title": article["title"], "summary": article["summary"]}
    if pure and tier == 2:  # a release feed is substantive by nature
        score += adjust["substantive"]
    if profile["noise_rx"].search(article["title"]) and not (
            profile["noise_unless_rx"] is not None and profile["noise_unless_rx"].search(text)):
        score *= adjust["build_noise"]
    time_sensitive = False
    if not pure:
        spoiler_topic = profile["spoiler_rx"] is not None and profile["spoiler_rx"].search(text)
        allowed = profile["allow_rx"] is not None and profile["allow_rx"].search(text)
        if spoiler_topic and not allowed:
            if clef.noul(state, {"spoiler": q["spoiler"]})["spoiler"] >= yes_at:
                return _verdict(group, reason="spoiler")
        asked = {"marketing": q["marketing"]}
        if tier == 2:
            asked["substantive"] = q["substantive"]
        if tier == 3:
            asked["major"] = q["major"]
            asked["time_sensitive"] = q["time_sensitive"]
        answers = clef.noul(state, asked)
        if answers["marketing"] >= yes_at:
            score *= adjust["marketing"]
        if tier == 2 and answers["substantive"] >= yes_at:
            score += adjust["substantive"]
        if tier == 3 and answers["major"] >= yes_at:
            score += adjust["major"]
        time_sensitive = tier == 3 and answers["time_sensitive"] >= yes_at
    if _host_matches(article.get("url"), profile["paywalled"]):
        score *= adjust["paywalled"]
    deliver = score + 1e-9 >= profile["deliver_min"]
    return _verdict(group, score, deliver=deliver, alert=deliver and time_sensitive,
                    reason="delivered" if deliver else "below threshold")
