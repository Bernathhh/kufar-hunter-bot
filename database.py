"""
SQLite Database manager for Kufar Hunter Bot.
Stores tracking queries and seen advertisements to avoid duplicates.
"""
import sqlite3
import json
import os
from datetime import datetime
from typing import List, Dict, Optional, Any, Set

DB_FILE = os.path.join(os.path.dirname(__file__), "kufar_hunter.db")

def get_connection(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str = DB_FILE):
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
        
        conn.commit()


def set_setting(key: str, value: str, db_path: str = DB_FILE):
    """Sets a persistent key-value setting."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()


def get_setting(key: str, default: Optional[str] = None, db_path: str = DB_FILE) -> Optional[str]:
    """Retrieves a persistent setting value."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default


def add_query(chat_id: int, name: str, params: Dict[str, Any], db_path: str = DB_FILE) -> int:
    """Adds a new search query to track and unmarks it from deleted_queries if present."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM deleted_queries WHERE chat_id = ? AND name = ?", (chat_id, name))
        cursor.execute(
            "INSERT INTO queries (chat_id, name, params_json, is_active) VALUES (?, ?, ?, 1)",
            (chat_id, name, json.dumps(params))
        )
        conn.commit()
        return cursor.lastrowid


def get_active_queries(db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Returns all active tracking queries across all chats."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, chat_id, name, params_json, created_at FROM queries WHERE is_active = 1")
        rows = cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "chat_id": r["chat_id"],
                "name": r["name"],
                "params": json.loads(r["params_json"]),
                "created_at": r["created_at"]
            })
        return result


def get_user_queries(chat_id: int, db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Returns all queries created by a specific user/chat."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, chat_id, name, params_json, is_active, created_at FROM queries WHERE chat_id = ? ORDER BY id DESC", (chat_id,))
        rows = cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "chat_id": r["chat_id"],
                "name": r["name"],
                "params": json.loads(r["params_json"]),
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"]
            })
        return result


def delete_query(query_id: int, chat_id: Optional[int] = None, db_path: str = DB_FILE) -> bool:
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


def mark_query_deleted(chat_id: int, name: str, db_path: str = DB_FILE):
    """Records that a query was explicitly deleted by user to prevent resurrection on restart."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO deleted_queries (chat_id, name) VALUES (?, ?)",
            (chat_id, name)
        )
        conn.commit()


def get_deleted_queries(chat_id: int, db_path: str = DB_FILE) -> Set[str]:
    """Returns set of query names that were deleted by user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM deleted_queries WHERE chat_id = ?", (chat_id,))
        return {r["name"] for r in cursor.fetchall()}


def is_ad_seen(query_id: int, ad_id: int, db_path: str = DB_FILE) -> bool:
    """Checks if an ad has already been recorded for this query."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_ads WHERE query_id = ? AND ad_id = ?", (query_id, ad_id))
        return cursor.fetchone() is not None


def get_ad_seen_price(query_id: int, ad_id: int, db_path: str = DB_FILE) -> Tuple[bool, Optional[float], Optional[float]]:
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
    db_path: str = DB_FILE
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


def mark_ads_seen(query_id: int, ad_ids: List[int], db_path: str = DB_FILE):
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
    db_path: str = DB_FILE
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


def get_query_seen_count(query_id: int, db_path: str = DB_FILE) -> int:
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
    db_path: str = DB_FILE
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


def get_all_users(db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Returns all registered users."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, username, first_name, last_name, first_seen, last_seen FROM users ORDER BY last_seen DESC")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_user_count(db_path: str = DB_FILE) -> int:
    """Returns total number of registered users."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM users")
        row = cursor.fetchone()
        return row["cnt"] if row else 0


# ==================== BLACKLIST (ЧС / НЕ ИНТЕРЕСНО) ====================

def add_to_blacklist(chat_id: int, ad_id: int, db_path: str = DB_FILE) -> bool:
    """Adds an ad_id to user's blacklist so it is hidden and never shown again."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO blacklisted_ads (chat_id, ad_id) VALUES (?, ?)",
            (chat_id, int(ad_id))
        )
        conn.commit()
        return cursor.rowcount > 0


def remove_from_blacklist(chat_id: int, ad_id: int, db_path: str = DB_FILE) -> bool:
    """Removes an ad_id from user's blacklist."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM blacklisted_ads WHERE chat_id = ? AND ad_id = ?",
            (chat_id, int(ad_id))
        )
        conn.commit()
        return cursor.rowcount > 0


def is_ad_blacklisted(chat_id: int, ad_id: int, db_path: str = DB_FILE) -> bool:
    """Checks if an ad_id is in user's blacklist."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM blacklisted_ads WHERE chat_id = ? AND ad_id = ?",
            (chat_id, int(ad_id))
        )
        return cursor.fetchone() is not None


def get_user_blacklist(chat_id: int, db_path: str = DB_FILE) -> Set[int]:
    """Returns a set of ad_ids currently blacklisted by user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT ad_id FROM blacklisted_ads WHERE chat_id = ?", (chat_id,))
        return {r["ad_id"] for r in cursor.fetchall()}


def get_blacklist_count(chat_id: int, db_path: str = DB_FILE) -> int:
    """Returns total blacklisted ads count for user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM blacklisted_ads WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row["cnt"] if row else 0


def clear_user_blacklist(chat_id: int, db_path: str = DB_FILE) -> int:
    """Clears all blacklisted ads for a user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM blacklisted_ads WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cursor.rowcount


# ==================== STOP WORDS (МИНУС-СЛОВА) ====================

def add_stop_word(chat_id: int, word: str, db_path: str = DB_FILE) -> bool:
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


def remove_stop_word(chat_id: int, word: str, db_path: str = DB_FILE) -> bool:
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


def get_stop_words(chat_id: int, db_path: str = DB_FILE) -> List[str]:
    """Returns list of stop words for a user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM stop_words WHERE chat_id = ? ORDER BY word ASC", (chat_id,))
        return [r["word"] for r in cursor.fetchall()]


def clear_stop_words(chat_id: int, db_path: str = DB_FILE) -> int:
    """Clears all stop words for a user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stop_words WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cursor.rowcount


# ==================== BLACKLISTED SELLERS (ЧС ПРОДАВЦОВ) ====================

def add_blacklisted_seller(chat_id: int, seller_name: str, account_id: Optional[str] = None, db_path: str = DB_FILE) -> bool:
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


def remove_blacklisted_seller(chat_id: int, seller_name: str, db_path: str = DB_FILE) -> bool:
    """Removes a seller from user's blacklisted sellers list."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM blacklisted_sellers WHERE chat_id = ? AND seller_name = ?",
            (chat_id, seller_name.strip())
        )
        conn.commit()
        return cursor.rowcount > 0


def get_blacklisted_sellers(chat_id: int, db_path: str = DB_FILE) -> Set[str]:
    """Returns set of blacklisted seller names for user."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT seller_name FROM blacklisted_sellers WHERE chat_id = ?", (chat_id,))
        return {r["seller_name"] for r in cursor.fetchall()}


def is_seller_blacklisted(chat_id: int, seller_name: str, account_id: Optional[str] = None, db_path: str = DB_FILE) -> bool:
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

def get_user_template(chat_id: int, db_path: str = DB_FILE) -> str:
    """Returns custom message template for contacting sellers, or default."""
    return get_setting(f"template_{chat_id}", DEFAULT_CONTACT_TEMPLATE, db_path=db_path)


def set_user_template(chat_id: int, template: str, db_path: str = DB_FILE):
    """Saves custom message template for contacting sellers."""
    set_setting(f"template_{chat_id}", template.strip(), db_path=db_path)


# ==================== CLOUD STATE BACKUP & RESTORE ====================

def export_database_state(db_path: str = DB_FILE) -> Dict[str, Any]:
    """Exports all queries, users, and blacklisted ads to a dictionary."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Queries
        cursor.execute("SELECT chat_id, name, params_json, is_active FROM queries")
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

        return {
            "version": 1,
            "exported_at": datetime.now().isoformat(),
            "queries": queries,
            "users": users,
            "blacklisted_ads": blacklisted,
            "settings": settings,
            "deleted_queries": deleted_queries,
            "stop_words": stop_words,
            "blacklisted_sellers": blacklisted_sellers
        }


def import_database_state(state: Dict[str, Any], db_path: str = DB_FILE) -> Dict[str, int]:
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
            if (cid, name) in deleted_set:
                continue
            if cid and name and params:
                cursor.execute(
                    "SELECT 1 FROM queries WHERE chat_id = ? AND name = ?",
                    (cid, name)
                )
                if not cursor.fetchone():
                    cursor.execute(
                        "INSERT INTO queries (chat_id, name, params_json, is_active) VALUES (?, ?, ?, ?)",
                        (cid, name, json.dumps(params), q.get("is_active", 1))
                    )
                    imported_queries += 1

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

        conn.commit()
        
    return {
        "queries": imported_queries,
        "users": imported_users,
        "blacklisted": imported_bl
    }

