#!/usr/bin/env python3
"""/topics and a twice-daily digest of FreshRSS articles that match the owner's topics."""
import html as htmllib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import interests
from clef import ClefError
from core import log
from freshrss import FreshRSSError

UTC = timezone.utc
POLL_SECONDS = 1800
LOOP_SECONDS = 30
TOPICS_PER_CALL = 3     # topics scored per Clef call: about 7 s per call on CPU
MAX_PER_POLL = 40       # every article costs a headline call (about 8 s), so a full poll is 6 to 9 minutes
MAX_TOPICS = 10
MAX_TOPIC_LEN = 80
MAX_SEEN = 5000
MAX_PENDING = 200
DIGEST_MAX_CHARS = 3500
TIER_QUOTA = {1: 10, 2: 8, 3: 4}   # items per digest; each tier cycles through its groups
MAX_ALERTS = 50
CUSTOM_SCORE = 0.8      # an extra /topics match counts as a Tier 2 article that passed the threshold
HEADLINES = 0           # section and tier number of big news; an item with tier 0 matched no profile group
TIER_TITLES = {HEADLINES: "🚨 Headlines", 1: "🔥 Core", 2: "📰 Regular", 3: "🧭 Situational"}
THRESHOLD = 0.5
USAGE = "Usage: /topics [add <topic> | rm <topic or number>]"


class News:
    name = "news"
    help = [
        "/topics - the news topics you follow",
        "/topics add <topic> - follow a topic",
        "/topics rm <topic or number> - unfollow",
    ]

    def __init__(self, ctx, chat_id, source, clef, now=lambda: datetime.now(UTC), tz=UTC,
                 digest_hours=(8, 18), profile=None):
        self.ctx, self.chat, self.source, self.clef = ctx, str(chat_id), source, clef
        self.profile = profile  # compiled interest profile (interests.py); None = custom topics only
        self.now, self.tz, self.hours = now, tz, tuple(sorted(digest_hours))
        self.lock = threading.RLock()
        self.failing = False
        self.commands = {"topics": self._command}
        self.callbacks = {}
        self.state = self._load()

    # -- persistence --

    def _load(self):
        default = {"topics": {}, "seen": None, "pending": [], "last_slot": None, "since": None,
                   "profile_version": None, "alerts": []}
        try:
            with open(self.ctx.state_path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return default
        except ValueError:
            log("news state unreadable, starting empty")
            return default
        ok = (isinstance(raw, dict) and isinstance(raw.get("topics"), dict)
              and all(isinstance(v, list) and all(isinstance(t, str) for t in v)
                      for v in raw["topics"].values())
              and (raw.get("seen") is None or isinstance(raw["seen"], list))
              and isinstance(raw.get("pending"), list)
              and (raw.get("last_slot") is None or isinstance(raw["last_slot"], str))
              and (raw.get("since") is None
                   or (isinstance(raw["since"], (int, float)) and not isinstance(raw["since"], bool)))
              and (raw.get("profile_version") is None
                   or (isinstance(raw["profile_version"], int) and not isinstance(raw["profile_version"], bool)))
              and isinstance(raw.get("alerts", []), list))
        if not ok:
            log("news state has the wrong shape, starting empty")
            return default
        state = {key: raw.get(key) for key in default}
        state["alerts"] = state["alerts"] or []
        return state

    def _save(self):
        tmp = self.ctx.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.ctx.state_path)

    # -- topics --

    def _topics(self):
        return self.state["topics"].setdefault(self.chat, [])

    def _list_text(self):
        topics = self._topics()
        if self.profile is None:
            if not topics:
                return "No topics yet. Add one: /topics add kubernetes security"
            return "Topics:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(topics, 1))
        lines = ["Profile groups (from git):"]
        lines += [f"- {title} (tier {tier})" for title, tier in interests.group_lines(self.profile)]
        if topics:
            lines += ["", "Your extra topics:"] + [f"{i}. {t}" for i, t in enumerate(topics, 1)]
        else:
            lines += ["", "No extra topics. Add one: /topics add kubernetes security"]
        return "\n".join(lines)

    def _command(self, args):
        with self.lock:
            topics = self._topics()
            if not args:
                self.ctx.send(self._list_text())
            elif args[0] == "add" and len(args) > 1:
                text = " ".join(args[1:]).strip()
                if len(text) > MAX_TOPIC_LEN:
                    self.ctx.send(f"Topic too long (max {MAX_TOPIC_LEN} characters).")
                elif text.lower() in (t.lower() for t in topics):
                    self.ctx.send(f"Already following: {text}")
                elif len(topics) >= MAX_TOPICS:
                    self.ctx.send(f"At most {MAX_TOPICS} topics. Remove one first.")
                else:
                    topics.append(text)
                    self._save()
                    self.ctx.send(f"Following: {text}\n\n" + self._list_text())
            elif args[0] == "rm" and len(args) > 1:
                text = " ".join(args[1:]).strip()
                index = int(text) - 1 if text.isdigit() else next(
                    (i for i, t in enumerate(topics) if t.lower() == text.lower()), -1)
                if 0 <= index < len(topics):
                    removed = topics.pop(index)
                    self._save()
                    self.ctx.send(f"Unfollowed: {removed}\n\n" + self._list_text())
                else:
                    self.ctx.send(f"Not following: {text}")
            else:
                self.ctx.send(USAGE)

    # -- polling --

    def poll_once(self):
        with self.lock:
            reseed = self.profile is not None and self.state["profile_version"] != self.profile["version"]
            if self.state["since"] is None or reseed:
                # first run, or a new profile: articles stored before now are never fetched or scored
                self.state["since"] = int(self.now().timestamp())
                self.state["seen"] = None
                if self.profile is not None:
                    self.state["profile_version"] = self.profile["version"]
                self._save()
            since = self.state["since"]
        try:
            articles = self.source.unread(since=since)
        except FreshRSSError as e:
            self._fail(f"freshrss unavailable ({e})")
            return False
        with self.lock:
            topics = list(self._topics())
            if self.state["seen"] is None:  # first run: seed what is already there, do not replay it
                self.state["seen"] = [a["id"] for a in articles]
                self._save()
                self.failing = False
                return True
            seen = set(self.state["seen"])
            queued = list(self.state["pending"])
        fresh = [a for a in articles if a["id"] not in seen]
        if not topics and self.profile is None:  # nothing to match against; do not hold these back
            self._finish(fresh, [])
            return True
        same = self.profile["same_story"] if self.profile is not None else None
        scored, matches, alerts = [], [], []
        for article in fresh[:MAX_PER_POLL]:
            try:
                item, is_alert = self._score(article, topics)
                if item is not None and not is_alert and same is not None:
                    item["story"] = self._story(article, queued + matches, same)
            except ClefError as e:
                self._fail(f"clef unavailable ({e}), articles stay unseen")
                break
            scored.append(article)
            if item is not None:
                (alerts if is_alert else matches).append(item)
        else:
            self.failing = False
        self._finish(scored, matches, alerts)
        return not self.failing

    def _score(self, article, topics):
        """(pending item or None, is_alert). ClefError propagates so the article is retried later."""
        verdict = None
        if self.profile is not None:
            result = interests.assess(self.profile, article, self.clef)
            if result["deliver"]:
                verdict = result
        hit = self._custom_hits(article, topics)
        base = {"id": article["id"], "title": article["title"], "url": article["url"]}
        if verdict is not None:
            rank = {"importance": verdict["importance"], "headline": verdict["headline"]}
            if verdict["group"] is None:  # big news outside every profile group
                return ({**base, **rank, "topics": hit, "tier": 2 if hit else HEADLINES,
                         "score": CUSTOM_SCORE if hit else 0.0, "group": None, "custom": hit}, False)
            return ({**base, **rank, "topics": [verdict["group"]] + hit, "tier": verdict["tier"],
                     "score": verdict["score"], "group": verdict["group"], "custom": hit}, verdict["alert"])
        if not hit:
            return None, False
        if self.profile is None:
            return {**base, "topics": hit}, False
        return {**base, "topics": hit, "tier": 2, "score": CUSTOM_SCORE, "group": None, "custom": hit}, False

    def _custom_hits(self, article, topics):
        state = {"title": article["title"], "summary": article["summary"]}
        hit = []
        for start in range(0, len(topics), TOPICS_PER_CALL):
            chunk = list(enumerate(topics))[start:start + TOPICS_PER_CALL]
            probs = self.clef.noul(state, {f"t{i}": f"Is this article about: {t}?" for i, t in chunk})
            hit += [t for i, t in chunk if probs[f"t{i}"] >= THRESHOLD]
        return hit

    def _story(self, article, queued, same):
        """The story id for a new item: that of a queued item Clef says is the same story, else its own id.
        One call, one question per candidate story; candidates are the queued titles sharing most words."""
        picks, stories = [], set()
        for i in interests.story_candidates(article["title"], [q["title"] for q in queued], len(queued)):
            story = queued[i].get("story", queued[i]["id"])
            if story not in stories:
                stories.add(story)
                picks.append((queued[i]["title"], story))
            if len(picks) == same["max"]:
                break
        if not picks:
            return article["id"]
        probs = self.clef.noul({"title": article["title"], "summary": ""},  # titles on both sides: a fast call
                               {f"same{n}": same["question"].replace(interests.TITLE_SLOT, title)
                                for n, (title, _) in enumerate(picks)})
        best = max(range(len(picks)), key=lambda n: probs[f"same{n}"])
        return picks[best][1] if probs[f"same{best}"] >= same["at"] else article["id"]

    def _fail(self, text):
        if not self.failing:
            log(text)
        self.failing = True

    def _finish(self, scored, matches, alerts=()):
        with self.lock:
            self.state["seen"] = (self.state["seen"] + [a["id"] for a in scored])[-MAX_SEEN:]
            self.state["pending"] = (self.state["pending"] + matches)[-MAX_PENDING:]
            self.state["alerts"] = (self.state["alerts"] + list(alerts))[-MAX_ALERTS:]
            self._save()

    def send_alerts(self):
        """Send queued alerts one by one; a failed send keeps the rest queued for the next pass."""
        while True:
            with self.lock:
                if not self.state["alerts"]:
                    return True
                item = self.state["alerts"][0]
            ok = self.ctx.send(f"⏰ {self._link(item)}", html=True)
            if ok is False:
                return False
            with self.lock:
                self.state["alerts"] = [a for a in self.state["alerts"] if a["id"] != item["id"]]
                self._save()

    # -- digest --

    def _slot(self):
        """The most recent scheduled digest time at or before now, as 'YYYY-MM-DDTHH' in local time."""
        local = self.now().astimezone(self.tz)
        past = [h for h in self.hours if h <= local.hour]
        day, hour = (local, past[-1]) if past else (local - timedelta(days=1), self.hours[-1])
        return f"{day.date().isoformat()}T{hour:02d}"

    def maybe_digest(self):
        with self.lock:
            slot = self._slot()
            last = self.state["last_slot"]
            if last is not None and slot <= last:
                return False
            self.state["last_slot"] = slot
            if last is None:  # first ever check: start the schedule, do not send on install
                self._save()
                return False
            followed = {t.lower() for t in self._topics()}  # drop matches for topics unfollowed since
            pending = []
            for item in self.state["pending"]:
                if (self.profile is not None and self.profile["skip_release_feeds"]
                        and self.profile["pure_rx"].search(item.get("url") or "")):
                    continue  # queued before release feeds were skipped
                if "tier" in item and (item.get("group") is not None or item.get("headline")):
                    # a profile match or a headline: nothing to unfollow
                    pending.append(item)
                    continue
                names = item.get("custom", item["topics"]) if "tier" in item else item["topics"]
                kept = [t for t in names if t.lower() in followed]
                if kept:
                    pending.append({**item, "topics": kept, **({"custom": kept} if "tier" in item else {})})
            if not pending:
                self.state["pending"] = []
                self._save()
                return False
            text, remaining = self._digest_text(pending)
            self.state["pending"] = remaining
            self._save()
        ok = self.ctx.send(text, html=self.profile is not None)
        if ok is False:
            with self.lock:
                self.state["pending"] = pending
                self.state["last_slot"] = last
                self._save()
            return False
        return True

    @staticmethod
    def _link(item):
        e = lambda t: htmllib.escape(t, quote=True)
        return f'<a href="{e(item["url"])}">{e(item["title"])}</a>'

    @staticmethod
    def _stories(pending):
        """One entry per story: the lead (most important article), how many articles, and the best of each."""
        members = {}
        for i, item in enumerate(pending):
            members.setdefault(item.get("story", item["id"]), []).append(i)
        stories = []
        for indexes in members.values():
            lead = max(indexes, key=lambda i: (pending[i].get("importance", 0.0),
                                                pending[i].get("score", CUSTOM_SCORE), -i))
            tiers = [t for t in (pending[i].get("tier", 2) for i in indexes) if t != HEADLINES]
            stories.append({
                "lead": lead, "count": len(indexes), "tier": min(tiers) if tiers else HEADLINES,
                "headline": any(pending[i].get("headline") for i in indexes),
                "importance": pending[lead].get("importance", 0.0),
                "score": max(pending[i].get("score", CUSTOM_SCORE) for i in indexes),
                "group": pending[lead].get("group") or tuple(pending[lead]["topics"])})
        return stories

    def _pick(self, pending):
        """(section, story) to show. Headlines first, the most important up to the profile's cap; then per tier
        up to its quota, cycling through the groups, most important first (then best score, then newest)."""
        stories = self._stories(pending)
        order = lambda s: (-s["importance"], -s["score"], -s["lead"])
        cap = self.profile["headlines"]["max"] if self.profile["headlines"] is not None else 0
        top = sorted((s for s in stories if s["headline"]), key=order)[:cap]
        chosen = [(HEADLINES, s) for s in top]
        for tier in (1, 2, 3):
            buckets = {}
            for s in sorted((s for s in stories if s["tier"] == tier and s not in top), key=order):
                buckets.setdefault(s["group"], []).append(s)
            queues, taken = list(buckets.values()), []
            while queues and len(taken) < TIER_QUOTA[tier]:
                for q in list(queues):
                    if len(taken) >= TIER_QUOTA[tier]:
                        break
                    taken.append(q.pop(0))
                    if not q:
                        queues.remove(q)
            chosen += [(tier, s) for s in sorted(taken, key=order)]
        return chosen, len(stories)

    def _digest_text(self, pending):
        """(message text in HTML, items to keep for the next digest: none)."""
        if self.profile is None:
            return self._digest_text_plain(pending)
        picked, total = self._pick(pending)
        lines, size, current, shown = [], 0, None, 0
        for section, story in picked:
            more = f" (+{story['count'] - 1})" if story["count"] > 1 else ""  # other articles on the same story
            line = f"• {self._link(pending[story['lead']])}{more}"
            if shown and size + len(line) > DIGEST_MAX_CHARS:
                break
            if section != current:
                lines.append(("" if not lines else "\n") + f"<b>{TIER_TITLES[section]}</b>")
                current = section
            lines.append(line)
            size += len(line) + 1
            shown += 1
        if total > shown:  # a digest is the news since the last one: what did not fit is dropped, not carried
            lines.append(f"\n{total - shown} more skipped")
        return f"📰 <b>News digest</b> · {shown} item{'s' if shown != 1 else ''}\n\n" + "\n".join(lines), []

    def _digest_text_plain(self, pending):
        lines, size, used = [], 0, 0
        for item in pending:
            line = f"- {item['title']} ({', '.join(item['topics'])})\n  {item['url']}".rstrip()
            if used and size + len(line) > DIGEST_MAX_CHARS:
                break
            lines.append(line)
            size += len(line) + 1
            used += 1
        head = f"News digest: {used} match{'es' if used != 1 else ''}"
        if used < len(pending):
            lines.append(f"+{len(pending) - used} more in the next digest")
        return head + "\n\n" + "\n".join(lines), pending[used:]

    # -- loop --

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        next_poll = 0.0
        while True:
            try:
                if time.monotonic() >= next_poll:
                    self.poll_once()
                    next_poll = time.monotonic() + POLL_SECONDS
                self.send_alerts()
                self.maybe_digest()
            except Exception as e:  # keep the module alive
                log(f"news loop failed: {type(e).__name__}")
            time.sleep(LOOP_SECONDS)
