#!/usr/bin/env python3
"""/topics and a twice-daily digest of FreshRSS articles that match the owner's topics."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from clef import ClefError
from core import log
from freshrss import FreshRSSError

UTC = timezone.utc
POLL_SECONDS = 1800
LOOP_SECONDS = 30
MAX_PER_POLL = 40       # about 7 s each on CPU, so a poll stays under 5 minutes
MAX_TOPICS = 10
MAX_TOPIC_LEN = 80
MAX_SEEN = 5000
MAX_PENDING = 200
DIGEST_MAX_CHARS = 3500
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
                 digest_hours=(8, 18)):
        self.ctx, self.chat, self.source, self.clef = ctx, str(chat_id), source, clef
        self.now, self.tz, self.hours = now, tz, tuple(sorted(digest_hours))
        self.lock = threading.RLock()
        self.failing = False
        self.commands = {"topics": self._command}
        self.callbacks = {}
        self.state = self._load()

    # -- persistence --

    def _load(self):
        default = {"topics": {}, "seen": None, "pending": [], "last_slot": None}
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
              and (raw.get("last_slot") is None or isinstance(raw["last_slot"], str)))
        if not ok:
            log("news state has the wrong shape, starting empty")
            return default
        return {key: raw.get(key) for key in default}

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
        if not topics:
            return "No topics yet. Add one: /topics add kubernetes security"
        return "Topics:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(topics, 1))

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
        try:
            articles = self.source.unread()
        except FreshRSSError as e:
            self._fail(f"freshrss unavailable ({e})")
            return False
        with self.lock:
            topics = list(self._topics())
            if self.state["seen"] is None:  # first run: do not replay the whole unread backlog
                self.state["seen"] = [a["id"] for a in articles]
                self._save()
                self.failing = False
                return True
            seen = set(self.state["seen"])
        fresh = [a for a in articles if a["id"] not in seen]
        if not topics:  # nothing to match against; do not hold these back for a later topic
            self._finish(fresh, [])
            return True
        scored, matches = [], []
        for article in fresh[:MAX_PER_POLL]:
            questions = {f"t{i}": f"Is this article about: {t}?" for i, t in enumerate(topics)}
            try:
                probs = self.clef.noul({"title": article["title"], "summary": article["summary"]}, questions)
            except ClefError as e:
                self._fail(f"clef unavailable ({e}), articles stay unseen")
                break
            scored.append(article)
            hit = [t for i, t in enumerate(topics) if probs[f"t{i}"] >= THRESHOLD]
            if hit:
                matches.append({"id": article["id"], "title": article["title"],
                                "url": article["url"], "topics": hit})
        else:
            self.failing = False
        self._finish(scored, matches)
        return not self.failing

    def _fail(self, text):
        if not self.failing:
            log(text)
        self.failing = True

    def _finish(self, scored, matches):
        with self.lock:
            self.state["seen"] = (self.state["seen"] + [a["id"] for a in scored])[-MAX_SEEN:]
            self.state["pending"] = (self.state["pending"] + matches)[-MAX_PENDING:]
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
            pending = self.state["pending"]
            if not pending:
                self._save()
                return False
            text, used = self._digest_text(pending)
            self.state["pending"] = pending[used:]
            self._save()
        self.ctx.send(text)
        return True

    def _digest_text(self, pending):
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
        return head + "\n\n" + "\n".join(lines), used

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
                self.maybe_digest()
            except Exception as e:  # keep the module alive
                log(f"news loop failed: {type(e).__name__}")
            time.sleep(LOOP_SECONDS)
