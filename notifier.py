import os
import requests

def send_slack_alert(competitor, new_articles, pricing_changes):
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return {"sent":False, "reason":"Slack webhook is not configured."}
    lines = [f"*CompetitorPulse AI — {competitor['name']}*"]
    if new_articles:
        lines.append(f"\n*New articles ({len(new_articles)}):*")
        for item in new_articles[:10]:
            title = item.get("title") or item.get("url")
            volume = item.get("search_volume")
            keyword = item.get("keyword") or "Unavailable"
            volume_text = str(volume) if volume is not None else "not available"
            lines.append(f"• <{item['url']}|{title}> — keyword: `{keyword}`, estimated monthly volume: `{volume_text}`")
    if pricing_changes:
        lines.append(f"\n*Pricing changes ({len(pricing_changes)}):*")
        for item in pricing_changes[:10]:
            lines.append(f"• {item.get('plan_name')}: `{item.get('price_text')}` — {item.get('source_url') or ''}")
    try:
        response = requests.post(webhook, json={"text":"\n".join(lines)}, timeout=10)
        if response.status_code >= 400:
            return {"sent":False, "reason":f"Slack returned HTTP {response.status_code}."}
        return {"sent":True, "reason":"Alert sent."}
    except Exception:
        return {"sent":False, "reason":"Slack request failed; check webhook URL and network."}
