#!/usr/bin/env python3
"""FreshRSS over its Google Reader compatible API (user + API password; no OIDC involved)."""
import html
import http.client
import json
import re
import urllib.parse
import urllib.request

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
SUMMARY_MAX = 600


class FreshRSSError(Exception):
    pass


def plain(text, limit=SUMMARY_MAX):
    return SPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()[:limit]


class FreshRSS:
    def __init__(self, base_url, user, password, timeout=20, opener=urllib.request.urlopen):
        self.base, self.user, self.password = base_url.rstrip("/") + "/api/greader.php", user, password
        self.timeout, self.opener = timeout, opener

    def _login(self):
        data = urllib.parse.urlencode({"Email": self.user, "Passwd": self.password}).encode()
        with self.opener(urllib.request.Request(f"{self.base}/accounts/ClientLogin", data=data),
                         timeout=self.timeout) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith("Auth="):
                    return line[5:]
        raise FreshRSSError("login")

    def unread(self, limit=100, since=None):
        """Newest-first unread articles as {id, title, summary, url}; `since` (epoch seconds) drops older ones."""
        try:
            token = self._login()
            params = {"xt": "user/-/state/com.google/read", "n": limit, "output": "json"}
            if since is not None:
                params["ot"] = int(since)
            query = urllib.parse.urlencode(params)
            req = urllib.request.Request(
                f"{self.base}/reader/api/0/stream/contents/reading-list?{query}",
                headers={"Authorization": f"GoogleLogin auth={token}"})
            with self.opener(req, timeout=self.timeout) as resp:
                items = json.load(resp)["items"]
            if not isinstance(items, list):
                raise FreshRSSError("shape")
            articles = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                links = item.get("canonical") or item.get("alternate") or [{}]
                articles.append({
                    "id": item["id"],
                    "title": plain(item.get("title"), 300) or "(no title)",
                    "summary": plain((item.get("summary") or {}).get("content")),
                    "url": links[0].get("href") or "",
                })
            return articles
        except FreshRSSError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, http.client.HTTPException) as e:
            raise FreshRSSError(type(e).__name__) from e
