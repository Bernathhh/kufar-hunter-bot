"""
SQLite Database manager for Kufar Hunter Bot.
Stores tracking queries and seen advertisements to avoid duplicates.
"""
import sqlite3
import json
import os
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
        
        # Seen ads table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen_ads (
                ad_id INTEGER NOT NULL,
                query_id INTEGER NOT NULL,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ad_id, query_id),
                FOREIGN KEY (query_id) REFERENCES queries(id) ON DELETE CASCADE
            )
        """)
        
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
        
        conn.commit()


def add_query(chat_id: int, name: str, params: Dict[str, Any], db_path: str = DB_FILE) -> int:
    """Adds a new search query to track."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
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
    """Deletes a query by ID (optionally verifying chat_id)."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        if chat_id is not None:
            cursor.execute("DELETE FROM queries WHERE id = ? AND chat_id = ?", (query_id, chat_id))
        else:
            cursor.execute("DELETE FROM queries WHERE id = ?", (query_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            cursor.execute("DELETE FROM seen_ads WHERE query_id = ?", (query_id,))
        conn.commit()
        return deleted


def is_ad_seen(query_id: int, ad_id: int, db_path: str = DB_FILE) -> bool:
    """Checks if an ad has already been recorded for this query."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_ads WHERE query_id = ? AND ad_id = ?", (query_id, ad_id))
        return cursor.fetchone() is not None


def mark_ads_seen(query_id: int, ad_ids: List[int], db_path: str = DB_FILE):
    """Batch marks ad IDs as seen."""
    if not ad_ids:
        return
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        records = [(ad_id, query_id) for ad_id in ad_ids]
        cursor.executemany("INSERT OR IGNORE INTO seen_ads (ad_id, query_id) VALUES (?, ?)", records)
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

