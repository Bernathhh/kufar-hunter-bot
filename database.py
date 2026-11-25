"""
SQLite Database manager for Kufar Hunter Bot.
Stores tracking queries and seen advertisements to avoid duplicates.
"""
from __future__ import annotations
import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any, Set, Tuple

DB_FILE = os.path.join(os.path.dirname(__file__), "kufar_hunter.db")

def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    if not db_path:
        db_path = DB_FILE
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: Optional[str] = None):
    """Initializes tables and indices."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Queries table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                params_json TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Seen ads table with price tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen_ads (
                ad_id INTEGER NOT NULL,
                query_id INTEGER NOT NULL,
                last_price_byn REAL,
                last_price_usd REAL,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ad_id, query_id),
                FOREIGN KEY (query_id) REFERENCES queries(id) ON DELETE CASCADE
            )
        """)

        # Migration: add price columns if table existed without them
        try:
            cursor.execute("ALTER TABLE seen_ads ADD COLUMN last_price_byn REAL")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE seen_ads ADD COLUMN last_price_usd REAL")
        except Exception:
            pass

        # Indexes for fast lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_seen_query ON seen_ads(query_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_queries_chat ON queries(chat_id)")

        # Registered bot users
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Blacklisted / hidden ads table (per user)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blacklisted_ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                ad_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, ad_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_blacklist_user ON blacklisted_ads(chat_id, ad_id)")

        # Deleted queries table (prevents deleted filters from being resurrected)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deleted_queries (
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, name)
            )
        """)

        # Persistent bot settings (e.g. check_interval_seconds)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Stop words / negative keywords (per chat)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stop_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, word)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stopwords_chat ON stop_words(chat_id)")

        # Blacklisted sellers (per chat)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blacklisted_sellers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                seller_name TEXT NOT NULL,
                account_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, seller_name)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_blsellers_chat ON blacklisted_sellers(chat_id)")

        # Favorites / Saved ads table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                ad_id INTEGER NOT NULL,
                title TEXT,
                price_byn REAL,
                price_usd REAL,
                ad_link TEXT,
                seller_name TEXT,
                image_url TEXT,
                status TEXT DEFAULT '',
                note TEXT DEFAULT '',
                is_closed INTEGER DEFAULT 0,
                last_checked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, ad_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fav_user ON favorites(chat_id, ad_id)")

        # Price history table (daily market snapshots per query)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                median_byn REAL,
                min_byn REAL,
                max_byn REAL,
                ads_count INTEGER DEFAULT 0,
                UNIQUE(query_id, day)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ph_query_day ON price_history(query_id, day)")

        # Safe column migrations for existing databases
        migrations = [
            "ALTER TABLE queries ADD COLUMN meta_json TEXT DEFAULT '{}'",
            "ALTER TABLE favorites ADD COLUMN status TEXT DEFAULT ''",
            "ALTER TABLE favorites ADD COLUMN note TEXT DEFAULT ''",
            "ALTER TABLE favorites ADD COLUMN is_closed INTEGER DEFAULT 0",
            "ALTER TABLE favorites ADD COLUMN last_checked_at TIMESTAMP",
        ]
        for sql in migrations:
            try:
                cursor.execute(sql)
            except Exception:
                pass
        
        conn.commit()


def _parse_json_dict(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def set_setting(key: str, value: str, db_path: Optional[str] = None):
    """Sets a persistent key-value setting."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()


def get_setting(key: str, default: Optional[str] = None, db_path: Optional[str] = None) -> Optional[str]:
    """Retrieves a persistent setting value."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default


def add_query(
    chat_id: int,
    name: str,
    params: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None
) -> int:
    """Adds a new search query to track and unmarks it from deleted_queries if present."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM deleted_queries WHERE chat_id = ? AND name = ?", (chat_id, name))
        cursor.execute(
            "INSERT INTO queries (chat_id, name, params_json, meta_json, is_active) VALUES (?, ?, ?, ?, 1)",
            (chat_id, name, json.dumps(params), json.dumps(meta or {}))
        )
        conn.commit()
        return cursor.lastrowid


def get_active_queries(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns all active tracking queries across all chats."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, chat_id, name, params_json, meta_json, created_at FROM queries WHERE is_active = 1")
        rows = cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "chat_id": r["chat_id"],
                "name": r["name"],
                "params": json.loads(r["params_json"]),
                "meta": _parse_json_dict(r["meta_json"] if "meta_json" in r.keys() else None),
                "created_at": r["created_at"]
            })
        return result


def get_user_queries(chat_id: int, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns all queries created by a specific user/chat."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, chat_id, name, params_json, meta_json, is_active, created_at FROM queries WHERE chat_id = ? ORDER BY id DESC", (chat_id,))
        rows = cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "chat_id": r["chat_id"],
                "name": r["name"],
                "params": json.loads(r["params_json"]),
                "meta": _parse_json_dict(r["meta_json"] if "meta_json" in r.keys() else None),
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"]
            })
        return result


# ==================== MONITORING PAUSE / RESUME ====================

def set_user_monitoring_paused(chat_id: int, paused: bool, db_path: Optional[str] = None):
    """Sets whether background monitoring is paused for a user."""
    set_setting(f"paused_{chat_id}", "1" if paused else "0", db_path=db_path)


def is_user_monitoring_paused(chat_id: int, db_path: Optional[str] = None) -> bool:
    """Checks if background monitoring is paused for a user."""
    return get_setting(f"paused_{chat_id}", "0", db_path=db_path) == "1"


def toggle_query_active(query_id: int, is_active: bool, chat_id: Optional[int] = None, db_path: Optional[str] = None) -> bool:
    """Toggles active state of a specific search query."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        if chat_id is not None:
            cursor.execute("UPDATE queries SET is_active = ? WHERE id = ? AND chat_id = ?", (1 if is_active else 0, query_id, chat_id))
        else:
            cursor.execute("UPDATE queries SET is_active = ? WHERE id = ?", (1 if is_active else 0, query_id))
        conn.commit()
        return cursor.rowcount > 0


def update_query(
    query_id: int,
    chat_id: int,
    name: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None
) -> bool:
    """Updates an existing search query's name, parameters, and/or bot meta without losing seen history."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        fields = []
        values: List[Any] = []
        if name is not None and name.strip():
            fields.append("name = ?")
            values.append(name.strip())
        if params is not None:
            fields.append("params_json = ?")
            values.append(json.dumps(params))
        if meta is not None:
            fields.append("meta_json = ?")
            values.append(json.dumps(meta))
        if not fields:
            return False
        values.extend([query_id, chat_id])
        cursor.execute(
            f"UPDATE queries SET {', '.join(fields)} WHERE id = ? AND chat_id = ?",
            tuple(values)
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_query(query_id: int, chat_id: Optional[int] = None, db_path: Optional[str] = None) -> bool:
    """Deletes a query by ID and marks it in deleted_queries to prevent resurrection on restart."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, name FROM queries WHERE id = ?", (query_id,))
        row = cursor.fetchone()
        if row:
            cid = row["chat_id"]
            name = row["name"]
            cursor.execute(
                "INSERT OR REPLACE INTO deleted_queries (chat_id, name) VALUES (?, ?)",
                (cid, name)
            )

        if chat_id is not None:
            cursor.execute("DELETE FROM queries WHERE id = ? AND chat_id = ?", (query_id, chat_id))
        else:
            cursor.execute("DELETE FROM queries WHERE id = ?", (query_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            cursor.execute("DELETE FROM seen_ads WHERE query_id = ?", (query_id,))
        conn.commit()
        return deleted


def mark_query_deleted(chat_id: int, name: str, db_path: Optional[str] = None):
    """Records that a query was explicitly deleted by user to prevent resurrection on restart."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO deleted_queries (chat_id, name) VALUES (?, ?)",
            (chat_id, name)
        )
        conn.commit()


def get_deleted_queries(chat_id: int, db_path: Optional[str] = None) -> Set[str]:
    """Returns set of query names that were deleted by user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM deleted_queries WHERE chat_id = ?", (chat_id,))
        return {r["name"] for r in cursor.fetchall()}


def is_ad_seen(query_id: int, ad_id: int, db_path: Optional[str] = None) -> bool:
    """Checks if an ad has already been recorded for this query."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_ads WHERE query_id = ? AND ad_id = ?", (query_id, ad_id))
        return cursor.fetchone() is not None


def get_ad_seen_price(query_id: int, ad_id: int, db_path: Optional[str] = None) -> Tuple[bool, Optional[float], Optional[float]]:
    """
    Returns (is_seen, last_price_byn, last_price_usd).
    If not seen before, returns (False, None, None).
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_price_byn, last_price_usd FROM seen_ads WHERE query_id = ? AND ad_id = ?",
            (query_id, ad_id)
        )
        row = cursor.fetchone()
        if row is None:
            return False, None, None
        return True, row["last_price_byn"], row["last_price_usd"]


def mark_ad_seen(
    query_id: int,
    ad_id: int,
    price_byn: Optional[float] = None,
    price_usd: Optional[float] = None,
    db_path: Optional[str] = None
):
    """Marks an ad as seen and records its price."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO seen_ads (ad_id, query_id, last_price_byn, last_price_usd, seen_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(ad_id, query_id) DO UPDATE SET
                last_price_byn = excluded.last_price_byn,
                last_price_usd = excluded.last_price_usd,
                seen_at = CURRENT_TIMESTAMP
        """, (ad_id, query_id, price_byn, price_usd))
        conn.commit()


def mark_ads_seen(query_id: int, ad_ids: List[int], db_path: Optional[str] = None):
    """Batch marks ad IDs as seen."""
    if not ad_ids:
        return
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        records = [(ad_id, query_id) for ad_id in ad_ids]
        cursor.executemany("INSERT OR IGNORE INTO seen_ads (ad_id, query_id) VALUES (?, ?)", records)
        conn.commit()


def mark_ads_seen_batch(
    query_id: int,
    ads: List[Dict[str, Any]],
    db_path: Optional[str] = None
):
    """Batch marks ads as seen with their current prices."""
    if not ads:
        return
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        records = [
            (a["ad_id"], query_id, a.get("price_byn"), a.get("price_usd"))
            for a in ads
        ]
        cursor.executemany("""
            INSERT INTO seen_ads (ad_id, query_id, last_price_byn, last_price_usd, seen_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(ad_id, query_id) DO UPDATE SET
                last_price_byn = excluded.last_price_byn,
                last_price_usd = excluded.last_price_usd,
                seen_at = CURRENT_TIMESTAMP
        """, records)
        conn.commit()


def get_query_seen_count(query_id: int, db_path: Optional[str] = None) -> int:
    """Returns the total number of ads seen for this query."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM seen_ads WHERE query_id = ?", (query_id,))
        row = cursor.fetchone()
        return row["cnt"] if row else 0


# ==================== USER REGISTRATION ====================

def register_or_update_user(
    chat_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    db_path: Optional[str] = None
) -> bool:
    """
    Registers a new bot user or updates existing user's last_seen timestamp.
    Returns True if this is a brand new user, False otherwise.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
        exists = cursor.fetchone() is not None
        if not exists:
            cursor.execute(
                "INSERT INTO users (chat_id, username, first_name, last_name, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (chat_id, username, first_name, last_name)
            )
            conn.commit()
            return True
        else:
            cursor.execute(
                "UPDATE users SET username = ?, first_name = ?, last_name = ?, last_seen = CURRENT_TIMESTAMP "
                "WHERE chat_id = ?",
                (username, first_name, last_name, chat_id)
            )
            conn.commit()
            return False


def get_all_users(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns all registered users."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, username, first_name, last_name, first_seen, last_seen FROM users ORDER BY last_seen DESC")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_user_count(db_path: Optional[str] = None) -> int:
    """Returns total number of registered users."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM users")
        row = cursor.fetchone()
        return row["cnt"] if row else 0


# ==================== BLACKLIST (ЧС / НЕ ИНТЕРЕСНО) ====================

def add_to_blacklist(chat_id: int, ad_id: int, db_path: Optional[str] = None) -> bool:
    """Adds an ad_id to user's blacklist so it is hidden and never shown again."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO blacklisted_ads (chat_id, ad_id) VALUES (?, ?)",
            (chat_id, int(ad_id))
        )
        conn.commit()
        return cursor.rowcount > 0


def remove_from_blacklist(chat_id: int, ad_id: int, db_path: Optional[str] = None) -> bool:
    """Removes an ad_id from user's blacklist."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM blacklisted_ads WHERE chat_id = ? AND ad_id = ?",
            (chat_id, int(ad_id))
        )
        conn.commit()
        return cursor.rowcount > 0


def is_ad_blacklisted(chat_id: int, ad_id: int, db_path: Optional[str] = None) -> bool:
    """Checks if an ad_id is in user's blacklist."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM blacklisted_ads WHERE chat_id = ? AND ad_id = ?",
            (chat_id, int(ad_id))
        )
        return cursor.fetchone() is not None


def get_user_blacklist(chat_id: int, db_path: Optional[str] = None) -> Set[int]:
    """Returns a set of ad_ids currently blacklisted by user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT ad_id FROM blacklisted_ads WHERE chat_id = ?", (chat_id,))
        return {r["ad_id"] for r in cursor.fetchall()}


def get_blacklist_count(chat_id: int, db_path: Optional[str] = None) -> int:
    """Returns total blacklisted ads count for user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM blacklisted_ads WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row["cnt"] if row else 0


def clear_user_blacklist(chat_id: int, db_path: Optional[str] = None) -> int:
    """Clears all blacklisted ads for a user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM blacklisted_ads WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cursor.rowcount


# ==================== STOP WORDS (МИНУС-СЛОВА) ====================

def add_stop_word(chat_id: int, word: str, db_path: Optional[str] = None) -> bool:
    """Adds a stop word for a user. Stored in lowercase."""
    clean_word = word.strip().lower()
    if not clean_word:
        return False
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO stop_words (chat_id, word) VALUES (?, ?)",
            (chat_id, clean_word)
        )
        conn.commit()
        return cursor.rowcount > 0


def remove_stop_word(chat_id: int, word: str, db_path: Optional[str] = None) -> bool:
    """Removes a stop word for a user."""
    clean_word = word.strip().lower()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM stop_words WHERE chat_id = ? AND word = ?",
            (chat_id, clean_word)
        )
        conn.commit()
        return cursor.rowcount > 0


def get_stop_words(chat_id: int, db_path: Optional[str] = None) -> List[str]:
    """Returns list of stop words for a user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM stop_words WHERE chat_id = ? ORDER BY word ASC", (chat_id,))
        return [r["word"] for r in cursor.fetchall()]


def clear_stop_words(chat_id: int, db_path: Optional[str] = None) -> int:
    """Clears all stop words for a user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stop_words WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cursor.rowcount


# ==================== BLACKLISTED SELLERS (ЧС ПРОДАВЦОВ) ====================

def add_blacklisted_seller(chat_id: int, seller_name: str, account_id: Optional[str] = None, db_path: Optional[str] = None) -> bool:
    """Adds a seller to user's blacklisted sellers list."""
    sname = seller_name.strip()
    if not sname:
        return False
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO blacklisted_sellers (chat_id, seller_name, account_id) VALUES (?, ?, ?)",
            (chat_id, sname, account_id)
        )
        conn.commit()
        return True


def remove_blacklisted_seller(chat_id: int, seller_name: str, db_path: Optional[str] = None) -> bool:
    """Removes a seller from user's blacklisted sellers list."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM blacklisted_sellers WHERE chat_id = ? AND seller_name = ?",
            (chat_id, seller_name.strip())
        )
        conn.commit()
        return cursor.rowcount > 0


def get_blacklisted_sellers(chat_id: int, db_path: Optional[str] = None) -> Set[str]:
    """Returns set of blacklisted seller names for user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT seller_name FROM blacklisted_sellers WHERE chat_id = ?", (chat_id,))
        return {r["seller_name"] for r in cursor.fetchall()}


def is_seller_blacklisted(chat_id: int, seller_name: str, account_id: Optional[str] = None, db_path: Optional[str] = None) -> bool:
    """Checks if a seller is in user's blacklist."""
    sname = (seller_name or "").strip()
    if not sname:
        return False
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        if account_id:
            cursor.execute(
                "SELECT 1 FROM blacklisted_sellers WHERE chat_id = ? AND (seller_name = ? OR account_id = ?)",
                (chat_id, sname, account_id)
            )
        else:
            cursor.execute(
                "SELECT 1 FROM blacklisted_sellers WHERE chat_id = ? AND seller_name = ?",
                (chat_id, sname)
            )
        return cursor.fetchone() is not None


# ==================== USER MESSAGE TEMPLATE (ШАБЛОН ДЛЯ СВЯЗИ) ====================

DEFAULT_CONTACT_TEMPLATE = "Здравствуйте! Готов забрать сегодня за наличные, актуально?"

def get_user_template(chat_id: int, db_path: Optional[str] = None) -> str:
    """Returns custom message template for contacting sellers, or default."""
    return get_setting(f"template_{chat_id}", DEFAULT_CONTACT_TEMPLATE, db_path=db_path)


def set_user_template(chat_id: int, template: str, db_path: Optional[str] = None):
    """Saves custom message template for contacting sellers."""
    set_setting(f"template_{chat_id}", template.strip(), db_path=db_path)


# ==================== FAVORITES (ИЗБРАННОЕ) ====================

def add_favorite(
    chat_id: int,
    ad_id: int,
    title: str = "",
    price_byn: Optional[float] = None,
    price_usd: Optional[float] = None,
    ad_link: str = "",
    seller_name: str = "",
    image_url: str = "",
    db_path: Optional[str] = None
) -> bool:
    """Adds an ad to user's favorites while preserving existing CRM note/status if present."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO favorites
               (chat_id, ad_id, title, price_byn, price_usd, ad_link, seller_name, image_url, status, note, is_closed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', 0, CURRENT_TIMESTAMP)
               ON CONFLICT(chat_id, ad_id) DO UPDATE SET
                 title = excluded.title,
                 price_byn = excluded.price_byn,
                 price_usd = excluded.price_usd,
                 ad_link = excluded.ad_link,
                 seller_name = excluded.seller_name,
                 image_url = excluded.image_url,
                 is_closed = 0""",
            (chat_id, int(ad_id), title, price_byn, price_usd, ad_link, seller_name, image_url)
        )
        conn.commit()
        return True


def remove_favorite(chat_id: int, ad_id: int, db_path: Optional[str] = None) -> bool:
    """Removes an ad from user's favorites."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM favorites WHERE chat_id = ? AND ad_id = ?", (chat_id, int(ad_id)))
        conn.commit()
        return cursor.rowcount > 0


def is_favorite(chat_id: int, ad_id: int, db_path: Optional[str] = None) -> bool:
    """Checks if an ad is in user's favorites."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM favorites WHERE chat_id = ? AND ad_id = ?", (chat_id, int(ad_id)))
        return cursor.fetchone() is not None


def get_user_favorites(chat_id: int, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns all favorites saved by a user, ordered by newest first."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ad_id, title, price_byn, price_usd, ad_link, seller_name, image_url, "
            "COALESCE(status, '') as status, COALESCE(note, '') as note, COALESCE(is_closed, 0) as is_closed, created_at "
            "FROM favorites WHERE chat_id = ? ORDER BY id DESC",
            (chat_id,)
        )
        return [dict(r) for r in cursor.fetchall()]


def update_favorite_crm(
    chat_id: int,
    ad_id: int,
    status: Optional[str] = None,
    note: Optional[str] = None,
    db_path: Optional[str] = None
) -> bool:
    """Updates CRM status tag and/or personal note on a saved favorite."""
    fields = []
    values: List[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status.strip()[:32])
    if note is not None:
        fields.append("note = ?")
        values.append(note.strip()[:400])
    if not fields:
        return False
    values.extend([chat_id, int(ad_id)])
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE favorites SET {', '.join(fields)} WHERE chat_id = ? AND ad_id = ?",
            tuple(values)
        )
        conn.commit()
        return cursor.rowcount > 0


def update_favorite_price_state(
    chat_id: int,
    ad_id: int,
    price_byn: Optional[float] = None,
    price_usd: Optional[float] = None,
    is_closed: Optional[int] = None,
    db_path: Optional[str] = None
) -> bool:
    """Updates tracked price and closed/sold state for a saved favorite."""
    fields = ["last_checked_at = CURRENT_TIMESTAMP"]
    values: List[Any] = []
    if price_byn is not None:
        fields.append("price_byn = ?")
        values.append(price_byn)
    if price_usd is not None:
        fields.append("price_usd = ?")
        values.append(price_usd)
    if is_closed is not None:
        fields.append("is_closed = ?")
        values.append(int(is_closed))
    values.extend([chat_id, int(ad_id)])
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE favorites SET {', '.join(fields)} WHERE chat_id = ? AND ad_id = ?",
            tuple(values)
        )
        conn.commit()
        return cursor.rowcount > 0


def get_all_favorites(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns all active/open favorites across all users for background tracking."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, ad_id, title, price_byn, price_usd, ad_link, seller_name, "
            "COALESCE(is_closed, 0) as is_closed, last_checked_at "
            "FROM favorites ORDER BY id DESC"
        )
        return [dict(r) for r in cursor.fetchall()]


def record_price_history(
    query_id: int,
    median_byn: Optional[float],
    min_byn: Optional[float],
    max_byn: Optional[float],
    ads_count: int,
    day: Optional[str] = None,
    db_path: Optional[str] = None
):
    """Stores or refreshes today's market price snapshot (median/min/max) for a search query."""
    if not query_id or median_byn is None:
        return
    if not day:
        minsk_tz = timezone(timedelta(hours=3))
        day = datetime.now(minsk_tz).strftime("%Y-%m-%d")
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO price_history (query_id, day, median_byn, min_byn, max_byn, ads_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (int(query_id), day, float(median_byn), min_byn, max_byn, int(ads_count))
        )
        conn.commit()


def get_price_history(query_id: int, days: int = 14, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns historical market price snapshots for a query ordered chronologically."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT day, median_byn, min_byn, max_byn, ads_count FROM price_history "
            "WHERE query_id = ? ORDER BY day DESC LIMIT ?",
            (int(query_id), int(days))
        )
        rows = [dict(r) for r in cursor.fetchall()]
        rows.reverse()
        return rows


def get_admin_users_overview(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns per-user summary for the admin dashboard."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.chat_id, u.username, u.first_name, u.last_name, u.last_seen,
                   COUNT(DISTINCT q.id) as queries_total,
                   SUM(CASE WHEN q.is_active = 1 THEN 1 ELSE 0 END) as queries_active,
                   (SELECT COUNT(*) FROM favorites f WHERE f.chat_id = u.chat_id) as favorites_count
            FROM users u
            LEFT JOIN queries q ON q.chat_id = u.chat_id
            GROUP BY u.chat_id
            ORDER BY u.last_seen DESC
        """)
        rows = []
        for r in cursor.fetchall():
            d = dict(r)
            d["queries_active"] = int(d.get("queries_active") or 0)
            d["queries_total"] = int(d.get("queries_total") or 0)
            d["favorites_count"] = int(d.get("favorites_count") or 0)
            d["paused"] = is_user_monitoring_paused(d["chat_id"], db_path=db_path)
            rows.append(d)
        return rows


def get_favorites_count(chat_id: int, db_path: Optional[str] = None) -> int:
    """Returns total favorites count for user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM favorites WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row["cnt"] if row else 0


def clear_user_favorites(chat_id: int, db_path: Optional[str] = None) -> int:
    """Clears all favorites for a user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM favorites WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cursor.rowcount


# ==================== SILENT / NIGHT MODE (ТИХИЙ РЕЖИМ) ====================

def get_silent_mode(chat_id: int, db_path: Optional[str] = None) -> str:
    """Returns silent hours setting for user: 'off', or '23:00-08:00', or custom 'HH:MM-HH:MM'."""
    return get_setting(f"silent_{chat_id}", "off", db_path=db_path) or "off"


def set_silent_mode(chat_id: int, value: str, db_path: Optional[str] = None):
    """Sets silent hours setting for user ('off', '23:00-08:00', etc.)."""
    set_setting(f"silent_{chat_id}", value.strip(), db_path=db_path)


def is_in_silent_hours(chat_id: int, current_dt: Optional[datetime] = None, db_path: Optional[str] = None) -> bool:
    """
    Checks if current time in Minsk timezone (UTC+3) falls within user's configured silent hours.
    """
    mode = get_silent_mode(chat_id, db_path=db_path)
    if not mode or mode.lower() in ("off", "none", "disabled", "0"):
        return False

    try:
        if current_dt is None:
            # Minsk / Belarus timezone: UTC+3
            current_dt = datetime.now(timezone(timedelta(hours=3)))
        
        parts = mode.split("-")
        if len(parts) != 2:
            return False
        
        start_h, start_m = map(int, parts[0].strip().split(":"))
        end_h, end_m = map(int, parts[1].strip().split(":"))
        
        cur_min = current_dt.hour * 60 + current_dt.minute
        start_min = start_h * 60 + start_m
        end_min = end_h * 60 + end_m
        
        if start_min <= end_min:
            return start_min <= cur_min < end_min
        else:
            # Overnight window, e.g. 23:00 (1380) to 08:00 (480)
            return cur_min >= start_min or cur_min < end_min
    except Exception:
        return False


def get_user_fast_alert(chat_id: int, db_path: Optional[str] = None) -> bool:
    """Returns True if Fast-Alert mode (button card first, photos asynchronously) is enabled."""
    val = get_setting(f"fast_alert_{chat_id}", "1", db_path=db_path)
    return str(val).strip().lower() not in ("0", "false", "off", "no")


def set_user_fast_alert(chat_id: int, enabled: bool, db_path: Optional[str] = None):
    """Sets Fast-Alert mode for a user."""
    set_setting(f"fast_alert_{chat_id}", "1" if enabled else "0", db_path=db_path)


def get_user_priority_mode(chat_id: int, db_path: Optional[str] = None) -> str:
    """Returns notification priority mode: 'hot' (bypass silence on hot deals), 'only_hot', or 'off'."""
    val = (get_setting(f"priority_mode_{chat_id}", "hot", db_path=db_path) or "hot").strip().lower()
    return val if val in ("hot", "only_hot", "off") else "hot"


def set_user_priority_mode(chat_id: int, mode: str, db_path: Optional[str] = None):
    """Persists notification priority mode ('hot', 'only_hot', or 'off')."""
    clean = mode.strip().lower() if mode else "hot"
    if clean not in ("hot", "only_hot", "off"):
        clean = "hot"
    set_setting(f"priority_mode_{chat_id}", clean, db_path=db_path)


# ==================== CLOUD STATE BACKUP & RESTORE ====================

def export_database_state(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Exports all queries, users, and blacklisted ads to a dictionary."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Queries
        cursor.execute("SELECT chat_id, name, params_json, meta_json, is_active FROM queries")
        queries = []
        for r in cursor.fetchall():
            try:
                params = json.loads(r["params_json"])
            except Exception:
                params = {}
            queries.append({
                "chat_id": r["chat_id"],
                "name": r["name"],
                "params": params,
                "meta": _parse_json_dict(r["meta_json"] if "meta_json" in r.keys() else None),
                "is_active": r["is_active"]
            })
            
        # Users
        cursor.execute("SELECT chat_id, username, first_name, last_name FROM users")
        users = [dict(r) for r in cursor.fetchall()]
        
        # Blacklist
        cursor.execute("SELECT chat_id, ad_id FROM blacklisted_ads")
        blacklisted = [dict(r) for r in cursor.fetchall()]

        # Settings
        cursor.execute("SELECT key, value FROM settings")
        settings = {r["key"]: r["value"] for r in cursor.fetchall()}

        # Deleted queries
        cursor.execute("SELECT chat_id, name FROM deleted_queries")
        deleted_queries = [dict(r) for r in cursor.fetchall()]

        # Stop words
        cursor.execute("SELECT chat_id, word FROM stop_words")
        stop_words = [dict(r) for r in cursor.fetchall()]

        # Blacklisted sellers
        cursor.execute("SELECT chat_id, seller_name, account_id FROM blacklisted_sellers")
        blacklisted_sellers = [dict(r) for r in cursor.fetchall()]

        # Favorites
        cursor.execute(
            "SELECT chat_id, ad_id, title, price_byn, price_usd, ad_link, seller_name, image_url, "
            "COALESCE(status, '') as status, COALESCE(note, '') as note, COALESCE(is_closed, 0) as is_closed, created_at "
            "FROM favorites"
        )
        favorites = [dict(r) for r in cursor.fetchall()]

        return {
            "version": 1,
            "exported_at": datetime.now().isoformat(),
            "queries": queries,
            "users": users,
            "blacklisted_ads": blacklisted,
            "settings": settings,
            "deleted_queries": deleted_queries,
            "stop_words": stop_words,
            "blacklisted_sellers": blacklisted_sellers,
            "favorites": favorites
        }


def import_database_state(state: Dict[str, Any], db_path: Optional[str] = None) -> Dict[str, int]:
    """Imports queries, users, and blacklisted ads from a state dictionary."""
    init_db(db_path)
    imported_queries = 0
    imported_users = 0
    imported_bl = 0
    
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Import Users
        for u in state.get("users", []):
            cid = u.get("chat_id")
            if cid:
                cursor.execute("SELECT 1 FROM users WHERE chat_id = ?", (cid,))
                if not cursor.fetchone():
                    cursor.execute(
                        "INSERT INTO users (chat_id, username, first_name, last_name, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (cid, u.get("username"), u.get("first_name"), u.get("last_name"))
                    )
                    imported_users += 1

        # Import Deleted Queries
        for d in state.get("deleted_queries", []):
            cid = d.get("chat_id")
            name = d.get("name")
            if cid and name:
                cursor.execute(
                    "INSERT OR REPLACE INTO deleted_queries (chat_id, name) VALUES (?, ?)",
                    (cid, name)
                )

        cursor.execute("SELECT chat_id, name FROM deleted_queries")
        deleted_set = {(r["chat_id"], r["name"]) for r in cursor.fetchall()}

        # Import Queries
        for q in state.get("queries", []):
            cid = q.get("chat_id")
            name = q.get("name")
            params = q.get("params", {})
            meta = q.get("meta") if isinstance(q.get("meta"), dict) else {}
            if name in ("PlayStation 5 Slim", "PlayStation Slim"):
                continue
            if (cid, name) in deleted_set:
                continue
            if cid and name and params:
                cursor.execute(
                    "SELECT 1 FROM queries WHERE chat_id = ? AND name = ?",
                    (cid, name)
                )
                if not cursor.fetchone():
                    cursor.execute(
                        "INSERT INTO queries (chat_id, name, params_json, meta_json, is_active) VALUES (?, ?, ?, ?, ?)",
                        (cid, name, json.dumps(params), json.dumps(meta), q.get("is_active", 1))
                    )
                    imported_queries += 1
                else:
                    cursor.execute(
                        "UPDATE queries SET is_active = ?, meta_json = COALESCE(NULLIF(?, '{}'), meta_json) WHERE chat_id = ? AND name = ?",
                        (q.get("is_active", 1), json.dumps(meta), cid, name)
                    )

        # Import Blacklist
        for b in state.get("blacklisted_ads", []):
            cid = b.get("chat_id")
            ad_id = b.get("ad_id")
            if cid and ad_id:
                cursor.execute(
                    "INSERT OR IGNORE INTO blacklisted_ads (chat_id, ad_id) VALUES (?, ?)",
                    (cid, int(ad_id))
                )
                imported_bl += 1

        # Import Stop Words
        for sw in state.get("stop_words", []):
            cid = sw.get("chat_id")
            w = sw.get("word")
            if cid and w:
                cursor.execute(
                    "INSERT OR IGNORE INTO stop_words (chat_id, word) VALUES (?, ?)",
                    (cid, w.strip().lower())
                )

        # Import Blacklisted Sellers
        for bs in state.get("blacklisted_sellers", []):
            cid = bs.get("chat_id")
            sname = bs.get("seller_name")
            acc_id = bs.get("account_id")
            if cid and sname:
                cursor.execute(
                    "INSERT OR REPLACE INTO blacklisted_sellers (chat_id, seller_name, account_id) VALUES (?, ?, ?)",
                    (cid, sname.strip(), acc_id)
                )

        # Import Settings
        for k, v in state.get("settings", {}).items():
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))

        # Import Favorites
        imported_favs = 0
        for f in state.get("favorites", []):
            cid = f.get("chat_id")
            ad_id = f.get("ad_id")
            if cid and ad_id:
                cursor.execute(
                    "INSERT OR REPLACE INTO favorites "
                    "(chat_id, ad_id, title, price_byn, price_usd, ad_link, seller_name, image_url, status, note, is_closed, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                    (
                        cid,
                        int(ad_id),
                        f.get("title", ""),
                        f.get("price_byn"),
                        f.get("price_usd"),
                        f.get("ad_link", ""),
                        f.get("seller_name", ""),
                        f.get("image_url", ""),
                        f.get("status", ""),
                        f.get("note", ""),
                        int(f.get("is_closed") or 0),
                        f.get("created_at"),
                    )
                )
                imported_favs += 1

        conn.commit()
        
    return {
        "queries": imported_queries,
        "users": imported_users,
        "blacklisted": imported_bl,
        "favorites": imported_favs
    }


def cleanup_legacy_queries(db_path: Optional[str] = None):
    """Purges any legacy starter queries (e.g. PlayStation 5 Slim) from the database."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM queries WHERE name IN ('PlayStation 5 Slim', 'PlayStation Slim')")
        cursor.execute("INSERT OR REPLACE INTO deleted_queries (chat_id, name) VALUES (747868993, 'PlayStation 5 Slim')")
        conn.commit()


