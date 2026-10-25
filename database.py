import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

DATABASE_PATH = os.environ.get("DATABASE_PATH", "competitor_pulse.db")

@contextmanager
def _connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            yield conn
    finally:
        conn.close()

def _now():
    return datetime.now(timezone.utc).isoformat()

def init_db():
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS competitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            last_scan_at TEXT,
            last_status TEXT
        );
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competitor_id INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            status TEXT NOT NULL,
            pages_checked INTEGER NOT NULL DEFAULT 0,
            articles_found INTEGER NOT NULL DEFAULT 0,
            message TEXT
        );
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competitor_id INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            title TEXT,
            published_at TEXT,
            description TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            keyword TEXT,
            search_volume INTEGER,
            trends_interest REAL,
            keyword_source TEXT,
            keyword_status TEXT,
            keyword_error TEXT,
            trends_series_json TEXT,
            trends_status TEXT,
            trends_error TEXT,
            volume_source TEXT,
            volume_status TEXT,
            volume_error TEXT,
            cpc REAL,
            competition REAL,
            country TEXT,
            provider_cached INTEGER,
            similar_keywords_json TEXT,
            volume_series_json TEXT,
            volume_keyword TEXT,
            trends_keyword TEXT,
            UNIQUE(competitor_id, url)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competitor_id INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            url TEXT,
            title TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pricing_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competitor_id INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
            plan_name TEXT NOT NULL,
            price_text TEXT NOT NULL,
            amount REAL,
            currency TEXT,
            billing_period TEXT,
            source_url TEXT,
            observed_at TEXT NOT NULL,
            UNIQUE(competitor_id, plan_name, price_text)
        );
        CREATE INDEX IF NOT EXISTS idx_articles_competitor_seen ON articles(competitor_id, first_seen_at);
        CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
        """)
        
        # Auto-migration: Add trends_interest column if it doesn't exist yet
        cursor = conn.execute("PRAGMA table_info(articles)")
        columns = [info["name"] for info in cursor.fetchall()]
        if "trends_interest" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN trends_interest REAL")
        if "keyword_error" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN keyword_error TEXT")
        if "trends_series_json" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN trends_series_json TEXT")
        if "trends_status" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN trends_status TEXT")
        if "trends_error" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN trends_error TEXT")
        if "volume_source" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN volume_source TEXT")
        if "volume_status" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN volume_status TEXT")
        if "volume_error" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN volume_error TEXT")
        if "cpc" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN cpc REAL")
        if "competition" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN competition REAL")
        if "country" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN country TEXT")
        if "provider_cached" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN provider_cached INTEGER")
        if "similar_keywords_json" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN similar_keywords_json TEXT")
        if "volume_series_json" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN volume_series_json TEXT")
        if "volume_keyword" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN volume_keyword TEXT")
        if "trends_keyword" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN trends_keyword TEXT")
            
        conn.commit()

def normalize_url(raw):
    parsed = urlparse(raw.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not parsed.hostname or not parsed.hostname.strip():
        raise ValueError("Please enter a valid website URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed.")
    path = parsed.path or ""
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))

def _dict(row):
    return dict(row) if row else None

def list_competitors():
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM competitors ORDER BY created_at DESC"
        ).fetchall()]

def add_competitor(name, url):
    url = normalize_url(url)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO competitors(name,url,created_at) VALUES(?,?,?)",
            (name.strip(), url, _now())
        )
        conn.commit()
        return _dict(conn.execute("SELECT * FROM competitors WHERE id=?", (cur.lastrowid,)).fetchone())

def get_competitor(competitor_id):
    with _connect() as conn:
        return _dict(conn.execute("SELECT * FROM competitors WHERE id=?", (competitor_id,)).fetchone())

def delete_competitor_record(competitor_id):
    with _connect() as conn:
        cur = conn.execute("DELETE FROM competitors WHERE id=?", (competitor_id,))
        conn.commit()
        return cur.rowcount > 0

def save_scan(competitor_id, status, pages_checked, articles_found, message, started_at):
    with _connect() as conn:
        cur = conn.execute("""INSERT INTO scans
            (competitor_id,started_at,status,pages_checked,articles_found,message)
            VALUES(?,?,?,?,?,?)""",
            (competitor_id, started_at, status, pages_checked, articles_found, message))
        conn.execute("UPDATE competitors SET last_scan_at=?,last_status=? WHERE id=?",
                     (started_at, status, competitor_id))
        conn.commit()
        return cur.lastrowid

def store_articles(competitor_id, article):
    url = article["url"]
    now = _now()
    with _connect() as conn:
        exists = conn.execute(
            "SELECT id FROM articles WHERE competitor_id=? AND url=?",
            (competitor_id, url)
        ).fetchone()
        if exists:
            conn.execute("""UPDATE articles SET title=COALESCE(NULLIF(?,''),title),
                published_at=COALESCE(NULLIF(?,''),published_at),
                description=COALESCE(NULLIF(?,''),description), last_seen_at=?
                WHERE competitor_id=? AND url=?""",
                (article.get("title"), article.get("published_at"), article.get("description"),
                 now, competitor_id, url))
            conn.commit()
            return False
        conn.execute("""INSERT INTO articles
            (competitor_id,url,title,published_at,description,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?,?)""",
            (competitor_id, url, article.get("title"), article.get("published_at"),
             article.get("description"), now, now))
        conn.commit()
        return True

def update_article_keyword(competitor_id, url, keyword_data):
    with _connect() as conn:
        conn.execute("""UPDATE articles SET keyword=?,search_volume=?,trends_interest=?,keyword_source=?,keyword_status=?,
            keyword_error=?,trends_series_json=?,trends_status=?,trends_error=?,volume_source=?,
            volume_status=?,volume_error=?,cpc=?,competition=?,country=?,provider_cached=?,
            similar_keywords_json=?,volume_series_json=?,volume_keyword=?,trends_keyword=?
            WHERE competitor_id=? AND url=?""",
            (keyword_data.get("keyword"), keyword_data.get("search_volume"),
             keyword_data.get("trends_interest"),
             keyword_data.get("source"), keyword_data.get("status"),
             keyword_data.get("error"),
             json.dumps(keyword_data.get("trends_series") or []),
             keyword_data.get("trends_status"),
             keyword_data.get("trends_error"),
             keyword_data.get("volume_source"),
             keyword_data.get("volume_status"),
             keyword_data.get("volume_error"),
             keyword_data.get("cpc"),
             keyword_data.get("competition"),
             keyword_data.get("country"),
             (
                 int(keyword_data["provider_cached"])
                 if keyword_data.get("provider_cached") is not None
                 else None
             ),
             json.dumps(keyword_data.get("similar_keywords") or []),
             json.dumps(keyword_data.get("volume_series") or []),
             keyword_data.get("volume_keyword"),
             keyword_data.get("trends_keyword"),
             competitor_id, url))
        conn.commit()

def create_event(competitor_id, event_type, summary, url=None, title=None, details=None):
    with _connect() as conn:
        cur = conn.execute("""INSERT INTO events
            (competitor_id,event_type,summary,url,title,details_json,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (competitor_id, event_type, summary, url, title,
             json.dumps(details or {}), _now()))
        conn.commit()
        return cur.lastrowid

def get_articles(competitor_id, limit=100):
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM articles WHERE competitor_id=? ORDER BY first_seen_at DESC LIMIT ?",
            (competitor_id, limit)).fetchall()]
        for row in rows:
            try:
                row["trends_series"] = json.loads(row.pop("trends_series_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                row["trends_series"] = []
                row.pop("trends_series_json", None)
            try:
                row["similar_keywords"] = json.loads(
                    row.pop("similar_keywords_json") or "[]"
                )
            except (TypeError, json.JSONDecodeError):
                row["similar_keywords"] = []
                row.pop("similar_keywords_json", None)
            try:
                row["volume_series"] = json.loads(
                    row.pop("volume_series_json") or "[]"
                )
            except (TypeError, json.JSONDecodeError):
                row["volume_series"] = []
                row.pop("volume_series_json", None)
            if row.get("provider_cached") is not None:
                row["provider_cached"] = bool(row["provider_cached"])
        return rows

def get_events(limit=50):
    with _connect() as conn:
        rows = conn.execute("""SELECT e.*, c.name AS competitor_name
            FROM events e JOIN competitors c ON c.id=e.competitor_id
            ORDER BY e.created_at DESC LIMIT ?""", (limit,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try: item["details"] = json.loads(item.pop("details_json") or "{}")
            except Exception: item["details"] = {}
            out.append(item)
        return out

def get_pricing(competitor_id):
    with _connect() as conn:
        rows = conn.execute("""SELECT plan_name,price_text,amount,currency,billing_period,
            source_url,MAX(observed_at) AS observed_at
            FROM pricing_snapshots WHERE competitor_id=?
            GROUP BY plan_name,price_text,amount,currency,billing_period,source_url
            ORDER BY observed_at DESC LIMIT 100""", (competitor_id,)).fetchall()
        return [dict(r) for r in rows]

def store_pricing_snapshot(competitor_id, plan):
    plan_name = (plan.get("plan_name") or "Pricing page").strip()[:120]
    price_text = (plan.get("price_text") or "").strip()[:120]
    if not price_text:
        return False
    with _connect() as conn:
        latest = conn.execute("""SELECT price_text FROM pricing_snapshots
            WHERE competitor_id=? AND plan_name=? ORDER BY observed_at DESC LIMIT 1""",
            (competitor_id, plan_name)).fetchone()
        if latest and latest["price_text"] == price_text:
            return False
        conn.execute("""INSERT INTO pricing_snapshots
            (competitor_id,plan_name,price_text,amount,currency,billing_period,source_url,observed_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (competitor_id, plan_name, price_text, plan.get("amount"), plan.get("currency"),
             plan.get("billing_period"), plan.get("source_url"), _now()))
        conn.commit()
        return latest is not None

def get_scans(competitor_id):
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scans WHERE competitor_id=? ORDER BY started_at DESC LIMIT 30",
            (competitor_id,)).fetchall()]

def get_dashboard_stats():
    with _connect() as conn:
        competitors = conn.execute("SELECT COUNT(*) n FROM competitors").fetchone()["n"]
        articles = conn.execute("SELECT COUNT(*) n FROM articles").fetchone()["n"]
        events = conn.execute("SELECT COUNT(*) n FROM events WHERE created_at >= datetime('now','-7 days')").fetchone()["n"]
        scans = conn.execute("SELECT COUNT(*) n FROM scans").fetchone()["n"]
        return {"competitors": competitors, "articles_tracked": articles,
                "events_7d": events, "scans_run": scans}

# --- UTILITY FUNCTION TO CLEAN UP BAD RECORDS ---
def purge_archive_articles():
    """Deletes articles with 'archives' in the URL or title to clean up the dashboard."""
    with _connect() as conn:
        cur = conn.execute("""
            DELETE FROM articles 
            WHERE LOWER(url) LIKE '%/archives%' 
               OR LOWER(title) LIKE '% archives%'
               OR LOWER(title) LIKE '%archive%'
        """)
        conn.commit()
        return cur.rowcount