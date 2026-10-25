"""Offline unit tests (no network): python -m unittest discover -s tests -v"""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_tmp.name, "t.db")
for k in ("DATAFORSEO_LOGIN", "KEYWORDS_EVERYWHERE_API_KEY", "SLACK_WEBHOOK_URL"):
    os.environ.pop(k, None)
os.environ["KEYWORD_AUTOCOMPLETE"] = "0"

import database as db
import keywords, scraper
from pricing import parse_plans


class Core(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def test_url_key_ignores_noise(self):
        a = scraper.url_key("https://www.Example.com/blog/post/?utm_source=x")
        b = scraper.url_key("http://example.com/blog/post")
        self.assertEqual(a, b)

    def test_normalize_input(self):
        self.assertEqual(scraper.normalize_input("Example.com")[0], "https://example.com")
        with self.assertRaises(ValueError):
            scraper.normalize_input("not a domain")

    def test_post_url_heuristic(self):
        self.assertTrue(scraper.looks_like_post_url("https://x.com/blog/how-to-win"))
        self.assertFalse(scraper.looks_like_post_url("https://x.com/blog/category/seo"))
        self.assertFalse(scraper.looks_like_post_url("https://x.com/pricing"))

    def test_dates(self):
        self.assertEqual(scraper.norm_date("2025-03-05T10:00:00+05:30"), "2025-03-05")
        self.assertEqual(scraper.norm_date("March 5, 2025"), "2025-03-05")

    def test_feed_and_extract(self):
        feed = "<rss><channel><item><title>A post</title><link>https://x.com/blog/a</link><pubDate>Wed, 05 Mar 2025 10:00:00 GMT</pubDate></item></channel></rss>"
        items = scraper.parse_feed(feed)
        self.assertEqual(items[0]["url"], "https://x.com/blog/a")
        html = ('<html><head><title>Great Post | X</title><meta property="og:type" content="article">'
                '<meta name="description" content="Desc"><meta property="article:published_time" content="2025-03-05">'
                '</head><body><article><h1>Great Post</h1>' + "word " * 200 + "</article></body></html>")
        art = scraper.extract_article("https://x.com/blog/great-post", html)
        self.assertEqual(art["published_at"], "2025-03-05")
        self.assertEqual(art["description"], "Desc")

    def test_keyword_candidates(self):
        c = keywords.derive_candidates({"title": "How to Write a Cold Email: 7 Templates (2026)", "url": "https://x.com/blog/cold-email"})
        self.assertIn("how to write a cold email", c)

    def test_enrich_without_key_is_honest(self):
        r = keywords.enrich([{"title": "Cold Email Guide", "url": "https://x.com/blog/a"}])[0]
        self.assertIsNone(r["search_volume"])
        self.assertEqual(r["status"], "API_NOT_CONFIGURED")

    def test_second_run_not_new(self):
        comp = db.add_competitor("T", "https://t-example.com")
        art = {"url": "https://t-example.com/blog/a", "title": "A"}
        key = scraper.url_key(art["url"])
        _, c1 = db.upsert_article(comp["id"], key, art, 1, True)
        _, c2 = db.upsert_article(comp["id"], key, art, 2, True)
        self.assertTrue(c1)
        self.assertFalse(c2)

    def test_pricing_change(self):
        comp = db.add_competitor("P", "https://p-example.com")
        plan = lambda p: [{"plan_name": "Pro", "price_text": p, "amount": 1, "currency": "$", "billing_period": None, "source_url": "u"}]
        self.assertEqual(db.sync_pricing(comp["id"], plan("$10")), [])
        ch = db.sync_pricing(comp["id"], plan("$12"))
        self.assertEqual(ch[0]["type"], "changed")

    def test_parse_plans(self):
        html = "<div><div class='c'><h3>Pro</h3><span>$</span><span>29</span><span>/mo</span></div></div>"
        self.assertEqual(parse_plans(html, "u")[0]["plan_name"], "Pro")


if __name__ == "__main__":
    unittest.main()
