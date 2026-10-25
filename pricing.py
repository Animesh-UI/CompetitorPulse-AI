import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Reuse the scraper's public-URL and redirect-safe fetch helpers.
from scraper import _get, _safe_public_url


USER_AGENT = (
    "CompetitorPulseBot/1.1 "
    "(+public-page monitoring; respectful crawler)"
)
TIMEOUT = 10
MAX_PAGE_BYTES = 1_500_000
MAX_PRICING_PAGES = 8
MAX_PLANS = 20

# Avoid interpreting years and long IDs as prices. Supports common currencies
# and monthly/yearly/per-user billing labels.
PRICE_RE = re.compile(
    r"(?P<currency>US\$|CA\$|AU\$|NZ\$|₹|€|£|\$|USD|CAD|AUD|INR|EUR|GBP)?\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<period>/\s*(?:month|mo|year|yr|user|seat)|"
    r"per\s+(?:month|year|user|seat)|"
    r"(?:USD|CAD|AUD|INR|EUR|GBP)\s*(?:/|per)\s*(?:month|year|user|seat))?",
    re.IGNORECASE,
)

PRICE_HINTS = ("pricing", "plans", "price", "packages", "cost", "billing")
CURRENCY_NORMALIZATION = {
    "₹": "INR",
    "€": "EUR",
    "£": "GBP",
    "$": "USD",
    "US$": "USD",
    "CA$": "CAD",
    "AU$": "AUD",
    "NZ$": "NZD",
}


def _same_host(url, base_host):
    """Match the exact host or a true subdomain, not lookalike suffixes."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    base = (base_host or "").lower().rstrip(".")
    return bool(host and base and (host == base or host.endswith("." + base)))


def _normalize_currency(value):
    if not value:
        return None
    cleaned = value.strip().upper()
    return CURRENCY_NORMALIZATION.get(cleaned, cleaned)


def _candidate_pricing_urls(base_url):
    parsed = urlparse(base_url)
    base_host = parsed.hostname or ""
    candidates = []

    # If the user added a pricing page directly, inspect it first.
    if any(word in parsed.path.lower() for word in PRICE_HINTS):
        candidates.append(base_url)

    for path in ("/pricing", "/plans", "/pricing-plans", "/packages", "/m365-pricing.html"):
        candidates.append(urljoin(base_url, path))

    # Discover pricing links from the competitor's homepage.
    try:
        response = _get(base_url)
        if response.status_code < 400 and "text/html" in response.headers.get(
            "content-type", "text/html"
        ).lower():
            soup = BeautifulSoup(response.text[:MAX_PAGE_BYTES], "html.parser")
            for anchor in soup.select("a[href]"):
                href = anchor.get("href", "").strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue

                label = (
                    anchor.get_text(" ", strip=True)
                    + " "
                    + href
                    + " "
                    + (anchor.get("aria-label") or "")
                ).lower()

                if not any(hint in label for hint in PRICE_HINTS):
                    continue

                target = urljoin(response.url, href)
                if (
                    _safe_public_url(target)
                    and _same_host(target, base_host)
                ):
                    candidates.insert(0, target)
    except Exception:
        # Fixed conventional paths are still tried if homepage discovery fails.
        pass

    result = []
    seen = set()
    for url in candidates:
        if not _safe_public_url(url):
            continue
        parsed_url = urlparse(url)
        normalized = parsed_url._replace(fragment="").geturl()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)

    return result[:MAX_PRICING_PAGES]


def _price_blocks(soup):
    """Return focused pricing sections/cards where possible."""
    selectors = (
        '[itemtype*="Product"]',
        '[class*="pricing-card"]',
        '[class*="price-card"]',
        '[class*="pricing-plan"]',
        '[class*="plan-card"]',
        '[class*="pricing"]',
        '[class*="plan"]',
        '[class*="tier"]',
        '[id*="pricing"]',
        '[id*="plan"]',
        "section",
        "article",
    )

    blocks = []
    seen_ids = set()
    for selector in selectors:
        for block in soup.select(selector):
            text = " ".join(block.get_text(" ", strip=True).split())
            if not text or len(text) > 5000:
                continue
            # Avoid processing the same DOM node multiple times.
            node_id = id(block)
            if node_id not in seen_ids:
                seen_ids.add(node_id)
                blocks.append(block)

    # Prefer smaller/focused blocks; the whole page is a last resort.
    blocks.sort(key=lambda block: len(block.get_text(" ", strip=True)))
    return blocks[:150] or [soup]


def _extract_plan(block, page_url):
    text = " ".join(block.get_text(" ", strip=True).split())
    if not text:
        return None

    match = PRICE_RE.search(text)
    if not match:
        return None

    amount_text = match.group("amount")
    try:
        amount = float(amount_text.replace(",", ""))
    except (TypeError, ValueError):
        return None

    # Ignore likely years or tiny fragments of long numeric IDs.
    if amount.is_integer() and 1900 <= amount <= 2100:
        return None

    currency_text = match.group("currency") or ""
    currency = _normalize_currency(currency_text)
    period_text = (match.group("period") or "").lower()

    if "year" in period_text or "yr" in period_text:
        billing_period = "yearly"
    elif "month" in period_text or re.search(r"\bmo\b", period_text):
        billing_period = "monthly"
    elif "user" in period_text:
        billing_period = "per-user"
    elif "seat" in period_text:
        billing_period = "per-seat"
    else:
        billing_period = None

    heading = block.find(["h1", "h2", "h3", "h4", "h5"])
    plan_name = heading.get_text(" ", strip=True) if heading else ""

    # Prefer an explicit price label when one exists, e.g. "$19 / month".
    price_element = block.select_one(
        '[class*="price"], [class*="amount"], [itemprop="price"]'
    )
    if price_element:
        candidate_price_text = " ".join(
            price_element.get_text(" ", strip=True).split()
        )
        if candidate_price_text and len(candidate_price_text) <= 120:
            price_text = candidate_price_text
        else:
            price_text = match.group(0).strip()
    else:
        price_text = match.group(0).strip()

    if not plan_name:
        # Use a short label from the block rather than a generic page title.
        plan_name = "Pricing option"

    return {
        "plan_name": plan_name[:120],
        "price_text": price_text[:120],
        "amount": amount,
        "currency": currency,
        "billing_period": billing_period,
        "source_url": page_url,
    }


def extract_pricing(base_url):
    """
    Discover public pricing pages and extract visible plan/price information.

    Return shape is kept compatible with the original implementation:
    {"status": ..., "source_url": ..., "plans": [...]}
    """
    try:
        if not _safe_public_url(base_url):
            return {
                "status": "FAILED",
                "message": "URL is not a publicly reachable HTTP(S) address.",
                "source_url": None,
                "plans": [],
            }

        candidates = _candidate_pricing_urls(base_url)
        all_plans = []
        successful_pages = []
        checked_pages = 0

        for url in candidates:
            try:
                if not _safe_public_url(url):
                    continue

                response = _get(url)
                checked_pages += 1

                if response.status_code in (401, 403, 429):
                    continue
                if response.status_code >= 400:
                    continue
                if "text/html" not in response.headers.get(
                    "content-type", "text/html"
                ).lower():
                    continue

                page_url = response.url
                soup = BeautifulSoup(response.text[:MAX_PAGE_BYTES], "html.parser")
                plans = []

                for block in _price_blocks(soup):
                    item = _extract_plan(block, page_url)
                    if item:
                        plans.append(item)

                # Deduplicate nested pricing cards by plan/price/currency.
                page_unique = {}
                for plan in plans:
                    key = (
                        plan["plan_name"].casefold(),
                        plan["price_text"].casefold(),
                        plan["currency"] or "",
                        plan["billing_period"] or "",
                    )
                    page_unique[key] = plan

                if page_unique:
                    successful_pages.append(page_url)
                    all_plans.extend(page_unique.values())

            except Exception:
                # One unavailable pricing page should not prevent trying others.
                continue

        unique = {}
        for plan in all_plans:
            key = (
                plan["plan_name"].casefold(),
                plan["price_text"].casefold(),
                plan["currency"] or "",
                plan["billing_period"] or "",
            )
            unique[key] = plan

        plans = list(unique.values())[:MAX_PLANS]
        if plans:
            return {
                "status": "SUCCESS",
                "source_url": successful_pages[0] if successful_pages else None,
                "plans": plans,
            }

        return {
            "status": "NO_PRICING_FOUND",
            "message": (
                f"Checked {checked_pages} candidate page(s), but no visible "
                "prices could be extracted. Prices may be loaded by JavaScript "
                "or may not be published on the site."
            ),
            "source_url": None,
            "plans": [],
        }

    except Exception as exc:
        return {
            "status": "FAILED",
            "message": f"Pricing scan failed: {str(exc)[:180]}",
            "source_url": None,
            "plans": [],
        }
