import os
import re
from typing import Any

import pandas as pd
import requests
from pytrends.exceptions import TooManyRequestsError
from pytrends.request import TrendReq

# Fix for the pandas downcasting warning/crash.
pd.set_option("future.no_silent_downcasting", True)

REQUEST_TIMEOUT = (10, 25)

TRENDS_HL = os.getenv("GOOGLE_TRENDS_HL", "en-US")
TRENDS_TZ = int(os.getenv("GOOGLE_TRENDS_TZ", "330"))
TRENDS_GEO = os.getenv("GOOGLE_TRENDS_GEO", "")
TRENDS_TIMEFRAME = os.getenv("GOOGLE_TRENDS_TIMEFRAME", "today 12-m")


def _candidate_keywords(article):
    """Return title-derived keyword candidates highest-signal first."""
    title = (article.get("title") or "").lower()
    title = re.sub(r"[^a-z0-9\s-]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    stop_words = {
        "the", "a", "an", "and", "or", "for", "to", "of", "in",
        "on", "with", "by", "how", "what", "why", "is", "are",
        "was", "were", "your", "about", "every", "this", "that",
        "from", "into", "out", "week", "know", "exactly", "2024",
        "2025", "2026", "archive", "archives", "category", "categories",
        "page", "home", "site", "read", "more", "learn", "everything",
        "need", "latest", "update", "updates", "news", "blog", "article",
        "articles", "guide", "tips", "best", "top", "meet", "our", "we",
        "us", "at", "should", "will", "can", "debuts", "finalists",
        "community", "awards", "works", "work", "partner", "complete"
    }

    words = [word for word in title.split() if word not in stop_words and len(word) >= 2]
    if not words:
        return []

    candidates = []
    max_words = min(len(words), 6)
    for size in range(max_words, 0, -1):
        candidates.append(" ".join(words[:size]))
    return candidates


def _result(
    keyword,
    volume_keyword=None,
    trends_keyword=None,
    search_volume=None,
    trends_interest=None,
    trends_series=None,
    volume_series=None,
    volume_source=None,
    volume_status=None,
    volume_error=None,
    cpc=None,
    competition=None,
    country=None,
    provider_cached=None,
    similar_keywords=None,
    trends_status=None,
    trends_error=None,
    status="API_ERROR",
    source="Google Trends",
    error=None,
):
    return {
        "keyword": keyword,
        "volume_keyword": volume_keyword or keyword,
        "trends_keyword": trends_keyword,
        "search_volume": search_volume,
        "trends_interest": trends_interest,
        "trends_series": trends_series or [],
        "volume_series": volume_series or [],
        "volume_source": volume_source,
        "volume_status": volume_status or status,
        "volume_error": volume_error or error,
        "cpc": cpc,
        "competition": competition,
        "country": country,
        "provider_cached": provider_cached,
        "similar_keywords": similar_keywords or [],
        "trends_status": trends_status or status,
        "trends_error": trends_error or error,
        "source": source,
        "status": status,
        "error": error,
    }


def _parse_trends_interest(data, keyword):
    if data is None or getattr(data, "empty", True) or keyword not in data.columns:
        return None
    series = data[keyword].dropna()
    return round(float(series.mean()), 2) if not series.empty else None


def _parse_trends_series(data, keyword):
    if data is None or getattr(data, "empty", True) or keyword not in data.columns:
        return []
    series = data[keyword].dropna()
    return [
        {"date": timestamp.date().isoformat(), "value": float(value)}
        for timestamp, value in series.items()
    ]


def _keywordseverywhere_status(status_code):
    if status_code == 401:
        return "INVALID_API_KEY"
    if status_code == 429:
        return "RATE_LIMITED"
    return "API_ERROR"


def _seodata_query(keyword):
    if os.getenv("SEODATA_ENABLED", "1").strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        return _result(
            keyword,
            status="PROVIDER_DISABLED",
            volume_status="PROVIDER_DISABLED",
            source="SeoData",
        )

    country = os.getenv("SEODATA_COUNTRY", "us").strip().lower() or "us"
    api_key = os.getenv("SEODATA_API_KEY", "").strip()
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.get(
            "https://api.seodata.dev/v1/keyword",
            params={"q": keyword, "country": country},
            headers=headers,
            timeout=20,
        )
    except requests.RequestException as exc:
        return _result(
            keyword,
            status="API_ERROR",
            volume_status="API_ERROR",
            source="SeoData",
            error=str(exc),
        )

    if response.status_code in (401, 403):
        return _result(
            keyword,
            status="INVALID_API_KEY",
            volume_status="INVALID_API_KEY",
            source="SeoData",
            error="SeoData rejected the API key.",
        )
    if response.status_code == 429:
        return _result(
            keyword,
            status="RATE_LIMITED",
            volume_status="RATE_LIMITED",
            source="SeoData",
            error="SeoData daily lookup limit reached.",
        )
    if response.status_code == 404:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        try:
            no_volume = (
                isinstance(payload, dict)
                and payload.get("keyword")
                and float(payload.get("volume")) == 0
            )
        except (TypeError, ValueError):
            no_volume = False
        if no_volume:
            return _result(
                keyword,
                status="NO_DATA",
                volume_status="NO_DATA",
                source="SeoData",
                error="SeoData has no search-volume data for this keyword.",
            )
    if response.status_code >= 400:
        return _result(
            keyword,
            status="API_ERROR",
            volume_status="API_ERROR",
            source="SeoData",
            error=f"SeoData returned HTTP {response.status_code}.",
        )

    try:
        payload = response.json()
    except ValueError:
        return _result(
            keyword,
            status="API_ERROR",
            volume_status="API_ERROR",
            source="SeoData",
            error="SeoData returned invalid JSON.",
        )

    if not isinstance(payload, dict) or payload.get("volume") is None:
        return _result(
            keyword,
            status="NO_DATA",
            volume_status="NO_DATA",
            source="SeoData",
            error="SeoData returned no search volume for this keyword.",
        )

    try:
        volume = int(payload["volume"])
        if volume < 0:
            raise ValueError
        cpc = float(payload["cpc"]) if payload.get("cpc") is not None else None
        competition = (
            float(payload["competition"])
            if payload.get("competition") is not None
            else None
        )
    except (TypeError, ValueError):
        return _result(
            keyword,
            status="API_ERROR",
            volume_status="API_ERROR",
            source="SeoData",
            error="SeoData returned invalid numeric keyword metrics.",
        )

    if volume == 0:
        return _result(
            keyword,
            status="NO_DATA",
            volume_status="NO_DATA",
            source="SeoData",
            error="SeoData has no search-volume data for this keyword.",
        )

    similar = payload.get("similar")
    similar_keywords = [
        item
        for item in similar
        if isinstance(item, dict) and isinstance(item.get("keyword"), str)
    ] if isinstance(similar, list) else []

    return _result(
        keyword=payload.get("keyword") or keyword,
        volume_keyword=payload.get("keyword") or keyword,
        search_volume=volume,
        volume_source="SeoData",
        volume_status="SUCCESS",
        cpc=cpc,
        competition=competition,
        country=payload.get("country") or country,
        provider_cached=payload.get("cached"),
        similar_keywords=similar_keywords,
        status="SUCCESS",
        source="SeoData",
    )


def _deep_find_first(value: Any, *keys):
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for nested in value.values():
            found = _deep_find_first(nested, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _deep_find_first(item, *keys)
            if found is not None:
                return found
    return None


def _keywords_everywhere_query(keyword, api_key):
    if not api_key:
        return _result(keyword, status="API_NOT_CONFIGURED", source="title-derived")

    try:
        response = requests.post(
            "https://api.keywordseverywhere.com/v1/get_keyword_data",
            data={
                "api_key": api_key,
                "keyword": keyword,
                "country": "US",
                "currency": "USD",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        return _result(keyword, status="API_ERROR", source="Keywords Everywhere", error=str(exc))

    if response.status_code == 401:
        return _result(keyword, status="INVALID_API_KEY", source="Keywords Everywhere", error="Invalid API key.")
    if response.status_code == 429:
        return _result(keyword, status="RATE_LIMITED", source="Keywords Everywhere", error="Rate limited by Keywords Everywhere.")
    if response.status_code >= 400:
        return _result(
            keyword,
            status=_keywordseverywhere_status(response.status_code),
            source="Keywords Everywhere",
            error=f"HTTP {response.status_code}",
        )

    try:
        payload = response.json()
    except ValueError:
        return _result(keyword, status="API_ERROR", source="Keywords Everywhere", error="Invalid JSON response.")

    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        first = data[0]
        volume = first.get("vol")
        if volume is not None:
            return _result(
                keyword,
                volume_keyword=keyword,
                search_volume=int(volume),
                volume_source="Keywords Everywhere",
                status="SUCCESS",
                source="Keywords Everywhere",
            )

    if isinstance(payload, dict):
        volume = _deep_find_first(payload, "vol", "search_volume", "volume")
        if volume is not None:
            return _result(
                keyword,
                volume_keyword=keyword,
                search_volume=int(volume),
                volume_source="Keywords Everywhere",
                status="SUCCESS",
                source="Keywords Everywhere",
            )

    return _result(keyword, status="API_ERROR", source="Keywords Everywhere", error="No search volume in response.")


def _dataforseo_query(keyword):
    login = os.getenv("DATAFORSEO_LOGIN", "").strip()
    password = os.getenv("DATAFORSEO_PASSWORD", "").strip()
    if not login or not password:
        return _result(keyword, status="API_NOT_CONFIGURED", source="DataForSEO")

    try:
        response = requests.post(
            "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live",
            auth=(login, password),
            json=[{
                "keywords": [keyword],
                "location_code": int(os.getenv("DATAFORSEO_LOCATION_CODE", "2356")),
                "language_code": os.getenv("DATAFORSEO_LANGUAGE_CODE", "en").strip() or "en",
            }],
            timeout=30,
        )
    except requests.RequestException as exc:
        return _result(keyword, status="API_ERROR", source="DataForSEO", error=str(exc))

    if response.status_code in (401, 403):
        return _result(keyword, status="INVALID_API_KEY", source="DataForSEO", error="Invalid DataForSEO credentials.")
    if response.status_code == 429:
        return _result(keyword, status="RATE_LIMITED", source="DataForSEO", error="Rate limited by DataForSEO.")
    if response.status_code >= 400:
        return _result(keyword, status="API_ERROR", source="DataForSEO", error=f"HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        return _result(keyword, status="API_ERROR", source="DataForSEO", error="Invalid JSON response.")

    try:
        result_items = payload["tasks"][0]["result"]
        item = next((row for row in result_items if row.get("keyword") == keyword), None)
    except (KeyError, IndexError, TypeError):
        item = None
    if not item:
        return _result(keyword, status="NO_DATA", source="DataForSEO", error="Provider returned no result for this keyword.")

    monthly_searches = item.get("monthly_searches") or []
    series = [
        {
            "date": f"{entry['year']:04d}-{entry['month']:02d}",
            "value": float(entry["search_volume"]),
        }
        for entry in monthly_searches
        if entry.get("year") is not None
        and entry.get("month") is not None
        and entry.get("search_volume") is not None
    ]
    volume = item.get("search_volume")
    if volume is None and not series:
        return _result(keyword, status="NO_DATA", source="DataForSEO", error="Provider returned no search-volume data.")
    return _result(
        keyword,
        volume_keyword=keyword,
        search_volume=int(volume) if volume is not None else None,
        volume_series=series,
        volume_source="DataForSEO Google Ads" if volume is not None else None,
        volume_status="SUCCESS",
        status="SUCCESS",
        source="DataForSEO Google Ads",
    )


def _google_trends_query(article, keywords_to_try, max_retries=3):
    for keyword in keywords_to_try:
        for attempt in range(max_retries):
            try:
                trends = TrendReq(hl=TRENDS_HL, tz=TRENDS_TZ, timeout=REQUEST_TIMEOUT)
                trends.build_payload(
                    kw_list=[keyword],
                    timeframe=TRENDS_TIMEFRAME,
                    geo=TRENDS_GEO,
                )
                data = trends.interest_over_time()
                interest = _parse_trends_interest(data, keyword)
                if interest is not None:
                    return _result(
                        keyword,
                        trends_keyword=keyword,
                        trends_interest=interest,
                        trends_series=_parse_trends_series(data, keyword),
                        trends_status="SUCCESS",
                        status="SUCCESS",
                    )
                break

            except (TooManyRequestsError, requests.exceptions.RequestException) as exc:
                is_rate_limit = "429" in str(exc) or "rate" in str(exc).lower()
                status = "RATE_LIMITED" if is_rate_limit else "API_ERROR"
                return _result(
                    keyword,
                    trends_keyword=keyword,
                    trends_status=status,
                    status=status,
                    error=str(exc),
                )

            except Exception as exc:
                return _result(
                    keyword,
                    trends_keyword=keyword,
                    trends_status="API_ERROR",
                    status="API_ERROR",
                    error=str(exc),
                )

    return _result(
        keywords_to_try[0],
        trends_keyword=keywords_to_try[0],
        trends_status="TRENDS_UNAVAILABLE",
        status="TRENDS_UNAVAILABLE",
    )


def enrich_keyword(article, max_retries=3):
    keywords_to_try = _candidate_keywords(article)

    if not keywords_to_try:
        return _result(None, status="NO_KEYWORD", source="title-derived")

    keyword = keywords_to_try[0]
    volume_result = _seodata_query(keyword)
    if volume_result.get("volume_status") != "SUCCESS":
        api_key = os.getenv("KEYWORDS_EVERYWHERE_API_KEY", "").strip()
        if api_key:
            alternate_result = _keywords_everywhere_query(keyword, api_key)
            volume_result = alternate_result

    if volume_result.get("volume_status") != "SUCCESS":
        dataforseo_login = os.getenv("DATAFORSEO_LOGIN", "").strip()
        dataforseo_password = os.getenv("DATAFORSEO_PASSWORD", "").strip()
        if dataforseo_login and dataforseo_password:
            dataforseo_result = _dataforseo_query(keywords_to_try[0])
            volume_result = dataforseo_result

    enabled = os.getenv("GOOGLE_TRENDS_ENABLED", "1").strip().lower()
    if enabled in {"1", "true", "yes", "on"}:
        trends_result = _google_trends_query(article, keywords_to_try, max_retries=max_retries)
    else:
        trends_result = _result(
            keyword,
            trends_status="API_NOT_CONFIGURED",
            status="API_NOT_CONFIGURED",
            source="Google Trends",
            error="Google Trends lookup is disabled.",
        )

    volume_succeeded = volume_result.get("volume_status") == "SUCCESS"
    trends_succeeded = trends_result.get("trends_status") == "SUCCESS"
    source_list = []
    if volume_succeeded and volume_result.get("source"):
        source_list.append(volume_result["source"])
    if trends_succeeded and trends_result.get("source"):
        source_list.append(trends_result["source"])
    status = "SUCCESS" if volume_succeeded or trends_succeeded else (
        volume_result.get("volume_status")
        if volume_result.get("volume_status") not in {
            "NO_DATA", "PROVIDER_DISABLED", "API_NOT_CONFIGURED"
        }
        else trends_result.get("trends_status")
    )
    return _result(
        keyword=trends_result.get("keyword") or volume_result.get("keyword") or keyword,
        volume_keyword=volume_result.get("volume_keyword") or volume_result.get("keyword") or keyword,
        trends_keyword=trends_result.get("trends_keyword"),
        search_volume=volume_result.get("search_volume") if volume_succeeded else None,
        trends_interest=trends_result.get("trends_interest") if trends_succeeded else None,
        trends_series=trends_result.get("trends_series") if trends_succeeded else [],
        volume_series=volume_result.get("volume_series") if volume_succeeded else [],
        volume_source=volume_result.get("volume_source") if volume_succeeded else None,
        volume_status=volume_result.get("volume_status"),
        volume_error=volume_result.get("volume_error"),
        cpc=volume_result.get("cpc") if volume_succeeded else None,
        competition=volume_result.get("competition") if volume_succeeded else None,
        country=volume_result.get("country") if volume_succeeded else None,
        provider_cached=volume_result.get("provider_cached") if volume_succeeded else None,
        similar_keywords=volume_result.get("similar_keywords") if volume_succeeded else [],
        trends_status=trends_result.get("trends_status"),
        trends_error=trends_result.get("trends_error"),
        status=status,
        source=(
            " + ".join(source_list)
            or (
                "title-derived"
                if volume_result.get("volume_status") in {
                    "NO_DATA", "PROVIDER_DISABLED", "API_NOT_CONFIGURED"
                }
                else volume_result.get("source") or trends_result.get("source")
            )
        ),
        error=volume_result.get("volume_error") or trends_result.get("trends_error"),
    )