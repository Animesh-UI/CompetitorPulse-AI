"""Target-keyword extraction + search-volume enrichment.

Volume providers (first configured one wins, next one is the fallback):
  * DataForSEO  - Google Ads search volume. Free $1 trial credit, no card needed.
                  One batched call (<=1000 keywords) costs ~ $0.09, so we send every
                  keyword of a scan in ONE request and cache results for 30 days.
                  Set DATAFORSEO_SANDBOX=1 to use their free sandbox (dummy numbers).
  * Keywords Everywhere - Google Keyword Planner data (paid credits).

Always-free signal (no key at all): Google Autocomplete. It never returns a volume,
so we do NOT pretend it does - we only report a demand signal (High / Medium / Low /
None) and related queries.

Failure handling: invalid key, no credit, rate limit and timeouts are reported as
explicit statuses on each article; the scan itself never fails because of the API.
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

import database as db

DFS_HOST = "sandbox.dataforseo.com" if os.environ.get("DATAFORSEO_SANDBOX") == "1" else "api.dataforseo.com"
DFS_VOLUME_URL = f"https://{DFS_HOST}/v3/keywords_data/google_ads/search_volume/live"
DFS_USER_URL = f"https://{DFS_HOST}/v3/appendix/user_data"
KE_URL = "https://api.keywordseverywhere.com/v1/get_keyword_data"
KE_CREDITS_URL = "https://api.keywordseverywhere.com/v1/credits"
AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"

STATUS_TEXT = {
    "SUCCESS": "Volume from provider",
    "NO_DATA": "Provider has no volume for this keyword",
    "API_NOT_CONFIGURED": "No keyword API key configured",
    "INVALID_API_KEY": "API key/credentials were rejected",
    "INSUFFICIENT_CREDITS": "API account is out of credits",
    "RATE_LIMITED": "API rate limit hit - try again later",
    "API_TIMEOUT": "API timed out",
    "API_ERROR": "API returned an error",
    "NO_KEYWORD": "Could not derive a keyword",
}


def _loc_code():
    return os.environ.get("DATAFORSEO_LOCATION_CODE", "2840")  # 2840 = United States


def _lang_code():
    return os.environ.get("DATAFORSEO_LANGUAGE_CODE", "en")


# ------------------------------------------------------------------ candidates
FILLER = {"ultimate", "complete", "definitive", "comprehensive", "essential", "full", "simple", "quick",
          "easy", "proven", "actionable", "practical"}
STOP = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "by", "is", "are", "was", "were",
        "your", "you", "our", "we", "my", "how", "what", "why", "when", "where", "which", "who", "do", "does",
        "can", "should", "will", "just", "that", "this", "these", "those", "it", "its", "at", "as", "from",
        "into", "about", "really", "need", "know", "everything", "things", "ways", "way", "tips"}
LEAD_PHRASES = [("a", "guide", "to"), ("the", "guide", "to"), ("guide", "to"), ("an", "introduction", "to"),
                ("introduction", "to"), ("everything", "you", "need", "to", "know", "about"),
                ("what", "you", "need", "to", "know", "about"), ("the",), ("a",), ("an",)]


def _words(s):
    return re.findall(r"[a-z0-9][a-z0-9\-]*", s.lower().replace("’", "").replace("'", ""))


def _strip_trailing_stop(tokens):
    while tokens and tokens[-1] in STOP:
        tokens = tokens[:-1]
    return tokens


def derive_candidates(article):
    """Ordered list (best first) of 1-4 candidate target keywords for an article."""
    out = []
    # 1. explicit <meta name=keywords> / JSON-LD keywords (first usable phrase)
    for t in (article.get("tags") or [])[:3]:
        w = _words(t)
        if 2 <= len(w) <= 6:
            out.append(" ".join(w))
            break

    # 2. natural phrase from the title's main clause
    title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", article.get("title") or "")
    parts = [p.strip() for p in re.split(r"\s+[|–—•·-]\s+|:\s+", title) if p.strip()]
    main = parts[0] if parts else title
    if len(_words(main)) < 2 and len(parts) > 1:
        main = parts[1]
    tokens = [t for t in _words(main) if not re.fullmatch(r"(19|20)\d{2}", t) and not t.isdigit()
              and t not in FILLER]
    for lead in LEAD_PHRASES:
        if tuple(tokens[:len(lead)]) == lead and len(tokens) > len(lead):
            tokens = tokens[len(lead):]
            break
    tokens = _strip_trailing_stop(tokens)
    natural = tokens[:7]
    if natural:
        out.append(" ".join(natural))

    # 3. compact head term (stop-words removed)
    compact = [t for t in natural if t not in STOP][:4]
    if compact:
        out.append(" ".join(compact))

    # 4. slug phrase
    slug = [s for s in urlparse(article.get("url") or "").path.split("/") if s]
    if slug:
        sw = [t for t in _words(re.sub(r"[-_]+", " ", slug[-1])) if not t.isdigit() and t not in STOP][:4]
        if sw:
            out.append(" ".join(sw))

    seen, final = set(), []
    for k in out:
        k = k.strip()
        if k and k not in seen and len(k) >= 3:
            seen.add(k)
            final.append(k)
    return final[:4]


# ----------------------------------------------------------------- autocomplete
def autocomplete(keyword):
    """Free Google Autocomplete. Returns list of suggestions ([] on any problem)."""
    if os.environ.get("KEYWORD_AUTOCOMPLETE", "1") == "0":
        return []
    try:
        r = requests.get(AUTOCOMPLETE_URL, params={"client": "firefox", "q": keyword, "hl": "en", "gl": "us"},
                         timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        data = json.loads(r.content.decode("utf-8", errors="replace"))
        return [s for s in data[1] if isinstance(s, str)][:10]
    except Exception:
        return []


def _demand(suggestions):
    n = len(suggestions)
    if n >= 8:
        return "High"
    if n >= 4:
        return "Medium"
    if n >= 1:
        return "Low"
    return "None"


# --------------------------------------------------------------------- providers
def _dfs_creds():
    login, pwd = os.environ.get("DATAFORSEO_LOGIN", "").strip(), os.environ.get("DATAFORSEO_PASSWORD", "").strip()
    return (login, pwd) if login and pwd else None


def _ke_key():
    return os.environ.get("KEYWORDS_EVERYWHERE_API_KEY", "").strip()


def configured_providers():
    order = os.environ.get("KEYWORD_PROVIDER", "auto").lower()
    avail = []
    if _dfs_creds() and order in ("auto", "dataforseo"):
        avail.append("DataForSEO")
    if _ke_key() and order in ("auto", "keywordseverywhere", "keywords_everywhere"):
        avail.append("Keywords Everywhere")
    return avail


def _dataforseo(keywords):
    """-> (status, {keyword: volume|None})"""
    creds = _dfs_creds()
    payload = [{"keywords": keywords[:1000], "location_code": int(_loc_code()), "language_code": _lang_code()}]
    try:
        r = requests.post(DFS_VOLUME_URL, auth=creds, json=payload, timeout=45)
    except requests.Timeout:
        return "API_TIMEOUT", {}
    except requests.RequestException:
        return "API_ERROR", {}
    if r.status_code in (401, 403):
        return "INVALID_API_KEY", {}
    if r.status_code == 402:
        return "INSUFFICIENT_CREDITS", {}
    if r.status_code == 429:
        return "RATE_LIMITED", {}
    try:
        data = r.json()
    except ValueError:
        return "API_ERROR", {}
    top = data.get("status_code") or 0
    tasks = data.get("tasks") or []
    task = tasks[0] if tasks else {}
    code = task.get("status_code") or top
    if code == 20000:
        vols = {}
        for item in task.get("result") or []:
            kw = (item.get("keyword") or "").lower()
            v = item.get("search_volume")
            vols[kw] = int(v) if isinstance(v, (int, float)) else None
        return "OK", vols
    if 40100 <= code < 40200:
        return "INVALID_API_KEY", {}
    if code in (40200, 40201):
        return "INSUFFICIENT_CREDITS", {}
    if code == 40202 or code == 50000 and "rate" in str(task.get("status_message", "")).lower():
        return "RATE_LIMITED", {}
    return "API_ERROR", {}


def _keywords_everywhere(keywords):
    form = [("country", os.environ.get("KEYWORDS_EVERYWHERE_COUNTRY", "us")),
            ("currency", os.environ.get("KEYWORDS_EVERYWHERE_CURRENCY", "USD")),
            ("dataSource", "gkp")] + [("kw[]", k) for k in keywords[:100]]
    try:
        r = requests.post(KE_URL, headers={"Accept": "application/json", "Authorization": f"Bearer {_ke_key()}"},
                          data=form, timeout=30)
    except requests.Timeout:
        return "API_TIMEOUT", {}
    except requests.RequestException:
        return "API_ERROR", {}
    if r.status_code in (401, 403):
        return "INVALID_API_KEY", {}
    if r.status_code == 402:
        return "INSUFFICIENT_CREDITS", {}
    if r.status_code == 429:
        return "RATE_LIMITED", {}
    if r.status_code >= 400:
        return "API_ERROR", {}
    try:
        rows = r.json().get("data") or []
    except ValueError:
        return "API_ERROR", {}
    vols = {}
    for row in rows:
        kw = (row.get("keyword") or "").lower()
        try:
            vols[kw] = int(row.get("vol")) if row.get("vol") is not None else None
        except (TypeError, ValueError):
            vols[kw] = None
    return "OK", vols


PROVIDER_FUNCS = {"DataForSEO": _dataforseo, "Keywords Everywhere": _keywords_everywhere}


def _lookup_volumes(keywords):
    """-> (provider|None, status, {kw: volume|None}). Uses cache + provider fallback chain."""
    providers = configured_providers()
    if not providers:
        return None, "API_NOT_CONFIGURED", {}
    last = (providers[0], "API_ERROR", {})
    loc = f"{_loc_code()}:{_lang_code()}"
    for prov in providers:
        volumes, todo = {}, []
        for kw in keywords:
            hit, vol = db.cache_get(kw, loc, prov)
            if hit:
                volumes[kw] = vol
            else:
                todo.append(kw)
        if todo:
            status, got = PROVIDER_FUNCS[prov](todo)
            if status != "OK":
                last = (prov, status, volumes)
                continue
            for kw in todo:
                v = got.get(kw)
                volumes[kw] = v
                db.cache_set(kw, loc, prov, v)
        return prov, "OK", volumes
    return last


# ------------------------------------------------------------------------ main
def enrich(articles):
    """articles: list of dicts (need title/url/tags). Returns list of result dicts (same order).

    result keys: keyword, search_volume, source, status, demand_signal, related
    """
    cands = [derive_candidates(a) for a in articles]
    all_kws = list(dict.fromkeys(k for c in cands for k in c))
    provider, status, volumes = _lookup_volumes(all_kws) if all_kws else (None, "NO_KEYWORD", {})

    def pick(c):
        """Choose the target keyword for one article."""
        if not c:
            return None, None
        if provider and status == "OK":
            with_vol = [(k, volumes.get(k)) for k in c if volumes.get(k) is not None]
            positive = [kv for kv in with_vol if kv[1] > 0]
            if positive:
                return positive[0]
            if with_vol:
                return with_vol[0]
        return c[0], None

    chosen = [pick(c) for c in cands]

    def ac(i):
        kw, _ = chosen[i]
        if not kw:
            return []
        s = autocomplete(kw)
        # Without a volume provider, prefer the candidate that Google actually autocompletes.
        if not s and not (provider and status == "OK"):
            for alt in cands[i][1:3]:
                s = autocomplete(alt)
                if s:
                    chosen[i] = (alt, None)
                    break
        time.sleep(0.05)
        return s

    with ThreadPoolExecutor(max_workers=4) as ex:
        suggestions = list(ex.map(ac, range(len(articles))))

    results = []
    for i, _ in enumerate(articles):
        kw, vol = chosen[i]
        if not kw:
            results.append({"keyword": None, "search_volume": None, "source": "title-derived",
                            "status": "NO_KEYWORD", "demand_signal": "None", "related": []})
            continue
        sugg = suggestions[i]
        related = [s for s in sugg if s.lower() != kw.lower()][:5]
        if provider and status == "OK":
            res_status = "SUCCESS" if vol is not None else "NO_DATA"
            source = provider
        elif provider:
            res_status, source = status, provider
        else:
            res_status, source = "API_NOT_CONFIGURED", "Google Autocomplete (no volume)"
        results.append({"keyword": kw, "search_volume": vol, "source": source, "status": res_status,
                        "demand_signal": _demand(sugg), "related": related})
    return results


def check_providers():
    """Live credential check (free calls). Used by the 'Test API keys' button."""
    out = []
    creds = _dfs_creds()
    if not creds:
        out.append({"provider": "DataForSEO", "configured": False, "ok": None,
                    "detail": "Not configured (set DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD - free $1 trial)."})
    else:
        try:
            r = requests.get(DFS_USER_URL, auth=creds, timeout=15)
            if r.status_code in (401, 403):
                out.append({"provider": "DataForSEO", "configured": True, "ok": False,
                            "detail": "Invalid credentials (use the API password from the dashboard, not your login password)."})
            else:
                data = r.json()
                res = ((data.get("tasks") or [{}])[0].get("result") or [{}])[0]
                bal = (res.get("money") or {}).get("balance")
                ok = (data.get("status_code") == 20000)
                out.append({"provider": "DataForSEO", "configured": True, "ok": ok,
                            "detail": f"Connected. Balance: ${bal:.2f}" if ok and bal is not None
                            else ("Connected." if ok else "Credentials rejected.")})
        except Exception as exc:
            out.append({"provider": "DataForSEO", "configured": True, "ok": False, "detail": f"Could not reach API: {str(exc)[:100]}"})
    key = _ke_key()
    if not key:
        out.append({"provider": "Keywords Everywhere", "configured": False, "ok": None,
                    "detail": "Not configured (optional, paid credits)."})
    else:
        try:
            r = requests.get(KE_CREDITS_URL, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=15)
            if r.status_code in (401, 403):
                out.append({"provider": "Keywords Everywhere", "configured": True, "ok": False, "detail": "Invalid API key."})
            elif r.status_code < 400:
                out.append({"provider": "Keywords Everywhere", "configured": True, "ok": True, "detail": "Key accepted."})
            else:
                out.append({"provider": "Keywords Everywhere", "configured": True, "ok": None, "detail": f"HTTP {r.status_code}"})
        except Exception as exc:
            out.append({"provider": "Keywords Everywhere", "configured": True, "ok": False, "detail": f"Could not reach API: {str(exc)[:100]}"})
    ac = autocomplete("competitor analysis")
    out.append({"provider": "Google Autocomplete", "configured": True, "ok": bool(ac),
                "detail": "Free demand signal available (no volume)." if ac else "Unreachable from this host."})
    return out
