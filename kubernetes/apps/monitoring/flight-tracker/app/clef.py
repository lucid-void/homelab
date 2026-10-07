#!/usr/bin/env python3
"""Client for the in-cluster Clef service (SystemOne request shape, `noul` questions only)."""
import json
import urllib.request


class ClefError(Exception):
    """Clef could not answer. Callers fail open (alerts) or retry later (news)."""


class Clef:
    def __init__(self, base_url, timeout=15, opener=urllib.request.urlopen):
        self.base_url, self.timeout, self.opener = base_url.rstrip("/"), timeout, opener

    def noul(self, state, questions):
        """questions: {id: instruction text}. Returns {id: probability the answer is yes}."""
        body = {"model": "clef-flash", "state": state,
                "questions": {qid: {"type": "noul", "instructions": text}
                              for qid, text in questions.items()}}
        req = urllib.request.Request(
            f"{self.base_url}/v1/systemone", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                answers = json.load(resp)["answers"]
            result = {}
            for qid in questions:
                p = answers[qid]["noul"]
                if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 1:
                    raise ValueError("probability")
                result[qid] = float(p)
            return result
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ClefError(type(e).__name__) from e
