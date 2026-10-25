"""Polite, SSRF-safe HTTP client used by every crawler module.

* Only public http(s) hosts are fetched (private / loopback / link-local blocked).
* Redirects are followed manually so every hop is re-validated.
* robots.txt is honoured for page fetches.
* Responses are size-capped.
* Bot walls (403 / 429 / Cloudflare-style challenge pages) are detected so the
  caller can report BLOCKED instead of silently returning "no blog".
"""
import ipaddress
import os
import socket
import threading
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 CompetitorPulse/2.0",
)
ROBOTS_TOKEN = "CompetitorPulse"
DEFAULT_TIMEOUT = float(os.environ.get("SCRAPER_TIMEOUT", "12"))
MAX_BYTES = int(os.environ.get("SCRAPER_MAX_BYTES", str(3_000_000)))
MAX_REDIRECTS = 5
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}

BLOCK_MARKERS = (
    "just a moment...", "cf-chl", "attention required! | cloudflare", "captcha-delivery",
    "px-captcha", "are you a robot", "verify you are human", "verifying you are human",
    "access denied", "request blocked", "enable javascript and cookies to continue",
    "bot detection", "pardon our interruption", "unusual traffic from your computer",
)

_dns_cache = {}
_dns_lock = threading.Lock()


class FetchError(Exception):
    """kind: unsafe | dns | timeout | network | robots"""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def public_url_problem(url):
    """Return None when the URL is a public http(s) address, else a human message."""
    try:
        p = urlparse(url)
    except Exception:
        return "That is not a valid URL."
    if p.scheme not in ("http", "https") or not p.hostname:
        return "Only http(s) URLs are supported."
    if p.port not in ALLOWED_PORTS:
        return "Unusual ports are not allowed."
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return "Local addresses are not allowed."
    now = time.time()
    with _dns_lock:
        cached = _dns_cache.get(host)
    if cached and now - cached[0] < 300:
        return cached[1]
    problem = None
    try:
        infos = socket.getaddrinfo(host, None)
        if not infos:
            problem = "Could not resolve this domain."
        for item in infos:
            if not ipaddress.ip_address(item[4][0]).is_global:
                problem = "Private or internal addresses are not allowed."
                break
    except socket.gaierror:
        problem = "Could not resolve this domain. Check the spelling."
    except Exception:
        problem = "Could not validate this address."
    with _dns_lock:
        _dns_cache[host] = (now, problem)
    return problem


def is_public_url(url):
    return public_url_problem(url) is None


class Page:
    def __init__(self, url, status, headers, content):
        self.url = url
        self.status = status
        self.headers = headers
        self.content = content
        self._text = None

    @property
    def content_type(self):
        return (self.headers.get("content-type") or "").lower()

    @property
    def is_html(self):
        ct = self.content_type
        return ct == "" or "html" in ct or "xhtml" in ct

    @property
    def text(self):
        if self._text is None:
            enc = "utf-8"
            ct = self.headers.get("content-type") or ""
            if "charset=" in ct.lower():
                enc = ct.lower().split("charset=")[1].split(";")[0].strip().strip("\"'") or "utf-8"
            try:
                self._text = self.content.decode(enc, errors="replace")
            except LookupError:
                self._text = self.content.decode("utf-8", errors="replace")
        return self._text


def looks_blocked(page):
    """True when the response is a bot wall rather than real content."""
    if page.status in (401, 403, 429):
        return True
    if (page.headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    if page.status in (200, 202, 503) and len(page.content) < 40_000 and page.is_html:
        head = page.text[:40_000].lower()
        if any(m in head for m in BLOCK_MARKERS):
            return True
    return False


class Fetcher:
    def __init__(self, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_BYTES):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })
        self.requests_made = 0
        self._robots = {}
        self._lock = threading.Lock()

    # -- robots.txt -----------------------------------------------------
    def _robots_for(self, url):
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if origin in self._robots:
                return self._robots[origin]
        parser, sitemaps = None, []
        try:
            page = self.get(origin + "/robots.txt", retries=0)
            if page.status == 200 and "html" not in page.content_type:
                parser = RobotFileParser()
                parser.parse(page.text.splitlines())
                parser.modified()  # without this can_fetch() always returns False
                sitemaps = parser.site_maps() or []
        except FetchError:
            pass
        with self._lock:
            self._robots[origin] = (parser, sitemaps)
        return parser, sitemaps

    def can_fetch(self, url):
        parser, _ = self._robots_for(url)
        return True if parser is None else parser.can_fetch(ROBOTS_TOKEN, url)

    def robots_sitemaps(self, origin):
        return list(self._robots_for(origin)[1])

    # -- fetching -------------------------------------------------------
    def get(self, url, check_robots=False, retries=1, accept=None):
        if check_robots and not self.can_fetch(url):
            raise FetchError("robots", "Disallowed by robots.txt")
        headers = {"Accept": accept} if accept else None
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            problem = public_url_problem(current)
            if problem:
                raise FetchError("dns" if "resolve" in problem else "unsafe", problem)
            resp = self._request(current, headers, retries)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                nxt = urljoin(current, resp.headers["Location"])
                resp.close()
                current = nxt
                continue
            return self._to_page(resp, current)
        raise FetchError("network", "Too many redirects.")

    def _request(self, url, headers, retries):
        last_exc = None
        for attempt in range(retries + 1):
            self.requests_made += 1
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=False,
                                        stream=True, headers=headers)
            except requests.Timeout as exc:
                last_exc = FetchError("timeout", "The request timed out.")
                if attempt < retries:
                    time.sleep(0.8)
                    continue
                raise last_exc from exc
            except requests.RequestException as exc:
                last_exc = FetchError("network", f"Network error: {str(exc)[:150]}")
                if attempt < retries:
                    time.sleep(0.8)
                    continue
                raise last_exc from exc
            if resp.status_code in (429, 502, 503, 504) and attempt < retries:
                wait = 1.5
                ra = resp.headers.get("Retry-After", "")
                if ra.isdigit():
                    wait = min(float(ra), 4.0)
                resp.close()
                time.sleep(wait)
                continue
            return resp
        raise last_exc or FetchError("network", "Request failed.")

    def _to_page(self, resp, url):
        try:
            raw = resp.raw.read(self.max_bytes, decode_content=True)
        except Exception:
            raw = b""
        finally:
            resp.close()
        return Page(url, resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, raw or b"")
