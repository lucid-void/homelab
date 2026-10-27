# News profile scoring: feed tags, keywords, Clef confirmation

Date: 2026-10-27. Status: design approved in chat (delivery choice made), spec awaiting review. Builds on `2026-10-06-clef-triage-and-news-design.md`.

## Goal

Replace the news module's "N free-text topics, one Clef question per topic" scoring with the owner's real interest profile (14 groups in 3 tiers, dislikes, ruled-out products), loaded from git. Articles from the 104 verified feeds are scored; those scoring at least 0.7 reach the owner in the twice-daily digest, and time-sensitive Tier 3 items are sent immediately.

Success: a day of real feed traffic (~110 items) yields a short digest (about 15 to 25 items), no spoilers for Expedition 33 or Steel Ball Run, no ruled-out products, press releases without numbers demoted, and Clef load of a few CPU-minutes a day.

## Evidence behind the design (2026-10-27 measurements)

- 104 feeds, about 110 items/day. Plain keywords from the profile match 39% of items; on a hand-labelled hard sample they gave 12 hits, 13 false alarms, 3 misses (homonyms: Signal, Matrix, Pixel, Thread, Matter).
- Clef as a broad "relevant to me" classifier was about equally noisy (57% precision, about 7 s per call). Clef as a marketing-without-numbers detector separated cleanly (mean 0.75 vs 0.18).
- FreshRSS `ot` filters on when FreshRSS stored the article, so an OPML import makes the whole backlog look new. The bot only ever sees the newest N unread articles per poll.

## Decisions

- The profile lives in git as `news-profile.json` (the bot is stdlib-only and has no YAML parser), delivered by the existing `configMapGenerator`.
- Feed = topic where possible: the FreshRSS category (imported from the OPML) names the group; GitHub release feeds are "pure" and match their group with no keywords and no Clef.
- Keywords decide candidates elsewhere; ambiguous words are only counted inside the right categories.
- Clef is a second stage on candidates only, with at most 3 yes/no questions per article in one call.
- Delivery: digest at 08:00 and 18:00 (Brussels) plus an immediate Telegram alert for Tier 3 items Clef judges time-sensitive.
- `/topics` keeps working for ad-hoc extra topics (old per-topic Clef path, scored as 0.8) and also lists the profile groups read-only.

## Profile file (`news-profile.json`)

```
version            integer; a change re-seeds the module (see Upgrade)
tiers              {"1": 1.0, "2": 0.6, "3": 0.3}
deliver_min        0.7
adjust             {"marketing": 0.6 (multiplier), "substantive": 0.2 (add), "major": 0.5 (add), "paywalled": 0.5 (multiplier)}
pure_feed_pattern  regex on the feed's site URL (origin.htmlUrl); default "github\.com/[^/]+/[^/]+/(releases|tags)"
groups             {key: {title, tier, categories[], keywords[], scoped_keywords[]}}
ruled_out          [terms]  -> article dropped when a term appears in the title
spoilers           {topics[], allow[]}
paywalled_domains  [hosts]
build_noise        {title_regex, unless_keywords[]}   (llama.cpp "bNNNN" builds)
questions          the exact Clef question texts (see below), so wording is tunable without code
```

The 14 groups: Tier 1 `hpc_infra`, `linux_desktop`, `homelab`, `local_ai`, `keyboards_electronics`, `printing_3d`; Tier 2 `mobile_privacy`, `gaming`, `pokemon_tcg`, `keyboard_business`, `shopping`, `investing`, `career`; Tier 3 `situational` (events, travel disruption, photography, anime announcements, home audio, EU consumer rights). Keywords, ruled-out terms and spoiler topics are taken verbatim from the owner's profile; `categories` are the OPML category names already imported into FreshRSS (e.g. "T1 Homelab").

## Matching and scoring (pure functions in `profile.py`)

1. **Candidates.** A group matches an article when (a) the article is from a pure release feed whose category is in the group's `categories`, or (b) a `keywords` entry matches the title or summary as a whole word, case-insensitive, or (c) a `scoped_keywords` entry matches AND the article's category is in the group's `categories`. The best group is the one with the lowest tier number (first listed on ties).
2. **Drops without Clef.** A `ruled_out` term in the title drops the article. A `build_noise` title (`^b\d+`) is multiplied by 0.5 unless an `unless_keywords` entry matches.
3. **Base score** is `tiers[tier]`. Pure release feeds also get `substantive` (+0.2) so a Tier 2 release (GrapheneOS) can pass, and skip Clef.
4. **Clef call** (non-pure candidates only; one `Clef.noul` call, at most 3 questions): `marketing` (all tiers), `substantive` (Tier 2), `major` and `time_sensitive` (Tier 3), `spoiler` (only if the text mentions a spoiler topic and contains no `allow` term).
   - `spoiler` at least 0.5: drop.
   - `marketing` at least 0.5: score x 0.6.
   - `substantive` at least 0.5: +0.2. `major` at least 0.5: +0.5.
5. **Paywalled domain** (article URL host in the list): score x 0.5.
6. **Deliver** when score >= `deliver_min` (0.7). Typical outcomes: Tier 1 article 1.0; Tier 1 press release 0.6 (dropped); Tier 2 substantive 0.8; Tier 3 major 0.8.
7. **Alert** when the group is Tier 3, the article is delivered, and `time_sensitive` is at least 0.5: it is sent immediately and excluded from the digest.

If any Clef call fails, the article is left unseen and retried next poll (nothing lost). A candidate with no Clef answer is never delivered unscored.

## Module changes

- `freshrss.py`: each article gains `feed` (origin title), `site` (origin.htmlUrl), `categories` (labels from `user/-/label/...`), unchanged otherwise; `unread(limit=250)` in the news poll; a new `labels()` call returns the category names that have subscriptions (used once at startup for the category check in Risks).
- `profile.py` (new): load and validate the profile (any problem returns an error string; the news module then stays off with a log line, the bot keeps running), candidate matching, scoring, question building.
- `news.py`: poll uses `profile.py` for profile groups and the existing per-topic path for custom `/topics`; digest groups items by tier (Tier 1, 2, 3), sorted by score then newest, at most 25 per digest, remainder carried to the next one; alert path sends immediately and is deduplicated through `seen`; `/topics` lists "Profile groups" read-only and "Your topics"; pending items carry `tier`, `score`, `group`.
- `main.py`: loads `/scripts/news-profile.json`, passes the profile to `News`.
- Kustomization: `news-profile.json` added to the `configMapGenerator` (Reloader restarts the bot on edits).

## Upgrade and safety

- **Re-seed on upgrade.** State stores `profile_version`. If it is missing or differs from the loaded profile's `version`, the next poll behaves like a first run: `since` is set to now and the current unread items are recorded as seen without scoring, so the imported backlog never reaches Clef or the digest.
- Alerts are sent through `Telegram.send` and use the same restore-on-failure pattern as the digest (a failed alert is retried on the next pass).
- Clef failure, FreshRSS failure and corrupt state behave as in the existing module (fail soft, log once, nothing marked seen on failure).
- The Clef client for news keeps its own 60 s timeout.

## Testing

- `profile.py` is pure and unit-tested with hand-built articles: pure-feed match, scoped-keyword homonyms (Signal in privacy feeds counts; Signal in an electronics feed does not), ruled-out drop, build-noise, each adjustment and the 0.7 boundary, spoiler allow-list, paywalled host, question selection per tier, malformed profile files.
- `news.py` tests use a fake Clef and fake source: scoring path, alert path and its dedup, digest tiering and cap, re-seed on version change, custom topics still work, Clef failure leaves articles unseen.
- Before enabling, a dry run in the cluster scores live unread items with real Clef (nothing is sent) and the owner reviews the top 30 and a sample of rejections; thresholds and question wording are adjusted in `news-profile.json` only.

## Risks

1. Clef question wording may need tuning (the marketing question was validated on 6 labelled items only). Mitigation: dry run and JSON-only tuning.
2. Pure-feed rule over-delivers for noisy repos (only llama.cpp builds identified so far). Mitigation: `build_noise`; add similar rules from the dry run.
3. Category names are coupled to the OPML import; a renamed category silently stops matching. Mitigation: profile validation logs categories that no subscribed feed uses (checked at startup).
4. Tier 3 alerts depend on Clef's time-sensitivity judgement; mislabels cost a few extra messages, never lost items.

## Out of scope (v1)

Stale-guide and Windows/macOS-only demotion (no reliable signal), cross-feed duplicate merging, Dutch retailer sources, per-feed priority weights, language handling beyond what the feeds provide, and any change to Gotify triage.
