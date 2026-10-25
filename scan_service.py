"""The scan pipeline: discover -> diff against history -> extract -> enrich -> pricing -> alert.

Scans run in a background thread so HTTP requests return immediately; the UI polls
/api/scans/<id>. Only one scan per competitor runs at a time.

"Second run only flags what's new":
  * every discovered URL is remembered by a normalised key (http/https, www, trailing slash,
    tracking params ignored) - known URLs are never fetched or flagged again
  * the first scan is a *baseline*: posts are stored but not flagged as new
  * a URL first seen later is "new" unless its publish date is clearly old (guards against
    flaky discovery re-surfacing old posts as new)
"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import database as db
import keywords
import notifier
import pricing
import scraper
from http_client import Fetcher

log = logging.getLogger("scan")
_running = {}
_lock = threading.Lock()

SCAN_BUDGET = int(os.environ.get("SCAN_TIME_BUDGET", "150"))          # seconds
BASELINE_DETAILS = int(os.environ.get("BASELINE_DETAIL_LIMIT", "40"))  # pages fetched on first scan
MAX_NEW_DETAILS = int(os.environ.get("MAX_NEW_DETAILS", "60"))
BACKFILL_PER_SCAN = int(os.environ.get("BACKFILL_PER_SCAN", "15"))
BASELINE_ENRICH = int(os.environ.get("BASELINE_ENRICH", "10"))
ENRICH_CAP = int(os.environ.get("ENRICH_CAP", "25"))
NEW_MAX_AGE_DAYS = int(os.environ.get("NEW_POST_MAX_AGE_DAYS", "30"))
ALERT_ON_BASELINE = os.environ.get("ALERT_ON_BASELINE", "1") != "0"


def is_running(competitor_id):
    with _lock:
        return competitor_id in _running


def start_scan(competitor_id, trigger="manual"):
    """-> (scan_id, started). If a scan is already running, returns its id and started=False."""
    with _lock:
        if competitor_id in _running:
            return _running[competitor_id], False
        scan_id = db.create_scan(competitor_id, trigger)
        _running[competitor_id] = scan_id
    threading.Thread(target=_worker, args=(competitor_id, scan_id), daemon=True, name=f"scan-{scan_id}").start()
    return scan_id, True


def _worker(cid, scan_id):
    try:
        _execute(cid, scan_id)
    except Exception as exc:  # never leave a scan stuck in RUNNING
        log.exception("scan %s crashed", scan_id)
        db.finish_scan(scan_id, "FAILED", f"Unexpected error: {str(exc)[:200]}")
    finally:
        with _lock:
            _running.pop(cid, None)


def scan_all_async(trigger="auto"):
    def run():
        for comp in db.list_competitors():
            if not comp.get("auto_scan"):
                continue
            _, started = start_scan(comp["id"], trigger)
            while started and is_running(comp["id"]):
                time.sleep(2)
    threading.Thread(target=run, daemon=True, name="scan-all").start()


def start_scheduler():
    try:
        hours = float(os.environ.get("AUTO_SCAN_HOURS", "0") or 0)
    except ValueError:
        hours = 0
    if hours <= 0:
        return

    def loop():
        while True:
            time.sleep(hours * 3600)
            scan_all_async("auto")
    threading.Thread(target=loop, daemon=True, name="scheduler").start()


# ----------------------------------------------------------------------------
def _too_old(published, lastmod, cutoff_days):
    d = published or lastmod
    if not d:
        return False
    try:
        return (date.today() - date.fromisoformat(d)).days > cutoff_days
    except ValueError:
        return False


def _execute(cid, scan_id):
    comp = db.get_competitor(cid)
    if not comp:
        db.finish_scan(scan_id, "FAILED", "Competitor no longer exists.")
        return
    started = time.time()
    deadline_ok = lambda: time.time() - started < SCAN_BUDGET  # noqa: E731
    fetcher = Fetcher()
    baseline = db.count_articles(cid) == 0
    age = db.last_ok_scan_age_days(cid)
    cutoff_days = max(NEW_MAX_AGE_DAYS, (age or 0) + 7)

    disc = scraper.discover(fetcher, comp["url"], comp.get("seed_url"), deadline_ok)
    details = {"sources": disc.get("sources", {}), "baseline": baseline}

    if disc["status"] in ("BLOCKED", "FAILED"):
        db.finish_scan(scan_id, disc["status"], disc["message"], fetcher.requests_made, 0, 0, 0, baseline, details)
        db.create_event(cid, "scan_problem", f"{comp['name']}: {disc['message']}"[:300])
        return

    cands = disc["candidates"]
    known = db.known_keys(cid)
    unseen = [c for c in cands if c["key"] not in known]
    limit = BASELINE_DETAILS if baseline else MAX_NEW_DETAILS
    to_fetch, silent_rest = unseen[:limit], unseen[limit:]

    # ---- fetch article pages (only unseen URLs -> second run is cheap) -------------------
    with ThreadPoolExecutor(max_workers=6) as ex:
        outcomes = list(ex.map(lambda c: scraper.fetch_article(fetcher, c, deadline_ok), to_fetch))

    new_articles, errors, blocked_pages = [], 0, 0
    for cand, (outcome, art) in zip(to_fetch, outcomes):
        status = "full"
        if outcome == "skip":
            db.add_seen(cid, cand["key"], None, "ignored")
            continue
        if outcome != "ok":
            errors += 1
            blocked_pages += outcome == "blocked"
            if cand.get("title") and (cand.get("trusted") or cand.get("published_at")):
                art, status = scraper.article_from_hint(cand), "hint"
            else:
                continue  # retry on next scan; not remembered
        art_key = scraper.url_key(art["url"])
        is_new = (not baseline) and not _too_old(art.get("published_at"), cand.get("lastmod"), cutoff_days)
        art_id, created = db.upsert_article(cid, art_key, art, scan_id, is_new, status, ",".join(cand["sources"]))
        db.add_seen(cid, cand["key"], art_id)
        db.add_seen(cid, art_key, art_id)
        if created and is_new:
            art["id"] = art_id
            new_articles.append(art)

    for cand in silent_rest:  # too many at once: remember them silently, backfill details later
        art = scraper.article_from_hint(cand)
        key = scraper.url_key(art["url"])
        art_id, _ = db.upsert_article(cid, key, art, scan_id, False, "pending", ",".join(cand["sources"]))
        db.add_seen(cid, cand["key"], art_id)

    # ---- backfill details of previously stored "pending" posts (never flagged new) -------
    pending = db.get_pending_articles(cid, BACKFILL_PER_SCAN) if deadline_ok() else []
    if pending:
        with ThreadPoolExecutor(max_workers=6) as ex:
            res = list(ex.map(lambda p: scraper.fetch_article(fetcher, {"url": p["url"], "title": p["title"],
                              "published_at": p["published_at"], "description": p["description"]}, deadline_ok), pending))
        for p, (outcome, art) in zip(pending, res):
            if outcome == "ok":
                db.update_article_details(p["id"], art)

    # ---- keyword enrichment ---------------------------------------------------------------
    baseline_sample = []
    to_enrich = new_articles[:ENRICH_CAP]
    if baseline:
        baseline_sample = db.get_recent_articles(cid, BASELINE_ENRICH)
        to_enrich = baseline_sample
    if to_enrich and deadline_ok():
        results = keywords.enrich(to_enrich)
        for art, kw in zip(to_enrich, results):
            db.update_article_keyword(art["id"], kw)
            art.update({"keyword": kw["keyword"], "search_volume": kw["search_volume"],
                        "keyword_source": kw["source"], "keyword_status": kw["status"],
                        "demand_signal": kw["demand_signal"]})
    for art in new_articles:
        db.create_event(cid, "new_article", f"New post: {art.get('title') or art['url']}", art["url"],
                        art.get("title"), {"keyword": art.get("keyword"), "search_volume": art.get("search_volume"),
                                           "status": art.get("keyword_status")})

    # ---- pricing --------------------------------------------------------------------------
    pricing_changes, pricing_info = [], {"status": "SKIPPED"}
    if deadline_ok():
        try:
            pr = pricing.extract_pricing(fetcher, comp["url"], disc.get("homepage_links"), deadline_ok)
            pricing_info = {"status": pr["status"], "source_url": pr.get("source_url"), "plans": len(pr["plans"])}
            if pr["status"] == "SUCCESS":
                pricing_changes = db.sync_pricing(cid, pr["plans"])
        except Exception:
            log.exception("pricing step failed")
            pricing_info = {"status": "ERROR"}
    for ch in pricing_changes:
        db.create_event(cid, "pricing_change", f"Pricing: {notifier._change_text(ch)}", ch.get("source_url"), ch["plan_name"], ch)
    details["pricing"] = pricing_info

    # ---- status ---------------------------------------------------------------------------
    total = db.count_articles(cid)
    if disc["status"] == "NO_BLOG_FOUND" and total == 0:
        status, msg = "NO_BLOG_FOUND", disc["message"]
    elif total == 0:
        status = "PARTIAL"
        msg = ("Pages were discovered but none could be confirmed as articles (JavaScript-rendered or "
               "non-standard markup).")
    elif blocked_pages and blocked_pages >= max(3, len(to_fetch) // 2):
        status = "PARTIAL"
        msg = f"The site blocked {blocked_pages} article requests; results may be incomplete."
    elif errors > max(5, len(to_fetch) // 2):
        status = "PARTIAL"
        msg = f"{errors} pages could not be fetched; results may be incomplete."
    else:
        status = "SUCCESS"
        src = ", ".join(f"{k}: {v}" for k, v in disc.get("sources", {}).items())
        if baseline:
            msg = f"Baseline captured: {total} posts tracked ({src})."
        elif new_articles:
            msg = f"{len(new_articles)} new post(s) detected out of {len(cands)} discovered ({src})."
        else:
            msg = f"No new posts. {len(cands)} posts discovered, all already known ({src})."
    if pricing_changes:
        msg += f" {len(pricing_changes)} pricing change(s)."
    if baseline:
        details["baseline_total"] = total

    # ---- alert ----------------------------------------------------------------------------
    alert = {"sent": False, "reason": "Nothing new to report."}
    comp_fresh = db.get_competitor(cid)
    if new_articles or pricing_changes:
        alert = notifier.send_alert(comp_fresh, new_articles, pricing_changes, False,
                                    db.count_recent_posts(cid, 7))
    elif baseline and total and ALERT_ON_BASELINE:
        alert = notifier.send_alert(comp_fresh, baseline_sample[:5], [], True, None, total)
    if alert.get("headline"):
        db.create_event(cid, "alert", f"Alert: {alert['headline']}" + ("" if alert["sent"] else " (not delivered)"),
                        None, None, {k: alert[k] for k in ("slack", "email") if k in alert})

    db.finish_scan(scan_id, status, msg, fetcher.requests_made, len(cands), len(new_articles), len(pricing_changes),
                   baseline, details, alert)
