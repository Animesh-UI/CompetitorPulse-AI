
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from dotenv import load_dotenv

# Add project root so imports work when running this file directly.
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Use a temporary database for tests.
_TMPDIR = tempfile.TemporaryDirectory(prefix="competitorpulse-test-")
os.environ["DATABASE_PATH"] = os.path.join(_TMPDIR.name, "test.db")

# Ensure tests don't depend on real credentials or send real alerts.
os.environ.pop("KEYWORDS_EVERYWHERE_API_KEY", None)
os.environ.pop("SLACK_WEBHOOK_URL", None)
os.environ.pop("SEODATA_API_KEY", None)
os.environ["SEODATA_ENABLED"] = "0"

import app as app_module
from database import (
    add_competitor, get_articles, get_events, store_articles,
    update_article_keyword,
)
from scraper import scan_competitor
from keyword_service import enrich_keyword


class CompetitorPulseTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def test_health(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])

    def test_dashboard_includes_real_keyword_data_controls(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"trendCompetitor", response.data)
        self.assertIn(b"Refresh data", response.data)
        self.assertIn(b"Google Trends interest", response.data)

    def test_add_competitor_requires_fields(self):
        response = self.client.post(
            "/api/competitors",
            json={"name": "Test"},
        )

        self.assertEqual(response.status_code, 400)

    def test_duplicate_article_is_not_new_twice(self):
        competitor = add_competitor(
            "Example Test",
            "https://example.com",
        )

        article = {
            "url": "https://example.com/blog/post-one",
            "title": "Post one",
            "published_at": "2026-01-01",
            "description": "Example",
        }

        self.assertTrue(
            store_articles(competitor["id"], article)
        )
        self.assertFalse(
            store_articles(competitor["id"], article)
        )

        self.assertEqual(
            len(get_articles(competitor["id"])),
            1,
        )

    def test_scan_invalid_scheme_is_handled(self):
        result = scan_competitor("file:///etc/passwd")

        self.assertEqual(result["status"], "FAILED")

    @patch.object(app_module, "send_slack_alert")
    @patch.object(app_module, "extract_pricing")
    @patch.object(app_module, "enrich_keyword")
    @patch.object(app_module, "scan_competitor")
    def test_first_scan_sets_baseline_and_second_only_reports_new_articles(
        self, mock_scan, mock_enrich, mock_pricing, mock_alert
    ):
        competitor = add_competitor(
            "Baseline Test", "https://baseline-test.example"
        )
        original = {
            "url": "https://baseline-test.example/blog/first-post",
            "title": "First post",
        }
        newly_published = {
            "url": "https://baseline-test.example/blog/second-post",
            "title": "Second post",
        }
        scan_result = {
            "status": "SUCCESS",
            "message": "Found articles.",
            "pages_checked": 3,
        }
        mock_scan.side_effect = [
            {**scan_result, "articles": [original]},
            {**scan_result, "articles": [original, newly_published]},
        ]
        mock_enrich.return_value = {
            "keyword": "competitor topic",
            "volume_status": "INVALID_API_KEY",
            "volume_error": "SeoData rejected the API key.",
            "trends_status": "API_NOT_CONFIGURED",
            "status": "INVALID_API_KEY",
        }
        mock_pricing.return_value = {"status": "NO_DATA", "plans": []}
        mock_alert.return_value = {"sent": False, "reason": "No alert configured."}

        first_response = self.client.post(
            f"/api/competitors/{competitor['id']}/scan"
        )
        second_response = self.client.post(
            f"/api/competitors/{competitor['id']}/scan"
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json["new_articles"], [])
        self.assertEqual(
            first_response.json["keyword_results"][0]["volume_status"],
            "INVALID_API_KEY",
        )
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(
            [item["url"] for item in second_response.json["new_articles"]],
            [newly_published["url"]],
        )
        events = [
            event for event in get_events()
            if event["competitor_id"] == competitor["id"]
            and event["event_type"] == "new_article"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["url"], newly_published["url"])

    def test_keyword_fallback_without_api_key(self):
        article = {
            "title": (
                "What Are Marvel Blind Bags? "
                "A New Way to Collect Marvel Comics"
            ),
            "url": (
                "https://www.veve.me/blog/news/"
                "what-are-marvel-blind-bags"
            ),
        }

        with patch.dict(
            os.environ,
            {"GOOGLE_TRENDS_ENABLED": "0"},
            clear=False,
        ):
            result = enrich_keyword(article)

        self.assertEqual(
            result["keyword"],
            "marvel blind bags new way collect",
        )
        self.assertIsNone(result["search_volume"])
        self.assertEqual(result["source"], "title-derived")
        self.assertEqual(
            result["status"],
            "API_NOT_CONFIGURED",
        )

    @patch("keyword_service.requests.post")
    def test_keywords_everywhere_api_success(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"vol": 1200}]
        }
        mock_response.raise_for_status.return_value = None

        with patch.dict(
            os.environ,
            {
                "KEYWORDS_EVERYWHERE_API_KEY": "test-key",
                "GOOGLE_TRENDS_ENABLED": "0",
            },
        ):
            result = enrich_keyword({
                "title": "Marvel Blind Bags",
                "url": "https://example.com/article",
            })

        self.assertEqual(result["search_volume"], 1200)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(
            result["source"],
            "Keywords Everywhere",
        )
        mock_post.assert_called_once()

    @patch("keyword_service.requests.post")
    def test_keywords_everywhere_invalid_key(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.status_code = 401

        with patch.dict(
            os.environ,
            {
                "KEYWORDS_EVERYWHERE_API_KEY": "invalid-test-key",
                "GOOGLE_TRENDS_ENABLED": "0",
            },
        ):
            result = enrich_keyword({
                "title": "Marvel Blind Bags",
                "url": "https://example.com/article",
            })

        self.assertIsNone(result["search_volume"])
        self.assertEqual(result["status"], "INVALID_API_KEY")
        self.assertEqual(
            result["source"],
            "Keywords Everywhere",
        )

    @patch("keyword_service.requests.post")
    def test_keywords_everywhere_rate_limit(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.status_code = 429

        with patch.dict(
            os.environ,
            {
                "KEYWORDS_EVERYWHERE_API_KEY": "test-key",
                "GOOGLE_TRENDS_ENABLED": "0",
            },
        ):
            result = enrich_keyword({
                "title": "Marvel Blind Bags",
                "url": "https://example.com/article",
            })

        self.assertEqual(result["status"], "RATE_LIMITED")
        self.assertIsNone(result["search_volume"])

    @patch("keyword_service.requests.post")
    def test_keywords_everywhere_api_error(self, mock_post):
        import requests

        mock_post.side_effect = requests.ConnectionError(
            "Simulated connection error"
        )

        with patch.dict(
            os.environ,
            {
                "KEYWORDS_EVERYWHERE_API_KEY": "test-key",
                "GOOGLE_TRENDS_ENABLED": "0",
            },
        ):
            result = enrich_keyword({
                "title": "Marvel Blind Bags",
                "url": "https://example.com/article",
            })

        self.assertEqual(result["status"], "API_ERROR")
        self.assertIsNone(result["search_volume"])

    @patch("keyword_service.TrendReq")
    def test_google_trends_returns_and_persists_real_series(self, mock_trend_req):
        import pandas as pd

        dataframe = pd.DataFrame(
            {"marvel blind bags": [15, 35, 75]},
            index=pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"]),
        )
        mock_trend_req.return_value.interest_over_time.return_value = dataframe

        with patch.dict(
            os.environ,
            {
                "GOOGLE_TRENDS_ENABLED": "1",
                "KEYWORDS_EVERYWHERE_API_KEY": "",
                "DATAFORSEO_LOGIN": "",
                "DATAFORSEO_PASSWORD": "",
            },
        ):
            result = enrich_keyword({
                "title": "Marvel Blind Bags",
                "url": "https://example.com/article",
            })

        self.assertEqual(result["trends_interest"], 41.67)
        self.assertEqual(
            result["trends_series"],
            [
                {"date": "2026-01-01", "value": 15.0},
                {"date": "2026-02-01", "value": 35.0},
                {"date": "2026-03-01", "value": 75.0},
            ],
        )
        competitor = add_competitor("Trend Test", "https://trend-test.example")
        article = {
            "url": "https://trend-test.example/article",
            "title": "Marvel Blind Bags",
        }
        store_articles(competitor["id"], article)
        update_article_keyword(competitor["id"], article["url"], result)
        saved = get_articles(competitor["id"])[0]
        self.assertEqual(saved["trends_series"], result["trends_series"])
        self.assertEqual(saved["trends_interest"], result["trends_interest"])

    @patch("keyword_service.requests.get")
    def test_seodata_returns_real_keyword_metrics(self, mock_get):
        mock_response = mock_get.return_value
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "keyword": "marvel blind bags",
            "volume": 2400,
            "cpc": 1.37,
            "competition": 0.62,
            "country": "us",
            "cached": True,
            "similar": [
                {"keyword": "marvel blind bag", "volume": 900, "cpc": 1.1}
            ],
        }

        with patch.dict(
            os.environ,
            {
                "SEODATA_ENABLED": "1",
                "SEODATA_API_KEY": "test-seodata-key",
                "SEODATA_COUNTRY": "us",
                "GOOGLE_TRENDS_ENABLED": "0",
            },
        ):
            result = enrich_keyword({
                "title": "Marvel Blind Bags",
                "url": "https://example.com/article",
            })

        self.assertEqual(result["search_volume"], 2400)
        self.assertEqual(result["volume_source"], "SeoData")
        self.assertEqual(result["volume_status"], "SUCCESS")
        self.assertEqual(result["cpc"], 1.37)
        self.assertEqual(result["competition"], 0.62)
        self.assertEqual(result["country"], "us")
        self.assertTrue(result["provider_cached"])
        self.assertEqual(result["similar_keywords"][0]["keyword"], "marvel blind bag")
        request_args, request_kwargs = mock_get.call_args
        self.assertEqual(request_args[0], "https://api.seodata.dev/v1/keyword")
        self.assertEqual(request_kwargs["params"], {
            "q": "marvel blind bags",
            "country": "us",
        })
        self.assertEqual(
            request_kwargs["headers"]["Authorization"],
            "Bearer test-seodata-key",
        )
        competitor = add_competitor(
            "SeoData Persistence Test",
            "https://seodata-persistence.example",
        )
        article = {
            "url": "https://seodata-persistence.example/article",
            "title": "Marvel Blind Bags",
        }
        store_articles(competitor["id"], article)
        update_article_keyword(competitor["id"], article["url"], result)
        saved = get_articles(competitor["id"])[0]
        self.assertEqual(saved["search_volume"], 2400)
        self.assertEqual(saved["cpc"], 1.37)
        self.assertEqual(saved["competition"], 0.62)
        self.assertEqual(saved["similar_keywords"][0]["keyword"], "marvel blind bag")

    @patch("keyword_service.requests.get")
    def test_seodata_public_anonymous_lookup_works_without_key(self, mock_get):
        mock_response = mock_get.return_value
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "keyword": "marvel blind bags",
            "volume": 2400,
            "cpc": None,
            "competition": 0.1,
            "country": "us",
            "cached": False,
        }

        with patch.dict(
            os.environ,
            {
                "SEODATA_ENABLED": "1",
                "SEODATA_API_KEY": "",
                "GOOGLE_TRENDS_ENABLED": "0",
            },
        ):
            result = enrich_keyword({
                "title": "Marvel Blind Bags",
                "url": "https://example.com/article",
            })

        self.assertEqual(result["search_volume"], 2400)
        self.assertNotIn("Authorization", mock_get.call_args.kwargs["headers"])

    @patch("keyword_service.requests.get")
    def test_seodata_invalid_api_key_is_reported(self, mock_get):
        mock_get.return_value.status_code = 401

        with patch.dict(
            os.environ,
            {
                "SEODATA_ENABLED": "1",
                "SEODATA_API_KEY": "invalid-test-key",
                "GOOGLE_TRENDS_ENABLED": "0",
            },
        ):
            result = enrich_keyword({
                "title": "Competitor article",
                "url": "https://example.com/article",
            })

        self.assertEqual(result["volume_status"], "INVALID_API_KEY")
        self.assertEqual(result["volume_error"], "SeoData rejected the API key.")
        self.assertIsNone(result["search_volume"])

    @patch("keyword_service.requests.get")
    def test_seodata_no_data_responses_never_become_numeric_metrics(self, mock_get):
        response = mock_get.return_value
        response.json.return_value = {
            "keyword": "unindexed phrase",
            "volume": 0,
            "cpc": 0,
            "competition": 0,
            "country": "us",
        }

        with patch.dict(
            os.environ,
            {
                "SEODATA_ENABLED": "1",
                "SEODATA_API_KEY": "",
                "GOOGLE_TRENDS_ENABLED": "0",
            },
        ):
            for status_code in (404, 200):
                with self.subTest(status_code=status_code):
                    response.status_code = status_code
                    result = enrich_keyword({
                        "title": "Unindexed Phrase",
                        "url": "https://example.com/article",
                    })

                    self.assertEqual(result["volume_status"], "NO_DATA")
                    self.assertIsNone(result["search_volume"])
                    self.assertIsNone(result["cpc"])
                    self.assertIsNone(result["competition"])

    @patch("keyword_service.requests.post")
    def test_dataforseo_uses_returned_volume_and_monthly_series(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tasks": [{
                "result": [{
                    "keyword": "marvel blind bags",
                    "search_volume": 1200,
                    "monthly_searches": [
                        {"year": 2026, "month": 2, "search_volume": 900},
                        {"year": 2026, "month": 3, "search_volume": 1200},
                    ],
                }],
            }],
        }

        with patch.dict(
            os.environ,
            {
                "DATAFORSEO_LOGIN": "test-login",
                "DATAFORSEO_PASSWORD": "test-password",
                "GOOGLE_TRENDS_ENABLED": "0",
                "KEYWORDS_EVERYWHERE_API_KEY": "",
            },
        ):
            result = enrich_keyword({
                "title": "Marvel Blind Bags",
                "url": "https://example.com/article",
            })

        self.assertEqual(result["search_volume"], 1200)
        self.assertEqual(result["volume_source"], "DataForSEO Google Ads")
        self.assertEqual(
            result["volume_series"],
            [
                {"date": "2026-02", "value": 900.0},
                {"date": "2026-03", "value": 1200.0},
            ],
        )
        self.assertEqual(result["trends_series"], [])
        self.assertIsNone(result["trends_interest"])

    @patch.object(app_module, "enrich_keyword")
    def test_trends_refresh_updates_existing_competitor_articles(self, mock_enrich):
        competitor = add_competitor("Refresh Test", "https://refresh-test.example")
        article = {
            "url": "https://refresh-test.example/article",
            "title": "Actual Competitor Topic",
        }
        store_articles(competitor["id"], article)
        mock_enrich.return_value = {
            "keyword": "actual competitor topic",
            "search_volume": 4321,
            "trends_interest": 37.5,
            "trends_series": [{"date": "2026-03-01", "value": 37.5}],
            "volume_source": "Test provider",
            "trends_status": "SUCCESS",
            "trends_error": None,
            "source": "Test provider",
            "status": "SUCCESS",
            "error": None,
        }

        response = self.client.post(
            f"/api/competitors/{competitor['id']}/trends/refresh"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["refreshed"], 1)
        self.assertEqual(get_articles(competitor["id"])[0]["trends_interest"], 37.5)

    @patch.object(app_module, "enrich_keyword")
    def test_trends_rate_limit_does_not_stop_keyword_volume_refresh(self, mock_enrich):
        competitor = add_competitor("Independent Refresh Test", "https://independent.example")
        for slug in ("article-one", "article-two"):
            store_articles(competitor["id"], {
                "url": f"https://independent.example/{slug}",
                "title": slug,
            })
        successful_volume = {
            "keyword": "real phrase",
            "search_volume": 1200,
            "volume_status": "SUCCESS",
            "trends_status": "RATE_LIMITED",
            "status": "SUCCESS",
        }
        mock_enrich.return_value = successful_volume

        response = self.client.post(
            f"/api/competitors/{competitor['id']}/trends/refresh"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["refreshed"], 2)
        self.assertEqual(mock_enrich.call_count, 2)

    @patch.object(app_module, "send_slack_alert")
    @patch.object(app_module, "extract_pricing")
    @patch.object(app_module, "enrich_keyword")
    @patch.object(app_module, "scan_competitor")
    def test_scan_backfills_keyword_metrics_for_existing_articles(
        self, mock_scan, mock_enrich, mock_pricing, mock_alert
    ):
        competitor = add_competitor("Backfill Test", "https://backfill-test.example")
        article = {
            "url": "https://backfill-test.example/blog/real-topic",
            "title": "Real Competitor Topic",
        }
        store_articles(competitor["id"], article)
        update_article_keyword(competitor["id"], article["url"], {
            "keyword": "real competitor topic",
            "volume_status": "API_ERROR",
            "volume_error": "SeoData returned HTTP 404.",
        })
        mock_scan.return_value = {
            "status": "SUCCESS",
            "message": "Found one article.",
            "pages_checked": 2,
            "articles": [article],
        }
        mock_enrich.return_value = {
            "keyword": "real competitor topic",
            "volume_keyword": "real competitor topic",
            "trends_keyword": "real competitor topic",
            "search_volume": 135000,
            "cpc": 15.18,
            "competition": 0.54,
            "country": "us",
            "provider_cached": True,
            "similar_keywords": [{"keyword": "competitor topic", "volume": 40500}],
            "volume_status": "SUCCESS",
            "trends_status": "API_NOT_CONFIGURED",
            "status": "SUCCESS",
            "source": "SeoData",
        }
        mock_pricing.return_value = {"status": "NO_DATA", "plans": []}
        mock_alert.return_value = {"sent": False, "reason": "No alert configured."}

        response = self.client.post(
            f"/api/competitors/{competitor['id']}/scan"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_enrich.call_count, 1)
        saved_article = get_articles(competitor["id"])[0]
        self.assertEqual(saved_article["search_volume"], 135000)
        self.assertEqual(saved_article["cpc"], 15.18)
        self.assertEqual(saved_article["competition"], 0.54)
        self.assertEqual(saved_article["similar_keywords"][0]["volume"], 40500)
        self.assertEqual(response.json["keyword_results"][0]["volume_status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
