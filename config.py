"""
Configuration loader for Kufar Hunter Bot.
Loads settings from config.json or environment variables.
"""
import os
import json
from typing import List, Optional

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN_HERE",
    "admin_chat_id": 0,
    "check_interval_seconds": 60,
    "initial_queries": []
}

def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    
    # Load from config.json if exists
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_config = json.load(f)
                config.update(user_config)
        except Exception as e:
            print(f"Warning: Could not read config.json ({e}), using defaults")
            
    # Override from environment variables if present
    env_token = os.getenv("BOT_TOKEN")
    if env_token:
        config["bot_token"] = env_token
        
    env_admin = os.getenv("ADMIN_CHAT_ID")
    if env_admin and env_admin.isdigit():
        config["admin_chat_id"] = int(env_admin)
        
    env_interval = os.getenv("CHECK_INTERVAL")
    if env_interval and env_interval.isdigit():
        config["check_interval_seconds"] = int(env_interval)

    # Initial starter queries are disabled - users manage their own searches
    config["initial_queries"] = []

    return config

def save_default_config_if_missing():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
