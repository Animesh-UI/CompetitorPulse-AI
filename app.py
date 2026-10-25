import os
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

from database import (
    init_db, list_competitors, add_competitor, get_competitor,
    get_articles, get_events, get_pricing, get_scans, save_scan,
    store_articles, store_pricing_snapshot, create_event, get_dashboard_stats,
    update_article_keyword,
)
from scraper import scan_competitor
from pricing import extract_pricing
from keyword_service import enrich_keyword
from notifier import send_slack_alert

load_dotenv()
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

init_db()

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "CompetitorPulse AI"})

@app.get("/api/dashboard")
def dashboard():
    return jsonify({
        "stats": get_dashboard_stats(),
        "competitors": list_competitors(),
        "recent_events": get_events(limit=12),
    })

@app.get("/api/competitors")
def competitors():
    return jsonify(list_competitors())

@app.post("/api/competitors")
def create_competitor():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()
    if not name or not url:
        return jsonify({"error": "Name and website URL are required."}), 400
    try:
        item = add_competitor(name, url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        return jsonify({"error": "Could not save competitor. It may already exist."}), 409
    return jsonify(item), 201

@app.delete("/api/competitors/<int:competitor_id>")
def delete_competitor(competitor_id):
    from database import delete_competitor_record
    ok = delete_competitor_record(competitor_id)
    if not ok:
        return jsonify({"error": "Competitor not found."}), 404
    return jsonify({"ok": True})

@app.post("/api/competitors/<int:competitor_id>/scan")
def scan(competitor_id):
    competitor = get_competitor(competitor_id)
    if not competitor:
        return jsonify({"error": "Competitor not found."}), 404

    started = datetime.now(timezone.utc).isoformat()
    result = scan_competitor(competitor["url"])
    scan_id = save_scan(
        competitor_id=competitor_id,
        status=result["status"],
        pages_checked=result.get("pages_checked", 0),
        articles_found=len(result.get("articles", [])),
        message=result.get("message", ""),
        started_at=started,
    )

    new_articles = []
    keyword_results = []
    stored_articles = {
        article["url"]: article
        for article in get_articles(competitor_id, limit=100)
    }
    is_baseline = not stored_articles and bool(result.get("articles"))
    for article in result.get("articles", []):
        is_new = store_articles(competitor_id, article)
        stored_article = stored_articles.get(article["url"], {})
        previous_volume_error = stored_article.get("volume_error") or ""
        should_enrich = (
            is_new
            or stored_article.get("volume_status") is None
            or (
                stored_article.get("volume_status") == "API_ERROR"
                and previous_volume_error == "SeoData returned HTTP 404."
            )
        )
        if should_enrich:
            keyword = enrich_keyword(article)

            article["keyword"] = keyword.get("keyword")
            article["volume_keyword"] = keyword.get("volume_keyword")
            article["trends_keyword"] = keyword.get("trends_keyword")
            article["search_volume"] = keyword.get("search_volume")
            article["trends_interest"] = keyword.get("trends_interest")
            article["trends_series"] = keyword.get("trends_series", [])
            article["volume_series"] = keyword.get("volume_series", [])
            article["keyword_source"] = keyword.get("source")
            article["keyword_status"] = keyword.get("status")
            article["keyword_error"] = keyword.get("error")
            article["trends_status"] = keyword.get("trends_status")
            article["trends_error"] = keyword.get("trends_error")
            article["volume_source"] = keyword.get("volume_source")
            article["volume_status"] = keyword.get("volume_status")
            article["volume_error"] = keyword.get("volume_error")
            article["cpc"] = keyword.get("cpc")
            article["competition"] = keyword.get("competition")
            article["country"] = keyword.get("country")
            article["provider_cached"] = keyword.get("provider_cached")
            article["similar_keywords"] = keyword.get("similar_keywords", [])

            update_article_keyword(competitor_id, article["url"], keyword)
            stored_articles[article["url"]] = {
                **stored_article,
                "volume_status": keyword.get("volume_status"),
            }
            keyword_results.append({
                "url": article["url"],
                "keyword": keyword.get("volume_keyword") or keyword.get("keyword"),
                "search_volume": keyword.get("search_volume"),
                "cpc": keyword.get("cpc"),
                "competition": keyword.get("competition"),
                "volume_status": keyword.get("volume_status"),
                "volume_error": keyword.get("volume_error"),
                "trends_interest": keyword.get("trends_interest"),
                "trends_status": keyword.get("trends_status"),
            })

        if is_new and not is_baseline:
            event_id = create_event(
                competitor_id, "new_article",
                f"New article detected: {article.get('title') or article['url']}",
                article.get("url"), article.get("title"), keyword
            )
            if event_id:
                new_articles.append(article)
            
    pricing_result = extract_pricing(competitor["url"])
    pricing_changes = []
    if pricing_result.get("status") == "SUCCESS":
        for plan in pricing_result.get("plans", []):
            changed = store_pricing_snapshot(competitor_id, plan)
            if changed:
                pricing_changes.append(plan)
                create_event(
                    competitor_id, "pricing_change",
                    f"Pricing changed: {plan.get('plan_name')} — {plan.get('price_text')}",
                    plan.get("source_url"), plan.get("plan_name"), {}
                )

    alert_result = {"sent": False, "reason": "No new content or pricing changes."}
    if new_articles or pricing_changes:
        alert_result = send_slack_alert(competitor, new_articles, pricing_changes)

    return jsonify({
        "scan_id": scan_id,
        "status": result["status"],
        "message": result.get("message", ""),
        "pages_checked": result.get("pages_checked", 0),
        "articles_found": len(result.get("articles", [])),
        "keyword_results": keyword_results,
        "new_articles": new_articles,
        "pricing_changes": pricing_changes,
        "alert": alert_result,
    })

@app.post("/api/competitors/<int:competitor_id>/trends/refresh")
def refresh_trends(competitor_id):
    competitor = get_competitor(competitor_id)
    if not competitor:
        return jsonify({"error": "Competitor not found."}), 404

    articles_to_refresh = get_articles(competitor_id, limit=12)
    results = []
    for article in articles_to_refresh:
        keyword_result = enrich_keyword(article)
        update_article_keyword(competitor_id, article["url"], keyword_result)
        results.append({
            "url": article["url"],
            "keyword": keyword_result.get("keyword"),
            "volume_keyword": keyword_result.get("volume_keyword"),
            "trends_keyword": keyword_result.get("trends_keyword"),
            "status": keyword_result.get("status"),
            "volume_status": keyword_result.get("volume_status"),
            "trends_status": keyword_result.get("trends_status"),
            "source": keyword_result.get("source"),
            "error": keyword_result.get("error"),
            "search_volume": keyword_result.get("search_volume"),
            "cpc": keyword_result.get("cpc"),
            "competition": keyword_result.get("competition"),
        })
        if keyword_result.get("volume_status") == "RATE_LIMITED":
            break

    return jsonify({
        "competitor": competitor["name"],
        "refreshed": len(results),
        "results": results,
    })

@app.get("/api/competitors/<int:competitor_id>/articles")
def articles(competitor_id):
    if not get_competitor(competitor_id):
        return jsonify({"error": "Competitor not found."}), 404
    return jsonify(get_articles(competitor_id))

@app.get("/api/competitors/<int:competitor_id>/pricing")
def pricing(competitor_id):
    if not get_competitor(competitor_id):
        return jsonify({"error": "Competitor not found."}), 404
    return jsonify(get_pricing(competitor_id))

@app.get("/api/competitors/<int:competitor_id>/scans")
def scans(competitor_id):
    if not get_competitor(competitor_id):
        return jsonify({"error": "Competitor not found."}), 404
    return jsonify(get_scans(competitor_id))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")