import unittest
from unittest.mock import patch

import requests

from scraper import scan_competitor


def _response(url, status_code=200, body="", content_type="text/html"):
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    response.headers["Content-Type"] = content_type
    response._content = body.encode("utf-8")
    return response


class ScraperScanTests(unittest.TestCase):
    base_url = "https://example.com/"
    robots_url = "https://example.com/robots.txt"
    article_url = "https://example.com/blog/new-post"

    def _homepage(self, robots_text="", homepage_body=""):
        homepage = homepage_body or (
            '<html><body><a href="/blog/new-post">New post</a></body></html>'
        )

        def fetch(url):
            if url == self.robots_url:
                return _response(url, body=robots_text, content_type="text/plain")
            if url == self.base_url:
                return _response(url, body=homepage)
            if url == self.article_url:
                return _response(
                    url,
                    body=(
                        '<html><head><meta property="og:title" '
                        'content="New post"></head><body><article>Post</article>'
                        '</body></html>'
                    ),
                )
            raise AssertionError(f"Unexpected URL requested: {url}")

        return fetch

    @patch("scraper._sitemap_urls", return_value=[])
    @patch("scraper._get")
    def test_finds_article_linked_from_homepage(self, mock_get, _mock_sitemaps):
        mock_get.side_effect = self._homepage("User-agent: *\nAllow: /\n")

        result = scan_competitor(self.base_url)

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual([article["url"] for article in result["articles"]], [
            self.article_url,
        ])

    @patch("scraper._sitemap_urls", return_value=[])
    @patch("scraper._get")
    def test_disallowed_article_is_reported_as_blocked(
        self, mock_get, _mock_sitemaps
    ):
        mock_get.side_effect = self._homepage(
            "User-agent: *\nDisallow: /blog/\n"
        )

        result = scan_competitor(self.base_url)

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("robots.txt disallows", result["message"])
        self.assertEqual(mock_get.call_count, 2)

    @patch("scraper._sitemap_urls", return_value=[])
    @patch("scraper._get")
    def test_homepage_without_article_urls_is_no_blog_found(
        self, mock_get, _mock_sitemaps
    ):
        mock_get.side_effect = self._homepage(
            "User-agent: *\nAllow: /\n",
            '<html><body><a href="/pricing">Pricing</a></body></html>',
        )

        result = scan_competitor(self.base_url)

        self.assertEqual(result["status"], "NO_BLOG_FOUND")
        self.assertEqual(result["articles"], [])

    @patch("scraper._sitemap_urls", return_value=[])
    @patch("scraper._get")
    def test_article_page_bot_block_returns_blocked(
        self, mock_get, _mock_sitemaps
    ):
        def fetch(url):
            if url == self.robots_url:
                return _response(
                    url, body="User-agent: *\nAllow: /\n",
                    content_type="text/plain",
                )
            if url == self.base_url:
                return _response(
                    url,
                    body='<html><a href="/blog/new-post">New post</a></html>',
                )
            if url == self.article_url:
                return _response(url, status_code=403)
            raise AssertionError(f"Unexpected URL requested: {url}")

        mock_get.side_effect = fetch

        result = scan_competitor(self.base_url)

        self.assertEqual(result["status"], "BLOCKED")

    @patch("scraper._sitemap_urls", return_value=[])
    @patch("scraper._get")
    def test_missing_candidate_page_is_partial_not_blocked(
        self, mock_get, _mock_sitemaps
    ):
        def fetch(url):
            if url == self.robots_url:
                return _response(
                    url, body="User-agent: *\nAllow: /\n",
                    content_type="text/plain",
                )
            if url == self.base_url:
                return _response(
                    url,
                    body='<html><a href="/blog/new-post">New post</a></html>',
                )
            if url == self.article_url:
                return _response(url, status_code=404)
            raise AssertionError(f"Unexpected URL requested: {url}")

        mock_get.side_effect = fetch

        result = scan_competitor(self.base_url)

        self.assertEqual(result["status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
