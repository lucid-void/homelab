#!/usr/bin/env python3
"""Interest-profile scoring for the news module: profile loading, group matching and scoring.

Pure apart from load_profile(); the Clef client is passed to assess(), so nothing here does I/O.
(Not called profile.py: that name shadows Python's own `profile` module.)
"""
import json
import math
import re
from urllib.parse import urlsplit

TIER_KEYS = ("1", "2", "3")
ADJUST_KEYS = ("marketing", "substantive", "major", "paywalled", "build_noise")
QUESTION_KEYS = ("marketing", "substantive", "major", "time_sensitive", "spoiler")
HEADLINE_PREFIX = "h_"  # keeps headline question ids apart from QUESTION_KEYS in a shared Clef call
TITLE_SLOT = "{title}"
FEED_TAG_RX = re.compile(r"^\s*\[[^\]]{1,30}\]\s*")
WORD_RX = re.compile(r"[^\W_](?:[\w.+#-]*[^\W_])?")
# Words that say nothing about which story a title is on (English and Dutch); a pre-filter only, Clef decides.
STOP_WORDS = frozenset("""
a about after all also and another any are been before being but can for from had has have having here how into its
just more most new not now our out over says than that the their them then there these they this those through was
were what when where which while who why will with you your
aan als bij dan dat deze die dit door een geen had heb hebben heeft het hij hoe hun kan maar meer met naar niet nog
ook over tot uit van voor waar was wat wie wordt zijn zich
""".split())


class ProfileError(Exception):
    """The profile file is missing or malformed, so the news module stays off."""


def _num(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where} must be a number")
    try:
        converted = float(value)
    except OverflowError as e:
        raise ProfileError(f"{where} is out of range") from e
    if not math.isfinite(converted):
        raise ProfileError(f"{where} must be a finite number")
    return converted


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
    except (re.error, OverflowError, RecursionError) as e:
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
    except (ValueError, RecursionError) as e:
        raise ProfileError(f"{path} is not valid JSON") from e
    return compile_profile(raw)


def _flag(value, name):
    if not isinstance(value, bool):
        raise ProfileError(f"{name} must be true or false")
    return value


def _count(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProfileError(f"{where} must be a whole number of at least 1")
    return value


def _headlines(raw):
    """The optional `headlines` section: questions asked about every article, matched or not."""
    if "headlines" not in raw:
        return None
    section = _section(raw, "headlines")
    questions = section.get("questions")
    if not isinstance(questions, dict) or not questions or not all(
            isinstance(k, str) and k and isinstance(v, str) and v.strip() for k, v in questions.items()):
        raise ProfileError("headlines.questions must be a non-empty object of question texts")
    return {"at": _num(section.get("at"), "headlines.at"), "max": _count(section.get("max"), "headlines.max"),
            "skip": set(_strings(section.get("skip_categories", []), "headlines.skip_categories", required=False)),
            "questions": {HEADLINE_PREFIX + k: v for k, v in questions.items()}}


def _same_story(raw):
    """The optional `same_story` section: collapses several articles on one story into one digest line."""
    if "same_story" not in raw:
        return None
    section = _section(raw, "same_story")
    question = section.get("question")
    if not isinstance(question, str) or TITLE_SLOT not in question:
        raise ProfileError(f"same_story.question must be a string containing {TITLE_SLOT}")
    return {"at": _num(section.get("at"), "same_story.at"), "question": question,
            "max": _count(section.get("max_candidates"), "same_story.max_candidates")}


def _routine_updates(raw):
    """The optional `routine_updates` section: titles that are only news when Clef calls the update serious."""
    if "routine_updates" not in raw:
        return None
    section = _section(raw, "routine_updates")
    question = section.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ProfileError("routine_updates.question must be a non-empty string")
    return {"rx": _regex(section.get("title_regex"), "routine_updates.title_regex"),
            "at": _num(section.get("at"), "routine_updates.at"), "question": question}


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
        "skip_release_feeds": _flag(raw.get("skip_release_feeds", False), "skip_release_feeds"),
        "groups": groups,
        "ruled_rx": _words(_strings(raw.get("ruled_out", []), "ruled_out", required=False)),
        "spoiler_rx": _words(_strings(spoilers.get("topics", []), "spoilers.topics", required=False)),
        "allow_rx": _words(_strings(spoilers.get("allow", []), "spoilers.allow", required=False)),
        "paywalled": tuple(d.lower() for d in _strings(raw.get("paywalled_domains", []),
                                                       "paywalled_domains", required=False)),
        "noise_rx": _regex(noise.get("title_regex"), "build_noise.title_regex"),
        "noise_unless_rx": _words(_strings(noise.get("unless_keywords", []),
                                           "build_noise.unless_keywords", required=False)),
        "headlines": _headlines(raw),
        "same_story": _same_story(raw),
        "routine_updates": _routine_updates(raw),
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


def _verdict(group, score=0.0, deliver=False, alert=False, reason="", importance=0.0, headline=False):
    return {"deliver": deliver, "alert": alert, "score": round(score, 3), "reason": reason,
            "tier": group["tier"] if group else None, "group": group["title"] if group else None,
            "importance": round(importance, 3), "headline": headline}


def story_words(title):
    """The words of a title that can tell two stories apart: lowercased, no feed tag, no stop words."""
    words = WORD_RX.findall(FEED_TAG_RX.sub("", title).lower())
    return {w for w in words if len(w) >= 3 and not w.isdigit() and w not in STOP_WORDS}


def story_candidates(title, titles, limit):
    """Indexes into `titles` that share a word with `title`: most shared words first, then newest (last) first."""
    words = story_words(title)
    shared = [(len(words & story_words(t)), i) for i, t in enumerate(titles)]
    return [i for n, i in sorted(shared, key=lambda x: (-x[0], -x[1])) if n][:limit]


def _importance(headlines, article, clef):
    """How big the news is, 0-1: the highest answer to the headline questions; 0.0 when headlines are off.
    Asked on the title alone, in a call of its own: Clef's time grows with questions x text, so a summary
    would triple it, and this runs for every article of every feed."""
    if headlines is None:
        return 0.0
    answers = clef.noul({"title": article["title"], "summary": ""}, headlines["questions"])
    return max(answers[q] for q in headlines["questions"])


def _host_matches(url, domains):
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        host = ""
    return any(host == d or host.endswith("." + d) for d in domains)


def assess(profile, article, clef):
    """Score one article. ClefError raised by `clef.noul` propagates (the caller retries later)."""
    headlines = profile["headlines"]
    is_release = bool(profile["pure_rx"].search(article.get("site") or ""))
    if profile["skip_release_feeds"] and is_release:
        return _verdict(None, reason="release feed")  # tags are not news
    state = {"title": article["title"], "summary": article["summary"]}
    match = match_group(profile, article)
    if match is None:
        if headlines is None or is_release or set(article.get("categories") or []) & headlines["skip"]:
            return _verdict(None, reason="no match")
        importance = _importance(headlines, article, clef)
        if importance + 1e-9 >= headlines["at"]:  # big news outside every group still reaches the digest
            return _verdict(None, deliver=True, reason="headline", importance=importance, headline=True)
        return _verdict(None, reason="no match", importance=importance)
    group, pure = match
    if profile["ruled_rx"] is not None and profile["ruled_rx"].search(article["title"]):
        return _verdict(group, reason="ruled out")
    routine = profile["routine_updates"]
    if routine is not None and not pure and routine["rx"].search(article["title"]):
        # e.g. a driver download post: a keyword match alone would deliver every one of them
        if clef.noul(state, {"serious": routine["question"]})["serious"] + 1e-9 < routine["at"]:
            return _verdict(group, reason="routine update")
    tier, adjust, yes_at, q = group["tier"], profile["adjust"], profile["yes_at"], profile["questions"]
    score = profile["tiers"][tier]
    text = _text(article)
    if pure and tier == 2:  # a release feed is substantive by nature
        score += adjust["substantive"]
    if profile["noise_rx"].search(article["title"]) and not (
            profile["noise_unless_rx"] is not None and profile["noise_unless_rx"].search(text)):
        score *= adjust["build_noise"]
    time_sensitive, importance = False, 0.0
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
        importance = _importance(headlines, article, clef)
    if _host_matches(article.get("url"), profile["paywalled"]):
        score *= adjust["paywalled"]
    passed = score + 1e-9 >= profile["deliver_min"]
    headline = headlines is not None and not pure and importance + 1e-9 >= headlines["at"]
    deliver = passed or headline  # a headline is delivered whatever its tier score; alerts keep the tier rule
    return _verdict(group, score, deliver=deliver, alert=passed and time_sensitive,
                    reason="delivered" if passed else "headline" if headline else "below threshold",
                    importance=importance, headline=headline)
