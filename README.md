# CompetitorPulse AI

A small competitor monitoring dashboard built with Flask, SQLite, BeautifulSoup, and Slack webhooks. It scans public pages for article metadata, stores canonical URLs to avoid repeat alerts, and records visible pricing snapshots.

## Features

- Add and remove competitor websites
- Discover likely article URLs from homepage links, `robots.txt`, and XML sitemaps
- Extract page title, canonical URL, publication date, and meta description where available
- Store first-seen history so unchanged article URLs are not flagged repeatedly
- Best-effort visible pricing extraction from common pricing pages
- Competitor-derived search metrics from SeoData, with anonymous lookup support
- Separate Google Trends interest scores and historical series where accessible
- Optional search-volume enrichment through Keywords Everywhere or DataForSEO
- Optional Slack alerts for new articles and detected pricing changes
- Clear scan statuses: `SUCCESS`, `PARTIAL`, `BLOCKED`, `NO_BLOG_FOUND`, or `FAILED`
- Robots.txt rules are checked before crawling; disallowed or inaccessible pages are not bypassed

## Important limitations

- This MVP uses bounded, synchronous scans and checks up to 12 pages per scan. Large sites may need pagination and background jobs later.
- Some websites use JavaScript rendering, anti-bot protections, or custom content routes. The app does not bypass access controls; a blocked scan should be treated as inconclusive.
- `NO_BLOG_FOUND` means no likely post URLs were discovered; `BLOCKED` means robots.txt or an HTTP access restriction prevented scanning; `PARTIAL` means candidate pages were reachable but article extraction was incomplete.
- Pricing extraction is heuristic and may miss prices or misread complex pricing cards. Verify important changes against the source page.
- Keyword candidates are derived from competitor article titles. SeoData provides estimated monthly search volume, CPC, competition, and related keywords; its anonymous API allows 20 lookups per day. Registering with SeoData is optional and provides a larger free allowance. Google Trends is a separate source for relative 0–100 interest and can rate-limit automated requests, so those values may be unavailable. The Trends panel shows only provider-returned data and never generates sample values or charts.
- Optional Keywords Everywhere and DataForSEO integrations are fallbacks for search-volume data; DataForSEO usage may incur charges.
- The **Refresh data** action retries enrichment for up to 12 of the selected competitor's latest articles. A provider error or rate limit is shown as a status, not replaced with invented values.
- Initial article discovery establishes a baseline. Existing articles are saved without creating a `new_article` event; later new canonical URLs create events.
- Render free web services can spin down. Persistent disks are not available on every plan and may require a paid plan. Confirm Render's current disk/database pricing before deploying if you need history to survive restarts. Do not rely on an ephemeral local SQLite file for important history.
- This project is an educational MVP, not a guarantee of exhaustive competitor coverage.

## Run locally

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env`, then run:

```bash
python app.py
```

Open http://127.0.0.1:5000

## Run tests

```bash
python -m unittest discover -s tests -v
```

## Deploy to Render

1. Push this folder to a GitHub repository.
2. In Render, choose **New → Blueprint** and connect the repository. Render reads `render.yaml`.
3. The free-service configuration uses a local SQLite file so the app can deploy without another service. This is suitable for a short demo, but the free instance's filesystem is ephemeral: monitoring history can be lost after a restart, spin-down, or redeploy. For reliable long-term history, use a persistent disk or managed database on a plan that supports it.
4. SeoData works anonymously by default. To use a registered account, set `SEODATA_API_KEY` privately in the environment; set `SEODATA_COUNTRY` to a two-letter country code. You can also configure `SLACK_WEBHOOK_URL`, `KEYWORDS_EVERYWHERE_API_KEY`, or `DATAFORSEO_LOGIN` plus `DATAFORSEO_PASSWORD`. Search-volume APIs may have usage limits or charges. Never put secrets in frontend JavaScript or commit `.env`.
5. Deploy, then open the generated `onrender.com` URL and check `/health`.

## Slack setup

Create a Slack incoming webhook in a workspace you control and set `SLACK_WEBHOOK_URL` in Render's environment settings. Leave it blank to disable notifications.

## DataForSEO setup

Set `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD` in Render environment settings to request Google Ads search volume and monthly search history. This can incur provider charges. Alternatively, set `KEYWORDS_EVERYWHERE_API_KEY` for its search-volume endpoint. Google Trends interest is independently fetched from Google Trends and may be rate-limited; the dashboard displays provider status and does not substitute sample data.

## SeoData setup

SeoData is the primary keyword metrics provider. Without credentials, the app uses its documented anonymous allowance (20 lookups/day per IP). For higher limits, register directly at seodata.dev and set `SEODATA_API_KEY` in your local `.env` or hosting environment; set `SEODATA_COUNTRY` to the desired two-letter country code. The app reads the returned monthly volume, CPC, competition, cache status, and similar-keyword list. SeoData does not currently provide Google Trends interest; Google Trends is requested independently and may return a rate-limit status.

## Demo flow (about 2 minutes)

1. Add three real competitor domains that have public blog/resource pages.
2. Run a scan and show the extracted article metadata and pricing result.
3. The first scan saves a baseline without new-article events. Run the same scan again; unchanged canonical URLs should not generate duplicate events, while newly published canonical URLs are reported once.
4. Show the article history and recent activity.
5. If Slack and keyword credentials are configured, show a real alert and provider-returned search volume.
6. Explain any `PARTIAL`/`BLOCKED` status honestly; do not present an inaccessible site as having no content.

## API routes

- `GET /health`
- `GET /api/dashboard`
- `GET /api/competitors`
- `POST /api/competitors` with JSON `{ "name": "Example", "url": "https://example.com" }`
- `DELETE /api/competitors/<id>`
- `POST /api/competitors/<id>/scan`
- `POST /api/competitors/<id>/trends/refresh`
- `GET /api/competitors/<id>/articles`
- `GET /api/competitors/<id>/pricing`
- `GET /api/competitors/<id>/scans`
