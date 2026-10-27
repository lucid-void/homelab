# News profile scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Telegram bot's news module score FreshRSS articles against the owner's interest profile (14 groups, 3 tiers) loaded from git, deliver those scoring 0.7 or more in the twice-daily digest, and send time-sensitive Tier 3 items immediately.

**Architecture:** A new pure module `interests.py` matches an article to a profile group (FreshRSS category = topic for GitHub release feeds, whole-word keywords elsewhere, ambiguous words only inside the right categories) and scores it, asking Clef at most 3 yes/no questions per candidate. `news.py` calls it from the poll loop, queues alerts and tiered digest items, and re-seeds when the profile `version` changes. The profile is `news-profile.json`, delivered in the existing ConfigMap.

**Tech Stack:** Python stdlib (`unittest`, `re`, `json`), Kustomize, Flux.

**Spec:** `docs/superpowers/specs/2026-10-27-news-profile-scoring-design.md` (read it first). Two deliberate deviations, both already verified: the engine module is `interests.py`, not `profile.py` (Python ships a `profile` module that a same-named file would shadow); and a spoiler check is a separate one-question Clef call, so a normal call never exceeds 3 questions.

## Global Constraints

- Test command (repo root, must end `OK`): `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`. Baseline before this plan: 239 tests; after Task 5: 308.
- The bot stays stdlib-only Python. No new dependencies, no YAML parser (the profile is JSON).
- All k8s tooling runs as `mise exec -- kubectl|kubeconform|kubeseal|flux ...`; `kubectl` is for diagnostics only; every config change goes through git and Flux.
- After editing a manifest under `kubernetes/`, run `.agents/scripts/validate-manifests.sh <path>`.
- Never write a raw Secret; never touch the live cluster except in Task 7 (and only after the owner says yes there).
- The module is named `interests.py`. Never create a file called `profile.py` in the app directory.
- Delivery threshold `0.7`; tier weights `1.0 / 0.6 / 0.3`; adjustments: marketing x0.6, Tier 2 substantive +0.2, Tier 3 major +0.5, paywalled x0.5, llama.cpp build noise x0.5.
- Every Clef call has at most 3 questions; Clef failure leaves an article unseen (retried), never delivered unscored.
- A broken or missing profile turns the news module off with one log line; it must never crash the bot.
- End every commit message with the attribution trailers your session instructions specify.
- Work on branch `feat/news-profile` (create it in Task 1) ideally in a git worktree; stage files explicitly; never stage the owner's two unrelated modified files (`design/decisions/jellyfin.md`, `design/docs/storage.md`) or the untracked `news-feeds.opml`.

## Review Focus

- **Homonyms (Signal, Matrix, Pixel, Thread, Matter) in electronics or general feeds must not become news.** Tests: `test_scoped_keyword_only_counts_inside_the_group_category` (Task 2), `test_homonyms_do_not_match_outside_their_category` (Task 3).
- **A Telegram outage must not lose a Tier 3 alert.** Tests: `test_a_failed_alert_send_is_kept_and_retried`, `test_queued_alerts_survive_a_restart` (Task 4).
- **Clef down in the middle of a poll must not mark articles seen or drop an alert.** Test: `test_clef_failure_leaves_the_article_unseen_and_loses_nothing` (Task 4).
- **A typo in `news-profile.json` must not crash the bot.** Tests: `ValidationTests` (Task 2), `test_a_missing_or_broken_profile_turns_news_off_with_a_log_line` (Task 5).
- **Deploying a new profile must not replay the imported backlog through Clef.** Test: `test_changed_profile_version_reseeds_instead_of_scoring_the_backlog` (Task 4).
- **Matches queued before the upgrade (no tier) must still reach the digest.** Test: `test_legacy_pending_items_without_a_tier_appear_under_tier_2` (Task 4).

---

## File map

| File | Responsibility |
|---|---|
| `kubernetes/apps/monitoring/flight-tracker/app/interests.py` (new) | load and validate the profile; match an article to a group; score it with Clef answers |
| `kubernetes/apps/monitoring/flight-tracker/app/news-profile.json` (new) | the owner's 14 groups, keywords, dislikes, thresholds and Clef question wording |
| `kubernetes/apps/monitoring/flight-tracker/app/freshrss.py` | articles gain `feed`, `site`, `categories`; `labels()`; default window 250 |
| `kubernetes/apps/monitoring/flight-tracker/app/news.py` | uses `interests.py`; alerts queue; tiered digest; re-seed on profile version change |
| `kubernetes/apps/monitoring/flight-tracker/app/main.py` | loads the profile, checks categories, passes the profile to `News` |
| `kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml` | ConfigMap carries `interests.py` and `news-profile.json` |
| `kubernetes/apps/monitoring/flight-tracker/tests/test_interests.py`, `test_news_profile_file.py` (new); `test_freshrss.py`, `test_news.py`, `test_main.py` | tests |
| `design/decisions/flight-tracker.md`, the 2026-10-27 spec | docs |

---

### Task 1: FreshRSS client carries feed, site and category labels

**Files:**
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/freshrss.py`
- Modify: `kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py`

**Interfaces:**
- Produces: `FreshRSS.unread(limit=250, since=None)` articles are `{id, title, summary, url, feed, site, categories}` (`feed` = origin title, `site` = origin `htmlUrl`, `categories` = FreshRSS labels such as `"T1 Homelab"`, all default to `""`/`[]`); `FreshRSS.labels() -> set[str]` of category names that have subscriptions, raising `FreshRSSError` on any failure.

- [ ] **Step 1: Create the branch**

```bash
git switch -c feat/news-profile
```

- [ ] **Step 2: Add the tests**

Apply this patch (new fixtures and five new tests, one updated assertion):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py b/kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py
index d026b49..d276113 100644
--- a/kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py
+++ b/kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py
@@ -21,8 +21,9 @@ class Resp(io.BytesIO):
 class FakeServer:
     """Answers ClientLogin, then the stream request."""
 
-    def __init__(self, items=None, login="SID=x\nLSID=y\nAuth=alice/tok123\n", error=None):
+    def __init__(self, items=None, login="SID=x\nLSID=y\nAuth=alice/tok123\n", error=None, subscriptions=None):
         self.items, self.login, self.error, self.requests = items, login, error, []
+        self.subscriptions = subscriptions
 
     def __call__(self, req, timeout):
         self.requests.append(req)
@@ -30,12 +31,19 @@ class FakeServer:
             raise self.error
         if req.full_url.endswith("/accounts/ClientLogin"):
             return Resp(self.login.encode())
+        if "/subscription/list" in req.full_url:
+            return Resp(json.dumps({"subscriptions": self.subscriptions}).encode())
         return Resp(json.dumps({"items": self.items}).encode())
 
 
 def item(id="tag:google.com,2005:reader/item/1", title="T", content="<p>Hello &amp; <b>bye</b></p>",
-         href="https://x.example/a"):
-    return {"id": id, "title": title, "summary": {"content": content}, "canonical": [{"href": href}]}
+         href="https://x.example/a", origin=None, categories=None):
+    d = {"id": id, "title": title, "summary": {"content": content}, "canonical": [{"href": href}]}
+    if origin is not None:
+        d["origin"] = origin
+    if categories is not None:
+        d["categories"] = categories
+    return d
 
 
 class FreshRSSTests(unittest.TestCase):
@@ -64,7 +72,38 @@ class FreshRSSTests(unittest.TestCase):
     def test_articles_are_flattened_to_plain_text(self):
         [a] = self.client(FakeServer([item()])).unread()
         self.assertEqual(a, {"id": "tag:google.com,2005:reader/item/1", "title": "T",
-                             "summary": "Hello & bye", "url": "https://x.example/a"})
+                             "summary": "Hello & bye", "url": "https://x.example/a",
+                             "feed": "", "site": "", "categories": []})
+
+    def test_feed_site_and_category_labels_come_from_the_item(self):
+        origin = {"streamId": "feed/7", "title": "Flux releases", "htmlUrl": "https://github.com/fluxcd/flux2/releases"}
+        cats = ["user/-/state/com.google/reading-list", "user/-/label/T1 Homelab", "user/-/label/Extra", 5, None]
+        [a] = self.client(FakeServer([item(origin=origin, categories=cats)])).unread()
+        self.assertEqual((a["feed"], a["site"], a["categories"]),
+                         ("Flux releases", "https://github.com/fluxcd/flux2/releases", ["T1 Homelab", "Extra"]))
+
+    def test_odd_origin_and_category_shapes_are_tolerated(self):
+        for origin, cats in (("x", "y"), ([], {}), (None, [[]])):
+            with self.subTest(origin=origin, cats=cats):
+                [a] = self.client(FakeServer([item(origin=origin, categories=cats)])).unread()
+                self.assertEqual((a["feed"], a["site"], a["categories"]), ("", "", []))
+
+    def test_default_window_is_250_articles(self):
+        s = FakeServer([item()])
+        self.client(s).unread()
+        self.assertEqual(parse_qs(urlparse(s.requests[1].full_url).query)["n"], ["250"])
+
+    def test_labels_lists_the_categories_that_have_subscriptions(self):
+        subs = [{"categories": [{"id": "user/-/label/A", "label": "A"}]},
+                {"categories": [{"label": "B"}, {"label": "A"}]}, {"categories": []}, {}]
+        s = FakeServer(subscriptions=subs)
+        self.assertEqual(self.client(s).labels(), {"A", "B"})
+        self.assertIn("/subscription/list", s.requests[1].full_url)
+
+    def test_labels_failures_are_freshrss_errors(self):
+        for s in (FakeServer(error=OSError("down")), FakeServer(subscriptions=None), FakeServer(subscriptions="x")):
+            with self.subTest(s=s), self.assertRaises(freshrss.FreshRSSError):
+                self.client(s).labels()
 
     def test_long_summary_is_cut_and_missing_fields_are_tolerated(self):
         long = item(content="x" * 5000)
PATCH
```

- [ ] **Step 3: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: failures and errors in `test_freshrss` only (fields missing, `labels` not defined, default window 100).

- [ ] **Step 4: Change the client**

Apply this patch (adds feed/site/categories, labels(), default limit 250):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/app/freshrss.py b/kubernetes/apps/monitoring/flight-tracker/app/freshrss.py
index b464102..1a406ba 100644
--- a/kubernetes/apps/monitoring/flight-tracker/app/freshrss.py
+++ b/kubernetes/apps/monitoring/flight-tracker/app/freshrss.py
@@ -20,6 +20,17 @@ def plain(text, limit=SUMMARY_MAX):
     return SPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()[:limit]
 
 
+def _text(value):
+    return value if isinstance(value, str) else ""
+
+
+def _labels(categories):
+    """Category labels from an item's `categories` ("user/-/label/<name>" entries); anything else is ignored."""
+    if not isinstance(categories, list):
+        return []
+    return [c.split("/label/", 1)[1] for c in categories if isinstance(c, str) and "/label/" in c]
+
+
 class FreshRSS:
     def __init__(self, base_url, user, password, timeout=20, opener=urllib.request.urlopen):
         self.base, self.user, self.password = base_url.rstrip("/") + "/api/greader.php", user, password
@@ -34,8 +45,9 @@ class FreshRSS:
                     return line[5:]
         raise FreshRSSError("login")
 
-    def unread(self, limit=100, since=None):
-        """Newest-first unread articles as {id, title, summary, url}; `since` (epoch seconds) drops older ones."""
+    def unread(self, limit=250, since=None):
+        """Newest-first unread articles as {id, title, summary, url, feed, site, categories};
+        `since` (epoch seconds) drops articles FreshRSS stored earlier."""
         try:
             token = self._login()
             params = {"xt": "user/-/state/com.google/read", "n": limit, "output": "json"}
@@ -54,14 +66,35 @@ class FreshRSS:
                 if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                     continue
                 links = item.get("canonical") or item.get("alternate") or [{}]
+                origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
                 articles.append({
                     "id": item["id"],
                     "title": plain(item.get("title"), 300) or "(no title)",
                     "summary": plain((item.get("summary") or {}).get("content")),
                     "url": links[0].get("href") or "",
+                    "feed": _text(origin.get("title")),
+                    "site": _text(origin.get("htmlUrl")),
+                    "categories": _labels(item.get("categories")),
                 })
             return articles
         except FreshRSSError:
             raise
         except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, http.client.HTTPException) as e:
             raise FreshRSSError(type(e).__name__) from e
+
+    def labels(self):
+        """The category names that have at least one subscription."""
+        try:
+            token = self._login()
+            req = urllib.request.Request(f"{self.base}/reader/api/0/subscription/list?output=json",
+                                         headers={"Authorization": f"GoogleLogin auth={token}"})
+            with self.opener(req, timeout=self.timeout) as resp:
+                subs = json.load(resp)["subscriptions"]
+            if not isinstance(subs, list):
+                raise FreshRSSError("shape")
+            return {c["label"] for s in subs if isinstance(s, dict) for c in s.get("categories") or []
+                    if isinstance(c, dict) and isinstance(c.get("label"), str)}
+        except FreshRSSError:
+            raise
+        except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException) as e:
+            raise FreshRSSError(type(e).__name__) from e
PATCH
```

- [ ] **Step 5: Run all tests**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/freshrss.py kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py
git commit -m "feat(telegram-bot): FreshRSS articles carry feed, site and category labels"
```

---

### Task 2: The scoring engine (`interests.py`)

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/interests.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_interests.py`

**Interfaces:**
- Produces: `interests.ProfileError`; `load_profile(path) -> dict` and `compile_profile(raw) -> dict` (compiled profile; raises `ProfileError` on any problem); `group_lines(profile) -> [(title, tier)]`; `missing_categories(profile, labels) -> [str]`; `match_group(profile, article) -> (group, pure) | None`; `assess(profile, article, clef) -> {deliver, alert, score, tier, group, reason}` where `clef.noul(state, {id: text}) -> {id: float}` (raises `ClefError`, which propagates). Compiled profile keys used by later tasks: `profile["version"]`, `profile["groups"]` (each has `title`, `tier`, `categories` as a set).
- Article dict consumed: `{id, title, summary, url, categories, site}`.

- [ ] **Step 1: Write the failing tests**

Create `kubernetes/apps/monitoring/flight-tracker/tests/test_interests.py`:

```python
import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import clef  # noqa: E402
import interests  # noqa: E402

RAW = {
    "version": 3,
    "tiers": {"1": 1.0, "2": 0.6, "3": 0.3},
    "deliver_min": 0.7,
    "yes_at": 0.5,
    "adjust": {"marketing": 0.6, "substantive": 0.2, "major": 0.5, "paywalled": 0.5, "build_noise": 0.5},
    "pure_feed_pattern": r"github\.com/[^/]+/[^/]+/(releases|tags)",
    "groups": {
        "homelab": {"title": "Homelab", "tier": 1, "categories": ["T1 Homelab"],
                    "keywords": ["Proxmox", "Cilium"], "scoped_keywords": ["Flux"]},
        "ai": {"title": "Local AI", "tier": 1, "categories": ["T1 AI"],
               "keywords": ["llama.cpp", "GGUF"], "scoped_keywords": ["Vulkan"]},
        "privacy": {"title": "Privacy", "tier": 2, "categories": ["T2 Privacy"],
                    "keywords": ["GrapheneOS"], "scoped_keywords": ["Signal", "Matrix"]},
        "gaming": {"title": "Gaming", "tier": 2, "categories": ["T2 Gaming"],
                   "keywords": ["Expedition 33"], "scoped_keywords": []},
        "other": {"title": "Situational", "tier": 3, "categories": ["T3 Other"],
                  "keywords": ["FOSDEM", "Steel Ball Run"], "scoped_keywords": ["strike"]},
    },
    "ruled_out": ["TrueNAS"],
    "spoilers": {"topics": ["Expedition 33", "Steel Ball Run"], "allow": ["patch notes", "release date"]},
    "paywalled_domains": ["ft.com"],
    "build_noise": {"title_regex": r"^b\d{3,}", "unless_keywords": ["Vulkan"]},
    "questions": {"marketing": "M?", "substantive": "S?", "major": "J?", "time_sensitive": "T?", "spoiler": "X?"},
}


def make(**changes):
    raw = copy.deepcopy(RAW)
    raw.update(changes)
    return interests.compile_profile(raw)


def article(title="t", summary="", url="https://x.example/a", categories=(), site=""):
    return {"id": "a", "title": title, "summary": summary, "url": url,
            "categories": list(categories), "site": site}


class FakeClef:
    """Answers each question id with a configured probability (default 0.1) and records the calls."""

    def __init__(self, **answers):
        self.answers, self.calls, self.error = answers, [], None

    def noul(self, state, questions):
        self.calls.append(sorted(questions))
        if self.error:
            raise self.error
        return {q: self.answers.get(q, 0.1) for q in questions}


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.p = make()

    def group(self, a):
        m = interests.match_group(self.p, a)
        return m and (m[0]["key"], m[1])

    def test_keyword_matches_whole_words_case_insensitively(self):
        self.assertEqual(self.group(article("New proxmox release")), ("homelab", False))
        self.assertIsNone(self.group(article("Proxmoxy things")))

    def test_regex_characters_in_keywords_are_literal(self):
        self.assertEqual(self.group(article("llama.cpp adds support")), ("ai", False))
        self.assertIsNone(self.group(article("llamaXcpp adds support")))

    def test_scoped_keyword_only_counts_inside_the_group_category(self):
        self.assertIsNone(self.group(article("Signal analyzer for ADCs", categories=["T1 AI"])))
        self.assertEqual(self.group(article("Signal adds backups", categories=["T2 Privacy"])), ("privacy", False))

    def test_pure_release_feed_matches_its_category_without_keywords(self):
        a = article("v1.2.3", site="https://github.com/org/repo/releases", categories=["T1 Homelab"])
        self.assertEqual(self.group(a), ("homelab", True))

    def test_release_feed_in_an_unrelated_category_does_not_match(self):
        a = article("v1.2.3", site="https://github.com/org/repo/releases", categories=["Core"])
        self.assertIsNone(self.group(a))

    def test_lowest_tier_wins_and_ties_go_to_the_first_group(self):
        a = article("Proxmox on GrapheneOS and llama.cpp and FOSDEM")
        self.assertEqual(self.group(a), ("homelab", False))

    def test_no_match(self):
        self.assertIsNone(self.group(article("Celebrity news")))


class AssessTests(unittest.TestCase):
    def setUp(self):
        self.p = make()

    def run_assess(self, a, clef_obj=None):
        c = clef_obj or FakeClef()
        return interests.assess(self.p, a, c), c

    def test_no_match_is_not_delivered_and_asks_nothing(self):
        v, c = self.run_assess(article("Celebrity news"))
        self.assertFalse(v["deliver"])
        self.assertEqual(c.calls, [])

    def test_tier1_keyword_article_is_delivered_after_one_marketing_check(self):
        v, c = self.run_assess(article("Cilium 1.20 adds things"))
        self.assertEqual((v["deliver"], v["score"], v["tier"], v["group"]), (True, 1.0, 1, "Homelab"))
        self.assertEqual(c.calls, [["marketing"]])

    def test_marketing_demotes_tier1_below_the_threshold(self):
        v, _ = self.run_assess(article("Cilium partner announcement"), FakeClef(marketing=0.9))
        self.assertEqual((v["deliver"], v["score"]), (False, 0.6))

    def test_pure_release_feed_skips_clef_entirely(self):
        a = article("v1.2.3", site="https://github.com/org/repo/releases", categories=["T1 Homelab"])
        v, c = self.run_assess(a)
        self.assertEqual((v["deliver"], v["score"]), (True, 1.0))
        self.assertEqual(c.calls, [])

    def test_tier2_release_feed_passes_without_clef(self):
        a = article("20261027", site="https://github.com/o/GrapheneOS/releases", categories=["T2 Privacy"])
        v, c = self.run_assess(a)
        self.assertEqual((v["deliver"], v["score"]), (True, 0.8))
        self.assertEqual(c.calls, [])

    def test_tier2_needs_a_substantive_yes(self):
        a = article("GrapheneOS news", categories=["T2 Privacy"])
        no, c = self.run_assess(a)
        self.assertEqual((no["deliver"], no["score"]), (False, 0.6))
        self.assertEqual(c.calls, [["marketing", "substantive"]])
        yes, _ = self.run_assess(a, FakeClef(substantive=0.9))
        self.assertEqual((yes["deliver"], yes["score"]), (True, 0.8))

    def test_tier3_needs_major_news_and_alerts_only_when_time_sensitive(self):
        a = article("FOSDEM tickets", categories=["T3 Other"])
        quiet, c = self.run_assess(a)
        self.assertEqual((quiet["deliver"], quiet["alert"], quiet["score"]), (False, False, 0.3))
        self.assertEqual(c.calls, [["major", "marketing", "time_sensitive"]])
        major, _ = self.run_assess(a, FakeClef(major=0.9))
        self.assertEqual((major["deliver"], major["alert"], major["score"]), (True, False, 0.8))
        urgent, _ = self.run_assess(a, FakeClef(major=0.9, time_sensitive=0.9))
        self.assertEqual((urgent["deliver"], urgent["alert"]), (True, True))

    def test_time_sensitive_alone_does_not_alert_below_the_threshold(self):
        v, _ = self.run_assess(article("FOSDEM tickets", categories=["T3 Other"]), FakeClef(time_sensitive=0.9))
        self.assertEqual((v["deliver"], v["alert"]), (False, False))

    def test_ruled_out_term_drops_without_asking_clef(self):
        v, c = self.run_assess(article("TrueNAS and Proxmox"))
        self.assertEqual((v["deliver"], v["reason"]), (False, "ruled out"))
        self.assertEqual(c.calls, [])

    def test_spoiler_yes_drops_and_asks_first(self):
        a = article("Expedition 33 ending explained", categories=["T2 Gaming"])
        v, c = self.run_assess(a, FakeClef(spoiler=0.9, substantive=0.9))
        self.assertEqual((v["deliver"], v["reason"]), (False, "spoiler"))
        self.assertEqual(c.calls, [["spoiler"]])

    def test_spoiler_check_passes_then_normal_scoring_follows(self):
        a = article("Expedition 33 secrets", categories=["T2 Gaming"])
        v, c = self.run_assess(a, FakeClef(substantive=0.9))
        self.assertTrue(v["deliver"])
        self.assertEqual(c.calls, [["spoiler"], ["marketing", "substantive"]])

    def test_spoiler_allow_term_skips_the_spoiler_question(self):
        a = article("Expedition 33 patch notes 1.2", categories=["T2 Gaming"])
        _, c = self.run_assess(a, FakeClef(substantive=0.9))
        self.assertEqual(c.calls, [["marketing", "substantive"]])

    def test_paywalled_host_and_its_subdomains_are_halved(self):
        for url in ("https://ft.com/x", "https://www.ft.com/x"):
            v, _ = self.run_assess(article("Cilium story", url=url))
            self.assertEqual((v["score"], v["deliver"]), (0.5, False), url)
        v, _ = self.run_assess(article("Cilium story", url="https://soft.com/x"))
        self.assertTrue(v["deliver"])

    def test_build_noise_is_halved_unless_a_keyword_rescues_it(self):
        site = "https://github.com/ggml-org/llama.cpp/releases"
        noisy = article("b11918: opencl: q3_k (#31172)", site=site, categories=["T1 AI"])
        v, _ = self.run_assess(noisy)
        self.assertEqual((v["deliver"], v["score"]), (False, 0.5))
        rescued = article("b11919: Vulkan: faster matmul", site=site, categories=["T1 AI"])
        v, _ = self.run_assess(rescued)
        self.assertEqual((v["deliver"], v["score"]), (True, 1.0))

    def test_threshold_boundary_is_inclusive(self):
        p = make(deliver_min=0.6)
        v = interests.assess(p, article("Cilium partner announcement"), FakeClef(marketing=0.9))
        self.assertTrue(v["deliver"])

    def test_clef_errors_propagate_so_the_caller_can_retry(self):
        c = FakeClef()
        c.error = clef.ClefError("OSError")
        with self.assertRaises(clef.ClefError):
            interests.assess(self.p, article("Cilium news"), c)


class HelperTests(unittest.TestCase):
    def test_group_lines_keep_profile_order(self):
        self.assertEqual(interests.group_lines(make())[:2], [("Homelab", 1), ("Local AI", 1)])

    def test_missing_categories_lists_unused_profile_categories(self):
        self.assertEqual(interests.missing_categories(make(), ["T1 Homelab", "Other"]),
                         ["T1 AI", "T2 Gaming", "T2 Privacy", "T3 Other"])


class ValidationTests(unittest.TestCase):
    def bad(self, mutate):
        raw = copy.deepcopy(RAW)
        mutate(raw)
        with self.assertRaises(interests.ProfileError):
            interests.compile_profile(raw)

    def test_each_kind_of_damage_is_a_profile_error(self):
        cases = [
            lambda r: r.pop("version"), lambda r: r.update(version="1"), lambda r: r.update(version=True),
            lambda r: r["tiers"].pop("2"), lambda r: r["tiers"].update({"1": "high"}),
            lambda r: r.pop("deliver_min"), lambda r: r.pop("yes_at"),
            lambda r: r["adjust"].pop("marketing"),
            lambda r: r["questions"].pop("spoiler"), lambda r: r["questions"].update(major=""),
            lambda r: r.update(groups={}), lambda r: r.update(groups=[]),
            lambda r: r["groups"]["ai"].update(tier=4), lambda r: r["groups"]["ai"].update(tier=True),
            lambda r: r["groups"]["ai"].update(title=""), lambda r: r["groups"]["ai"].update(categories=[]),
            lambda r: r["groups"]["ai"].update(keywords="x"), lambda r: r["groups"]["ai"].update(keywords=[1]),
            lambda r: r.update(pure_feed_pattern="("), lambda r: r.pop("pure_feed_pattern"),
            lambda r: r["build_noise"].update(title_regex="["), lambda r: r.update(spoilers=[]),
            lambda r: r.update(ruled_out="TrueNAS"), lambda r: r.update(paywalled_domains=[3]),
        ]
        for i, mutate in enumerate(cases):
            with self.subTest(case=i):
                self.bad(mutate)

    def test_not_an_object(self):
        for raw in ([], "x", 3, None):
            with self.assertRaises(interests.ProfileError):
                interests.compile_profile(raw)

    def test_load_profile_reports_unreadable_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(interests.ProfileError):
                interests.load_profile(os.path.join(d, "missing.json"))
            path = os.path.join(d, "bad.json")
            with open(path, "w") as f:
                f.write("{nope")
            with self.assertRaises(interests.ProfileError):
                interests.load_profile(path)
            with open(path, "w") as f:
                json.dump(RAW, f)
            self.assertEqual(interests.load_profile(path)["version"], 3)

    def test_optional_lists_may_be_omitted(self):
        raw = copy.deepcopy(RAW)
        for key in ("ruled_out", "paywalled_domains"):
            raw.pop(key)
        raw["spoilers"] = {}
        raw["build_noise"].pop("unless_keywords")
        raw["groups"]["ai"].pop("scoped_keywords")
        interests.compile_profile(raw)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `ModuleNotFoundError: No module named 'interests'`.

- [ ] **Step 3: Write the engine**

Create `kubernetes/apps/monitoring/flight-tracker/app/interests.py`:

```python
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
```

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/interests.py kubernetes/apps/monitoring/flight-tracker/tests/test_interests.py
git commit -m "feat(telegram-bot): interest-profile matching and scoring engine"
```

---

### Task 3: The owner's profile file

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/news-profile.json`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_news_profile_file.py`

**Interfaces:**
- Consumes: `interests.load_profile`, `match_group`, `assess`, `group_lines`, `missing_categories` (Task 2).
- Produces: `news-profile.json` with `version` 1 and 14 groups whose `categories` equal the OPML category names already imported into FreshRSS.

- [ ] **Step 1: Write the failing tests**

Create `kubernetes/apps/monitoring/flight-tracker/tests/test_news_profile_file.py`:

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import interests  # noqa: E402

PATH = os.path.join(os.path.dirname(__file__), "..", "app", "news-profile.json")
# Category names created by the OPML import (docs: the owner's FreshRSS).
OPML_CATEGORIES = {
    "T1 HPC/AI infra", "T1 Linux desktop", "T1 Homelab", "T1 Local AI / agents", "T1 Keyboards/electronics",
    "T1 3D printing", "T2 Mobile privacy", "T2 Gaming", "T2 Pokemon TCG", "T2 EU compliance/business",
    "T2 Shopping/hardware", "T2 Investing/career", "T3 events/travel/other", "Core already in OPML"}


class Quiet:
    """A Clef stand-in that never says yes."""

    def noul(self, state, questions):
        return {q: 0.1 for q in questions}


def art(title, categories, summary="", site="", url="https://example.org/a"):
    return {"id": "a", "title": title, "summary": summary, "url": url, "categories": categories, "site": site}


class NewsProfileFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = interests.load_profile(PATH)

    def verdict(self, a):
        return interests.assess(self.p, a, Quiet())

    def test_the_file_loads_with_the_fourteen_groups(self):
        self.assertEqual(len(self.p["groups"]), 14)
        self.assertEqual([t for _, t in interests.group_lines(self.p)], [1] * 6 + [2] * 7 + [3])

    def test_every_group_category_exists_in_the_opml_import(self):
        self.assertEqual(interests.missing_categories(self.p, OPML_CATEGORIES), [])

    def test_homonyms_do_not_match_outside_their_category(self):
        for title in ("Signal analyzer, 10 Gbps USB logic analyzer", "Pixel art fish tank for an LED matrix",
                      "Thread loose in NVIDIA whitepaper", "What matter most to AI resilience"):
            with self.subTest(title=title):
                m = interests.match_group(self.p, art(title, ["T1 Keyboards/electronics"]))
                self.assertTrue(m is None or m[0]["key"] != "mobile_privacy")
                self.assertTrue(m is None or m[0]["key"] != "shopping")

    def test_a_homonym_does_match_inside_its_category(self):
        m = interests.match_group(self.p, art("Introducing Signal Secure Backups", ["T2 Mobile privacy"]))
        self.assertEqual(m[0]["key"], "mobile_privacy")

    def test_github_release_feeds_are_pure_matches(self):
        a = art("v2.10.0", ["T1 Homelab"], site="https://github.com/fluxcd/flux2/releases")
        v = self.verdict(a)
        self.assertEqual((v["deliver"], v["group"]), (True, "Homelab and self-hosting"))

    def test_llama_cpp_build_numbers_are_noise_unless_vulkan(self):
        site = "https://github.com/ggml-org/llama.cpp/releases"
        noisy = self.verdict(art("b11918: opencl: initial support for q3_k (#31172)", ["T1 Local AI / agents"], site=site))
        rescued = self.verdict(art("b11919: vulkan: faster matmul", ["T1 Local AI / agents"], site=site))
        self.assertEqual((noisy["deliver"], rescued["deliver"]), (False, True))

    def test_ruled_out_products_are_dropped(self):
        v = self.verdict(art("Bambu X2D review", ["T1 3D printing"]))
        self.assertEqual((v["deliver"], v["reason"]), (False, "ruled out"))

    def test_expedition_33_patch_notes_are_allowed_but_spoilers_are_not(self):
        class Spoils(Quiet):
            def noul(self, state, questions):
                return {q: 0.9 if q == "spoiler" else 0.1 for q in questions}
        patch = interests.assess(self.p, art("Expedition 33 patch notes 1.4", ["T2 Gaming"]), Spoils())
        plot = interests.assess(self.p, art("Expedition 33 ending explained", ["T2 Gaming"]), Spoils())
        self.assertNotEqual(patch["reason"], "spoiler")
        self.assertEqual(plot["reason"], "spoiler")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: errors in `test_news_profile_file` (`FileNotFoundError` for `news-profile.json`).

- [ ] **Step 3: Write the profile**

Create `kubernetes/apps/monitoring/flight-tracker/app/news-profile.json`:

```json
{
  "version": 1,
  "tiers": {
    "1": 1.0,
    "2": 0.6,
    "3": 0.3
  },
  "deliver_min": 0.7,
  "yes_at": 0.5,
  "adjust": {
    "marketing": 0.6,
    "substantive": 0.2,
    "major": 0.5,
    "paywalled": 0.5,
    "build_noise": 0.5
  },
  "pure_feed_pattern": "github\\.com/[^/]+/[^/]+/(releases|tags)",
  "groups": {
    "hpc_infra": {
      "title": "HPC and AI datacenter infra",
      "tier": 1,
      "categories": [
        "T1 HPC/AI infra"
      ],
      "keywords": [
        "HPE Cray",
        "Cray EX",
        "HPCM",
        "Slurm",
        "Slingshot",
        "InfiniBand",
        "RoCE",
        "LUMI",
        "Sigma2",
        "EuroHPC",
        "Top500",
        "AMD Instinct",
        "EPYC",
        "GB300",
        "NVL72",
        "DCGM",
        "Run:AI",
        "Kubeflow",
        "GPFS",
        "Rocky Linux",
        "RHEL 9"
      ],
      "scoped_keywords": [
        "VAST",
        "DDN"
      ]
    },
    "linux_desktop": {
      "title": "Linux desktop and graphics",
      "tier": 1,
      "categories": [
        "T1 Linux desktop"
      ],
      "keywords": [
        "CachyOS",
        "Arch Linux",
        "Hyprland",
        "Quickshell",
        "Wayland",
        "PipeWire",
        "RADV",
        "Navi 31",
        "Neovim",
        "LazyVim",
        "AstroNvim",
        "HDR on Linux"
      ],
      "scoped_keywords": [
        "Mesa",
        "Proton",
        "HDR"
      ]
    },
    "homelab": {
      "title": "Homelab and self-hosting",
      "tier": 1,
      "categories": [
        "T1 Homelab"
      ],
      "keywords": [
        "Proxmox",
        "FluxCD",
        "Flux CD",
        "Cilium",
        "Gateway API",
        "Terraform",
        "Packer",
        "Ansible",
        "Renovate",
        "Zitadel",
        "CloudNativePG",
        "VictoriaMetrics",
        "UniFi",
        "ZeroTier",
        "Jellyfin",
        "Sonarr",
        "Radarr",
        "Prowlarr",
        "yt-dlp",
        "ytdl-sub",
        "Immich",
        "Paperless-ngx",
        "FreshRSS",
        "Sealed Secrets",
        "democratic-csi",
        "MS-02",
        "UGREEN",
        "Synology"
      ],
      "scoped_keywords": [
        "Flux",
        "Talos"
      ]
    },
    "local_ai": {
      "title": "Local AI and agentic tooling",
      "tier": 1,
      "categories": [
        "T1 Local AI / agents"
      ],
      "keywords": [
        "llama.cpp",
        "Intel Arc",
        "Qwen",
        "gpt-oss",
        "GGUF",
        "unsloth",
        "bartowski",
        "LM Studio",
        "Claude Code",
        "agentic coding",
        "open-weight"
      ],
      "scoped_keywords": [
        "MoE",
        "MCP",
        "Vulkan",
        "agentic"
      ]
    },
    "keyboards_electronics": {
      "title": "Split keyboards and DIY electronics",
      "tier": 1,
      "categories": [
        "T1 Keyboards/electronics"
      ],
      "keywords": [
        "ZMK",
        "QMK",
        "Zephyr",
        "Corne",
        "crkbd",
        "Charybdis",
        "PMW3610",
        "trackball",
        "Gateron",
        "nice!nano",
        "nice!view",
        "splitkb",
        "KiCad",
        "JLCPCB",
        "STM32",
        "RP2040",
        "nRF52840",
        "MeshCore",
        "Meshtastic",
        "USB-C PD",
        "split keyboard",
        "mechanical keyboard"
      ],
      "scoped_keywords": [
        "ESP32",
        "LoRa"
      ]
    },
    "printing_3d": {
      "title": "3D printing",
      "tier": 1,
      "categories": [
        "T1 3D printing"
      ],
      "keywords": [
        "Bambu",
        "H2D",
        "P3C",
        "H3C",
        "OrcaSlicer",
        "FULU",
        "Prusa",
        "Snapmaker",
        "toolchanger",
        "multi-material",
        "3D printer",
        "3D printing"
      ],
      "scoped_keywords": []
    },
    "mobile_privacy": {
      "title": "Mobile privacy",
      "tier": 2,
      "categories": [
        "T2 Mobile privacy"
      ],
      "keywords": [
        "GrapheneOS",
        "UnifiedPush",
        "microG",
        "Molly",
        "Element X",
        "Aurora Store",
        "Obtainium",
        "F-Droid",
        "ntfy"
      ],
      "scoped_keywords": [
        "Signal",
        "Matrix",
        "Pixel"
      ]
    },
    "gaming": {
      "title": "Gaming",
      "tier": 2,
      "categories": [
        "T2 Gaming"
      ],
      "keywords": [
        "Expedition 33",
        "Marvel Rivals",
        "Guild Wars 2",
        "ProtonDB",
        "Atmosphere",
        "Atmosphère",
        "CTGP"
      ],
      "scoped_keywords": []
    },
    "pokemon_tcg": {
      "title": "Pokémon TCG",
      "tier": 2,
      "categories": [
        "T2 Pokemon TCG"
      ],
      "keywords": [
        "Pokemon TCG",
        "Pokémon TCG",
        "Cardmarket",
        "pull rate",
        "Mega Dream",
        "Gem Pack"
      ],
      "scoped_keywords": [
        "Pokemon",
        "Pokémon"
      ]
    },
    "keyboard_business": {
      "title": "Keyboard business (EU)",
      "tier": 2,
      "categories": [
        "T2 EU compliance/business"
      ],
      "keywords": [
        "GPSR",
        "Radio Equipment Directive",
        "EN 18031",
        "Battery Regulation",
        "WEEE",
        "bijberoep"
      ],
      "scoped_keywords": [
        "Etsy"
      ]
    },
    "shopping": {
      "title": "Gear you are shopping for",
      "tier": 2,
      "categories": [
        "T2 Shopping/hardware"
      ],
      "keywords": [
        "XREAL",
        "RayNeo",
        "Viture",
        "Breezy Desktop",
        "AirPods Pro 3",
        "RTX 5090",
        "Homey",
        "IKEA smart home"
      ],
      "scoped_keywords": [
        "Matter",
        "Thread"
      ]
    },
    "investing": {
      "title": "Investing",
      "tier": 2,
      "categories": [
        "T2 Investing/career"
      ],
      "keywords": [
        "SanDisk",
        "Alphabet",
        "AMD",
        "NVIDIA",
        "semiconductor",
        "Belgian investment tax"
      ],
      "scoped_keywords": []
    },
    "career": {
      "title": "Career and industry moves",
      "tier": 2,
      "categories": [
        "T2 Investing/career"
      ],
      "keywords": [
        "HPE",
        "Lenovo ISG",
        "solutions architect",
        "pre-sales"
      ],
      "scoped_keywords": []
    },
    "situational": {
      "title": "Situational (events, travel, other)",
      "tier": 3,
      "categories": [
        "T3 events/travel/other"
      ],
      "keywords": [
        "Fri3d",
        "BruCON",
        "40C3",
        "FOSDEM",
        "NMBS",
        "De Lijn",
        "SNCF",
        "Brussels Airport",
        "Nikon D300",
        "aurora forecast",
        "Steel Ball Run",
        "Sonos",
        "AirPlay",
        "Nanoleaf",
        "EU261",
        "ECC Belgium"
      ],
      "scoped_keywords": [
        "strike",
        "staking",
        "CCC",
        "aurora"
      ]
    }
  },
  "ruled_out": [
    "Bambu X2D",
    "Rokid Max 2",
    "Rokid AR Spatial",
    "Kimi K2.7",
    "Sony XM4",
    "Nothing Ear",
    "UE Fits",
    "TrueNAS",
    "HP DL360 Gen9"
  ],
  "spoilers": {
    "topics": [
      "Expedition 33",
      "Steel Ball Run"
    ],
    "allow": [
      "patch notes",
      "hotfix",
      "release date",
      "announcement",
      "announces"
    ]
  },
  "paywalled_domains": [
    "ft.com",
    "wsj.com",
    "bloomberg.com",
    "theinformation.com",
    "economist.com",
    "nytimes.com",
    "barrons.com",
    "tijd.be",
    "seekingalpha.com",
    "hbr.org"
  ],
  "build_noise": {
    "title_regex": "^b\\d{3,}",
    "unless_keywords": [
      "Vulkan",
      "Intel Arc",
      "SYCL"
    ]
  },
  "questions": {
    "marketing": "Is this mainly marketing or a press release without measured numbers or benchmarks?",
    "substantive": "Does this contain substantive technical or factual content, such as release notes, measurements, a security issue or a concrete announcement, rather than a vague overview?",
    "major": "Is this major news in its field, such as a significant release, a security incident, a policy change or a large change in price or availability?",
    "time_sensitive": "Is this time-sensitive and actionable soon, such as a strike, an event ticket sale, a call for papers deadline, a recall or a service disruption?",
    "spoiler": "Does this reveal plot details or story spoilers?"
  }
}
```

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/news-profile.json kubernetes/apps/monitoring/flight-tracker/tests/test_news_profile_file.py
git commit -m "feat(telegram-bot): the owner's news interest profile"
```

---

### Task 4: The news module uses the profile

**Files:**
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/news.py`
- Modify: `kubernetes/apps/monitoring/flight-tracker/tests/test_news.py`

**Interfaces:**
- Consumes: `interests.assess`, `interests.group_lines` (Task 2); `FreshRSS.unread` article fields (Task 1); `tests/test_interests.py` fixture `RAW` (the new news tests import it).
- Produces: `News(ctx, chat_id, source, clef, now=..., tz=UTC, digest_hours=(8, 18), profile=None)`. With `profile=None` behaviour is exactly as before. With a profile: state gains `profile_version` and `alerts`; pending items gain `tier`, `score`, `group`, `custom`; `News.send_alerts() -> bool` (True when the queue is empty or fully sent); the digest is grouped by tier, capped at `DIGEST_MAX_ITEMS = 25`; `/topics` lists the profile groups read-only above the custom topics.

- [ ] **Step 1: Add the tests**

Apply this patch (profile fixtures, ProfileClef, Harness hooks, ProfileTests and ProfileDigestTests):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/tests/test_news.py b/kubernetes/apps/monitoring/flight-tracker/tests/test_news.py
index 6ba9c07..5d7fd1c 100644
--- a/kubernetes/apps/monitoring/flight-tracker/tests/test_news.py
+++ b/kubernetes/apps/monitoring/flight-tracker/tests/test_news.py
@@ -1,3 +1,4 @@
+import copy
 import json
 import os
 import sys
@@ -9,7 +10,9 @@ sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
 import clef  # noqa: E402
 import core  # noqa: E402
 import freshrss  # noqa: E402
+import interests  # noqa: E402
 import news  # noqa: E402
+import test_interests  # noqa: E402  (shares its small profile fixture)
 
 UTC = timezone.utc
 T0 = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
@@ -46,19 +49,40 @@ class FakeClef:
                 for qid, text in questions.items()}
 
 
+class ProfileClef(FakeClef):
+    """Profile questions get the configured probability (default 0.1); custom-topic questions work as in FakeClef."""
+
+    def __init__(self, **answers):
+        super().__init__()
+        self.answers = answers
+
+    def noul(self, state, questions):
+        if any(q.startswith("t") and q[1:].isdigit() for q in questions):
+            return super().noul(state, questions)
+        self.calls.append((state, questions))
+        if self.error:
+            raise self.error
+        return {q: self.answers.get(q, 0.1) for q in questions}
+
+
 class Harness:
+    profile = None
+
     def __init__(self):
         self.dir = tempfile.TemporaryDirectory()
         unittest.addModuleCleanup(self.dir.cleanup)
         self.path = os.path.join(self.dir.name, "news.json")
         self.now, self.sent = T0, []
         self.send_ok = True
-        self.source, self.clef = FakeSource(), FakeClef()
+        self.source, self.clef = FakeSource(), self.make_clef()
         self.news = self.make()
 
+    def make_clef(self):
+        return FakeClef()
+
     def make(self):
         ctx = core.Ctx("news", lambda text, buttons=None: (self.sent.append(text), self.send_ok)[1], self.path)
-        return news.News(ctx, CHAT, self.source, self.clef, now=lambda: self.now)
+        return news.News(ctx, CHAT, self.source, self.clef, now=lambda: self.now, profile=self.profile)
 
     def say(self, *args):
         self.news.commands["topics"](list(args))
@@ -371,5 +395,232 @@ class DigestTests(unittest.TestCase):
         self.assertTrue(all(isinstance(x, str) for x in h.news.help))
 
 
+def profile_for_tests(version=3):
+    raw = copy.deepcopy(test_interests.RAW)
+    raw["version"] = version
+    return interests.compile_profile(raw)
+
+
+def part(n, title, categories=(), site="", url=None, summary=""):
+    return {**art(n, title, url), "summary": summary, "categories": list(categories), "site": site, "feed": "f"}
+
+
+class ProfileHarness(Harness):
+    def __init__(self, version=3, **answers):
+        self.profile = profile_for_tests(version)
+        self._answers = answers
+        super().__init__()
+
+    def make_clef(self):
+        return ProfileClef(**self._answers)
+
+    def started(self):
+        """Run the first poll so later polls score instead of seeding."""
+        self.seed()
+        self.sent.clear()
+        return self
+
+
+RELEASE = "https://github.com/org/repo/releases"
+
+
+class ProfileTests(unittest.TestCase):
+    def test_first_poll_seeds_the_backlog_and_records_the_profile_version(self):
+        h = ProfileHarness()
+        h.poll([part(1, "Cilium old news", ["T1 Homelab"])])
+        self.assertEqual(h.clef.calls, [])
+        st = h.state()
+        self.assertEqual((st["seen"], st["profile_version"], st["pending"]), (["a1"], 3, []))
+
+    def test_changed_profile_version_reseeds_instead_of_scoring_the_backlog(self):
+        h = ProfileHarness(version=3).started()
+        h.poll([part(2, "Cilium new", ["T1 Homelab"])])
+        self.assertEqual(len(h.state()["pending"]), 1)
+        h.profile = profile_for_tests(version=4)
+        h.news = h.make()
+        h.now = T0 + timedelta(hours=1)
+        calls = len(h.clef.calls)
+        h.poll([part(3, "Cilium backlog", ["T1 Homelab"])])
+        st = h.state()
+        self.assertEqual(len(h.clef.calls), calls)
+        self.assertEqual((st["profile_version"], st["since"]), (4, int(h.now.timestamp())))
+        self.assertIn("a3", st["seen"])
+        self.assertEqual(len(st["pending"]), 1)  # the earlier match is kept, the backlog is not scored
+
+    def test_pure_release_article_is_queued_with_tier_score_and_group_and_no_clef_call(self):
+        h = ProfileHarness().started()
+        h.poll([part(2, "v1.2.3", ["T1 Homelab"], site=RELEASE)])
+        [item] = h.state()["pending"]
+        self.assertEqual((item["tier"], item["score"], item["group"], item["topics"]), (1, 1.0, "Homelab", ["Homelab"]))
+        self.assertEqual(h.clef.calls, [])
+
+    def test_marketing_demotes_a_tier1_article_out_of_the_digest(self):
+        h = ProfileHarness(marketing=0.9).started()
+        h.poll([part(2, "Cilium partner announcement", ["T1 Homelab"])])
+        st = h.state()
+        self.assertEqual((st["pending"], st["seen"][-1]), ([], "a2"))
+
+    def test_ruled_out_products_never_reach_clef(self):
+        h = ProfileHarness().started()
+        h.poll([part(2, "TrueNAS and Proxmox", ["T1 Homelab"])])
+        self.assertEqual((h.state()["pending"], h.clef.calls), ([], []))
+
+    def test_unmatched_articles_are_marked_seen_without_any_call(self):
+        h = ProfileHarness().started()
+        h.poll([part(2, "Celebrity news")])
+        self.assertEqual((h.state()["seen"][-1], h.clef.calls), ("a2", []))
+
+    def test_time_sensitive_tier3_becomes_an_alert_that_is_sent_once_and_cleared(self):
+        h = ProfileHarness(major=0.9, time_sensitive=0.9).started()
+        h.poll([part(2, "FOSDEM tickets on sale", ["T3 Other"], url="https://fosdem.example/t")])
+        st = h.state()
+        self.assertEqual((len(st["alerts"]), st["pending"]), (1, []))
+        self.assertTrue(h.news.send_alerts())
+        self.assertEqual(len(h.sent), 1)
+        self.assertIn("FOSDEM tickets on sale", h.sent[0])
+        self.assertIn("https://fosdem.example/t", h.sent[0])
+        self.assertEqual(h.state()["alerts"], [])
+        self.assertTrue(h.news.send_alerts())
+        self.assertEqual(len(h.sent), 1)
+
+    def test_a_failed_alert_send_is_kept_and_retried(self):
+        h = ProfileHarness(major=0.9, time_sensitive=0.9).started()
+        h.poll([part(2, "FOSDEM tickets on sale", ["T3 Other"])])
+        h.send_ok = False
+        self.assertFalse(h.news.send_alerts())
+        self.assertEqual(len(h.state()["alerts"]), 1)
+        h.send_ok = True
+        self.assertTrue(h.news.send_alerts())
+        self.assertEqual(h.state()["alerts"], [])
+
+    def test_queued_alerts_survive_a_restart(self):
+        h = ProfileHarness(major=0.9, time_sensitive=0.9).started()
+        h.poll([part(2, "FOSDEM tickets on sale", ["T3 Other"])])
+        h.news = h.make()
+        self.assertEqual(len(h.news.state["alerts"]), 1)
+
+    def test_major_but_not_urgent_tier3_goes_to_the_digest(self):
+        h = ProfileHarness(major=0.9).started()
+        h.poll([part(2, "FOSDEM schedule published", ["T3 Other"])])
+        st = h.state()
+        self.assertEqual((st["alerts"], len(st["pending"]), st["pending"][0]["tier"]), ([], 1, 3))
+
+    def test_clef_failure_leaves_the_article_unseen_and_loses_nothing(self):
+        h = ProfileHarness().started()
+        h.clef.error = clef.ClefError("OSError")
+        self.assertFalse(h.poll([part(2, "Cilium news", ["T1 Homelab"])]))
+        st = h.state()
+        self.assertNotIn("a2", st["seen"])
+        h.clef.error = None
+        self.assertTrue(h.poll([part(2, "Cilium news", ["T1 Homelab"])]))
+        self.assertEqual(len(h.state()["pending"]), 1)
+
+    def test_custom_topics_still_work_alongside_the_profile(self):
+        h = ProfileHarness().started()
+        h.say("add", "rust")
+        h.poll([part(2, "Rust 2.0 released")])
+        [item] = h.state()["pending"]
+        self.assertEqual((item["tier"], item["score"], item["topics"], item["group"]), (2, 0.8, ["rust"], None))
+
+    def test_an_article_matching_the_profile_and_a_custom_topic_is_one_item(self):
+        h = ProfileHarness().started()
+        h.say("add", "cilium")
+        h.poll([part(2, "Cilium 1.20", ["T1 Homelab"])])
+        [item] = h.state()["pending"]
+        self.assertEqual(item["topics"], ["Homelab", "cilium"])
+
+    def test_topics_command_lists_profile_groups_and_custom_topics(self):
+        h = ProfileHarness()
+        h.say("add", "rust")
+        h.sent.clear()
+        h.say()
+        text = h.sent[0]
+        self.assertIn("Profile groups", text)
+        self.assertIn("Homelab (tier 1)", text)
+        self.assertIn("1. rust", text)
+
+    def test_topics_command_without_custom_topics_still_explains_adding(self):
+        h = ProfileHarness()
+        h.say()
+        self.assertIn("/topics add", h.sent[0])
+
+    def test_bad_profile_version_in_state_starts_empty(self):
+        h = ProfileHarness()
+        with open(h.path, "w") as f:
+            json.dump({"topics": {}, "seen": [], "pending": [], "profile_version": "x"}, f)
+        h.news = h.make()
+        self.assertIsNone(h.news.state["profile_version"])
+        self.assertIsNone(h.news.state["seen"])
+
+
+class ProfileDigestTests(unittest.TestCase):
+    def due(self, h):
+        h.news.maybe_digest()
+        h.now = T0 + timedelta(hours=1)
+
+    def queue(self, h, items):
+        h.news.state["pending"] = items
+
+    def item(self, n, tier, score=1.0, group="G", title=None):
+        return {"id": f"p{n}", "title": title or f"title {n}", "url": f"https://x/{n}",
+                "topics": [group], "tier": tier, "score": score, "group": group, "custom": []}
+
+    def test_digest_is_grouped_by_tier_then_score_then_newest_first(self):
+        h = ProfileHarness().started()
+        self.due(h)
+        self.queue(h, [self.item(1, 2, 0.8), self.item(2, 1, 1.0), self.item(3, 3, 0.8), self.item(4, 1, 1.0)])
+        self.assertTrue(h.news.maybe_digest())
+        text = h.sent[-1]
+        self.assertIn("News digest: 4 items", text)
+        order = [text.index(f"title {n}") for n in (4, 2, 1, 3)]
+        self.assertEqual(order, sorted(order))
+        self.assertLess(text.index("Tier 1"), text.index("Tier 2"))
+        self.assertLess(text.index("Tier 2"), text.index("Tier 3"))
+        self.assertEqual(h.state()["pending"], [])
+
+    def test_digest_is_capped_and_the_rest_waits_for_the_next_one(self):
+        h = ProfileHarness().started()
+        self.due(h)
+        self.queue(h, [self.item(n, 1) for n in range(news.DIGEST_MAX_ITEMS + 5)])
+        h.news.maybe_digest()
+        self.assertEqual(h.sent[-1].count("https://x/"), news.DIGEST_MAX_ITEMS)
+        self.assertIn("+5 more", h.sent[-1])
+        self.assertEqual(len(h.state()["pending"]), 5)
+
+    def test_char_limit_also_splits_a_digest(self):
+        h = ProfileHarness().started()
+        self.due(h)
+        self.queue(h, [self.item(n, 1, title="t" * 300) for n in range(20)])
+        h.news.maybe_digest()
+        self.assertLessEqual(len(h.sent[-1]), 4096)
+        self.assertGreater(len(h.state()["pending"]), 0)
+
+    def test_failed_send_restores_everything(self):
+        h = ProfileHarness().started()
+        self.due(h)
+        self.queue(h, [self.item(1, 1), self.item(2, 2, 0.8)])
+        h.send_ok = False
+        self.assertFalse(h.news.maybe_digest())
+        self.assertEqual(len(h.state()["pending"]), 2)
+
+    def test_legacy_pending_items_without_a_tier_appear_under_tier_2(self):
+        h = ProfileHarness().started()
+        self.due(h)
+        h.say("add", "rust")
+        self.queue(h, [{"id": "old", "title": "Old Rust news", "url": "https://x/old", "topics": ["rust"]}])
+        self.assertTrue(h.news.maybe_digest())
+        self.assertIn("Tier 2", h.sent[-1])
+        self.assertIn("Old Rust news", h.sent[-1])
+
+    def test_custom_only_items_are_dropped_once_the_topic_is_unfollowed(self):
+        h = ProfileHarness().started()
+        self.due(h)
+        item = {**self.item(1, 2, 0.8, group=None), "group": None, "topics": ["rust"], "custom": ["rust"]}
+        self.queue(h, [item])
+        self.assertFalse(h.news.maybe_digest())
+        self.assertEqual(h.state()["pending"], [])
+        self.assertEqual(len(h.sent), 0)
+
+
 if __name__ == "__main__":
     unittest.main()
PATCH
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: many errors in `test_news` (`News()` does not accept `profile`).

- [ ] **Step 3: Change the module**

Apply this patch (profile scoring, alerts queue, re-seed, tiered digest):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/app/news.py b/kubernetes/apps/monitoring/flight-tracker/app/news.py
index 4c19326..7343b01 100644
--- a/kubernetes/apps/monitoring/flight-tracker/app/news.py
+++ b/kubernetes/apps/monitoring/flight-tracker/app/news.py
@@ -6,6 +6,7 @@ import threading
 import time
 from datetime import datetime, timedelta, timezone
 
+import interests
 from clef import ClefError
 from core import log
 from freshrss import FreshRSSError
@@ -20,6 +21,10 @@ MAX_TOPIC_LEN = 80
 MAX_SEEN = 5000
 MAX_PENDING = 200
 DIGEST_MAX_CHARS = 3500
+DIGEST_MAX_ITEMS = 25
+MAX_ALERTS = 50
+CUSTOM_SCORE = 0.8      # an extra /topics match counts as a Tier 2 article that passed the threshold
+TIER_TITLES = {1: "Tier 1 - core", 2: "Tier 2 - regular", 3: "Tier 3 - situational"}
 THRESHOLD = 0.5
 USAGE = "Usage: /topics [add <topic> | rm <topic or number>]"
 
@@ -33,8 +38,9 @@ class News:
     ]
 
     def __init__(self, ctx, chat_id, source, clef, now=lambda: datetime.now(UTC), tz=UTC,
-                 digest_hours=(8, 18)):
+                 digest_hours=(8, 18), profile=None):
         self.ctx, self.chat, self.source, self.clef = ctx, str(chat_id), source, clef
+        self.profile = profile  # compiled interest profile (interests.py); None = custom topics only
         self.now, self.tz, self.hours = now, tz, tuple(sorted(digest_hours))
         self.lock = threading.RLock()
         self.failing = False
@@ -45,7 +51,8 @@ class News:
     # -- persistence --
 
     def _load(self):
-        default = {"topics": {}, "seen": None, "pending": [], "last_slot": None, "since": None}
+        default = {"topics": {}, "seen": None, "pending": [], "last_slot": None, "since": None,
+                   "profile_version": None, "alerts": []}
         try:
             with open(self.ctx.state_path) as f:
                 raw = json.load(f)
@@ -61,11 +68,16 @@ class News:
               and isinstance(raw.get("pending"), list)
               and (raw.get("last_slot") is None or isinstance(raw["last_slot"], str))
               and (raw.get("since") is None
-                   or (isinstance(raw["since"], (int, float)) and not isinstance(raw["since"], bool))))
+                   or (isinstance(raw["since"], (int, float)) and not isinstance(raw["since"], bool)))
+              and (raw.get("profile_version") is None
+                   or (isinstance(raw["profile_version"], int) and not isinstance(raw["profile_version"], bool)))
+              and isinstance(raw.get("alerts", []), list))
         if not ok:
             log("news state has the wrong shape, starting empty")
             return default
-        return {key: raw.get(key) for key in default}
+        state = {key: raw.get(key) for key in default}
+        state["alerts"] = state["alerts"] or []
+        return state
 
     def _save(self):
         tmp = self.ctx.state_path + ".tmp"
@@ -80,9 +92,17 @@ class News:
 
     def _list_text(self):
         topics = self._topics()
-        if not topics:
-            return "No topics yet. Add one: /topics add kubernetes security"
-        return "Topics:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(topics, 1))
+        if self.profile is None:
+            if not topics:
+                return "No topics yet. Add one: /topics add kubernetes security"
+            return "Topics:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(topics, 1))
+        lines = ["Profile groups (from git):"]
+        lines += [f"- {title} (tier {tier})" for title, tier in interests.group_lines(self.profile)]
+        if topics:
+            lines += ["", "Your extra topics:"] + [f"{i}. {t}" for i, t in enumerate(topics, 1)]
+        else:
+            lines += ["", "No extra topics. Add one: /topics add kubernetes security"]
+        return "\n".join(lines)
 
     def _command(self, args):
         with self.lock:
@@ -118,8 +138,13 @@ class News:
 
     def poll_once(self):
         with self.lock:
-            if self.state["since"] is None:  # first run: articles older than the install are never fetched
+            reseed = self.profile is not None and self.state["profile_version"] != self.profile["version"]
+            if self.state["since"] is None or reseed:
+                # first run, or a new profile: articles stored before now are never fetched or scored
                 self.state["since"] = int(self.now().timestamp())
+                self.state["seen"] = None
+                if self.profile is not None:
+                    self.state["profile_version"] = self.profile["version"]
                 self._save()
             since = self.state["since"]
         try:
@@ -136,41 +161,77 @@ class News:
                 return True
             seen = set(self.state["seen"])
         fresh = [a for a in articles if a["id"] not in seen]
-        if not topics:  # nothing to match against; do not hold these back for a later topic
+        if not topics and self.profile is None:  # nothing to match against; do not hold these back
             self._finish(fresh, [])
             return True
-        scored, matches = [], []
+        scored, matches, alerts = [], [], []
         for article in fresh[:MAX_PER_POLL]:
-            state = {"title": article["title"], "summary": article["summary"]}
-            hit = []
             try:
-                for start in range(0, len(topics), TOPICS_PER_CALL):
-                    chunk = list(enumerate(topics))[start:start + TOPICS_PER_CALL]
-                    probs = self.clef.noul(state, {f"t{i}": f"Is this article about: {t}?" for i, t in chunk})
-                    hit += [t for i, t in chunk if probs[f"t{i}"] >= THRESHOLD]
+                item, is_alert = self._score(article, topics)
             except ClefError as e:
                 self._fail(f"clef unavailable ({e}), articles stay unseen")
                 break
             scored.append(article)
-            if hit:
-                matches.append({"id": article["id"], "title": article["title"],
-                                "url": article["url"], "topics": hit})
+            if item is not None:
+                (alerts if is_alert else matches).append(item)
         else:
             self.failing = False
-        self._finish(scored, matches)
+        self._finish(scored, matches, alerts)
         return not self.failing
 
+    def _score(self, article, topics):
+        """(pending item or None, is_alert). ClefError propagates so the article is retried later."""
+        verdict = None
+        if self.profile is not None:
+            result = interests.assess(self.profile, article, self.clef)
+            if result["deliver"]:
+                verdict = result
+        hit = self._custom_hits(article, topics)
+        base = {"id": article["id"], "title": article["title"], "url": article["url"]}
+        if verdict is not None:
+            return ({**base, "topics": [verdict["group"]] + hit, "tier": verdict["tier"],
+                     "score": verdict["score"], "group": verdict["group"], "custom": hit}, verdict["alert"])
+        if not hit:
+            return None, False
+        if self.profile is None:
+            return {**base, "topics": hit}, False
+        return {**base, "topics": hit, "tier": 2, "score": CUSTOM_SCORE, "group": None, "custom": hit}, False
+
+    def _custom_hits(self, article, topics):
+        state = {"title": article["title"], "summary": article["summary"]}
+        hit = []
+        for start in range(0, len(topics), TOPICS_PER_CALL):
+            chunk = list(enumerate(topics))[start:start + TOPICS_PER_CALL]
+            probs = self.clef.noul(state, {f"t{i}": f"Is this article about: {t}?" for i, t in chunk})
+            hit += [t for i, t in chunk if probs[f"t{i}"] >= THRESHOLD]
+        return hit
+
     def _fail(self, text):
         if not self.failing:
             log(text)
         self.failing = True
 
-    def _finish(self, scored, matches):
+    def _finish(self, scored, matches, alerts=()):
         with self.lock:
             self.state["seen"] = (self.state["seen"] + [a["id"] for a in scored])[-MAX_SEEN:]
             self.state["pending"] = (self.state["pending"] + matches)[-MAX_PENDING:]
+            self.state["alerts"] = (self.state["alerts"] + list(alerts))[-MAX_ALERTS:]
             self._save()
 
+    def send_alerts(self):
+        """Send queued alerts one by one; a failed send keeps the rest queued for the next pass."""
+        while True:
+            with self.lock:
+                if not self.state["alerts"]:
+                    return True
+                item = self.state["alerts"][0]
+            ok = self.ctx.send(f"\u23f0 {item['title']} ({', '.join(item['topics'])})\n{item['url']}".rstrip())
+            if ok is False:
+                return False
+            with self.lock:
+                self.state["alerts"] = [a for a in self.state["alerts"] if a["id"] != item["id"]]
+                self._save()
+
     # -- digest --
 
     def _slot(self):
@@ -193,15 +254,19 @@ class News:
             followed = {t.lower() for t in self._topics()}  # drop matches for topics unfollowed since
             pending = []
             for item in self.state["pending"]:
-                kept = [t for t in item["topics"] if t.lower() in followed]
+                if "tier" in item and item.get("group") is not None:  # a profile match: nothing to unfollow
+                    pending.append(item)
+                    continue
+                names = item.get("custom", item["topics"]) if "tier" in item else item["topics"]
+                kept = [t for t in names if t.lower() in followed]
                 if kept:
-                    pending.append({**item, "topics": kept})
+                    pending.append({**item, "topics": kept, **({"custom": kept} if "tier" in item else {})})
             if not pending:
                 self.state["pending"] = []
                 self._save()
                 return False
-            text, used = self._digest_text(pending)
-            self.state["pending"] = pending[used:]
+            text, remaining = self._digest_text(pending)
+            self.state["pending"] = remaining
             self._save()
         ok = self.ctx.send(text)
         if ok is False:
@@ -213,6 +278,33 @@ class News:
         return True
 
     def _digest_text(self, pending):
+        """(message text, items left for the next digest)."""
+        if self.profile is None:
+            return self._digest_text_plain(pending)
+        order = sorted(range(len(pending)),
+                       key=lambda i: (pending[i].get("tier", 2), -pending[i].get("score", CUSTOM_SCORE), -i))
+        chosen, size = [], 0
+        for i in order:
+            item = pending[i]
+            line = f"- {item['title']} ({', '.join(item['topics'])})\n  {item['url']}".rstrip()
+            if chosen and (len(chosen) >= DIGEST_MAX_ITEMS or size + len(line) > DIGEST_MAX_CHARS):
+                break
+            chosen.append((i, line))
+            size += len(line) + 1
+        lines, current = [], None
+        for i, line in chosen:
+            tier = pending[i].get("tier", 2)
+            if tier != current:
+                lines.append(("" if not lines else "\n") + TIER_TITLES[tier])
+                current = tier
+            lines.append(line)
+        left = [item for i, item in enumerate(pending) if i not in {i for i, _ in chosen}]
+        if left:
+            lines.append(f"\n+{len(left)} more in the next digest")
+        n = len(chosen)
+        return f"News digest: {n} item{'s' if n != 1 else ''}\n\n" + "\n".join(lines), left
+
+    def _digest_text_plain(self, pending):
         lines, size, used = [], 0, 0
         for item in pending:
             line = f"- {item['title']} ({', '.join(item['topics'])})\n  {item['url']}".rstrip()
@@ -224,7 +316,7 @@ class News:
         head = f"News digest: {used} match{'es' if used != 1 else ''}"
         if used < len(pending):
             lines.append(f"+{len(pending) - used} more in the next digest")
-        return head + "\n\n" + "\n".join(lines), used
+        return head + "\n\n" + "\n".join(lines), pending[used:]
 
     # -- loop --
 
@@ -238,6 +330,7 @@ class News:
                 if time.monotonic() >= next_poll:
                     self.poll_once()
                     next_poll = time.monotonic() + POLL_SECONDS
+                self.send_alerts()
                 self.maybe_digest()
             except Exception as e:  # keep the module alive
                 log(f"news loop failed: {type(e).__name__}")
PATCH
```

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK` (including every pre-existing news test, which runs with `profile=None`).

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/news.py kubernetes/apps/monitoring/flight-tracker/tests/test_news.py
git commit -m "feat(telegram-bot): news scores articles against the interest profile"
```

---

### Task 5: Wire it into the bot

**Files:**
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/main.py`
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml`
- Modify: `kubernetes/apps/monitoring/flight-tracker/tests/test_main.py`

**Interfaces:**
- Consumes: `interests.load_profile`, `interests.ProfileError`, `interests.missing_categories`, `FreshRSS.labels`, `News(..., profile=...)`.
- Produces: `main.load_news_profile(path) -> profile | None` (logs once and returns `None` on any `ProfileError`); `main.warn_unused_categories(profile, source)` (logs, never raises); env `NEWS_PROFILE` (default `/scripts/news-profile.json`). The ConfigMap now carries `interests.py` and `news-profile.json`; the news module is registered only when the profile loads.

- [ ] **Step 1: Add the tests**

Apply this patch (NewsProfileTests):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/tests/test_main.py b/kubernetes/apps/monitoring/flight-tracker/tests/test_main.py
index 13f4ed3..848263d 100644
--- a/kubernetes/apps/monitoring/flight-tracker/tests/test_main.py
+++ b/kubernetes/apps/monitoring/flight-tracker/tests/test_main.py
@@ -1,5 +1,6 @@
 import os
 import sys
+import tempfile
 import unittest
 from unittest import mock
 
@@ -21,5 +22,47 @@ class DigestHoursTests(unittest.TestCase):
                 lg.assert_called_once()
 
 
+PROFILE = os.path.join(os.path.dirname(__file__), "..", "app", "news-profile.json")
+
+
+class NewsProfileTests(unittest.TestCase):
+    def test_the_shipped_profile_loads(self):
+        self.assertEqual(main.load_news_profile(PROFILE)["version"], 1)
+
+    def test_a_missing_or_broken_profile_turns_news_off_with_a_log_line(self):
+        with tempfile.TemporaryDirectory() as d:
+            broken = os.path.join(d, "broken.json")
+            with open(broken, "w") as f:
+                f.write("{nope")
+            for path in (os.path.join(d, "missing.json"), broken):
+                with self.subTest(path=path), mock.patch("main.core.log") as lg:
+                    self.assertIsNone(main.load_news_profile(path))
+                    lg.assert_called_once()
+
+    def test_unused_profile_categories_are_logged_once(self):
+        profile = main.load_news_profile(PROFILE)
+        source = mock.Mock()
+        source.labels.return_value = {"T1 Homelab"}
+        with mock.patch("main.core.log") as lg:
+            main.warn_unused_categories(profile, source)
+        lg.assert_called_once()
+        self.assertIn("T1 HPC/AI infra", lg.call_args[0][0])
+
+    def test_nothing_is_logged_when_every_category_has_a_feed(self):
+        profile = main.load_news_profile(PROFILE)
+        source = mock.Mock()
+        source.labels.return_value = {c for g in profile["groups"] for c in g["categories"]}
+        with mock.patch("main.core.log") as lg:
+            main.warn_unused_categories(profile, source)
+        lg.assert_not_called()
+
+    def test_a_freshrss_failure_while_checking_categories_never_raises(self):
+        source = mock.Mock()
+        source.labels.side_effect = main.freshrss.FreshRSSError("OSError")
+        with mock.patch("main.core.log") as lg:
+            main.warn_unused_categories(main.load_news_profile(PROFILE), source)
+        lg.assert_called_once()
+
+
 if __name__ == "__main__":
     unittest.main()
PATCH
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: errors in `test_main` (`load_news_profile` is not defined).

- [ ] **Step 3: Wire main.py and the ConfigMap**

Apply this patch (load_news_profile, warn_unused_categories, profile passed to News):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/app/main.py b/kubernetes/apps/monitoring/flight-tracker/app/main.py
index f907d59..4d2b732 100644
--- a/kubernetes/apps/monitoring/flight-tracker/app/main.py
+++ b/kubernetes/apps/monitoring/flight-tracker/app/main.py
@@ -10,12 +10,14 @@ import clef
 import core
 import flights
 import freshrss
+import interests
 import news
 import status
 import triage
 
 DEFAULT_CLEF_URL = "http://clef.ai.svc.cluster.local:8080"
 DEFAULT_FRESHRSS_URL = "http://freshrss.freshrss.svc.cluster.local"
+DEFAULT_PROFILE_PATH = "/scripts/news-profile.json"
 
 
 def parse_digest_hours(text):
@@ -30,6 +32,26 @@ def parse_digest_hours(text):
     return tuple(sorted(hours))
 
 
+def load_news_profile(path):
+    """The compiled interest profile, or None (news stays off) with a log line when it cannot be used."""
+    try:
+        return interests.load_profile(path)
+    except interests.ProfileError as e:
+        core.log(f"news profile unusable ({e}), news module off")
+        return None
+
+
+def warn_unused_categories(profile, source):
+    """Log profile categories that no subscribed feed uses: a renamed category silently stops matching."""
+    try:
+        missing = interests.missing_categories(profile, source.labels())
+    except freshrss.FreshRSSError as e:
+        core.log(f"could not check FreshRSS categories ({e})")
+        return
+    if missing:
+        core.log("profile categories with no subscribed feed: " + ", ".join(missing))
+
+
 def digest_tz():
     name = os.environ.get("DIGEST_TZ", "Europe/Brussels")
     try:
@@ -63,9 +85,12 @@ def main():
         source = freshrss.FreshRSS(os.environ.get("FRESHRSS_URL", DEFAULT_FRESHRSS_URL),
                                    fr_user, fr_password)
         hours = parse_digest_hours(os.environ.get("DIGEST_HOURS", "8,18"))
-        news_clef = clef.Clef(clef_url, timeout=60)  # news scores chunks of topics on CPU: allow longer
-        bot.register(news.News(bot.ctx("news"), chat_id, source, news_clef,
-                               tz=digest_tz(), digest_hours=hours))
+        news_clef = clef.Clef(clef_url, timeout=60)  # news scores on CPU: allow longer than triage
+        profile = load_news_profile(os.environ.get("NEWS_PROFILE", DEFAULT_PROFILE_PATH))
+        if profile is not None:
+            warn_unused_categories(profile, source)
+            bot.register(news.News(bot.ctx("news"), chat_id, source, news_clef,
+                                   tz=digest_tz(), digest_hours=hours, profile=profile))
     else:
         core.log("FRESHRSS_USER or FRESHRSS_API_PASSWORD missing or empty, news module off")
     bot.register(status.Status(bot.ctx("status"), lambda promql: status.vm_query(vm_url, promql)))
PATCH
```

Apply this patch (adds interests.py and news-profile.json to the ConfigMap):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml b/kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
index ca0c872..5a23342 100644
--- a/kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
+++ b/kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
@@ -15,7 +15,9 @@ configMapGenerator:
       - core.py
       - flights.py
       - freshrss.py
+      - interests.py
       - main.py
+      - news-profile.json
       - news.py
       - status.py
       - triage.py
PATCH
```

- [ ] **Step 4: Run tests and validate the manifest**

```bash
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests
mise exec -- kubectl kustomize kubernetes/apps/monitoring/flight-tracker/app | mise exec -- kubeconform -strict -ignore-missing-schemas -summary -
.agents/scripts/validate-manifests.sh kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
mise exec -- kubectl kustomize kubernetes/apps/monitoring/flight-tracker/app | grep -E '^  (interests.py|news-profile.json|news.py):'
```
Expected: tests `OK` (308); kubeconform `Invalid: 0, Errors: 0`; the validate script exits 0; the grep prints the three file keys.

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/main.py kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml kubernetes/apps/monitoring/flight-tracker/tests/test_main.py
git commit -m "feat(telegram-bot): load the news profile and ship it in the ConfigMap"
```

---

### Task 6: Documentation

**Files:**
- Modify: `design/decisions/flight-tracker.md`
- Modify: `docs/superpowers/specs/2026-10-27-news-profile-scoring-design.md`

Docs describe the implemented state and never record deployed versions. Read each file's surrounding text first; verify every name and number against the code before writing it.

- [ ] **Step 1: `design/decisions/flight-tracker.md`**

1. In "Current state", add `interests.py` to the list of Python files, mention the delivered `news-profile.json`, and keep the file count correct (count the files in `kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml`).
2. Add these rules next to the existing news rules, and edit the existing news bullets so none of them contradicts the new ones (the "first poll never scores the backlog" bullet must also say a changed profile `version` re-seeds; the "at most 40 articles / 3 topics per call" bullet now applies to extra `/topics` only; the fetch window is 250):

```markdown
- **News is scored from `news-profile.json`**, not from `/topics`. The file holds the 14 groups in 3
  tiers, keywords, ruled-out products, spoiler topics, paywalled domains, the `0.7` delivery
  threshold and the exact Clef question wording, so tuning is a JSON edit (Reloader restarts the
  bot). Matching and scoring are pure functions in `interests.py`.
- **A release feed is its own topic.** A GitHub `releases` or `tags` feed (by the feed's site URL)
  whose FreshRSS category is a group's category matches that group with no keywords and no Clef
  call. Elsewhere whole-word keywords decide; `scoped_keywords` (Signal, Matrix, Pixel, Thread,
  Matter and similar) only count inside the group's categories.
- **FreshRSS category names are part of the config.** `categories` in the profile must equal the
  OPML category names; a renamed category silently stops matching. The bot logs profile categories
  with no subscribed feed at startup.
- **Score = tier weight (1.0 / 0.6 / 0.3), adjusted by at most 3 Clef answers:** marketing without
  numbers x0.6, Tier 2 substantive +0.2 (release feeds count as substantive), Tier 3 major +0.5;
  paywalled domains x0.5; llama.cpp `bNNNN` builds x0.5 unless the text mentions Vulkan, Intel Arc
  or SYCL. A spoiler topic (Expedition 33, Steel Ball Run) gets one extra Clef call first, unless the
  text has an allow term such as "patch notes". Ruled-out products are dropped without any call.
- **Delivery.** 0.7 or more goes to the 08:00/18:00 digest, grouped by tier, at most 25 items per
  digest (the rest wait). Tier 3 items Clef judges time-sensitive are sent at once: they are queued
  in `news.json` (`alerts`) and retried every 30 s until Telegram accepts them.
- **A new profile `version` re-seeds the module** like a first run (new `since`, current unread
  recorded as seen, nothing scored), so an OPML import or a big profile change never replays the
  backlog. Pure tuning (thresholds, wording, keywords) should not bump `version`.
- **A missing or broken profile turns news off** with one log line; the rest of the bot runs.
- **`/topics` lists the profile groups read-only;** extra free-text topics (max 10) still work and
  count as Tier 2 with score 0.8.
```

- [ ] **Step 2: The spec**

In `docs/superpowers/specs/2026-10-27-news-profile-scoring-design.md`, replace every `profile.py` with `interests.py` and add one sentence under "Matching and scoring" step 4: the `spoiler` question is a separate one-question Clef call made before the main call, so a normal call never exceeds 3 questions.

- [ ] **Step 3: Commit**

```bash
git add design/decisions/flight-tracker.md docs/superpowers/specs/2026-10-27-news-profile-scoring-design.md
git commit -m "docs: news profile scoring"
```

---

### Task 7: Dry run, rollout and tuning

**This task changes the live cluster (the bot restarts and starts sending alerts). It is run by the controller with the owner, not delegated. Stop for the owner's explicit yes before Step 4.**

Reference numbers from the pre-plan dry run on the owner's live FreshRSS (newest 250 unread, real Clef): 62 candidates, 24 Clef calls in 157 s (about 6.5 s each), 51 delivered (33 of them release-feed items), 0 alerts. A few weak Tier 2 hits ("Baby box" from PokéBeach) are expected; tuning follows below.

- [ ] **Step 1: Final local check**

```bash
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests
git status --short
git log --oneline main..HEAD
```
Expected: `OK` (308); only the owner's two unrelated modified files and `news-feeds.opml` besides the branch's commits; 6 commits.

- [ ] **Step 2: Dry run on live data (read-only, nothing is sent)**

Copy the new code into the bot pod's `/tmp` and score the newest 250 unread articles with real Clef:

```bash
k() { mise exec -- kubectl -n monitoring exec -i deploy/flight-tracker -- "$@"; }
k sh -c 'mkdir -p /tmp/dry'
for f in interests.py news-profile.json freshrss.py; do k sh -c "cat > /tmp/dry/$f" < kubernetes/apps/monitoring/flight-tracker/app/$f; done
k sh -c 'cp /scripts/clef.py /tmp/dry/clef.py'
k python3 - > /tmp/dryrun.json <<'EOF'
import os, sys, json, time
sys.path.insert(0, "/tmp/dry")
import freshrss, interests, clef
f = freshrss.FreshRSS(os.environ["FRESHRSS_URL"], os.environ["FRESHRSS_USER"], os.environ["FRESHRSS_API_PASSWORD"])
P = interests.load_profile("/tmp/dry/news-profile.json")
C = clef.Clef(os.environ.get("CLEF_URL", "http://clef.ai.svc.cluster.local:8080"), timeout=90)
out, t0, calls = [], time.time(), 0
class Counting:
    def noul(self, s, q):
        global calls
        calls += 1
        return C.noul(s, q)
for a in f.unread(limit=250):
    v = interests.assess(P, a, Counting())
    if v["reason"] != "no match":
        out.append({"title": a["title"][:80], "feed": a["feed"][:28], "pure": bool(P["pure_rx"].search(a["site"])), **v})
print(json.dumps({"secs": round(time.time() - t0), "clef_calls": calls, "rows": out}))
EOF
```

Then print the result: counts, the non-pure articles that were NOT delivered, and the non-pure ones that were:

```bash
python3 - <<'EOF'
import json, collections
d = json.load(open("/tmp/dryrun.json")); R = d["rows"]
print(f"{d['secs']}s, {d['clef_calls']} Clef calls; candidates {len(R)}; delivered {sum(r['deliver'] for r in R)}; alerts {sum(r['alert'] for r in R)}")
for r in R:
    if not r["pure"]: print(f"{r['score']:.2f} T{r['tier']} {'DELIVER' if r['deliver'] else r['reason']:<15} | {r['feed']:<28} | {r['title']}")
EOF
```
Show the output to the owner. Expect roughly the reference numbers. Clean up with `k sh -c 'rm -rf /tmp/dry'`.

- [ ] **Step 3: Tune with the owner (JSON only)**

If the owner finds too many weak hits, change `news-profile.json` and re-run Step 2 (copy the new file, no redeploy needed): lower or raise `deliver_min`, move noisy keywords from `keywords` to `scoped_keywords`, add `ruled_out` terms, or reword a `questions` text. Do not bump `version` for tuning. Commit each change with explicit paths.

- [ ] **Step 4: Push (owner's yes first)**

Ask: "Ready to merge `feat/news-profile` into `main` and push? The bot restarts, re-seeds, and then sends the digest at 08:00/18:00 plus immediate Tier 3 alerts." On a yes:

```bash
git switch main && git merge --ff-only feat/news-profile && git push origin main
mise exec -- flux reconcile kustomization flight-tracker -n flux-system --with-source
mise exec -- kubectl -n monitoring rollout status deploy/flight-tracker --timeout=120s
```

- [ ] **Step 5: Verify the live bot**

```bash
mise exec -- kubectl -n monitoring logs deploy/flight-tracker --tail=20
mise exec -- kubectl -n monitoring exec deploy/flight-tracker -- python3 -c "import json; s=json.load(open('/data/news.json')); print('profile_version', s['profile_version'], '| seen', len(s['seen'] or []), '| pending', len(s['pending']), '| alerts', len(s['alerts']))"
```
Expected: log has `flight-tracker started`, no `news profile unusable`, no `profile categories with no subscribed feed`; state shows `profile_version 1` and a non-empty `seen` after the first poll (the module re-seeded). Ask the owner to send `/topics` in Telegram and confirm it lists the 14 profile groups.

- [ ] **Step 6: After the first digest**

After the next 08:00 or 18:00 Brussels slot, ask the owner to judge the digest (too many? weak items? missing things?). Tune per Step 3 (no `version` bump). Also confirm `llama-swap` decode is still healthy:

```bash
mise exec -- kubectl -n ai exec deploy/llama-swap -- curl -s -m 300 localhost:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"qwen-smart","messages":[{"role":"user","content":"Write a 120 word paragraph about the sea."}],"max_tokens":160,"chat_template_kwargs":{"enable_thinking":false}}' | python3 -c "import sys,json; t=json.load(sys.stdin)['timings']; print(round(t['predicted_per_second'],2),'tok/s (baseline 7.7-8.7)')"
```

**Back-out:** `git revert` the merge commit; the bot returns to custom-topics-only news (the profile is simply not loaded). State written by the new version stays valid for the old one (extra keys are ignored).
