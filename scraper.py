import ipaddress
import json
import re
import socket
import functools
import concurrent.futures
from urllib.robotparser import RobotFileParser
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree

USER_AGENT = "CompetitorPulseBot/1.1 (+public-page monitoring; respectful crawler)"
TIMEOUT = 12
MAX_PAGES = 12
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2_000_000
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "fbclid", "gclid", "ref",
}
ARTICLE_TYPES = {
    "article", "blogposting", "newsarticle", "techarticle",
}
EXCLUDED_PATH_PARTS = {
    "tag", "tags", "category", "categories", "author", "authors",
    "page", "search", "pricing", "contact", "about", "login",
    "signup", "privacy", "terms", "wp-json", "feed", "archive", "archives",
}
CONTENT_HINTS = (
    "/blog/", "/blogs/", "/article/", "/articles/", "/news/",
    "/resources/", "/resource/", "/insights/", "/insight/", "/post/",
    "/posts/",
)
MONTH_ARCHIVE_RE = re.compile(r"^/\d{4}/\d{1,2}/?$")
YEAR_ARCHIVE_RE = re.compile(r"^/\d{4}/?$")


# OPTIMIZATION: Cache DNS resolutions to speed up multiple requests to the same host
@functools.lru_cache(maxsize=256)
def _safe_public_url(url):
    """Accept only public HTTP(S) URLs and reject local/private destinations."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False

        host = parsed.hostname.rstrip(".").lower()
        if (
            host == "localhost"
            or host.endswith(".localhost")
            or host.endswith(".local")
        ):
            return False

        try:
            ip = ipaddress.ip_address(host)
            return ip.is_global
        except ValueError:
            pass

        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not addresses:
            return False

        for item in addresses:
            ip = ipaddress.ip_address(item[4][0].split("%")[0])
            if not ip.is_global:
                return False
        return True
    except (ValueError, OSError, socket.gaierror):
        return False
    except Exception:
        return False


def _canonical(url):
    """Normalize a URL and remove common analytics parameters."""
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(query, doseq=True),
            "",
        )
    )


def _get(url):
    """Fetch a public URL while validating each redirect."""
    current_url = url
    session = requests.Session()

    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not _safe_public_url(current_url):
                raise ValueError(
                    "URL or redirect target is not a publicly reachable HTTP(S) address."
                )

            response = session.get(
                current_url,
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
                allow_redirects=False,
                stream=True,
            )

            if response.status_code not in (301, 302, 303, 307, 308):
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        response.close()
                        raise ValueError("Response is larger than the 2 MB crawl limit.")
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
                return response

            location = response.headers.get("Location")
            response.close()
            if not location:
                raise requests.RequestException("Redirect response did not include a Location header.")
            current_url = urljoin(current_url, location)

        raise requests.TooManyRedirects("Too many redirects.")
    finally:
        session.close()


def _same_domain(url, base_host):
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        base = (base_host or "").lower().rstrip(".")
        if not host or not base:
            return False
        allowed_bases = {base}
        if base.startswith("www."):
            allowed_bases.add(base[4:])
        return any(
            host == allowed_base or host.endswith("." + allowed_base)
            for allowed_base in allowed_bases
        )
    except Exception:
        return False


def _flatten_jsonld(value):
    output = []
    if isinstance(value, list):
        for item in value:
            output.extend(_flatten_jsonld(item))
    elif isinstance(value, dict):
        output.append(value)
        if value.get("@graph"):
            output.extend(_flatten_jsonld(value["@graph"]))
        for key in ("mainEntity", "itemListElement", "subjectOf"):
            if value.get(key):
                output.extend(_flatten_jsonld(value[key]))
    return output


def _jsonld_objects(soup):
    objects = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            objects.extend(_flatten_jsonld(json.loads(raw)))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return objects


def _schema_types(obj):
    raw_type = obj.get("@type", [])
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    return {str(item).lower().split("/")[-1] for item in types if item}


def _parse_date(soup, jsonld_objects):
    for obj in jsonld_objects:
        if _schema_types(obj) & ARTICLE_TYPES:
            for key in ("datePublished", "dateCreated", "dateModified"):
                if obj.get(key):
                    return str(obj[key]).strip()[:80]

    for selector, attr in (
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="date"]', "content"),
        ('meta[name="pubdate"]', "content"),
        ('meta[name="publish_date"]', "content"),
        ('meta[itemprop="datePublished"]', "content"),
        ("time[datetime]", "datetime"),
    ):
        element = soup.select_one(selector)
        if element and element.get(attr):
            return element.get(attr).strip()[:80]
    return ""


def _extract_article(page_url, soup):
    jsonld = _jsonld_objects(soup)
    article_objects = [
        obj for obj in jsonld if _schema_types(obj) & ARTICLE_TYPES
    ]

    canonical_element = soup.select_one('link[rel="canonical"]')
    canonical_url = (
        urljoin(page_url, canonical_element.get("href"))
        if canonical_element and canonical_element.get("href")
        else page_url
    )

    title = ""
    description = ""

    for selector in (
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
        'meta[name="title"]',
    ):
        element = soup.select_one(selector)
        if element and element.get("content", "").strip():
            title = element["content"].strip()
            break

    if not title:
        for obj in article_objects:
            if obj.get("headline") or obj.get("name"):
                title = str(obj.get("headline") or obj.get("name")).strip()
                break

    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)

    for selector in (
        'meta[name="description"]',
        'meta[property="og:description"]',
        'meta[name="twitter:description"]',
    ):
        element = soup.select_one(selector)
        if element and element.get("content", "").strip():
            description = element["content"].strip()
            break

    if not description:
        for obj in article_objects:
            if obj.get("description"):
                description = str(obj["description"]).strip()
                break

    published = _parse_date(soup, jsonld)
    return {
        "url": _canonical(canonical_url),
        "title": re.sub(r"\s+", " ", title).strip()[:500],
        "published_at": published,
        "description": re.sub(r"\s+", " ", description).strip()[:2000],
    }


def _is_archive_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    parts = [part for part in path.split("/") if part]

    if MONTH_ARCHIVE_RE.match(path) or YEAR_ARCHIVE_RE.match(path):
        return True
    if len(parts) >= 2 and parts[-2].isdigit() and len(parts[-2]) == 4:
        if parts[-1].isdigit() and 1 <= int(parts[-1]) <= 12:
            return True
    if any(part in {"category", "categories", "tag", "tags", "author", "page", "archive", "archives"} for part in parts):
        return True
    return False


def _looks_like_content_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    if not path or path == "/" or _is_archive_url(url):
        return False

    parts = [part for part in path.split("/") if part]
    if any(part in EXCLUDED_PATH_PARTS for part in parts):
        return False

    if any(marker in path + "/" for marker in CONTENT_HINTS):
        if len(parts) <= 1:
            return False
        if parts[-1] in {"blog", "blogs", "news", "resources", "insights", "articles", "posts"}:
            return False
        return True
    return len(parts) >= 2


def _sitemap_urls(base_url, robots_text=None):
    parsed = urlparse(base_url)
    base_host = parsed.hostname or ""
    candidates = [
        urljoin(base_url, "/sitemap.xml"),
        urljoin(base_url, "/sitemap_index.xml"),
    ]
    urls = []
    seen_sitemaps = set()

    if robots_text is None:
        try:
            robots = _get(urljoin(base_url, "/robots.txt"))
            if robots.status_code < 400:
                robots_text = robots.text
        except Exception:
            robots_text = ""
    for line in (robots_text or "").splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_url = line.split(":", 1)[1].strip()
            if sitemap_url:
                candidates.insert(0, sitemap_url)

    for sitemap_url in candidates:
        sitemap_url = _canonical(sitemap_url)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        
        # OPTIMIZATION: Only check a maximum of 2 sitemaps instead of 6
        if len(seen_sitemaps) > 2:
            break

        try:
            response = _get(sitemap_url)
            if response.status_code >= 400:
                continue
            root = ElementTree.fromstring(response.text[:MAX_RESPONSE_BYTES])
        except Exception:
            continue

        locs = [
            element.text.strip()
            for element in root.iter()
            if element.tag.lower().endswith("loc") and element.text
        ]

        child_sitemaps = [
            loc for loc in locs
            if loc.lower().split("?", 1)[0].endswith(".xml")
            and _same_domain(loc, base_host)
        ]
        page_urls = [
            loc for loc in locs
            if not loc.lower().split("?", 1)[0].endswith(".xml")
            and _same_domain(loc, base_host)
            and _looks_like_content_url(loc) # OPTIMIZATION: Filter early
        ]

        # OPTIMIZATION: Only check a maximum of 2 child sitemaps instead of 4
        for child_url in child_sitemaps[:2]:
            try:
                child_response = _get(child_url)
                if child_response.status_code >= 400:
                    continue
                child_root = ElementTree.fromstring(
                    child_response.text[:MAX_RESPONSE_BYTES]
                )
                for element in child_root.iter():
                    if element.tag.lower().endswith("loc") and element.text:
                        child_loc = element.text.strip()
                        if _same_domain(child_loc, base_host) and _looks_like_content_url(child_loc):
                            page_urls.append(child_loc)
            except Exception:
                continue

        for page_url in page_urls:
            if _same_domain(page_url, base_host):
                urls.append(page_url)
        
        # OPTIMIZATION: If we already have enough URLs, stop crawling sitemaps
        if len(urls) >= 20:
            return list(dict.fromkeys(urls))

    return list(dict.fromkeys(urls))


def _robots_policy(url):
    robots_url = urljoin(url, "/robots.txt")
    try:
        response = _get(robots_url)
    except (requests.RequestException, ValueError) as exc:
        return None, "", "FAILED", (
            "Could not verify robots.txt: " + str(exc)[:120]
        )

    if response.status_code in (401, 403, 429) or response.status_code >= 500:
        return None, "", "BLOCKED", (
            f"Could not access robots.txt (HTTP {response.status_code}); "
            "the scan stopped to respect the site's crawl restrictions."
        )

    robots_text = response.text if response.status_code < 400 else ""
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(robots_text.splitlines())
    return parser, robots_text, None, None


def _is_probable_article(data, soup, source_url):
    if not data.get("title") or _is_archive_url(data.get("url", source_url)):
        return False

    title_lower = data.get("title", "").lower()
    if title_lower.endswith(" archives") or title_lower.endswith(" archive"):
        return False
    if " archives - " in title_lower or " archive - " in title_lower:
        return False

    jsonld = _jsonld_objects(soup)
    has_article_schema = any(_schema_types(obj) & ARTICLE_TYPES for obj in jsonld)
    has_article_element = soup.find("article") is not None
    has_published_date = bool(data.get("published_at"))
    path = urlparse(data.get("url", source_url)).path.lower()

    has_content_path = any(hint in path + "/" for hint in CONTENT_HINTS)
    has_specific_slug = len([part for part in path.split("/") if part]) >= 2

    return (
        has_article_schema
        or has_article_element
        or (has_published_date and (has_content_path or has_specific_slug))
        or (has_content_path and has_specific_slug and bool(data.get("description")))
    )


def scan_competitor(base_url):
    result = {
        "status": "FAILED",
        "message": "Scan did not complete.",
        "pages_checked": 0,
        "articles": [],
    }

    try:
        base_url = _canonical(base_url)
        robots, robots_text, robots_status, robots_message = _robots_policy(base_url)
        if robots_status:
            return {
                **result,
                "status": robots_status,
                "message": robots_message,
            }
        if not robots.can_fetch(USER_AGENT, base_url):
            return {
                **result,
                "status": "BLOCKED",
                "message": "robots.txt disallows crawling the website homepage.",
            }

        response = _get(base_url)

        if response.status_code in (401, 403, 429):
            return {
                **result,
                "status": "BLOCKED",
                "message": (
                    f"Website returned HTTP {response.status_code}. "
                    "Respect the site's access restrictions."
                ),
            }

        if response.status_code >= 400:
            return {
                **result,
                "message": f"Homepage returned HTTP {response.status_code}.",
            }

        if "text/html" not in response.headers.get("content-type", "text/html").lower():
            return {
                **result,
                "message": "Homepage did not return HTML.",
            }

        base_host = urlparse(response.url).hostname or ""
        homepage = BeautifulSoup(response.text, "html.parser")
        result["pages_checked"] = 1

        candidates = []
        for anchor in homepage.select("a[href]"):
            href = anchor.get("href", "").strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue

            full_url = _canonical(urljoin(response.url, href))
            if (
                _same_domain(full_url, base_host)
                and _looks_like_content_url(full_url)
            ):
                candidates.append(full_url)

        for sitemap_url in _sitemap_urls(response.url, robots_text=robots_text):
            normalized = _canonical(sitemap_url)
            if (
                _same_domain(normalized, base_host)
                and _looks_like_content_url(normalized)
            ):
                candidates.append(normalized)

        candidates = list(dict.fromkeys(candidates))
        candidates.sort(
            key=lambda url: 0 if any(
                hint in urlparse(url).path.lower() + "/" for hint in CONTENT_HINTS
            ) else 1
        )
        robots_by_origin = {
            (urlparse(base_url).scheme, urlparse(base_url).netloc.lower()): robots
        }
        permitted_candidates = []
        disallowed_candidates = 0
        for url in candidates:
            parsed_url = urlparse(url)
            origin = (parsed_url.scheme, parsed_url.netloc.lower())
            candidate_robots = robots_by_origin.get(origin)
            if candidate_robots is None:
                candidate_robots, _, policy_status, _ = _robots_policy(url)
                if policy_status:
                    disallowed_candidates += 1
                    continue
                robots_by_origin[origin] = candidate_robots
            if candidate_robots.can_fetch(USER_AGENT, url):
                permitted_candidates.append(url)
            else:
                disallowed_candidates += 1
                
        candidates = permitted_candidates[: MAX_PAGES - 1]
        
        # OPTIMIZATION: Fetch article pages concurrently
        def fetch_and_extract(url):
            try:
                page_response = _get(url)
                if page_response.status_code in (401, 403, 429):
                    return ("blocked", None, True)
                if (
                    page_response.status_code >= 400
                    or "text/html" not in page_response.headers.get("content-type", "text/html").lower()
                ):
                    return ("failed", None, True)

                soup = BeautifulSoup(page_response.text, "html.parser")
                data = _extract_article(page_response.url, soup)

                if _is_probable_article(data, soup, url):
                    return ("success", data, True)
                return ("failed", None, True)
            except Exception:
                return ("failed", None, False)

        articles = []
        blocked_pages = 0
        failed_pages = 0
        checked_count = 1

        # Use 4 threads to fetch the pages in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_url = {executor.submit(fetch_and_extract, url): url for url in candidates}
            for future in concurrent.futures.as_completed(future_to_url):
                status, data, was_checked = future.result()
                if was_checked:
                    checked_count += 1
                
                if status == "blocked":
                    blocked_pages += 1
                elif status == "failed":
                    failed_pages += 1
                elif status == "success":
                    articles.append(data)

        result["pages_checked"] = checked_count

        unique_articles = []
        seen_urls = set()
        for article in articles:
            article_url = article.get("url")
            if article_url and article_url not in seen_urls:
                seen_urls.add(article_url)
                unique_articles.append(article)

        result["articles"] = unique_articles

        if unique_articles:
            result["status"] = "SUCCESS"
            result["message"] = (
                f"Found {len(unique_articles)} article(s) after checking "
                f"{result['pages_checked']} page(s)."
            )
        elif not candidates and disallowed_candidates:
            result["status"] = "BLOCKED"
            result["message"] = (
                "Article URLs were discovered, but robots.txt disallows crawling "
                "them. Respect the site's access restrictions."
            )
        elif not candidates:
            result["status"] = "NO_BLOG_FOUND"
            result["message"] = (
                "No likely individual article URLs were found on the homepage "
                "or in accessible sitemaps. The site may use JavaScript, block "
                "crawlers, or use an unrecognized URL pattern."
            )
        elif blocked_pages == len(candidates):
            result["status"] = "BLOCKED"
            result["message"] = (
                "Article pages were discovered, but access was blocked or "
                "unavailable. Respect the site's access restrictions."
            )
        else:
            result["status"] = "PARTIAL"
            result["message"] = (
                f"Discovered {len(candidates)} candidate URL(s), but could not "
                "confidently extract individual article metadata. Archive pages "
                "and category listings are filtered out."
            )

        return result

    except requests.Timeout:
        return {
            **result,
            "message": "Request timed out. Try again later.",
        }
    except requests.RequestException as exc:
        return {
            **result,
            "message": f"Network error: {str(exc)[:180]}",
        }
    except ValueError as exc:
        return {
            **result,
            "message": str(exc),
        }
    except Exception as exc:
        return {
            **result,
            "message": f"Unexpected scraper error: {str(exc)[:180]}",
        }