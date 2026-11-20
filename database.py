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
