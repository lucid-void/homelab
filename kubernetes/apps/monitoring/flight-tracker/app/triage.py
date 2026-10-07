#!/usr/bin/env python3
"""Decide whether a Gotify message is worth a Telegram message. Always fails open."""
import time

from clef import ClefError
from core import log

CRITICAL = 8
THRESHOLD = 0.5
COOLDOWN = 300      # seconds without asking Clef after it failed, so a dead Clef costs one timeout
MAX_TITLE = 200
MAX_MESSAGE = 2000   # the Clef service refuses bodies over 64 KiB, which would silently fail open
QUESTION = ("Does this notification need the owner's attention? Yes for failures, outages, "
            "security findings or anything that needs action. No for routine success reports, "
            "tests and informational events.")


def is_critical(msg):
    """Priority >= 8, or no usable priority: never judged, always forwarded."""
    p = msg.get("priority")
    return isinstance(p, bool) or not isinstance(p, int) or p >= CRITICAL


class Triage:
    def __init__(self, clef, threshold=THRESHOLD, cooldown=COOLDOWN, clock=time.monotonic):
        self.clef, self.threshold, self.cooldown, self.clock = clef, threshold, cooldown, clock
        self.skip_until = 0.0

    def judge(self, msg):
        """True = forward. False only when Clef answered and said the message is noise."""
        if is_critical(msg):
            return True
        if self.clock() < self.skip_until:
            return True
        state = {"title": str(msg.get("title") or "")[:MAX_TITLE],
                 "message": str(msg.get("message") or "")[:MAX_MESSAGE],
                 "priority": msg.get("priority")}
        try:
            p = self.clef.noul(state, {"needed": QUESTION})["needed"]
        except ClefError as e:
            self.skip_until = self.clock() + self.cooldown
            log(f"triage unavailable ({e}), forwarding unjudged for {self.cooldown:g}s")
            return True
        keep = p >= self.threshold
        log(f"triage message {msg.get('id')} p={p:.2f} {'forward' if keep else 'drop'}")
        return keep
