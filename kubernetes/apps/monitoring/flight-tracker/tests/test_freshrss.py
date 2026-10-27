import http.client
import io
import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import freshrss  # noqa: E402


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeServer:
    """Answers ClientLogin, then the stream request."""

    def __init__(self, items=None, login="SID=x\nLSID=y\nAuth=alice/tok123\n", error=None, subscriptions=None):
        self.items, self.login, self.error, self.requests = items, login, error, []
        self.subscriptions = subscriptions

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.error:
            raise self.error
        if req.full_url.endswith("/accounts/ClientLogin"):
            return Resp(self.login.encode())
        if "/subscription/list" in req.full_url:
            return Resp(json.dumps({"subscriptions": self.subscriptions}).encode())
        return Resp(json.dumps({"items": self.items}).encode())


def item(id="tag:google.com,2005:reader/item/1", title="T", content="<p>Hello &amp; <b>bye</b></p>",
         href="https://x.example/a", origin=None, categories=None):
    d = {"id": id, "title": title, "summary": {"content": content}, "canonical": [{"href": href}]}
    if origin is not None:
        d["origin"] = origin
    if categories is not None:
        d["categories"] = categories
    return d


class FreshRSSTests(unittest.TestCase):
    def client(self, server):
        return freshrss.FreshRSS("http://freshrss/", "alice", "pw", opener=server)

    def test_login_then_unread_request_shape(self):
        s = FakeServer([item()])
        self.client(s).unread(limit=50)
        login, stream = s.requests
        self.assertEqual(login.full_url, "http://freshrss/api/greader.php/accounts/ClientLogin")
        self.assertEqual(parse_qs(login.data.decode()), {"Email": ["alice"], "Passwd": ["pw"]})
        url = urlparse(stream.full_url)
        self.assertEqual(url.path, "/api/greader.php/reader/api/0/stream/contents/reading-list")
        self.assertEqual(parse_qs(url.query), {"xt": ["user/-/state/com.google/read"], "n": ["50"], "output": ["json"]})
        self.assertEqual(stream.get_header("Authorization"), "GoogleLogin auth=alice/tok123")

    def test_ot_is_sent_only_when_since_is_given(self):
        s = FakeServer([item()])
        self.client(s).unread()
        self.assertNotIn("ot", parse_qs(urlparse(s.requests[1].full_url).query))
        s = FakeServer([item()])
        self.client(s).unread(since=1790000000.7)
        self.assertEqual(parse_qs(urlparse(s.requests[1].full_url).query)["ot"], ["1790000000"])

    def test_articles_are_flattened_to_plain_text(self):
        [a] = self.client(FakeServer([item()])).unread()
        self.assertEqual(a, {"id": "tag:google.com,2005:reader/item/1", "title": "T",
                             "summary": "Hello & bye", "url": "https://x.example/a",
                             "feed": "", "site": "", "categories": []})

    def test_feed_site_and_category_labels_come_from_the_item(self):
        origin = {"streamId": "feed/7", "title": "Flux releases", "htmlUrl": "https://github.com/fluxcd/flux2/releases"}
        cats = ["user/-/state/com.google/reading-list", "user/-/label/T1 Homelab", "user/-/label/Extra", 5, None]
        [a] = self.client(FakeServer([item(origin=origin, categories=cats)])).unread()
        self.assertEqual((a["feed"], a["site"], a["categories"]),
                         ("Flux releases", "https://github.com/fluxcd/flux2/releases", ["T1 Homelab", "Extra"]))

    def test_odd_origin_and_category_shapes_are_tolerated(self):
        for origin, cats in (("x", "y"), ([], {}), (None, [[]])):
            with self.subTest(origin=origin, cats=cats):
                [a] = self.client(FakeServer([item(origin=origin, categories=cats)])).unread()
                self.assertEqual((a["feed"], a["site"], a["categories"]), ("", "", []))

    def test_default_window_is_250_articles(self):
        s = FakeServer([item()])
        self.client(s).unread()
        self.assertEqual(parse_qs(urlparse(s.requests[1].full_url).query)["n"], ["250"])

    def test_labels_lists_the_categories_that_have_subscriptions(self):
        subs = [{"categories": [{"id": "user/-/label/A", "label": "A"}]},
                {"categories": [{"label": "B"}, {"label": "A"}]}, {"categories": []}, {}]
        s = FakeServer(subscriptions=subs)
        self.assertEqual(self.client(s).labels(), {"A", "B"})
        self.assertIn("/subscription/list", s.requests[1].full_url)

    def test_labels_failures_are_freshrss_errors(self):
        for s in (FakeServer(error=OSError("down")), FakeServer(subscriptions=None), FakeServer(subscriptions="x")):
            with self.subTest(s=s), self.assertRaises(freshrss.FreshRSSError):
                self.client(s).labels()

    def test_long_summary_is_cut_and_missing_fields_are_tolerated(self):
        long = item(content="x" * 5000)
        bare = {"id": "tag:google.com,2005:reader/item/2"}
        a, b = self.client(FakeServer([long, bare, "junk", {"title": "no id"}])).unread()
        self.assertEqual(len(a["summary"]), freshrss.SUMMARY_MAX)
        self.assertEqual((b["title"], b["summary"], b["url"]), ("(no title)", "", ""))

    def test_login_without_auth_line_is_an_error(self):
        with self.assertRaises(freshrss.FreshRSSError):
            self.client(FakeServer([], login="Error=BadAuthentication")).unread()

    def test_network_and_shape_failures_are_freshrss_errors(self):
        for s in (FakeServer(error=OSError("down")), FakeServer(items=None),
                  FakeServer(error=http.client.IncompleteRead(b"x")),
                  FakeServer(error=http.client.BadStatusLine("x"))):
            with self.subTest(s=s), self.assertRaises(freshrss.FreshRSSError):
                self.client(s).unread()

    def test_items_must_be_a_list(self):
        for items in ({}, "x", 5, None):
            with self.subTest(items=items), self.assertRaises(freshrss.FreshRSSError):
                self.client(FakeServer(items=items)).unread()

    def test_plain(self):
        self.assertEqual(freshrss.plain("<a href='x'>a</a>  &lt;b&gt;\n c"), "a <b> c")
        self.assertEqual(freshrss.plain(None), "")


if __name__ == "__main__":
    unittest.main()
