#!/usr/bin/env python3
"""
Kufar Hunter Telegram Bot.
Interactive button-driven UI mirroring Kufar filters 1-in-1.
Private access restricted to the configured admin chat ID.
"""
import sys
import os
import time
import logging
import threading
import concurrent.futures
import html
import json
import http.server
import socketserver
import re
import statistics
import signal
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set

# Setup path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import load_config
from database import (
    init_db,
    add_query,
    get_active_queries,
    get_user_queries,
    delete_query,
    is_ad_seen,
    get_ad_seen_price,
    mark_ad_seen,
    mark_ads_seen,
    mark_ads_seen_batch,
    get_query_seen_count,
    register_or_update_user,
    get_all_users,
    get_user_count,
    add_to_blacklist,
    remove_from_blacklist,
    is_ad_blacklisted,
    get_user_blacklist,
    get_blacklist_count,
    clear_user_blacklist,
    get_setting,
    set_setting,
    export_database_state,
    import_database_state,
    add_stop_word,
    remove_stop_word,
    get_stop_words,
    clear_stop_words,
    add_blacklisted_seller,
    remove_blacklisted_seller,
    get_blacklisted_sellers,
    is_seller_blacklisted,
    get_user_template,
    set_user_template,
    DEFAULT_CONTACT_TEMPLATE,
    cleanup_legacy_queries,
    add_favorite,
    remove_favorite,
    is_favorite,
    get_user_favorites,
    get_favorites_count,
    clear_user_favorites,
    get_silent_mode,
    set_silent_mode,
    is_in_silent_hours,
    get_user_fast_alert,
    set_user_fast_alert,
    get_user_priority_mode,
    set_user_priority_mode,
    set_user_monitoring_paused,
    is_user_monitoring_paused,
    toggle_query_active,
    update_favorite_price_state,
    get_all_favorites,
    record_price_history,
    get_admin_users_overview,
)
from kufar_api import (
    parse_kufar_url_or_query,
    fetch_ads,
    fetch_ad_details,
    fetch_ad_description,
    fetch_ad_photos,
    fetch_ad_live_state,
    compute_market_analysis,
    extract_phone_from_text,
    fetch_ad_phone_with_token
)
from telegram_api import TelegramClient
from webapp_ui import create_http_handler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("kufar_bot")

START_TIME = datetime.now()
TOTAL_CHECKS = 0
NEW_ADS_FOUND = 0
FORCE_CHECK_EVENT = threading.Event()
STOP_EVENT = threading.Event()

# Timestamp of last saved backup & scheduled cloud sync settings
LAST_BACKUP_TIME: Optional[datetime] = None
BACKUP_FILE = os.path.join(os.path.dirname(__file__), "backup_state.json")
BACKUP_DIRTY: bool = False
BACKUP_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()
BACKUP_SCHEDULES: Dict[str, List[str]] = {
    "1": ["09:00"],
    "2": ["09:00", "21:00"],
    "3": ["09:00", "15:00", "21:00"],
    "4": ["03:00", "09:00", "15:00", "21:00"],
    "off": [],
}

# Global clients for automatic background state syncing
GLOBAL_TG: Optional[TelegramClient] = None
GLOBAL_CONFIG: Dict[str, Any] = {}

# Photos cache for on-demand gallery viewing (ad_id -> list of urls)
AD_PHOTOS_CACHE: Dict[int, List[str]] = {}

# Full ad data cache for fast callbacks (seller blacklist, contact template, etc.)
AD_DATA_CACHE: Dict[int, Dict[str, Any]] = {}

# Map of (chat_id, ad_id) -> list of Telegram message IDs sent for this ad
AD_MESSAGES_MAP: Dict[Tuple[int, int], List[int]] = {}

# User session state for interactive wizard (chat_id -> dict)
USER_STATES: Dict[int, Dict[str, Any]] = {}
PENDING_QUERIES: Dict[str, Dict[str, Any]] = {}
STOP_OUTPUT_REQUESTED: Set[int] = set()
ACTIVE_BATCH_CHATS: Set[int] = set()

# Recent API health / cycle events for Admin Status dashboard
API_HEALTH_EVENTS: List[Dict[str, str]] = []
LAST_PH_SNAPSHOT: Dict[int, float] = {}
LAST_FAVS_TRACK_TS: float = 0.0


def record_api_event(level: str, message: str):
    """Records a compact timestamped runtime/API event for admin monitoring."""
    minsk_tz = timezone(timedelta(hours=3))
    ts = datetime.now(minsk_tz).strftime("%H:%M:%S")
    with STATE_LOCK:
        API_HEALTH_EVENTS.append({"time": ts, "level": level, "message": message[:160]})
        if len(API_HEALTH_EVENTS) > 25:
            del API_HEALTH_EVENTS[:-25]


def _delete_ad_messages(tg: TelegramClient, chat_id: int, ad_id: int, fallback_msg_id: Optional[int] = None):
    """Thread-safely pops tracked Telegram message IDs for an ad and deletes them."""
    with STATE_LOCK:
        mids = list(AD_MESSAGES_MAP.pop((chat_id, ad_id), []))
    if fallback_msg_id and fallback_msg_id not in mids:
        mids.append(fallback_msg_id)
    for m in mids:
        try:
            tg.delete_message(chat_id, m)
        except Exception as e:
            logger.debug(f"Could not delete message {m}: {e}")


def evaluate_query_meta_for_ad(
    ad: Dict[str, Any],
    meta: Optional[Dict[str, Any]],
    market_median: Optional[float] = None,
    description: str = ""
) -> Tuple[bool, List[str]]:
    """
    Applies per-query meta rules (private_only, local stop_words, only_bargain_pct)
    and returns (allowed, matched_plus_words).
    """
    if not meta:
        return True, []

    if meta.get("private_only") and ad.get("company_ad"):
        return False, []

    text = f"{ad.get('subject', '')} {description or ad.get('description', '')}".lower()
    local_stops = [str(w).strip().lower() for w in (meta.get("stop_words") or []) if str(w).strip()]
    if local_stops and any(sw in text for sw in local_stops):
        return False, []

    min_bargain = int(meta.get("only_bargain_pct") or 0)
    price_byn = ad.get("price_byn")
    if min_bargain > 0:
        if not market_median or not price_byn or price_byn <= 0:
            return False, []
        diff_pct = ((price_byn - market_median) / market_median) * 100.0
        if diff_pct > -min_bargain:
            return False, []

    plus_words = [str(w).strip().lower() for w in (meta.get("plus_words") or []) if str(w).strip()]
    matched_plus = [pw for pw in plus_words if pw in text]
    return True, matched_plus

CATEGORIES = [
    ("5040", "Приставки и игры"),
    ("17010", "Мобильные телефоны"),
    ("16000", "Компьютерная техника"),
    ("16040", "Ноутбуки"),
    ("5000", "Электроника"),
    ("15000", "Бытовая техника"),
    ("2000", "Авто и запчасти"),
    ("1000", "Недвижимость"),
    ("21000", "Мебель и интерьер"),
    ("4000", "Хобби, спорт, туризм"),
    ("all", "Все категории")
]

REGIONS = [
    ("7", "Минск"),
    ("5", "Минская обл."),
    ("1", "Брестская обл."),
    ("2", "Гомельская обл."),
    ("3", "Гродненская обл."),
    ("4", "Могилевская обл."),
    ("6", "Витебская обл."),
    ("all", "Вся Беларусь")
]

def get_webapp_url() -> Optional[str]:
    """Returns HTTPS Mini App URL if configured or running on Render."""
    base = (
        os.getenv("WEBAPP_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or GLOBAL_CONFIG.get("webapp_url")
        or ""
    ).strip()
    if base.startswith("https://"):
        return base.rstrip("/") + "/webapp"
    return None


def get_main_keyboard(chat_id: int = 0) -> Optional[Dict[str, Any]]:
    """Legacy reply keyboard removed in favor of inline Dashboard Hub & Mini App."""
    return None


MAIN_KEYBOARD = None


def build_personalized_message(chat_id: int, ad: Dict[str, Any]) -> str:
    """Renders user's contact template with seller name, item subject, and price."""
    tpl = get_user_template(chat_id)
    seller_raw = (ad.get("seller") or "").strip()
    subject_raw = (ad.get("subject") or "").strip()
    price_byn = ad.get("price_byn")
    price_str = f"{price_byn:,.0f} BYN".replace(",", " ") if price_byn else ""

    msg = tpl
    if "{name}" in msg:
        msg = msg.replace("{name}", seller_raw if seller_raw else "продавец")
    if "{item}" in msg:
        msg = msg.replace("{item}", subject_raw if subject_raw else "товар")
    if "{price}" in msg:
        msg = msg.replace("{price}", price_str)
    return msg.strip()


def make_ad_keyboard(
    ad_link: str,
    ad_id: int,
    photo_count: int = 0,
    seller_name: Optional[str] = None,
    is_blacklisted: bool = False,
    is_fav: bool = False,
    copy_msg: Optional[str] = None,
    phone_number: Optional[str] = None
) -> Dict[str, Any]:
    """Builds compact single-screen action keyboard with 1-tap text/phone copy and Kufar link."""
    fav_text = "★ Убрать" if is_fav else "☆ В избр."
    fav_cb = f"cb:unfav:{ad_id}" if is_fav else f"cb:fav:{ad_id}"
    bl_text = "↩ Вернуть" if is_blacklisted else "× Скрыть"
    bl_cb = f"cb:unbl:{ad_id}" if is_blacklisted else f"cb:bl:{ad_id}"

    rows: List[List[Dict[str, Any]]] = [
        [
            {"text": "Открыть / Написать ↗", "url": ad_link},
            (
                {"text": "Скопировать текст", "copy_text": {"text": copy_msg[:256]}}
                if copy_msg else
                {"text": "Написать", "callback_data": f"cb:contact:{ad_id}"}
            ),
        ]
    ]
    if phone_number:
        rows.append([
            {"text": f"Скопировать тел: {phone_number}", "copy_text": {"text": phone_number[:256]}}
        ])
    rows.append([
        {"text": fav_text, "callback_data": fav_cb},
        {"text": bl_text, "callback_data": bl_cb},
        {"text": "Бан автора", "callback_data": f"cb:bl_seller:{ad_id}"},
    ])
    return {"inline_keyboard": rows}


def calculate_market_stats(ads: List[Dict[str, Any]]) -> Optional[float]:
    """Calculates median market price across current search results."""
    prices = [a["price_byn"] for a in ads if a.get("price_byn") and a["price_byn"] > 0]
    if len(prices) >= 3:
        try:
            return round(float(statistics.median(prices)), 2)
        except Exception:
            return None
    return None


def get_bargain_badge(price_byn: Optional[float], median_byn: Optional[float]) -> Optional[str]:
    """Returns clean market price badge comparing ad price to market median."""
    if price_byn is None or median_byn is None or median_byn <= 0:
        return None
    diff_ratio = (price_byn - median_byn) / median_byn
    med_str = f"{median_byn:,.0f} BYN".replace(",", " ")
    if diff_ratio <= -0.18:
        pct = round(abs(diff_ratio) * 100)
        return f"<b>Выгодно: -{pct}% от рынка</b> (медиана ~{med_str})"
    elif diff_ratio <= -0.07:
        pct = round(abs(diff_ratio) * 100)
        return f"<b>Ниже рынка: -{pct}%</b> (медиана ~{med_str})"
    elif diff_ratio >= 0.20:
        pct = round(diff_ratio * 100)
        return f"<b>Выше рынка: +{pct}%</b> (медиана ~{med_str})"
    elif diff_ratio >= 0.08:
        pct = round(diff_ratio * 100)
        return f"Чуть выше рынка (+{pct}%, медиана ~{med_str})"
    else:
        return f"В рынке (медиана ~{med_str})"


def extract_smart_badges(subject: str, description: Optional[str]) -> List[str]:
    """
    Extracts key selling points, equipment and nuances from title and description cleanly.
    """
    text = (subject + " " + (description or "")).lower()
    badges = []

    if re.search(r'(2\s*(геймпада|джойстика|контроллера|dualsense|dualshock)|два\s*(геймпада|джойстика|dualsense)|пара\s*(геймпадов|джойстиков)|\+2-й\s*геймпад|\+второй\s*(геймпад|джойстик))', text):
        badges.append("2 геймпада")
    elif re.search(r'(3\s*(геймпада|джойстика|контроллера)|три\s*(геймпада|джойстика))', text):
        badges.append("3 геймпада")
    elif re.search(r'(геймпад|джойстик|dualsense|dualshock)', text):
        badges.append("Геймпад")

    if re.search(r'(с\s+дисковод|дисковод|disc\s*edition|disk\s*edition|blu-?ray|дисковая\s+версия)', text) and not re.search(r'(без\s+дисковод|дисковод\s+отдельно)', text):
        badges.append("С дисководом")
    elif re.search(r'(digital\s*edition|диджитал|без\s+дисковод|digital\s*версия|без\s+привода)', text):
        badges.append("Digital")

    if re.search(r'(полный\s+комплект|родная\s+коробка|заводская\s+коробка|в\s+коробке|коробка\s+есть|есть\s+коробка|full\s*set)', text):
        badges.append("Полный комплект")

    if re.search(r'(\d+\s*(диска|дисков|игры|игр)|диски\s+в\s+комплекте|игры\s+в\s+комплекте|отдам\s+(диски|игры)|игры\s+на\s+аккаунт|аккаунт\s+с\s+играми)', text):
        badges.append("Игры в комплекте")

    if re.search(r'(на\s+гаранти|официальная\s+гарантия|гарантийный\s+талон|чек\s+о\s+покупке|есть\s+чек|чек\s+и\s+гарантия|действует\s+гарантия|куплен\s+в\s+(5\s*элемент|электросил|21\s*век))', text):
        badges.append("На гарантии")

    if re.search(r'(японк|япония|cfi-?\d+00|japan|\(jp\))', text):
        badges.append("Япония")
    elif re.search(r'(европ|европейка|евротест|cfi-?\d+16|europe|\(eu\))', text):
        badges.append("Европа")
    elif re.search(r'(китай|китаянка|бэкап|cfi-?\d+09|china|\(cn\))', text):
        badges.append("Китай (бэкап)")

    if re.search(r'(прошит|прошивка|jailbreak|hen|goldhen)', text):
        badges.append("Прошита")

    has_defect_word = re.search(r'(дефект|нюанс|не\s+работает|дрифт|дрифтит|шумит|греется|царапин|трещин|скол|глючит)', text)
    if has_defect_word:
        is_negated = re.search(r'(без\s+(дефектов|нюансов|царапин|сколов|проблем|глюков)|не\s+(шумит|греется|глючит|дрифтит)|отличное\s+состояние|в\s+идеале)', text)
        if not is_negated:
            badges.append("Есть нюансы")

    return badges


class ReusableThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_health_server(port: int):
    try:
        app_ctx = {
            "tg": GLOBAL_TG,
            "config": GLOBAL_CONFIG,
            "start_time": START_TIME,
            "get_counters": lambda: (TOTAL_CHECKS, NEW_ADS_FOUND),
            "sync_backup": save_local_backup,
            "trigger_force_check": lambda: FORCE_CHECK_EVENT.set(),
            "send_query_ads_to_chat": lambda cid, qid: output_ads_for_query(GLOBAL_TG, cid, qid, background=True) if GLOBAL_TG else None,
            "send_all_ads_to_chat": lambda cid: output_all_user_ads(GLOBAL_TG, cid, background=True) if GLOBAL_TG else None,
            "send_single_ad_to_chat": lambda cid, ad, qn, med: send_ad_notification(GLOBAL_TG, cid, ad, qn, market_median=med) if GLOBAL_TG else False,
            "get_api_events": lambda: list(reversed(API_HEALTH_EVENTS)),
            "sync_cloud_backup_now": lambda: sync_backup_to_telegram(GLOBAL_TG, GLOBAL_CONFIG, force_notify=False) if (GLOBAL_TG and GLOBAL_CONFIG) else None,
            "evaluate_query_meta": evaluate_query_meta_for_ad,
            "categories": CATEGORIES,
            "regions": REGIONS,
        }
        handler_cls = create_http_handler(app_ctx)
        server = ReusableThreadingTCPServer(("0.0.0.0", port), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Mini App & Health HTTP server running on port {port}")
    except Exception as e:
        logger.warning(f"Could not start HTTP server on port {port}: {e}")


def keep_alive_loop():
    """Pings the public Render URL every 10 minutes to prevent Render Free Tier from sleeping."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    health_url = url.rstrip("/") + "/health"
    logger.info(f"Keep-alive background pinger started for {health_url}")
    time.sleep(60)  # Wait 1 minute after startup before first ping
    while not STOP_EVENT.is_set():
        try:
            req = urllib.request.Request(
                health_url,
                headers={"User-Agent": "KufarHunterKeepAlive/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                logger.debug(f"Keep-alive ping to {health_url} succeeded (status: {resp.status})")
        except Exception as e:
            logger.debug(f"Keep-alive ping error to {health_url}: {e}")
        time.sleep(600)  # Ping every 10 minutes (Render sleep threshold is 15 minutes)


def start_keep_alive_pinger():
    """Starts background thread to ping Render public URL and prevent spin-down."""
    if os.getenv("RENDER_EXTERNAL_URL"):
        thread = threading.Thread(target=keep_alive_loop, daemon=True)
        thread.start()


def parse_price_range(prc_param: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """
    Parses Kufar 'prc' parameter, e.g. 'r:100000,210000' -> (1000.0, 2100.0).
    Values in prc are in kopecks (x100).
    """
    if not prc_param or not str(prc_param).startswith("r:"):
        return None, None
    raw = str(prc_param)[2:]
    parts = raw.split(",")
    if len(parts) != 2:
        return None, None
    try:
        min_p = float(parts[0]) / 100.0 if parts[0] and parts[0] not in ("0", "") else None
        max_p = float(parts[1]) / 100.0 if parts[1] and parts[1] not in ("999999999", "0", "") else None
        return min_p, max_p
    except Exception:
        return None, None


def format_params_human(p: Dict[str, Any]) -> str:
    """Formats Kufar search parameters into clean, minimal Russian typography."""
    lines = []

    if p.get("query"):
        lines.append(f"<b>Ключевые слова:</b> <code>{html.escape(p['query'])}</code>")
    else:
        lines.append("<b>Ключевые слова:</b> <i>все в категории</i>")

    cat_id = str(p.get("cat", ""))
    cat_name = dict(CATEGORIES).get(cat_id, f"Код {cat_id}" if cat_id else "Все категории")
    cte_map = {"1": "Приставки", "2": "Игры", "3": "Аксессуары"}
    if str(p.get("cte", "")) in cte_map:
        cat_name += f" ({cte_map[str(p['cte'])]})"
    lines.append(f"<b>Категория:</b> {cat_name}")

    pb_map = {
        "5": "Apple", "24": "Samsung", "35": "Xiaomi",
        "11": "Huawei", "12": "Honor", "9": "Google", "20": "OnePlus", "22": "Realme"
    }
    cbc_map = {
        "31": "Sony", "13": "Microsoft Xbox", "17": "Nintendo",
        "30": "Steam Deck", "27": "Sega", "36": "Anbernic", "14": "Lenovo", "16": "MSI"
    }
    brand = p.get("brand") or cbc_map.get(str(p.get("cbc", ""))) or pb_map.get(str(p.get("pb", ""))) or ""
    if brand:
        lines.append(f"<b>Производитель:</b> {html.escape(str(brand))}")

    cnd_map = {"1": "Б/у", "2": "Новое"}
    if str(p.get("cnd", "")) in cnd_map:
        lines.append(f"<b>Состояние:</b> {cnd_map[str(p['cnd'])]}")

    cmp_map = {"0": "Частные лица", "1": "Компании"}
    if str(p.get("cmp", "")) in cmp_map:
        lines.append(f"<b>Продавец:</b> {cmp_map[str(p['cmp'])]}")

    rgn_id = str(p.get("rgn", ""))
    rgn_name = dict(REGIONS).get(rgn_id, f"Регион {rgn_id}" if rgn_id else "Вся Беларусь")
    lines.append(f"<b>Регион:</b> {rgn_name}")

    if p.get("ar"):
        if "22,23,24,25,26,27,28,29,30" in str(p["ar"]):
            lines.append("<b>Районы:</b> Все 9 районов Минска")
        else:
            lines.append(f"<b>Районы:</b> <code>{html.escape(str(p['ar']))}</code>")

    if p.get("prc"):
        prc_str = str(p["prc"]).replace("r:", "")
        parts = prc_str.split(",")
        if len(parts) == 2:
            try:
                min_p = int(parts[0]) // 100 if parts[0] else 0
                max_p = int(parts[1]) // 100 if parts[1] else 0
                if min_p and max_p and max_p < 9000000:
                    lines.append(f"<b>Цена:</b> от <b>{min_p:,}</b> до <b>{max_p:,} BYN</b>".replace(",", " "))
                elif max_p and max_p < 9000000:
                    lines.append(f"<b>Цена:</b> до <b>{max_p:,} BYN</b>".replace(",", " "))
                elif min_p:
                    lines.append(f"<b>Цена:</b> от <b>{min_p:,} BYN</b>".replace(",", " "))
            except Exception:
                lines.append(f"<b>Цена:</b> <code>{html.escape(str(p['prc']))}</code>")
    else:
        lines.append("<b>Цена:</b> Любая")

    sort_map = {
        "lst.d": "Сначала новые",
        "lst.a": "Сначала старые",
        "prc.a": "Сначала дешёвые",
        "prc.d": "Сначала дорогие",
    }
    sort_val = str(p.get("sort", "lst.d"))
    lines.append(f"<b>Сортировка:</b> {sort_map.get(sort_val, sort_val)}")
    return "\n".join(lines)


def parse_ad_timing(iso_str: str) -> Dict[str, Any]:
    """Parses ad list_time and determines whether it is freshly published or reactivated."""
    if not iso_str:
        return {"header": "🔥 <b>Новое объявление на Куфаре!</b>", "time_str": "Только что", "is_repost": False, "note": ""}

    try:
        clean_iso = iso_str[:-1] + "+00:00" if iso_str.endswith("Z") else iso_str
        dt_utc = datetime.fromisoformat(clean_iso)
        minsk_tz = timezone(timedelta(hours=3))
        dt_minsk = dt_utc.astimezone(minsk_tz)
        now_minsk = datetime.now(minsk_tz)

        diff_sec = int((now_minsk - dt_minsk).total_seconds())

        # If creation date was more than 24 hours ago, it's an update / reactivation / price change
        is_repost = diff_sec > 86400

        if diff_sec < 60:
            ago = f"{max(0, diff_sec)} сек. назад"
        elif diff_sec < 3600:
            ago = f"{diff_sec // 60} мин. назад"
        elif diff_sec < 86400:
            ago = f"{diff_sec // 3600} ч. назад"
        else:
            days = diff_sec // 86400
            ago = f"{days} дн. назад"

        time_part = dt_minsk.strftime("%H:%M:%S")
        if dt_minsk.date() == now_minsk.date():
            res = f"Сегодня в {time_part}"
        elif (now_minsk.date() - dt_minsk.date()).days == 1:
            res = f"Вчера в {time_part}"
        else:
            res = dt_minsk.strftime("%d.%m в %H:%M:%S")

        full_time = f"{res} ({ago})" if ago else res

        if is_repost:
            header = "<b>ОБНОВЛЕНИЕ / ПОДНЯТО ИЗ АРХИВА</b>"
            note = "<i>Продавец поднял объявление, изменил цену или активировал из архива.</i>"
        else:
            header = "<b>НОВОЕ ОБЪЯВЛЕНИЕ</b>"
            note = ""

        return {
            "header": header,
            "time_str": full_time,
            "is_repost": is_repost,
            "note": note
        }
    except Exception:
        return {"header": "<b>НОВОЕ ОБЪЯВЛЕНИЕ</b>", "time_str": iso_str, "is_repost": False, "note": ""}


def normalize_by_phone(raw_phone: Optional[str]) -> Optional[str]:
    """Normalizes a Belarusian (or international) phone number to E.164 for 1-tap dialing."""
    if not raw_phone:
        return None
    raw_str = str(raw_phone).strip()
    digits = re.sub(r"\D", "", raw_str)
    if digits.startswith("375") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("80") and len(digits) == 11:
        return f"+375{digits[2:]}"
    if len(digits) == 9 and digits[:2] in ("25", "29", "33", "44", "17"):
        return f"+375{digits}"
    if raw_str.startswith("+") and 10 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def resolve_phone_info(ad: Dict[str, Any], description: str) -> Tuple[Optional[str], str]:
    """Extracts normalized clickable phone (if present) and returns (e164_phone, html_label)."""
    cached = ad.get("_resolved_phone")
    if cached:
        return cached, f"{html.escape(cached)} (нажмите для звонка)"

    direct_raw = ad.get("phone") or ad.get("phone_number")
    if direct_raw:
        norm = normalize_by_phone(direct_raw)
        if norm:
            ad["_resolved_phone"] = norm
            return norm, f"{html.escape(norm)} (нажмите для звонка)"

    phone_in_text = extract_phone_from_text(description)
    if phone_in_text:
        norm = normalize_by_phone(phone_in_text)
        if norm:
            ad["_resolved_phone"] = norm
            return norm, f"{html.escape(norm)} (нажмите для звонка)"

    kufar_token = os.getenv("KUFAR_BEARER_TOKEN")
    if kufar_token:
        direct_phone = fetch_ad_phone_with_token(ad["ad_id"], kufar_token)
        if direct_phone:
            norm = normalize_by_phone(direct_phone)
            if norm:
                ad["_resolved_phone"] = norm
                return norm, f"{html.escape(norm)} (нажмите для звонка)"

    link = ad.get("ad_link", "")
    if ad.get("phone_hidden") is False:
        return None, f"<a href=\"{link}\">Есть на Kufar (кнопка «Позвонить»)</a>"
    return None, "<i>Скрыт продавцом (только чат)</i>"


def resolve_phone_string(ad: Dict[str, Any], description: str) -> str:
    """Returns formatted HTML phone label for ad captions."""
    _, label = resolve_phone_info(ad, description)
    return label


def format_ad_caption(
    ad: Dict[str, Any],
    query_name: str,
    description: str = "",
    update_type: str = "new",
    old_price_byn: Optional[float] = None,
    new_price_byn: Optional[float] = None,
    market_median: Optional[float] = None,
    max_desc_len: int = 280
) -> str:
    """Creates a sleek, modern HTML caption for the ad, with structured blocks and elegant badges."""
    subject = html.escape(ad.get("subject", "Объявление"))
    link = ad.get("ad_link", "")
    price_byn = ad.get("price_byn")
    price_usd = ad.get("price_usd")
    timing = parse_ad_timing(ad.get("list_time", ""))
    phone_str = resolve_phone_string(ad, description)

    if price_byn is not None:
        price_str = f"<b>{price_byn:,.2f} BYN</b>".replace(",", " ")
        if price_usd is not None:
            price_str += f" <i>(~${price_usd:,.0f})</i>".replace(",", " ")
    else:
        price_str = "<b>Договорная / Бесплатно</b>"

    loc_parts = []
    if ad.get("region"):
        loc_parts.append(ad["region"])
    if ad.get("area") and ad["area"] != ad.get("region"):
        loc_parts.append(ad["area"])
    location = html.escape(", ".join(loc_parts)) if loc_parts else "Беларусь"

    # Seller inspection (Частное лицо vs Магазин / ИП)
    raw_seller = (ad.get("seller") or "").strip()
    is_company = ad.get("company_ad", False)
    seller_lower = raw_seller.lower()
    if is_company or any(tag in seller_lower for tag in ["ип ", "ооо", "магазин", "store", "shop", "сервис", "дилер"]):
        seller_label = f"{html.escape(raw_seller or 'Компания')} <i>(магазин)</i>"
    else:
        seller_label = f"{html.escape(raw_seller or 'Частное лицо')} <i>(частник)</i>"

    time_label = "Создано:" if timing["is_repost"] else "Опубликовано:"

    # Format header and price depending on whether this is a price update
    note_line = None
    if update_type == "price_change" and old_price_byn is not None and new_price_byn is not None:
        diff_byn = old_price_byn - new_price_byn
        if diff_byn > 0:
            pct = round((diff_byn / old_price_byn) * 100) if old_price_byn > 0 else 0
            header_text = f"<b>ЦЕНА СНИЖЕНА (-{diff_byn:,.0f} BYN)</b>\n────────────────────"
            price_line = (
                f"<b>{new_price_byn:,.2f} BYN</b>"
                + (f" <i>(~${price_usd:,.0f})</i>" if price_usd is not None else "")
                + f"  ·  <s>{old_price_byn:,.2f} BYN</s> (-{pct}%)"
            )
            note_line = "<i>Продавец снизил цену.</i>"
        else:
            raise_byn = abs(diff_byn)
            pct = round((raise_byn / old_price_byn) * 100) if old_price_byn > 0 else 0
            header_text = f"<b>ЦЕНА ВЫРОСЛА (+{raise_byn:,.0f} BYN)</b>\n────────────────────"
            price_line = (
                f"<b>{new_price_byn:,.2f} BYN</b>"
                + (f" <i>(~${price_usd:,.0f})</i>" if price_usd is not None else "")
                + f"  ·  <s>{old_price_byn:,.2f} BYN</s> (+{pct}%)"
            )
            note_line = "<i>Продавец поднял цену.</i>"
    else:
        header_text = f"{timing['header']}\n────────────────────"
        price_line = price_str
        note_line = timing.get("note")

    effective_price = new_price_byn if (update_type == "price_change" and new_price_byn is not None) else price_byn
    bargain_badge = get_bargain_badge(effective_price, market_median)
    if bargain_badge:
        price_line += f"\n{bargain_badge}"

    clean_tag = re.sub(r'[^a-zA-Zа-яА-Я0-9_]', '', query_name) or 'поиск'
    lines = [
        header_text,
        f"<b>{subject}</b>",
        price_line,
        "────────────────────",
        f"<b>Локация:</b> {location}",
        f"<b>Продавец:</b> {seller_label}",
        f"<b>{time_label}</b> {timing['time_str']}",
        f"<b>Телефон:</b> {phone_str}",
        f"<i>Поиск: #{html.escape(clean_tag)}</i>"
    ]
    if note_line:
        lines.append(f"\n{note_line}")

    smart_badges = extract_smart_badges(ad.get("subject", ""), description)
    if ad.get("_matched_plus"):
        lines.append("\n<b>Совпадение по словам:</b> " + ", ".join(html.escape(w) for w in ad["_matched_plus"]))
    if smart_badges:
        lines.append("\n<b>Комплект / детали:</b> " + " · ".join(smart_badges))

    if description and max_desc_len > 0:
        raw_desc = description.strip()
        if len(raw_desc) > max_desc_len:
            raw_desc = raw_desc[:max(0, max_desc_len - 3)].rstrip() + "..."
        clean_desc = html.escape(raw_desc)
        lines.append(f"\n<blockquote>{clean_desc}</blockquote>")

    return "\n".join(lines)


def _split_album_chunks(urls: List[str]) -> List[List[str]]:
    """
    Splits image URLs into Telegram-valid sendMediaGroup chunks (each chunk has 2..10 items,
    except when total is 1). Avoids a trailing 1-item chunk by borrowing 1 from previous chunk.
    """
    if len(urls) <= 10:
        return [urls]
    chunks: List[List[str]] = []
    idx = 0
    while idx < len(urls):
        remaining = len(urls) - idx
        if remaining == 11:
            chunks.append(urls[idx:idx + 9])
            idx += 9
        else:
            take = min(10, remaining)
            chunks.append(urls[idx:idx + take])
            idx += take
    return chunks


def _build_safe_photo_caption(
    ad: Dict[str, Any],
    query_name: str,
    description: str,
    update_type: str,
    old_price_byn: Optional[float],
    new_price_byn: Optional[float],
    market_median: Optional[float]
) -> str:
    """Builds HTML caption guaranteed <= 1020 chars without ever breaking open HTML tags."""
    for desc_limit in (280, 140, 60, 0):
        candidate = format_ad_caption(
            ad, query_name,
            description=description,
            update_type=update_type,
            old_price_byn=old_price_byn,
            new_price_byn=new_price_byn,
            market_median=market_median,
            max_desc_len=desc_limit
        )
        if len(candidate) <= 1020:
            return candidate
    # Fallback without HTML formatting if somehow header/title alone exceeded 1020 chars
    plain = re.sub(r'<[^>]+>', '', candidate)
    return html.escape(plain[:1000] + "...")


def _evaluate_priority(
    ad: Dict[str, Any],
    effective_price: Optional[float],
    market_median: Optional[float],
    update_type: str = "new",
    old_price_byn: Optional[float] = None,
    new_price_byn: Optional[float] = None
) -> bool:
    """Returns True if ad matches plus-words or offers strong discount vs market / previous price."""
    if ad.get("_matched_plus"):
        return True
    if market_median and market_median > 0 and effective_price and effective_price > 0:
        if ((effective_price - market_median) / market_median) <= -0.18:
            return True
    if update_type == "price_change" and old_price_byn and new_price_byn and old_price_byn > new_price_byn:
        if old_price_byn > 0 and ((old_price_byn - new_price_byn) / old_price_byn) >= 0.10:
            return True
    return False


def _should_disable_notification(
    chat_id: int,
    is_hot: bool,
    query_meta: Optional[Dict[str, Any]],
    priority_mode: str
) -> bool:
    """Computes Telegram disable_notification flag respecting quiet hours and priority override."""
    if is_hot and priority_mode in ("hot", "only_hot"):
        return False
    if priority_mode == "only_hot":
        return True
    return is_in_silent_hours(chat_id) or bool((query_meta or {}).get("silent_query"))


def _build_single_screen_action_text(
    ad: Dict[str, Any],
    photo_count: int,
    seller_name: str,
    phone_e164: Optional[str],
    personalized_msg: str,
    is_hot: bool = False,
    market_median: Optional[float] = None,
    update_type: str = "new",
    old_price_byn: Optional[float] = None,
    new_price_byn: Optional[float] = None
) -> str:
    """Builds compact single-screen action card with priority badge, price, seller, phone, and quick reply quote."""
    subj_short = (ad.get("subject") or "Объявление").strip()
    if len(subj_short) > 48:
        subj_short = subj_short[:45].rstrip() + "..."
    p_byn = new_price_byn if (update_type == "price_change" and new_price_byn is not None) else ad.get("price_byn")
    price_short = f" · <b>{p_byn:,.0f} BYN</b>".replace(",", " ") if p_byn is not None else ""
    if update_type == "price_change" and old_price_byn is not None and new_price_byn is not None:
        diff = old_price_byn - new_price_byn
        if diff > 0:
            price_short += f" <i>(-{diff:,.0f} BYN)</i>".replace(",", " ")

    bargain_str = ""
    if market_median and market_median > 0 and p_byn and p_byn > 0:
        pct = round(((p_byn - market_median) / market_median) * 100)
        if pct <= -18:
            bargain_str = f" · <b>{pct}% от рынка</b>"

    seller_disp = html.escape(seller_name or "Автор")
    ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad['ad_id']}")
    if phone_e164:
        contact_line = f"<b>Продавец:</b> {seller_disp} · <b>Тел:</b> {html.escape(phone_e164)}"
    elif ad.get("phone_hidden") is False:
        contact_line = f"<b>Продавец:</b> {seller_disp} · <a href=\"{html.escape(ad_link, quote=True)}\">телефон на Kufar</a>"
    else:
        contact_line = f"<b>Продавец:</b> {seller_disp} · только чат Kufar"

    prefix = "🔥 <b>ПРИОРИТЕТ</b> · " if is_hot else ""
    photo_badge = f" · <i>{photo_count} фото</i>" if photo_count else ""
    quote_msg = html.escape(personalized_msg[:180])
    return (
        f"{prefix}<b>{html.escape(subj_short)}</b>{price_short}{bargain_str}{photo_badge}\n"
        f"{contact_line}\n"
        f"<blockquote>{quote_msg}</blockquote>"
    )


def _deliver_fast_media(
    tg: TelegramClient,
    chat_id: int,
    ad: Dict[str, Any],
    query_name: str,
    image_urls: List[str],
    description: Optional[str],
    update_type: str,
    old_price_byn: Optional[float],
    new_price_byn: Optional[float],
    market_median: Optional[float],
    card_msg_id: Optional[int],
    had_phone: bool,
    personalized_msg: str,
    is_fav: bool
):
    """Enriches details/phone if missing and silently appends photo album after fast action card."""
    ad_id = ad["ad_id"]
    urls = list(image_urls)
    desc = description
    try:
        if desc is None:
            fetched_desc, fetched_photos = fetch_ad_details(ad_id, timeout=5)
            desc = fetched_desc
            ad["description"] = desc
            if fetched_photos:
                seen_u = set(urls)
                for u in fetched_photos:
                    if u not in seen_u:
                        seen_u.add(u)
                        urls.append(u)
                ad["image_urls"] = urls
            with STATE_LOCK:
                AD_DATA_CACHE[ad_id] = ad
                if urls:
                    AD_PHOTOS_CACHE[ad_id] = urls

            # If a phone was found in the delayed description, patch the fast card buttons in-place
            if not had_phone and card_msg_id:
                new_phone_e164, _ = resolve_phone_info(ad, desc or "")
                if new_phone_e164:
                    ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
                    updated_markup = make_ad_keyboard(
                        ad_link,
                        ad_id,
                        photo_count=len(urls),
                        seller_name=ad.get("seller", ""),
                        is_blacklisted=False,
                        is_fav=is_fav,
                        copy_msg=personalized_msg,
                        phone_number=new_phone_e164
                    )
                    try:
                        tg.edit_message_reply_markup(chat_id, card_msg_id, reply_markup=updated_markup)
                    except Exception:
                        pass

        if not urls:
            return

        photo_caption = _build_safe_photo_caption(
            ad, query_name, desc or "", update_type,
            old_price_byn, new_price_byn, market_median
        )
        added_mids: List[int] = []
        if len(urls) > 1:
            for chunk_idx, chunk in enumerate(_split_album_chunks(urls[:20])):
                media = []
                for item_idx, u in enumerate(chunk):
                    entry: Dict[str, Any] = {"type": "photo", "media": u}
                    if chunk_idx == 0 and item_idx == 0:
                        entry["caption"] = photo_caption
                        entry["parse_mode"] = "HTML"
                    media.append(entry)
                res_album = tg.send_media_group(chat_id, media, disable_notification=True)
                if res_album and isinstance(res_album, list):
                    added_mids.extend(m["message_id"] for m in res_album if isinstance(m, dict) and "message_id" in m)
                else:
                    break
        else:
            res_photo = tg.send_photo(chat_id, urls[0], caption=photo_caption, disable_notification=True)
            if res_photo and isinstance(res_photo, dict) and "message_id" in res_photo:
                added_mids.append(res_photo["message_id"])

        if added_mids:
            with STATE_LOCK:
                existing = AD_MESSAGES_MAP.get((chat_id, ad_id), [])
                existing.extend(added_mids)
                AD_MESSAGES_MAP[(chat_id, ad_id)] = existing
    except Exception as e:
        logger.debug(f"Fast media delivery error for ad {ad_id}: {e}")


def send_ad_notification(
    tg: TelegramClient,
    chat_id: int,
    ad: Dict[str, Any],
    query_name: str,
    update_type: str = "new",
    old_price_byn: Optional[float] = None,
    new_price_byn: Optional[float] = None,
    market_median: Optional[float] = None,
    query_meta: Optional[Dict[str, Any]] = None,
    fast_mode: Optional[bool] = None,
    async_media: bool = True
) -> bool:
    """
    Sends notification using either Fast-Alert mode (instant card + buttons first, silent photos after)
    or standard mode (native media group album + compact control card).
    """
    ad_id = ad["ad_id"]
    if is_ad_blacklisted(chat_id, ad_id):
        logger.info(f"Ad {ad_id} is blacklisted by chat {chat_id}, skipping")
        return False

    seller_name = ad.get("seller", "")
    account_id = ad.get("account_id")
    if is_seller_blacklisted(chat_id, seller_name, account_id):
        logger.info(f"Seller '{seller_name}' is blacklisted by chat {chat_id}, skipping ad {ad_id}")
        return False

    fast_alert = get_user_fast_alert(chat_id) if fast_mode is None else bool(fast_mode)
    stop_words = get_stop_words(chat_id)
    meta_dict = query_meta or {}
    need_desc_filter = bool(stop_words or meta_dict.get("stop_words") or meta_dict.get("plus_words"))

    image_urls = list(ad.get("image_urls") or ([ad["image_url"]] if ad.get("image_url") else []))
    description = ad.get("description")
    if (description is None and (not fast_alert or need_desc_filter)) or (not image_urls and description is None) or (not fast_alert and len(image_urls) <= 1):
        fetched_desc, fetched_photos = fetch_ad_details(ad_id, timeout=4 if fast_alert else 6)
        if description is None:
            description = fetched_desc
            ad["description"] = description
        if fetched_photos:
            seen_u = set(image_urls)
            for u in fetched_photos:
                if u not in seen_u:
                    seen_u.add(u)
                    image_urls.append(u)
            ad["image_urls"] = image_urls

    # Check global and per-query stop-words + plus-words
    if stop_words:
        check_text = f"{ad.get('subject', '')} {description or ''}".lower()
        for sw in stop_words:
            if sw in check_text:
                logger.info(f"Ad {ad_id} matches stop-word '{sw}', skipping")
                return False

    meta_ok, matched_plus = evaluate_query_meta_for_ad(
        ad, query_meta, market_median=market_median, description=description or ""
    )
    if not meta_ok:
        logger.info(f"Ad {ad_id} filtered out by query-level rules for '{query_name}'")
        return False
    if matched_plus:
        ad["_matched_plus"] = matched_plus

    with STATE_LOCK:
        AD_DATA_CACHE[ad_id] = ad
        if len(AD_DATA_CACHE) > 1000:
            for old_k in list(AD_DATA_CACHE.keys())[:200]:
                AD_DATA_CACHE.pop(old_k, None)

        ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
        if image_urls:
            AD_PHOTOS_CACHE[ad_id] = image_urls

        if len(AD_MESSAGES_MAP) > 1000:
            for old_k in list(AD_MESSAGES_MAP.keys())[:200]:
                AD_MESSAGES_MAP.pop(old_k, None)

    is_fav = is_favorite(chat_id, ad_id)
    phone_e164, _ = resolve_phone_info(ad, description or "")
    personalized_msg = build_personalized_message(chat_id, ad)

    effective_price = new_price_byn if (update_type == "price_change" and new_price_byn is not None) else ad.get("price_byn")
    is_hot = _evaluate_priority(
        ad, effective_price, market_median,
        update_type=update_type,
        old_price_byn=old_price_byn,
        new_price_byn=new_price_byn
    )
    priority_mode = get_user_priority_mode(chat_id)
    disable_notif = _should_disable_notification(chat_id, is_hot, query_meta, priority_mode)

    markup = make_ad_keyboard(
        ad_link,
        ad_id,
        photo_count=len(image_urls),
        seller_name=seller_name,
        is_blacklisted=False,
        is_fav=is_fav,
        copy_msg=personalized_msg,
        phone_number=phone_e164
    )

    # 0. Fast-Alert mode ("Молния"): deliver action card + buttons IMMEDIATELY, stream photos silently after
    if fast_alert and image_urls:
        action_text = _build_single_screen_action_text(
            ad, len(image_urls), seller_name, phone_e164, personalized_msg,
            is_hot=(is_hot and priority_mode != "off"),
            market_median=market_median,
            update_type=update_type,
            old_price_byn=old_price_byn,
            new_price_byn=new_price_byn
        )
        res_fast = tg.send_message(
            chat_id,
            action_text,
            reply_markup=markup,
            disable_notification=disable_notif
        )
        if res_fast and isinstance(res_fast, dict) and "message_id" in res_fast:
            card_mid = res_fast["message_id"]
            with STATE_LOCK:
                AD_MESSAGES_MAP[(chat_id, ad_id)] = [card_mid]
            delivery_args = (
                tg, chat_id, ad, query_name, image_urls, description,
                update_type, old_price_byn, new_price_byn, market_median,
                card_mid, bool(phone_e164), personalized_msg, is_fav
            )
            if async_media:
                threading.Thread(target=_deliver_fast_media, args=delivery_args, daemon=True).start()
            else:
                _deliver_fast_media(*delivery_args)
            return True
        return False

    # 1. Multiple photos: send ALL photos immediately as native Telegram album(s)
    if len(image_urls) > 1:
        photo_caption = _build_safe_photo_caption(
            ad, query_name, description or "", update_type,
            old_price_byn, new_price_byn, market_median
        )
        mids: List[int] = []
        album_ok = True
        for chunk_idx, chunk in enumerate(_split_album_chunks(image_urls[:20])):
            media = []
            for item_idx, u in enumerate(chunk):
                entry: Dict[str, Any] = {"type": "photo", "media": u}
                if chunk_idx == 0 and item_idx == 0:
                    entry["caption"] = photo_caption
                    entry["parse_mode"] = "HTML"
                media.append(entry)

            res_album = tg.send_media_group(chat_id, media, disable_notification=disable_notif)
            if res_album and isinstance(res_album, list):
                mids.extend(m["message_id"] for m in res_album if isinstance(m, dict) and "message_id" in m)
            else:
                if chunk_idx == 0:
                    album_ok = False
                break

        if album_ok and mids:
            action_text = _build_single_screen_action_text(
                ad, len(image_urls), seller_name, phone_e164, personalized_msg,
                is_hot=(is_hot and priority_mode != "off"),
                market_median=market_median,
                update_type=update_type,
                old_price_byn=old_price_byn,
                new_price_byn=new_price_byn
            )
            res_btn = tg.send_message(
                chat_id,
                action_text,
                reply_markup=markup,
                disable_notification=disable_notif
            )
            if res_btn and isinstance(res_btn, dict) and "message_id" in res_btn:
                mids.append(res_btn["message_id"])
            with STATE_LOCK:
                AD_MESSAGES_MAP[(chat_id, ad_id)] = mids
            return True

    # 2. Single photo (or fallback if album upload was rejected)
    if image_urls:
        photo_caption = _build_safe_photo_caption(
            ad, query_name, description or "", update_type,
            old_price_byn, new_price_byn, market_median
        )
        res_photo = tg.send_photo(
            chat_id,
            image_urls[0],
            caption=photo_caption,
            reply_markup=markup,
            disable_notification=disable_notif
        )
        if res_photo and isinstance(res_photo, dict) and "message_id" in res_photo:
            with STATE_LOCK:
                AD_MESSAGES_MAP[(chat_id, ad_id)] = [res_photo["message_id"]]
            return True

    # 3. Text-only fallback
    caption = format_ad_caption(
        ad, query_name,
        description=description or "",
        update_type=update_type,
        old_price_byn=old_price_byn,
        new_price_byn=new_price_byn,
        market_median=market_median
    )
    if is_hot and priority_mode != "off":
        caption = f"🔥 <b>ПРИОРИТЕТ</b>\n{caption}"
    res_msg = tg.send_message(
        chat_id,
        caption,
        disable_web_page_preview=False,
        reply_markup=markup,
        disable_notification=disable_notif
    )
    if res_msg and isinstance(res_msg, dict) and "message_id" in res_msg:
        with STATE_LOCK:
            AD_MESSAGES_MAP[(chat_id, ad_id)] = [res_msg["message_id"]]
        return True

    return False


def check_single_query(tg: TelegramClient, query: Dict[str, Any], chat_id_fallback: Optional[int] = None) -> int:
    """Checks Kufar for a specific query and sends notifications for new listings and price changes."""
    global NEW_ADS_FOUND
    qid = query["id"]
    chat_id = query.get("chat_id") or chat_id_fallback
    if not chat_id:
        logger.error(f"Cannot check query {qid}: chat_id is missing")
        return 0
    if is_user_monitoring_paused(chat_id):
        return 0
    name = query["name"]
    params = query["params"]
    meta = query.get("meta") or {}

    # 1. Parse user's desired budget range
    min_budget, max_budget = parse_price_range(params.get("prc"))

    # 2. Build search params for Kufar API without upper price ceiling,
    # so we can detect price raises above user filter (e.g. 2100 -> 2500 BYN)
    # as well as price drops from above into the filter!
    api_params = dict(params)
    api_params["size"] = "50"  # Check top 50 items to never miss quick edits or price changes
    if min_budget is not None:
        # Keep min price on Kufar API to filter out tiny irrelevant accessories/cables
        api_params["prc"] = f"r:{int(min_budget * 100)},999999999"
    else:
        api_params.pop("prc", None)

    try:
        ads = fetch_ads(api_params)
    except Exception as e:
        logger.error(f"Error fetching ads for query '{name}': {e}")
        record_api_event("error", f"Ошибка Kufar для '{name}': {e}")
        return 0

    if not ads:
        return 0

    seen_count = get_query_seen_count(qid)

    # First run: seed database to avoid spamming historical ads
    if seen_count == 0:
        logger.info(f"Seeding historical ads for query '{name}' (count: {len(ads)})")
        mark_ads_seen_batch(qid, ads)
        return 0

    bl = get_user_blacklist(chat_id)
    user_stops = get_stop_words(chat_id)
    market_info = compute_market_analysis(
        params,
        user_stop_words=user_stops,
        local_stop_words=meta.get("stop_words"),
        exclude_company=bool(meta.get("private_only")),
        ttl_seconds=300,
    )
    market_median = market_info.get("median") or calculate_market_stats(ads)

    # Save daily price history snapshot at most once every 10 minutes per query
    now_ts = time.time()
    if market_median is not None and now_ts - LAST_PH_SNAPSHOT.get(qid, 0) >= 600:
        record_price_history(
            qid,
            market_median,
            market_info.get("min"),
            market_info.get("max"),
            int(market_info.get("count") or len(ads)),
        )
        LAST_PH_SNAPSHOT[qid] = now_ts

    sent_count = 0

    for ad in reversed(ads):
        ad_id = ad["ad_id"]
        if ad_id in bl:
            continue

        if is_seller_blacklisted(chat_id, ad.get("seller", ""), ad.get("account_id")):
            continue

        curr_byn = ad.get("price_byn")
        curr_usd = ad.get("price_usd")

        is_seen, last_byn, last_usd = get_ad_seen_price(qid, ad_id)

        if not is_seen:
            # Completely new listing!
            # Check if this new listing is within user's budget:
            is_within_budget = True
            if max_budget is not None and curr_byn is not None and curr_byn > max_budget:
                is_within_budget = False
            if min_budget is not None and curr_byn is not None and curr_byn < min_budget:
                is_within_budget = False

            # Always record current price in DB
            mark_ad_seen(qid, ad_id, price_byn=curr_byn, price_usd=curr_usd)

            meta_allowed, matched_plus = evaluate_query_meta_for_ad(ad, meta, market_median=market_median)
            if is_within_budget and meta_allowed:
                if sent_count >= 5:
                    # Already recorded as seen above; cap burst notifications per query per cycle to 5
                    continue
                if matched_plus:
                    ad["_matched_plus"] = matched_plus
                try:
                    if send_ad_notification(
                        tg, chat_id, ad, name,
                        update_type="new",
                        market_median=market_median,
                        query_meta=meta
                    ):
                        sent_count += 1
                        with STATE_LOCK:
                            NEW_ADS_FOUND += 1
                        record_api_event("ok", f"Отправлено [{name}]: {ad.get('subject', '')[:40]}")
                        time.sleep(0.25)
                except Exception as e:
                    logger.error(f"Error sending ad notification: {e}")

        else:
            # Existing listing -> check if seller changed the price!
            if last_byn is not None and curr_byn is not None:
                diff = curr_byn - last_byn
                if abs(diff) >= 1.0:  # Price changed by 1 BYN or more
                    # Update price in DB immediately
                    mark_ad_seen(qid, ad_id, price_byn=curr_byn, price_usd=curr_usd)

                    should_notify = False
                    if diff > 0:
                        if max_budget is None or last_byn <= max_budget:
                            should_notify = True
                            logger.info(f"Price RAISED for ad {ad_id}: {last_byn} -> {curr_byn} BYN (+{diff})!")
                    else:
                        if max_budget is None or curr_byn <= max_budget:
                            should_notify = True
                            logger.info(f"Price DROPPED for ad {ad_id}: {last_byn} -> {curr_byn} BYN ({diff})!")

                    meta_allowed, matched_plus = evaluate_query_meta_for_ad(ad, meta, market_median=market_median)
                    if should_notify and meta_allowed:
                        if matched_plus:
                            ad["_matched_plus"] = matched_plus
                        try:
                            if send_ad_notification(
                                tg, chat_id, ad, name,
                                update_type="price_change",
                                old_price_byn=last_byn,
                                new_price_byn=curr_byn,
                                market_median=market_median,
                                query_meta=meta
                            ):
                                sent_count += 1
                                with STATE_LOCK:
                                    NEW_ADS_FOUND += 1
                                time.sleep(0.25)
                        except Exception as e:
                            logger.error(f"Error sending price update notification: {e}")

            elif last_byn is None and curr_byn is not None:
                mark_ad_seen(qid, ad_id, price_byn=curr_byn, price_usd=curr_usd)

    return sent_count


def check_favorites_tracker(tg: TelegramClient):
    """Periodically checks saved favorites for price drops or listing removal/sold status."""
    favs = [f for f in get_all_favorites() if not f.get("is_closed")]
    for fav in favs[:15]:
        if STOP_EVENT.is_set():
            break
        chat_id = fav["chat_id"]
        ad_id = fav["ad_id"]
        title = html.escape((fav.get("title") or f"Объявление #{ad_id}")[:80])
        ad_link = fav.get("ad_link") or f"https://www.kufar.by/item/{ad_id}"
        live = fetch_ad_live_state(ad_id)
        if not live.get("checked"):
            time.sleep(0.5)
            continue

        if live.get("closed"):
            update_favorite_price_state(chat_id, ad_id, is_closed=1)
            msg = (
                f"<b>Избранное — лот снят или продан</b>\n"
                f"{title}\n"
                f"<a href=\"{html.escape(ad_link)}\">Проверить на Kufar</a>"
            )
            try:
                tg.send_message(chat_id, msg, disable_notification=is_in_silent_hours(chat_id))
            except Exception:
                pass
        else:
            old_byn = fav.get("price_byn")
            new_byn = live.get("price_byn")
            new_usd = live.get("price_usd")
            if new_byn is not None and old_byn is not None and (old_byn - new_byn) >= 1.0:
                update_favorite_price_state(chat_id, ad_id, price_byn=new_byn, price_usd=new_usd, is_closed=0)
                diff = old_byn - new_byn
                msg = (
                    f"<b>Избранное — цена снижена (-{diff:,.0f} BYN)</b>\n"
                    f"<b>{title}</b>\n"
                    f"<b>{new_byn:,.0f} BYN</b> <s>{old_byn:,.0f} BYN</s>\n"
                    f"<a href=\"{html.escape(ad_link)}\">Открыть объявление</a>"
                ).replace(",", " ")
                try:
                    tg.send_message(chat_id, msg, disable_notification=is_in_silent_hours(chat_id))
                except Exception:
                    pass
            else:
                update_favorite_price_state(chat_id, ad_id, price_byn=new_byn, price_usd=new_usd, is_closed=0)
        time.sleep(1.0)


def favorites_tracker_loop(tg: TelegramClient, config: dict):
    """Dedicated background thread checking favorites so main search loop stays non-blocking."""
    while not STOP_EVENT.is_set():
        try:
            check_favorites_tracker(tg)
        except Exception as e:
            logger.error(f"Error in favorites tracker loop: {e}")
        if STOP_EVENT.wait(timeout=900):
            break


def hunter_loop(tg: TelegramClient, config: dict):
    """Background monitoring loop with parallel query execution and minimal inter-cycle latency."""
    global TOTAL_CHECKS
    logger.info("Kufar Hunter loop started.")
    record_api_event("info", "Фоновый мониторинг запущен")

    while not STOP_EVENT.is_set():
        interval = max(5, int(config.get("check_interval_seconds", 60)))
        try:
            queries = get_active_queries()
            if queries:
                logger.debug(f"Running check for {len(queries)} active queries...")
                if len(queries) == 1:
                    try:
                        check_single_query(tg, queries[0])
                    except Exception as e:
                        logger.error(f"Error checking query '{queries[0]['name']}': {e}")
                        record_api_event("error", f"Исключение '{queries[0]['name']}': {e}")
                else:
                    workers = min(4, len(queries))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                        fut_map = {pool.submit(check_single_query, tg, q): q for q in queries}
                        for fut in concurrent.futures.as_completed(fut_map):
                            if STOP_EVENT.is_set():
                                break
                            q = fut_map[fut]
                            try:
                                fut.result()
                            except Exception as e:
                                logger.error(f"Error checking query '{q['name']}': {e}")
                                record_api_event("error", f"Исключение '{q['name']}': {e}")
                TOTAL_CHECKS += 1

            try:
                maybe_run_scheduled_backup(tg, config)
            except Exception as e:
                logger.error(f"Error in scheduled backup: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in hunter loop: {e}")
            record_api_event("error", f"Цикл: {e}")

        FORCE_CHECK_EVENT.wait(timeout=interval)
        FORCE_CHECK_EVENT.clear()


# ==================== UI & COMMAND HANDLERS ====================

def save_local_backup():
    """Silently saves current state to backup_state.json and marks cloud backup dirty (no Telegram spam)."""
    global LAST_BACKUP_TIME, BACKUP_DIRTY
    try:
        state = export_database_state()
        has_data = bool(state.get("queries") or state.get("blacklisted_ads") or state.get("blacklisted_sellers") or state.get("stop_words") or state.get("favorites"))
        if has_data:
            with open(BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            LAST_BACKUP_TIME = datetime.now()
            BACKUP_DIRTY = True
    except Exception as e:
        logger.error(f"Error saving local backup: {e}")


def sync_backup_to_telegram(
    tg: TelegramClient,
    config: dict,
    force_notify: bool = False,
    in_place_only: bool = False
):
    """
    Syncs database state to Telegram Cloud.
    Edits the pinned backup message/document in-place without sending duplicate files or re-pinning.
    Triggered only on schedule (1-4x/day), manual admin action, or graceful shutdown (in_place_only).
    """
    global LAST_BACKUP_TIME, BACKUP_DIRTY
    admin_chat_id = config.get("admin_chat_id", 0)
    if not admin_chat_id:
        return

    with BACKUP_LOCK:
        try:
            state = export_database_state()
            has_data = bool(state.get("queries") or state.get("blacklisted_ads") or state.get("blacklisted_sellers") or state.get("stop_words") or state.get("favorites"))
            if has_data:
                with open(BACKUP_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                LAST_BACKUP_TIME = datetime.now()

            raw_json = json.dumps(state, ensure_ascii=False)
            minsk_tz = timezone(timedelta(hours=3))
            updated_time = datetime.now(minsk_tz).strftime("%d.%m.%Y в %H:%M:%S")
            q_count = len(state.get("queries", []))
            bl_count = len(state.get("blacklisted_ads", []))
            bl_sellers = len(state.get("blacklisted_sellers", []))
            sw_count = len(state.get("stop_words", []))
            fav_count = len(state.get("favorites", []))

            backup_text = (
                f"💾 <b>Kufar Hunter — Синхронизация данных</b>\n"
                f"<i>Хранит ваши фильтры, ЧС, избранное и настройки.</i>\n\n"
                f"• Поисков: <b>{q_count}</b>\n"
                f"• В Избранном: <b>{fav_count}</b>\n"
                f"• В ЧС объявлений: <b>{bl_count}</b>\n"
                f"• В ЧС продавцов: <b>{bl_sellers}</b>\n"
                f"• Стоп-слов: <b>{sw_count}</b>\n"
                f"<i>Синхронизировано (Минск): {updated_time}</i>\n\n"
                f"#KUFAR_BACKUP_START\n"
                f"<pre><code class=\"language-json\">{html.escape(raw_json)}</code></pre>\n"
                f"#KUFAR_BACKUP_END"
            )

            chat_info = tg.get_chat(admin_chat_id)
            pinned = chat_info.get("pinned_message") if chat_info else None

            if not has_data and not force_notify:
                if pinned and ("#KUFAR_BACKUP_START" in pinned.get("text", "") or "<!--KUFAR_BACKUP_START-->" in pinned.get("text", "") or pinned.get("document")):
                    logger.warning("Local state is empty while cloud backup exists; skipping overwrite.")
                    return

            if len(backup_text) <= 4000:
                if pinned and ("#KUFAR_BACKUP_START" in pinned.get("text", "") or "<!--KUFAR_BACKUP_START-->" in pinned.get("text", "")):
                    tg.edit_message_text(admin_chat_id, pinned["message_id"], backup_text, parse_mode="HTML")
                elif not in_place_only:
                    old_mid = pinned.get("message_id") if pinned and (pinned.get("document") or "#KUFAR_BACKUP" in (pinned.get("text", "") + pinned.get("caption", ""))) else None
                    msg = tg.send_message(admin_chat_id, backup_text, disable_notification=True)
                    if msg and isinstance(msg, dict) and "message_id" in msg:
                        tg.pin_chat_message(admin_chat_id, msg["message_id"], disable_notification=True)
                        if old_mid and old_mid != msg["message_id"]:
                            try:
                                tg.delete_message(admin_chat_id, old_mid)
                            except Exception:
                                pass
            else:
                caption = (
                    f"💾 <b>Kufar Hunter — Резервная копия</b>\n\n"
                    f"• Поисков: {q_count}, Избр.: {fav_count}, В ЧС: {bl_count}, Продавцов: {bl_sellers}, Стоп-слов: {sw_count}\n"
                    f"🕒 {updated_time} (Минск)\n#KUFAR_BACKUP_DOC"
                )
                if pinned and pinned.get("document"):
                    # Replace file in-place: zero new messages, zero pin notifications!
                    ok = tg.edit_message_media_document(
                        admin_chat_id,
                        pinned["message_id"],
                        "kufar_backup.json",
                        raw_json.encode("utf-8"),
                        caption=caption,
                    )
                    if ok:
                        BACKUP_DIRTY = False
                        record_api_event("ok", f"Облачный бэкап обновлён ({updated_time})")
                        if force_notify:
                            tg.send_message(
                                admin_chat_id,
                                f"✅ <b>Резервная копия обновлена в закрепе!</b>\n"
                                f"Поисков: {q_count} · Избранное: {fav_count} · ЧС: {bl_count}"
                            )
                        return

                if not in_place_only:
                    old_mid = pinned.get("message_id") if pinned else None
                    doc_msg = tg.send_document(
                        admin_chat_id,
                        "kufar_backup.json",
                        raw_json.encode("utf-8"),
                        caption=caption,
                        disable_notification=True
                    )
                    if doc_msg and isinstance(doc_msg, dict) and "message_id" in doc_msg:
                        tg.pin_chat_message(admin_chat_id, doc_msg["message_id"], disable_notification=True)
                        if old_mid and old_mid != doc_msg["message_id"]:
                            try:
                                tg.delete_message(admin_chat_id, old_mid)
                            except Exception:
                                pass

            BACKUP_DIRTY = False
            record_api_event("ok", f"Облачный бэкап сохранён ({updated_time})")
            if force_notify:
                tg.send_message(
                    admin_chat_id,
                    f"✅ <b>Резервная копия сохранена в Telegram!</b>\n"
                    f"🎯 Поисков: {q_count}\n"
                    f"⭐️ Избранное: {fav_count}\n"
                    f"🚫 В ЧС объявлений: {bl_count}\n"
                    f"👤 В ЧС продавцов: {bl_sellers}\n"
                    f"⛔ Стоп-слов: {sw_count}"
                )
        except Exception as e:
            logger.error(f"Error syncing backup to Telegram: {e}")
            record_api_event("error", f"Ошибка бэкапа: {e}")


def maybe_run_scheduled_backup(tg: TelegramClient, config: dict):
    """
    Runs Telegram Cloud backup only at configured fixed times of day (1-4x daily in Minsk UTC+3),
    never spamming on user actions.
    """
    freq = str(get_setting("backup_freq", "2") or "2").strip()
    slots = BACKUP_SCHEDULES.get(freq, BACKUP_SCHEDULES["2"])
    if not slots:
        return

    minsk_tz = timezone(timedelta(hours=3))
    now_dt = datetime.now(minsk_tz)
    day_str = now_dt.strftime("%Y-%m-%d")
    now_min = now_dt.hour * 60 + now_dt.minute
    last_slot = get_setting("last_backup_slot", "") or ""

    for slot in slots:
        try:
            sh, sm = map(int, slot.split(":"))
            slot_min = sh * 60 + sm
        except Exception:
            continue
        # Trigger once within the 30-minute window after each scheduled time
        if 0 <= (now_min - slot_min) <= 30:
            slot_key = f"{day_str}_{slot}"
            if last_slot != slot_key:
                set_setting("last_backup_slot", slot_key)
                logger.info(f"Running scheduled Telegram Cloud backup for slot {slot_key} (freq={freq}x/day)")
                sync_backup_to_telegram(tg, config, force_notify=False)
                break


def restore_state_from_telegram(tg: TelegramClient, config: dict) -> bool:
    """
    Checks Telegram live cloud backup first (contains the most up-to-date blacklist & searches),
    then falls back to local backup_state.json if Telegram is unreachable.
    Does NOT unpin the backup message so future updates and restarts can always restore.
    """
    global LAST_BACKUP_TIME
    admin_chat_id = config.get("admin_chat_id", 0)

    # 1. First, check Telegram pinned message (it always has the most up-to-date live data!)
    if admin_chat_id:
        try:
            chat_info = tg.get_chat(admin_chat_id)
            pinned = chat_info.get("pinned_message") if chat_info else None
            if pinned:
                state = None
                pinned_text = pinned.get("text", "")

                if ("#KUFAR_BACKUP_START" in pinned_text and "#KUFAR_BACKUP_END" in pinned_text) or \
                   ("<!--KUFAR_BACKUP_START-->" in pinned_text and "<!--KUFAR_BACKUP_END-->" in pinned_text):
                    part = ""
                    if "#KUFAR_BACKUP_START" in pinned_text:
                        part = pinned_text.split("#KUFAR_BACKUP_START")[1].split("#KUFAR_BACKUP_END")[0].strip()
                    elif "<!--KUFAR_BACKUP_START-->" in pinned_text:
                        part = pinned_text.split("<!--KUFAR_BACKUP_START-->")[1].split("<!--KUFAR_BACKUP_END-->")[0].strip()
                    part = part.replace("<pre><code class=\"language-json\">", "").replace("</code></pre>", "")
                    part = part.replace("<pre>", "").replace("</pre>", "").replace("<code>", "").replace("</code>", "").strip()
                    part = html.unescape(part)
                    try:
                        state = json.loads(part)
                    except Exception as e:
                        logger.error(f"Error decoding JSON from pinned text: {e}")

                elif pinned.get("document"):
                    doc = pinned["document"]
                    file_id = doc.get("file_id")
                    if file_id:
                        file_info = tg.get_file(file_id)
                        if file_info and file_info.get("file_path"):
                            content = tg.download_file(file_info["file_path"])
                            if content:
                                try:
                                    state = json.loads(content.decode("utf-8"))
                                except Exception as e:
                                    logger.error(f"Error decoding JSON from document: {e}")

                if state and isinstance(state, dict):
                    stats = import_database_state(state)
                    logger.info(f"Successfully restored live state from Telegram Cloud: {stats}")
                    LAST_BACKUP_TIME = datetime.now()
                    # Keep pinned so subsequent restarts can always restore!
                    return True
        except Exception as e:
            logger.error(f"Error restoring live state from Telegram Cloud: {e}")

    # 2. Fallback to local backup_state.json if Telegram has no pinned message
    if os.path.exists(BACKUP_FILE):
        try:
            with open(BACKUP_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state and (state.get("queries") or state.get("blacklisted_ads")):
                stats = import_database_state(state)
                logger.info(f"Loaded state from fallback backup_state.json: {stats}")
                if state.get("exported_at"):
                    try:
                        LAST_BACKUP_TIME = datetime.fromisoformat(state["exported_at"])
                    except Exception:
                        LAST_BACKUP_TIME = datetime.now()
                else:
                    LAST_BACKUP_TIME = datetime.now()
                return True
        except Exception as e:
            logger.error(f"Error loading local backup_state.json: {e}")

    return False


def check_and_notify_new_user(tg: TelegramClient, user: Dict[str, Any], config: dict):
    """Registers user in database and notifies admin if this user is new."""
    chat_id = user.get("id")
    if not chat_id:
        return
    username = user.get("username")
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")

    is_new = register_or_update_user(chat_id, username, first_name, last_name)
    admin_chat_id = config.get("admin_chat_id", 0)

    if is_new and admin_chat_id and chat_id != admin_chat_id:
        full_name = f"{first_name} {last_name}".strip() or "Без имени"
        u_str = f"@{username}" if username else "<i>нет username</i>"
        text = (
            f"👤 <b>Новый пользователь запустил бота!</b>\n\n"
            f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"👤 <b>Имя:</b> {html.escape(full_name)}\n"
            f"🔗 <b>Username:</b> {u_str}\n"
            f"📅 <b>Дата:</b> {datetime.now().strftime('%d.%m.%Y в %H:%M:%S')}"
        )
        try:
            tg.send_message(admin_chat_id, text)
        except Exception as e:
            logger.error(f"Error notifying admin of new user: {e}")


def notify_admin_new_filter(
    tg: TelegramClient,
    chat_id: int,
    user: Dict[str, Any],
    qid: int,
    name: str,
    params: Dict[str, Any],
    config: dict
):
    """Notifies admin whenever any user adds a new search filter."""
    admin_chat_id = config.get("admin_chat_id", 0)
    if not admin_chat_id:
        return

    username = user.get("username")
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip() or "Без имени"
    u_str = f"@{username}" if username else f"ID: {chat_id}"

    who = "Вы (админ)" if chat_id == admin_chat_id else f"{html.escape(full_name)} ({u_str})"

    text = (
        f"🎯 <b>Пользователь добавил фильтр!</b>\n\n"
        f"👤 <b>Пользователь:</b> {who}\n"
        f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n"
        f"🏷 <b>Поиск [ID: {qid}]:</b> {html.escape(name)}\n\n"
        f"{format_params_human(params)}"
    )
    if chat_id != admin_chat_id:
        try:
            tg.send_message(admin_chat_id, text)
        except Exception as e:
            logger.error(f"Error notifying admin of new filter: {e}")


def _passes_budget_and_title_filters(
    chat_id: int,
    ad: Dict[str, Any],
    min_budget: Optional[float],
    max_budget: Optional[float],
    bl: Set[int],
    stop_words: List[str]
) -> bool:
    """Quick pre-filter before sending: blacklist, seller blacklist, budget bounds, and title stop-words."""
    ad_id = ad["ad_id"]
    if ad_id in bl:
        return False
    if is_seller_blacklisted(chat_id, ad.get("seller", ""), ad.get("account_id")):
        return False
    curr_byn = ad.get("price_byn")
    if curr_byn is not None:
        if max_budget is not None and curr_byn > max_budget:
            return False
        if min_budget is not None and curr_byn < min_budget:
            return False
    if stop_words:
        subj_lower = (ad.get("subject") or "").lower()
        if any(sw in subj_lower for sw in stop_words):
            return False
    return True


def _run_batch_ad_output(
    tg: TelegramClient,
    chat_id: int,
    queries: List[Dict[str, Any]],
    header_text: str,
    empty_text: str,
    done_text_fn
):
    """Shared worker for streaming current ads for one or more queries with instant stop support."""
    if chat_id in ACTIVE_BATCH_CHATS:
        tg.send_message(chat_id, "⏳ <b>Вывод объявлений уже выполняется.</b> Нажмите «🛑 Остановить вывод объявлений», чтобы прервать его.")
        return

    ACTIVE_BATCH_CHATS.add(chat_id)
    STOP_OUTPUT_REQUESTED.discard(chat_id)
    status_msg_id = None

    try:
        stop_markup = {
            "inline_keyboard": [
                [{"text": "Остановить вывод", "callback_data": "cb:stop_output"}]
            ]
        }
        status_msg = tg.send_message(chat_id, header_text, reply_markup=stop_markup)
        if isinstance(status_msg, dict):
            status_msg_id = status_msg.get("message_id")

        bl = get_user_blacklist(chat_id)
        stop_words = get_stop_words(chat_id)
        seen_in_run: Set[int] = set()
        total_shown = 0
        stopped_early = False

        for q in queries:
            if chat_id in STOP_OUTPUT_REQUESTED:
                stopped_early = True
                break

            qid = q["id"]
            q_name = q["name"]
            params = dict(q["params"])
            q_meta = q.get("meta") or {}
            min_budget, max_budget = parse_price_range(params.get("prc"))
            api_params = dict(params)
            api_params["size"] = "50"

            try:
                ads = fetch_ads(api_params)
            except Exception as e:
                logger.error(f"Error fetching ads for query {qid}: {e}")
                continue

            q_market = compute_market_analysis(
                params,
                user_stop_words=stop_words,
                local_stop_words=q_meta.get("stop_words"),
                exclude_company=bool(q_meta.get("private_only")),
                ttl_seconds=300,
            )
            q_median = q_market.get("median") or calculate_market_stats(ads)
            shown_for_query = 0

            for ad in ads:
                if chat_id in STOP_OUTPUT_REQUESTED:
                    stopped_early = True
                    break
                if shown_for_query >= 3 or total_shown >= 5:
                    break
                ad_id = ad["ad_id"]
                if ad_id in seen_in_run:
                    continue
                if not _passes_budget_and_title_filters(chat_id, ad, min_budget, max_budget, bl, stop_words):
                    continue
                meta_ok, matched_plus = evaluate_query_meta_for_ad(ad, q_meta, market_median=q_median)
                if not meta_ok:
                    continue
                if matched_plus:
                    ad["_matched_plus"] = matched_plus

                seen_in_run.add(ad_id)
                mark_ad_seen(qid, ad_id, price_byn=ad.get("price_byn"), price_usd=ad.get("price_usd"))
                try:
                    if send_ad_notification(tg, chat_id, ad, q_name, market_median=q_median, query_meta=q_meta, fast_mode=False):
                        total_shown += 1
                        shown_for_query += 1
                        for _ in range(4):
                            if chat_id in STOP_OUTPUT_REQUESTED:
                                stopped_early = True
                                break
                            time.sleep(0.2)
                        if stopped_early:
                            break
                except Exception as e:
                    logger.error(f"Error displaying ad {ad_id}: {e}")

            if stopped_early:
                break

        if status_msg_id:
            try:
                tg.delete_message(chat_id, status_msg_id)
            except Exception:
                pass

        if stopped_early:
            STOP_OUTPUT_REQUESTED.discard(chat_id)
            tg.send_message(
                chat_id,
                f"🛑 <b>Вывод объявлений остановлен!</b>\nВсего показано: <b>{total_shown}</b> объявлений.",
                reply_markup=get_main_keyboard(chat_id)
            )
            return

        if total_shown == 0:
            tg.send_message(chat_id, empty_text, reply_markup=get_main_keyboard(chat_id))
        else:
            tg.send_message(chat_id, done_text_fn(total_shown), reply_markup=get_main_keyboard(chat_id))
    finally:
        ACTIVE_BATCH_CHATS.discard(chat_id)
        STOP_OUTPUT_REQUESTED.discard(chat_id)


def output_all_user_ads(tg: TelegramClient, chat_id: int, background: bool = True):
    """Outputs current listings for all active user searches, respecting blacklisted items & stop-words."""
    all_queries = get_user_queries(chat_id)
    if not all_queries:
        text = (
            "📭 <b>У вас пока нет активных поисков.</b>\n\n"
            "Нажмите кнопку <b>«➕ Добавить поиск»</b>, чтобы создать первый фильтр!"
        )
        tg.send_message(chat_id, text, reply_markup=get_main_keyboard(chat_id))
        return

    active_queries = [q for q in all_queries if q.get("is_active", True)]
    if not active_queries:
        tg.send_message(
            chat_id,
            "⏸ <b>Все ваши поиски сейчас поставлены на паузу.</b>\n\n"
            "Включите нужный фильтр в разделе <b>«📋 Мои поиски»</b> или нажмите <b>«Показать объявления»</b> у конкретного поиска.",
            reply_markup=get_main_keyboard(chat_id)
        )
        return

    header = (
        f"🔍 <b>Подбираю свежие объявления...</b>"
    )
    empty = "📭 Пока нет подходящих объявлений."
    done_fn = lambda cnt: (
        f"✅ <b>Показано {cnt} свежих объявлений.</b>\n"
        f"<i>Все остальные лоты и фильтры — в Mini App.</i>"
    )

    if background:
        threading.Thread(
            target=_run_batch_ad_output,
            args=(tg, chat_id, active_queries, header, empty, done_fn),
            daemon=True
        ).start()
    else:
        _run_batch_ad_output(tg, chat_id, active_queries, header, empty, done_fn)


def output_ads_for_query(tg: TelegramClient, chat_id: int, qid: int, background: bool = True):
    """Outputs current listings for a specific search query."""
    queries = get_user_queries(chat_id)
    target = next((q for q in queries if q["id"] == qid), None)
    if not target:
        tg.send_message(chat_id, "⚠️ Этот поиск не найден или был удалён.", reply_markup=get_main_keyboard(chat_id))
        return

    q_name = target["name"]
    header = f"🔍 <b>Загружаю актуальные объявления для «{html.escape(q_name)}»...</b>"
    empty = f"📭 По поиску «{html.escape(q_name)}» нет подходящих объявлений (или все отфильтрованы стоп-словами / ЧС)."
    done_fn = lambda cnt: f"✅ <b>Показано {cnt} объявлений по поиску «{html.escape(q_name)}».</b>"

    if background:
        threading.Thread(
            target=_run_batch_ad_output,
            args=(tg, chat_id, [target], header, empty, done_fn),
            daemon=True
        ).start()
    else:
        _run_batch_ad_output(tg, chat_id, [target], header, empty, done_fn)


def _send_or_edit(
    tg: TelegramClient,
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    edit_msg_id: Optional[int] = None,
    disable_web_page_preview: bool = True
):
    """Edits the dashboard hub message in-place when possible, preventing chat clutter."""
    if edit_msg_id:
        ok = tg.edit_message_text(
            chat_id,
            edit_msg_id,
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview
        )
        if ok:
            return
    tg.send_message(
        chat_id,
        text,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_web_page_preview
    )


def show_main_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Displays clean, minimal Inline Hub without cluttering chat."""
    paused = is_user_monitoring_paused(chat_id)
    queries = get_user_queries(chat_id)
    fav_cnt = get_favorites_count(chat_id)
    admin_chat_id = int(GLOBAL_CONFIG.get("admin_chat_id", 0) or 0)
    is_admin = bool(admin_chat_id and chat_id == admin_chat_id)

    status_icon = "На паузе" if paused else "● Активен"
    text = (
        f"<b>KUFAR HUNTER</b> · {status_icon}\n"
        f"Поисков: <b>{len(queries)}</b> · Избранное: <b>{fav_cnt}</b>\n\n"
        f"<i>Отправьте ссылку kufar.by или откройте Mini App.</i>"
    )

    buttons: List[List[Dict[str, Any]]] = []
    webapp_url = get_webapp_url()
    if webapp_url:
        buttons.append([{"text": "Открыть Mini App ↗", "web_app": {"url": webapp_url}}])

    pause_btn = (
        {"text": "▶ Возобновить", "callback_data": "cb:hub_resume"}
        if paused else
        {"text": "⏸ Пауза", "callback_data": "cb:hub_pause"}
    )
    buttons.append([
        {"text": f"Поиски · {len(queries)}", "callback_data": "cb:my_searches"},
        {"text": f"Избранное · {fav_cnt}", "callback_data": "cb:fav_menu"}
    ])
    buttons.append([
        {"text": "Свежие (3 шт)", "callback_data": "cb:all_ads"},
        pause_btn
    ])
    if is_admin:
        buttons.append([
            {"text": "Фильтры", "callback_data": "cb:settings"},
            {"text": "Статус (Админ)", "callback_data": "cb:stats"}
        ])
    else:
        buttons.append([{"text": "Настройки и ЧС", "callback_data": "cb:settings"}])

    _send_or_edit(tg, chat_id, text, reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


PRIORITY_MODE_LABELS = {
    "hot": "Пробой тишины (топ-лоты)",
    "only_hot": "Громко только топ-лоты",
    "off": "Обычный по расписанию",
}


def show_settings_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Unified settings & filters submenu rendered cleanly in-place."""
    interval = int(GLOBAL_CONFIG.get("check_interval_seconds", 60))
    silent_mode = get_silent_mode(chat_id)
    silent_badge = "Выкл" if silent_mode in ("off", "") else silent_mode
    fast_on = get_user_fast_alert(chat_id)
    pmode = get_user_priority_mode(chat_id)
    pm_label = PRIORITY_MODE_LABELS.get(pmode, pmode)
    sw_cnt = len(get_stop_words(chat_id))
    bl_ads = get_blacklist_count(chat_id)
    bl_sellers = len(get_blacklisted_sellers(chat_id))

    text = (
        f"<b>НАСТРОЙКИ И ФИЛЬТРЫ</b>\n"
        f"────────────────────\n"
        f"<b>Интервал:</b> {interval} сек.\n"
        f"<b>Молния:</b> {'Вкл (кнопка сразу)' if fast_on else 'Выкл (сначала фото)'}\n"
        f"<b>Громкий приоритет:</b> {html.escape(pm_label)}\n"
        f"<b>Тихий режим:</b> {html.escape(silent_badge)}\n"
        f"<b>Стоп-слова:</b> {sw_cnt} шт.\n"
        f"<b>Чёрный список:</b> {bl_ads} объявл. · {bl_sellers} авт.\n"
    )
    buttons = [
        [
            {"text": f"Интервал · {interval}с", "callback_data": "cb:interval_menu"},
            {"text": f"Молния: {'Вкл' if fast_on else 'Выкл'}", "callback_data": "cb:fast_toggle"}
        ],
        [
            {"text": "Громкий приоритет", "callback_data": "cb:priority_menu"},
            {"text": "Тихий режим", "callback_data": "cb:silent_menu"}
        ],
        [
            {"text": f"Стоп-слова · {sw_cnt}", "callback_data": "cb:sw_menu"},
            {"text": "Чёрный список", "callback_data": "cb:bl_menu"}
        ],
        [
            {"text": "Шаблон ответа", "callback_data": "cb:tpl_menu"},
            {"text": "Справка", "callback_data": "cb:help"}
        ],
        [{"text": "‹ Назад в меню", "callback_data": "cb:menu"}]
    ]
    _send_or_edit(tg, chat_id, text, reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


def show_stats_menu(tg: TelegramClient, chat_id: int, config: dict, edit_msg_id: Optional[int] = None):
    """Displays clean bot statistics in-place inside Hub (server metrics strictly admin-only)."""
    admin_chat_id = int(config.get("admin_chat_id", 0) or 0)
    is_admin = bool(admin_chat_id and chat_id == admin_chat_id)
    user_queries = len(get_user_queries(chat_id))
    bl_count = get_blacklist_count(chat_id)
    fav_count = get_favorites_count(chat_id)
    sw_count = len(get_stop_words(chat_id))
    bl_sellers_count = len(get_blacklisted_sellers(chat_id))
    silent_mode_str = get_silent_mode(chat_id)
    silent_display = "Выкл" if silent_mode_str in ("off", "") else silent_mode_str
    paused_str = "На паузе" if is_user_monitoring_paused(chat_id) else "● Активен"

    lines = [
        "<b>МОЙ ПРОФИЛЬ</b>" if not is_admin else "<b>СТАТУС СИСТЕМЫ</b>",
        "────────────────────",
        f"<b>Мониторинг:</b> {paused_str}",
        f"<b>Поисков:</b> {user_queries} · <b>Избранное:</b> {fav_count}",
        f"<b>Стоп-слов:</b> {sw_count} · <b>ЧС:</b> {bl_count} / {bl_sellers_count} авт.",
        f"<b>Тихий режим:</b> {html.escape(silent_display)}",
    ]
    if is_admin:
        uptime_str = str(datetime.now() - START_TIME).split(".")[0]
        backup_str = LAST_BACKUP_TIME.strftime('%d.%m.%Y %H:%M') if LAST_BACKUP_TIME else "актуальна"
        lines.extend([
            "────────────────────",
            f"<b>Аптайм:</b> {uptime_str} · <b>Проверок:</b> {TOTAL_CHECKS}",
            f"<b>Интервал:</b> {config.get('check_interval_seconds', 60)}с · <b>Пользователей:</b> {get_user_count()}",
            f"<b>Активных фильтров:</b> {len(get_active_queries())} · <b>Бэкап:</b> {backup_str}",
        ])
    buttons = [
        [
            {"text": "Обновить", "callback_data": "cb:stats"},
            {"text": "‹ Назад", "callback_data": "cb:menu"}
        ]
    ]
    _send_or_edit(tg, chat_id, "\n".join(lines), reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


def show_help_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Displays structured help guide inside Hub."""
    help_text = (
        "<b>СПРАВКА</b>\n"
        "────────────────────\n"
        "<b>1. Поиск:</b> выставьте фильтры на <b>kufar.by</b> и отправьте ссылку в чат — бот перенесёт их 1-в-1.\n"
        "<b>2. Альбомы:</b> каждое новое объявление приходит полным фотоальбомом с оценкой цены относительно рынка.\n"
        "<b>3. Приложение:</b> откройте <b>Mini App</b> кнопкой у поля ввода для работы со списками, лентой и аналитикой.\n\n"
        "<i>Команды:</i> /start · /app · /all · /favs · /settings · /pause · /resume"
    )
    markup = {
        "inline_keyboard": [
            [{"text": "Добавить поиск", "callback_data": "cb:add_options"}],
            [{"text": "‹ Назад в настройки", "callback_data": "cb:settings"}]
        ]
    }
    _send_or_edit(tg, chat_id, help_text, reply_markup=markup, edit_msg_id=edit_msg_id)


def prompt_add_search_options(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Shows options to add search: by link, constructor, or Mini App."""
    text = (
        "<b>ДОБАВЛЕНИЕ ПОИСКА</b>\n"
        "────────────────────\n"
        "• <b>Ссылка Kufar.by</b> — перенос всех фильтров сайта 1-в-1.\n"
        "• <b>Конструктор</b> — пошаговый выбор категории, региона и цены."
    )
    buttons: List[List[Dict[str, Any]]] = [
        [{"text": "Вставить ссылку Kufar", "callback_data": "cb:add_link"}],
        [{"text": "Пошаговый конструктор", "callback_data": "cb:wiz_start"}],
    ]
    webapp_url = get_webapp_url()
    if webapp_url:
        buttons.append([{"text": "Настроить в Mini App ↗", "web_app": {"url": webapp_url}}])
    buttons.append([{"text": "‹ Назад", "callback_data": "cb:my_searches"}])
    _send_or_edit(tg, chat_id, text, reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


def show_my_searches(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Displays all searches cleanly packaged in ONE in-place hub card with per-query sub-controls."""
    queries = get_user_queries(chat_id)
    if not queries:
        text = (
            "<b>ВАШИ ПОИСКИ</b>\n"
            "────────────────────\n"
            "Пока нет сохранённых фильтров.\n\n"
            "Отправьте ссылку <b>kufar.by</b> прямо в чат или нажмите «Добавить»."
        )
        markup = {
            "inline_keyboard": [
                [{"text": "+ Добавить поиск", "callback_data": "cb:add_options"}],
                [{"text": "‹ Назад", "callback_data": "cb:menu"}]
            ]
        }
        _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=edit_msg_id)
        return

    paused_user = is_user_monitoring_paused(chat_id)
    header_note = " <i>(общая пауза)</i>" if paused_user else ""
    blocks = [f"<b>ВАШИ ПОИСКИ · {len(queries)}</b>{header_note}\n────────────────────"]
    buttons: List[List[Dict[str, Any]]] = []

    for idx, q in enumerate(queries[:6], 1):
        qid = q["id"]
        name = re.sub(r'^[^\wА-Яа-яЁёA-Za-z0-9]+', '', str(q["name"])).strip() or q["name"]
        params = q["params"]
        seen = get_query_seen_count(qid)
        is_active = q.get("is_active", True)
        state_lbl = "Активен" if is_active else "Пауза"

        blocks.append(f"<b>{idx}. {html.escape(name)}</b> · <i>{state_lbl}</i>\n{format_params_human(params)}\n<i>Просмотрено: {seen}</i>")
        short_lbl = name[:14] + "…" if len(name) > 15 else name
        prefix = f"{idx}. " if len(queries) > 1 else ""
        toggle_btn = (
            {"text": f"⏸ Пауза {idx}" if len(queries) > 1 else "⏸ Пауза", "callback_data": f"cb:toggle_q:{qid}:0"}
            if is_active else
            {"text": f"▶ Вкл {idx}" if len(queries) > 1 else "▶ Включить", "callback_data": f"cb:toggle_q:{qid}:1"}
        )
        buttons.append([
            {"text": f"{prefix}{short_lbl}", "callback_data": f"cb:show_ads:{qid}"},
            {"text": f"Цены {idx}" if len(queries) > 1 else "Цены", "callback_data": f"cb:market_stats:{qid}"}
        ])
        buttons.append([
            toggle_btn,
            {"text": f"× Удалить {idx}" if len(queries) > 1 else "× Удалить", "callback_data": f"cb:del:{qid}"}
        ])

    if len(queries) > 6:
        blocks.append(f"<i>...ещё {len(queries) - 6} поисков доступно в Mini App.</i>")

    buttons.append([
        {"text": "+ Добавить", "callback_data": "cb:add_options"},
        {"text": "‹ Назад", "callback_data": "cb:menu"}
    ])
    _send_or_edit(tg, chat_id, "\n\n".join(blocks), reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


def show_favorites_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Displays saved favorite ads cleanly."""
    favs = get_user_favorites(chat_id)
    if not favs:
        text = (
            "<b>ИЗБРАННОЕ</b>\n"
            "────────────────────\n"
            "Пока пусто. Отмечайте интересные объявления кнопкой <b>«☆ В избр.»</b>."
        )
        markup = {
            "inline_keyboard": [
                [{"text": "Свежие в чат", "callback_data": "cb:all_ads"}],
                [{"text": "‹ Назад", "callback_data": "cb:menu"}]
            ]
        }
        _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=edit_msg_id)
        return

    tg.send_message(chat_id, f"<b>ИЗБРАННОЕ · {len(favs)}</b>")

    for f in favs[:10]:
        ad_id = f["ad_id"]
        title = html.escape(f.get("title") or f"Объявление #{ad_id}")
        price_byn = f.get("price_byn")
        price_usd = f.get("price_usd")
        if price_byn:
            price_str = f"<b>{price_byn:,.2f} BYN</b>".replace(",", " ")
            if price_usd:
                price_str += f" <i>(~${price_usd:,.0f})</i>".replace(",", " ")
        else:
            price_str = "<b>Цена не указана</b>"

        sname = html.escape(f.get("seller_name") or "Частное лицо")
        link = f.get("ad_link") or f"https://www.kufar.by/item/{ad_id}"

        item_text = (
            f"<b>{title}</b>\n"
            f"{price_str}\n"
            f"Продавец: {sname}"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Открыть ↗", "url": link},
                    {"text": "Написать", "callback_data": f"cb:contact:{ad_id}"}
                ],
                [{"text": "× Убрать", "callback_data": f"cb:unfav_list:{ad_id}"}]
            ]
        }
        img_url = f.get("image_url")
        if img_url:
            tg.send_photo(chat_id, img_url, caption=item_text, reply_markup=markup)
        else:
            tg.send_message(chat_id, item_text, reply_markup=markup, disable_web_page_preview=True)

    if len(favs) > 10:
        tg.send_message(chat_id, f"<i>...ещё {len(favs) - 10} объявлений доступно в Mini App.</i>")

    clear_markup = {
        "inline_keyboard": [
            [{"text": "Очистить всё", "callback_data": "cb:fav_clear"}, {"text": "‹ В меню", "callback_data": "cb:menu"}]
        ]
    }
    tg.send_message(chat_id, "<b>Управление избранным</b>", reply_markup=clear_markup)


def show_blacklist_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Displays blacklisted ads count and blocked sellers with one-click unblocking in-place."""
    bl_ads_count = get_blacklist_count(chat_id)
    bl_sellers = sorted(get_blacklisted_sellers(chat_id))

    sellers_str = (
        "\n".join(f"• <code>{html.escape(s)}</code>" for s in bl_sellers)
        if bl_sellers else "<i>нет заблокированных продавцов</i>"
    )
    text = (
        f"<b>ЧЁРНЫЙ СПИСОК</b>\n"
        f"────────────────────\n"
        f"<b>Скрытых объявлений:</b> {bl_ads_count} шт.\n"
        f"<b>Заблокированных авторов ({len(bl_sellers)}):</b>\n{sellers_str}"
    )
    buttons = []
    for idx, sname in enumerate(bl_sellers[:8]):
        short_s = sname[:20] + "…" if len(sname) > 21 else sname
        buttons.append([{"text": f"↩ Разблокировать: {short_s}", "callback_data": f"cb:unbl_seller:{idx}"}])
    if bl_ads_count > 0:
        buttons.append([{"text": f"Очистить ЧС объявлений ({bl_ads_count})", "callback_data": "cb:bl_clear_ads"}])
    buttons.append([{"text": "‹ Назад в настройки", "callback_data": "cb:settings"}])
    _send_or_edit(tg, chat_id, text, reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


def show_silent_mode_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Displays menu for configuring night/silent hours in-place."""
    current_mode = get_silent_mode(chat_id)
    is_active_now = is_in_silent_hours(chat_id)

    if not current_mode or current_mode.lower() in ("off", "none", "disabled", "0"):
        status_str = "<b>Выключен</b>\n<i>Уведомления приходят со звуком круглосуточно.</i>"
    else:
        active_badge = " · <b>активно сейчас</b>" if is_active_now else ""
        status_str = (
            f"<b>Включен: {html.escape(current_mode)}</b>{active_badge}\n"
            f"<i>В эти часы уведомления приходят без звука.</i>"
        )

    text = (
        f"<b>ТИХИЙ РЕЖИМ</b>\n"
        f"────────────────────\n"
        f"{status_str}"
    )
    buttons = [
        [
            {"text": "23:00 – 08:00", "callback_data": "cb:silent_set:23:00-08:00"},
            {"text": "00:00 – 09:00", "callback_data": "cb:silent_set:00:00-09:00"}
        ],
        [
            {"text": "Свои часы", "callback_data": "cb:silent_custom"},
            {"text": "Выключить", "callback_data": "cb:silent_set:off"}
        ],
        [{"text": "‹ Назад в настройки", "callback_data": "cb:settings"}]
    ]
    _send_or_edit(tg, chat_id, text, reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


def show_interval_options(tg: TelegramClient, chat_id: int, current: int, edit_msg_id: Optional[int] = None):
    """Shows speed/interval options in-place."""
    text = (
        f"<b>ИНТЕРВАЛ ПРОВЕРКИ</b>\n"
        f"────────────────────\n"
        f"Текущий интервал: <b>{current} сек.</b>\n"
        f"<i>Режимы 5–10 сек. подходят для быстрой «снайперской» охоты.</i>"
    )
    def _lbl(sec: int) -> str:
        return f"● {sec} сек" if int(current) == sec else f"{sec} сек"

    markup = {
        "inline_keyboard": [
            [
                {"text": _lbl(5), "callback_data": "cb:interval:5"},
                {"text": _lbl(10), "callback_data": "cb:interval:10"},
                {"text": _lbl(15), "callback_data": "cb:interval:15"}
            ],
            [
                {"text": _lbl(30), "callback_data": "cb:interval:30"},
                {"text": _lbl(60), "callback_data": "cb:interval:60"},
                {"text": _lbl(120), "callback_data": "cb:interval:120"}
            ],
            [{"text": "‹ Назад в настройки", "callback_data": "cb:settings"}]
        ]
    }
    _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=edit_msg_id)


def show_priority_mode_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Shows notification priority mode options in-place."""
    cur = get_user_priority_mode(chat_id)
    def _pl(mode: str, title: str) -> str:
        return f"● {title}" if cur == mode else title

    text = (
        f"<b>ГРОМКИЙ ПРИОРИТЕТ</b>\n"
        f"────────────────────\n"
        f"Приоритетными считаются объявления по <b>+словам</b>, со скидкой от <b>-18% от рынка</b> или со снижением цены на 10%+.\n\n"
        f"Текущий режим: <b>{html.escape(PRIORITY_MODE_LABELS.get(cur, cur))}</b>"
    )
    markup = {
        "inline_keyboard": [
            [{"text": _pl("hot", "Пробой тишины (горячие громко)"), "callback_data": "cb:priority_set:hot"}],
            [{"text": _pl("only_hot", "Только топ-лоты громко, остальные тихо"), "callback_data": "cb:priority_set:only_hot"}],
            [{"text": _pl("off", "Обычный режим (строго по часам тишины)"), "callback_data": "cb:priority_set:off"}],
            [{"text": "‹ Назад в настройки", "callback_data": "cb:settings"}]
        ]
    }
    _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=edit_msg_id)


def show_stop_words_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Displays current stop words and interactive management buttons in-place."""
    sw_list = get_stop_words(chat_id)
    if sw_list:
        words_str = ", ".join(f"<code>{html.escape(w)}</code>" for w in sw_list)
        text = (
            f"<b>СТОП-СЛОВА</b>\n"
            f"────────────────────\n"
            f"{words_str}\n\n"
            f"<i>Объявления с этими словами скрываются автоматически.</i>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "+ Добавить", "callback_data": "cb:sw_add"},
                    {"text": "Удалить слово", "callback_data": "cb:sw_del_menu"}
                ],
                [{"text": "Очистить все", "callback_data": "cb:sw_clear"}],
                [{"text": "‹ Назад в настройки", "callback_data": "cb:settings"}]
            ]
        }
    else:
        text = (
            "<b>СТОП-СЛОВА</b>\n"
            "────────────────────\n"
            "Список пуст. Добавьте слова-исключения (напр. <code>аренда, чехол, запчасти</code>)."
        )
        markup = {
            "inline_keyboard": [
                [{"text": "+ Добавить стоп-слова", "callback_data": "cb:sw_add"}],
                [{"text": "‹ Назад в настройки", "callback_data": "cb:settings"}]
            ]
        }
    _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=edit_msg_id)


def show_template_menu(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Displays current quick contact message template in-place."""
    tpl = get_user_template(chat_id)
    text = (
        "<b>ШАБЛОН ОТВЕТА ПРОДАВЦУ</b>\n"
        "────────────────────\n"
        f"<code>{html.escape(tpl)}</code>\n\n"
        "<i>Переменные:</i> <code>{name}</code> · <code>{item}</code> · <code>{price}</code>"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "Изменить", "callback_data": "cb:tpl_edit"},
                {"text": "Сбросить", "callback_data": "cb:tpl_reset"}
            ],
            [{"text": "‹ Назад в настройки", "callback_data": "cb:settings"}]
        ]
    }
    _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=edit_msg_id)


def handle_link_submission(tg: TelegramClient, chat_id: int, url: str):
    """Parses a Kufar URL submitted by the user and asks for confirmation."""
    params = parse_kufar_url_or_query(url)

    # Protection against empty-category and empty-query searches (e.g. searching all of Kufar)
    cat_id = params.get("cat")
    query_str = params.get("query")

    if not query_str and (not cat_id or str(cat_id) == "all"):
        text = (
            "<b>КАТЕГОРИЯ НЕ ВЫБРАНА</b>\n"
            "────────────────────\n"
            "В ссылке заданы только регион или цена. Чтобы не получать все объявления подряд, выберите категорию:"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "Игры и приставки", "callback_data": "cb:cat_fix:5040"}],
                [{"text": "Мобильные телефоны", "callback_data": "cb:cat_fix:17010"}],
                [{"text": "Ноутбуки и ПК", "callback_data": "cb:cat_fix:16000"}],
                [{"text": "Вся электроника", "callback_data": "cb:cat_fix:5000"}],
                [{"text": "× Отмена", "callback_data": "cb:menu"}]
            ]
        }
        USER_STATES[chat_id] = {"step": "WAIT_CAT_FIX", "params": params}
        tg.send_message(chat_id, text, reply_markup=markup)
        return

    ads = fetch_ads(params)
    count = len(ads)

    query_name = params.get("query")
    if not query_name:
        cat_name = dict(CATEGORIES).get(str(cat_id), "Куфар поиск")
        brand = params.get("brand") or ("Sony" if str(params.get("cbc")) == "31" else "")
        if brand:
            query_name = f"{cat_name} ({brand})"
        else:
            query_name = cat_name

    if len(PENDING_QUERIES) > 200:
        for old_k in list(PENDING_QUERIES.keys())[:100]:
            PENDING_QUERIES.pop(old_k, None)
    pending_id = f"p_{chat_id}_{int(time.time() * 1000)}"
    PENDING_QUERIES[pending_id] = {
        "chat_id": chat_id,
        "name": query_name,
        "params": params,
        "initial_ads": [a["ad_id"] for a in ads]
    }

    text = (
        f"<b>ФИЛЬТРЫ РАСПОЗНАНЫ</b>\n"
        f"────────────────────\n"
        f"<b>Название:</b> {html.escape(query_name)}\n\n"
        f"{format_params_human(params)}\n\n"
        f"<b>Найдено сейчас:</b> {count} объявл."
    )
    markup = {
        "inline_keyboard": [
            [{"text": "✓ Подтвердить и запустить", "callback_data": f"cb:confirm:{pending_id}"}],
            [{"text": "× Отмена", "callback_data": f"cb:cancel:{pending_id}"}]
        ]
    }
    tg.send_message(chat_id, text, reply_markup=markup)


# ==================== WIZARD STEPS ====================

def wizard_step_category(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Step 1: Category selection (in-place)."""
    USER_STATES[chat_id] = {"step": "CAT", "data": {}}
    text = "<b>ШАГ 1 / 4 · КАТЕГОРИЯ</b>"
    buttons = []
    row = []
    for cid, cname in CATEGORIES:
        row.append({"text": cname, "callback_data": f"cb:wiz_cat:{cid}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "‹ Назад", "callback_data": "cb:add_options"}])

    _send_or_edit(tg, chat_id, text, reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


def wizard_step_region(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Step 2: Region selection (in-place)."""
    USER_STATES[chat_id]["step"] = "RGN"
    text = "<b>ШАГ 2 / 4 · РЕГИОН</b>"
    buttons = []
    row = []
    for rid, rname in REGIONS:
        row.append({"text": rname, "callback_data": f"cb:wiz_rgn:{rid}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "× Отмена", "callback_data": "cb:menu"}])

    _send_or_edit(tg, chat_id, text, reply_markup={"inline_keyboard": buttons}, edit_msg_id=edit_msg_id)


def wizard_step_query(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Step 3: Keyword prompt (in-place)."""
    USER_STATES[chat_id]["step"] = "QUERY"
    text = (
        "<b>ШАГ 3 / 4 · КЛЮЧЕВЫЕ СЛОВА</b>\n"
        "────────────────────\n"
        "Напр.: <code>ps5 slim</code>, <code>rtx 4070</code>, <code>iphone 13</code>."
    )
    markup = {
        "inline_keyboard": [
            [{"text": "Пропустить слова ›", "callback_data": "cb:wiz_skip_query"}],
            [{"text": "× Отмена", "callback_data": "cb:menu"}]
        ]
    }
    _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=edit_msg_id)


def wizard_step_price(tg: TelegramClient, chat_id: int, edit_msg_id: Optional[int] = None):
    """Step 4: Price bounds prompt (in-place)."""
    USER_STATES[chat_id]["step"] = "PRICE"
    text = (
        "<b>ШАГ 4 / 4 · БЮДЖЕТ (BYN)</b>\n"
        "────────────────────\n"
        "Укажите диапазон: <code>1000 2100</code>, <code>до 1500</code> или <code>от 800</code>."
    )
    markup = {
        "inline_keyboard": [
            [{"text": "Любая цена ›", "callback_data": "cb:wiz_skip_price"}],
            [{"text": "× Отмена", "callback_data": "cb:menu"}]
        ]
    }
    _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=edit_msg_id)


def wizard_finish(tg: TelegramClient, chat_id: int):
    """Compiles wizard results and presents summary."""
    data = USER_STATES.get(chat_id, {}).get("data", {})
    USER_STATES.pop(chat_id, None)

    params: Dict[str, Any] = {"sort": "lst.d", "size": "30"}
    
    if data.get("query"):
        params["query"] = data["query"]
    if data.get("cat") and data["cat"] != "all":
        params["cat"] = data["cat"]
    if data.get("rgn") and data["rgn"] != "all":
        params["rgn"] = data["rgn"]
        if data["rgn"] == "7":
            # For Minsk, default to all districts
            params["ar"] = "v.or:22,23,24,25,26,27,28,29,30"
    if data.get("prc"):
        params["prc"] = data["prc"]

    ads = fetch_ads(params)
    count = len(ads)

    name = data.get("query") or dict(CATEGORIES).get(data.get("cat", ""), "Поиск")

    if len(PENDING_QUERIES) > 200:
        for old_k in list(PENDING_QUERIES.keys())[:100]:
            PENDING_QUERIES.pop(old_k, None)
    pending_id = f"p_{chat_id}_{int(time.time() * 1000)}"
    PENDING_QUERIES[pending_id] = {
        "chat_id": chat_id,
        "name": name,
        "params": params,
        "initial_ads": [a["ad_id"] for a in ads]
    }

    text = (
        f"<b>ФИЛЬТР СФОРМИРОВАН</b>\n"
        f"────────────────────\n"
        f"<b>Название:</b> {html.escape(name)}\n\n"
        f"{format_params_human(params)}\n\n"
        f"<b>Найдено сейчас:</b> {count} объявл."
    )
    markup = {
        "inline_keyboard": [
            [{"text": "✓ Запустить отслеживание", "callback_data": f"cb:confirm:{pending_id}"}],
            [{"text": "× Отмена", "callback_data": f"cb:cancel:{pending_id}"}]
        ]
    }
    tg.send_message(chat_id, text, reply_markup=markup)


# ==================== MAIN DISPATCHER ====================

def process_callback_query(tg: TelegramClient, cb: Dict[str, Any], config: dict):
    """Handles all inline button clicks."""
    cb_id = cb["id"]
    from_user = cb.get("from", {})
    chat_id = from_user.get("id")
    data = cb.get("data", "")
    msg = cb.get("message", {})
    msg_id = msg.get("message_id")

    # Register user and notify admin if new
    check_and_notify_new_user(tg, from_user, config)

    answered = False

    def ack(text: Optional[str] = None, show_alert: bool = False):
        nonlocal answered
        if not answered:
            answered = True
            tg.answer_callback_query(cb_id, text=text, show_alert=show_alert)

    try:
        _dispatch_callback_query(tg, chat_id, data, msg, msg_id, from_user, config, ack)
    finally:
        if not answered:
            tg.answer_callback_query(cb_id)


def _dispatch_callback_query(
    tg: TelegramClient,
    chat_id: int,
    data: str,
    msg: Dict[str, Any],
    msg_id: Optional[int],
    from_user: Dict[str, Any],
    config: dict,
    ack
):
    """Routes inline callback queries with in-place submenu navigation and proper toast feedback."""
    if data == "cb:menu":
        ack()
        USER_STATES.pop(chat_id, None)
        show_main_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:close_msg":
        ack()
        if msg_id:
            tg.delete_message(chat_id, msg_id)

    elif data == "cb:settings":
        ack()
        USER_STATES.pop(chat_id, None)
        show_settings_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:stats":
        ack()
        USER_STATES.pop(chat_id, None)
        show_stats_menu(tg, chat_id, config, edit_msg_id=msg_id)

    elif data == "cb:help":
        ack()
        USER_STATES.pop(chat_id, None)
        show_help_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:add_options":
        ack()
        USER_STATES.pop(chat_id, None)
        prompt_add_search_options(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:interval_menu":
        ack()
        USER_STATES.pop(chat_id, None)
        show_interval_options(tg, chat_id, config.get("check_interval_seconds", 60), edit_msg_id=msg_id)

    elif data == "cb:stop_output":
        STOP_OUTPUT_REQUESTED.add(chat_id)
        ack(text="🛑 Останавливаю вывод объявлений...")

    elif data in ("cb:my_searches", "cb:list"):
        ack()
        USER_STATES.pop(chat_id, None)
        show_my_searches(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:all_ads":
        ack(text="📦 Загружаю актуальные объявления...")
        USER_STATES.pop(chat_id, None)
        output_all_user_ads(tg, chat_id)

    elif data in ("cb:pause", "cb:hub_pause"):
        set_user_monitoring_paused(chat_id, True)
        save_local_backup()
        ack(text="⏸ Мониторинг поставлен на паузу")
        show_main_menu(tg, chat_id, edit_msg_id=msg_id if data == "cb:hub_pause" else None)

    elif data in ("cb:resume", "cb:hub_resume"):
        set_user_monitoring_paused(chat_id, False)
        save_local_backup()
        ack(text="▶️ Мониторинг возобновлен!")
        FORCE_CHECK_EVENT.set()
        show_main_menu(tg, chat_id, edit_msg_id=msg_id if data == "cb:hub_resume" else None)

    elif data.startswith("cb:toggle_q:"):
        parts = data.split(":")
        qid = int(parts[2])
        make_active = bool(int(parts[3]))
        toggle_query_active(qid, make_active, chat_id=chat_id)
        save_local_backup()
        status_txt = "возобновлен (активен)" if make_active else "поставлен на паузу"
        ack(text=f"Поиск [ID: {qid}] {status_txt}")
        show_my_searches(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:add_link":
        ack()
        USER_STATES[chat_id] = {"step": "WAIT_URL", "data": {}}
        text = (
            "<b>ССЫЛКА С KUFAR.BY</b>\n"
            "────────────────────\n"
            "Настройте фильтры на сайте, скопируйте ссылку из адресной строки и отправьте сообщением в чат."
        )
        markup = {"inline_keyboard": [[{"text": "‹ Назад", "callback_data": "cb:add_options"}]]}
        _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=msg_id)

    elif data == "cb:wiz_start":
        ack()
        wizard_step_category(tg, chat_id, edit_msg_id=msg_id)

    elif data.startswith("cb:wiz_cat:"):
        ack()
        cat_id = data.split(":")[-1]
        if chat_id not in USER_STATES:
            USER_STATES[chat_id] = {"step": "CAT", "data": {}}
        USER_STATES[chat_id]["data"]["cat"] = cat_id
        wizard_step_region(tg, chat_id, edit_msg_id=msg_id)

    elif data.startswith("cb:wiz_rgn:"):
        ack()
        rgn_id = data.split(":")[-1]
        if chat_id not in USER_STATES:
            USER_STATES[chat_id] = {"step": "RGN", "data": {}}
        USER_STATES[chat_id]["data"]["rgn"] = rgn_id
        wizard_step_query(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:wiz_skip_query":
        ack()
        if chat_id not in USER_STATES:
            USER_STATES[chat_id] = {"step": "QUERY", "data": {}}
        wizard_step_price(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:wiz_skip_price":
        ack()
        wizard_finish(tg, chat_id)

    elif data.startswith("cb:cat_fix:"):
        cat_id = data.split(":")[-1]
        state = USER_STATES.pop(chat_id, {})
        params = state.get("params", {})
        params["cat"] = cat_id
        ads = fetch_ads(params)
        count = len(ads)
        cat_name = dict(CATEGORIES).get(str(cat_id), "Поиск товаров")
        brand = params.get("brand") or ("Sony" if str(params.get("cbc")) == "31" else "")
        if brand and not params.get("query"):
            query_name = f"{cat_name} ({brand})"
        else:
            query_name = params.get("query") or cat_name

        if len(PENDING_QUERIES) > 200:
            for old_k in list(PENDING_QUERIES.keys())[:100]:
                PENDING_QUERIES.pop(old_k, None)
        pending_id = f"p_{chat_id}_{int(time.time() * 1000)}"
        PENDING_QUERIES[pending_id] = {
            "chat_id": chat_id,
            "name": query_name,
            "params": params,
            "initial_ads": [a["ad_id"] for a in ads]
        }
        text = (
            f"🔎 <b>Распознаны фильтры Куфара 1 в 1:</b>\n\n"
            f"🏷 <b>Название:</b> {html.escape(query_name)}\n\n"
            f"{format_params_human(params)}\n\n"
            f"📊 <b>Найдено сейчас на сайте:</b> {count} объявлений\n\n"
            f"Запустить отслеживание новых объявлений по этим фильтрам?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "✓ Подтвердить и запустить", "callback_data": f"cb:confirm:{pending_id}"}],
                [{"text": "× Отмена", "callback_data": f"cb:cancel:{pending_id}"}]
            ]
        }
        tg.send_message(chat_id, text, reply_markup=markup)

    elif data.startswith("cb:confirm:"):
        pending_id = data.split(":")[-1]
        item = PENDING_QUERIES.pop(pending_id, None)
        if not item:
            tg.send_message(chat_id, "⚠️ Данный поиск устарел. Создайте его заново.")
            return

        qid = add_query(chat_id, item["name"], item["params"])
        if item["initial_ads"]:
            mark_ads_seen(qid, item["initial_ads"])

        # Notify admin of new filter
        notify_admin_new_filter(tg, chat_id, from_user, qid, item["name"], item["params"], config)

        # Save locally; scheduled Telegram Cloud backup handles periodic sync without spam
        save_local_backup()

        tg.send_message(
            chat_id,
            f"🎉 <b>Поиск «{html.escape(item['name'])}» успешно активирован!</b> [ID: {qid}]\n\n"
            f"🔔 Теперь я буду непрерывно мониторить Kufar и <b>мгновенно</b> присылать новые объявления!",
            reply_markup=get_main_keyboard(chat_id)
        )

    elif data.startswith("cb:cancel:"):
        pending_id = data.split(":")[-1]
        PENDING_QUERIES.pop(pending_id, None)
        if msg_id:
            tg.delete_message(chat_id, msg_id)
        tg.send_message(chat_id, "❌ Настройка поиска отменена.", reply_markup=get_main_keyboard(chat_id))

    elif data.startswith("cb:del:"):
        qid = int(data.split(":")[-1])
        delete_query(qid, chat_id=chat_id)
        save_local_backup()
        ack(text=f"🗑 Поиск [ID: {qid}] удалён")
        show_my_searches(tg, chat_id, edit_msg_id=msg_id)
        admin_chat_id = config.get("admin_chat_id", 0)
        if admin_chat_id and chat_id != admin_chat_id:
            try:
                tg.send_message(admin_chat_id, f"🗑 <b>Пользователь {chat_id} удалил свой поиск [ID: {qid}].</b>")
            except Exception:
                pass

    elif data.startswith("cb:show_ads:") or data.startswith("cb:check:"):
        qid = int(data.split(":")[-1])
        ack(text="📦 Загружаю объявления...")
        output_ads_for_query(tg, chat_id, qid)

    elif data.startswith("cb:photos:"):
        ack(text="📸 Все фотографии к объявлению уже отображены в чате")

    elif data.startswith("cb:contact:"):
        ad_id = int(data.split(":")[-1])
        ad = AD_DATA_CACHE.get(ad_id, {})
        ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
        seller_raw = (ad.get("seller") or "").strip()
        subject_raw = (ad.get("subject") or "").strip()
        personalized_msg = build_personalized_message(chat_id, ad)

        ack()
        text = (
            f"💬 <b>Готовое сообщение продавцу:</b>\n"
            f"👤 Продавец: <b>{html.escape(seller_raw or 'Не указан')}</b>\n"
            f"📌 Товар: <b>{html.escape(subject_raw or 'Товар')}</b>\n\n"
            f"<code>{html.escape(personalized_msg)}</code>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Открыть диалог на Kufar ↗", "url": ad_link},
                    {"text": "Скопировать текст", "copy_text": {"text": personalized_msg[:256]}},
                ],
                [{"text": "× Закрыть", "callback_data": "cb:close_msg"}]
            ]
        }
        tg.send_message(chat_id, text, reply_markup=markup)

    elif data.startswith("cb:fav:"):
        ad_id = int(data.split(":")[-1])
        ad = AD_DATA_CACHE.get(ad_id, {})
        title = ad.get("subject", "")
        price_byn = ad.get("price_byn")
        price_usd = ad.get("price_usd")
        ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
        sname = ad.get("seller", "")
        img_url = ad.get("image_url", "")

        add_favorite(chat_id, ad_id, title=title, price_byn=price_byn, price_usd=price_usd, ad_link=ad_link, seller_name=sname, image_url=img_url)
        save_local_backup()
        ack(text="⭐️ Добавлено в Избранное!", show_alert=False)

        is_bl = is_ad_blacklisted(chat_id, ad_id)
        photos = AD_PHOTOS_CACHE.get(ad_id, [])
        new_markup = make_ad_keyboard(
            ad_link, ad_id,
            photo_count=len(photos),
            seller_name=sname,
            is_blacklisted=is_bl,
            is_fav=True,
            copy_msg=build_personalized_message(chat_id, ad) if ad else None,
            phone_number=ad.get("_resolved_phone")
        )
        try:
            tg.edit_message_reply_markup(chat_id, msg_id, reply_markup=new_markup)
        except Exception:
            pass

    elif data.startswith("cb:unfav:"):
        ad_id = int(data.split(":")[-1])
        remove_favorite(chat_id, ad_id)
        save_local_backup()
        ack(text="Удалено из Избранного", show_alert=False)

        ad = AD_DATA_CACHE.get(ad_id, {})
        ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
        sname = ad.get("seller", "")
        is_bl = is_ad_blacklisted(chat_id, ad_id)
        photos = AD_PHOTOS_CACHE.get(ad_id, [])
        new_markup = make_ad_keyboard(
            ad_link, ad_id,
            photo_count=len(photos),
            seller_name=sname,
            is_blacklisted=is_bl,
            is_fav=False,
            copy_msg=build_personalized_message(chat_id, ad) if ad else None,
            phone_number=ad.get("_resolved_phone")
        )
        try:
            tg.edit_message_reply_markup(chat_id, msg_id, reply_markup=new_markup)
        except Exception:
            pass

    elif data.startswith("cb:unfav_list:"):
        ad_id = int(data.split(":")[-1])
        remove_favorite(chat_id, ad_id)
        save_local_backup()
        ack(text="Удалено из Избранного", show_alert=False)
        try:
            tg.delete_message(chat_id, msg_id)
        except Exception:
            pass

    elif data == "cb:fav_clear":
        cnt = clear_user_favorites(chat_id)
        save_local_backup()
        ack(text=f"Удалено {cnt} объявлений из Избранного", show_alert=True)
        show_favorites_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:fav_menu":
        ack()
        USER_STATES.pop(chat_id, None)
        favs = get_user_favorites(chat_id)
        show_favorites_menu(tg, chat_id, edit_msg_id=msg_id if not favs else None)

    elif data.startswith("cb:market_stats:"):
        qid = int(data.split(":")[-1])
        queries = get_user_queries(chat_id)
        q = next((item for item in queries if item["id"] == qid), None)
        if not q:
            ack(text="Поиск не найден", show_alert=True)
            return

        ack(text="📊 Анализирую рынок на Kufar...", show_alert=False)
        params = dict(q["params"])
        params["size"] = "50"
        ads = fetch_ads(params)

        if not ads:
            tg.send_message(chat_id, f"ℹ️ По запросу «{html.escape(q['name'])}» сейчас нет объявлений на Kufar.")
            return

        prices = [a["price_byn"] for a in ads if a.get("price_byn") and a["price_byn"] > 0]
        if not prices:
            tg.send_message(chat_id, f"ℹ️ По запросу «{html.escape(q['name'])}» нет объявлений с указанной ценой.")
            return

        min_p = min(prices)
        max_p = max(prices)
        med_p = round(float(statistics.median(prices)), 2)
        avg_p = round(float(statistics.mean(prices)), 2)

        sorted_ads = sorted([a for a in ads if a.get("price_byn") and a["price_byn"] > 0], key=lambda x: x["price_byn"])
        top3_lines = []
        for i, a in enumerate(sorted_ads[:3], 1):
            top3_lines.append(
                f"{i}. <a href=\"{a['ad_link']}\">{html.escape(a['subject'])}</a> — <b>{a['price_byn']:,.0f} BYN</b>".replace(",", " ")
            )

        report = (
            f"📊 <b>Аналитика цен: {html.escape(q['name'])}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Проанализировано объявлений:</b> {len(prices)} шт.\n\n"
            f"💵 <b>Минимальная цена:</b> <b>{min_p:,.0f} BYN</b>\n"
            f"⚖️ <b>Медианная (рыночная):</b> <b>{med_p:,.0f} BYN</b>\n"
            f"📈 <b>Средняя цена:</b> {avg_p:,.0f} BYN\n"
            f"🏷 <b>Максимальная цена:</b> {max_p:,.0f} BYN\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 <b>Топ-3 самых выгодных предложений прямо сейчас:</b>\n"
            + "\n".join(top3_lines)
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Показать объявления", "callback_data": f"cb:show_ads:{qid}"},
                    {"text": "× Закрыть", "callback_data": "cb:close_msg"}
                ]
            ]
        }
        tg.send_message(chat_id, report, reply_markup=markup, disable_web_page_preview=True)

    elif data == "cb:silent_menu":
        ack()
        USER_STATES.pop(chat_id, None)
        show_silent_mode_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data.startswith("cb:silent_set:"):
        val = data.split(":", 2)[-1]
        set_silent_mode(chat_id, val)
        save_local_backup()
        ack(text="Настройки тишины обновлены!")
        show_silent_mode_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:silent_custom":
        ack()
        USER_STATES[chat_id] = {"step": "WAIT_SILENT_HOURS"}
        text = (
            "🌙 <b>Настройка часов тишины</b>\n\n"
            "Отправьте сообщением желаемый интервал в формате <code>ЧЧ:ММ-ЧЧ:ММ</code>\n"
            "(например: <code>22:30-07:30</code> или <code>01:00-09:00</code>):"
        )
        markup = {"inline_keyboard": [[{"text": "‹ Назад", "callback_data": "cb:silent_menu"}]]}
        _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=msg_id)

    elif data == "cb:bl_menu":
        ack()
        USER_STATES.pop(chat_id, None)
        show_blacklist_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data.startswith("cb:unbl_seller:"):
        idx = int(data.split(":")[-1])
        bl_sellers = sorted(get_blacklisted_sellers(chat_id))
        if 0 <= idx < len(bl_sellers):
            sname = bl_sellers[idx]
            remove_blacklisted_seller(chat_id, sname)
            save_local_backup()
            ack(text=f"✅ Продавец «{sname}» разблокирован!", show_alert=False)
        else:
            ack(text="Продавец уже удалён из ЧС", show_alert=False)
        show_blacklist_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:bl_clear_ads":
        cnt = clear_user_blacklist(chat_id)
        save_local_backup()
        ack(text=f"🧹 ЧС объявлений очищен! Удалено: {cnt}", show_alert=True)
        show_blacklist_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data.startswith("cb:bl:"):
        ad_id = int(data.split(":")[-1])
        add_to_blacklist(chat_id, ad_id)
        save_local_backup()
        ack(text="🚫 Объявление добавлено в ЧС и удалено!", show_alert=False)
        _delete_ad_messages(tg, chat_id, ad_id, msg_id)

    elif data.startswith("cb:bl_seller:"):
        ad_id = int(data.split(":")[-1])
        ad = AD_DATA_CACHE.get(ad_id, {})
        sname = (ad.get("seller") or "").strip()
        account_id = ad.get("account_id")
        if sname:
            add_blacklisted_seller(chat_id, sname, account_id)
        add_to_blacklist(chat_id, ad_id)
        save_local_backup()
        disp_name = f"«{sname}»" if sname else "данного продавца"
        ack(
            text=f"👤 Продавец {disp_name} добавлен в ЧС! Все его объявления скрыты.",
            show_alert=True
        )
        _delete_ad_messages(tg, chat_id, ad_id, msg_id)

    elif data.startswith("cb:unbl:"):
        ad_id = int(data.split(":")[-1])
        remove_from_blacklist(chat_id, ad_id)
        save_local_backup()
        ack(text="✅ Объявление убрано из ЧС", show_alert=False)
        ad = AD_DATA_CACHE.get(ad_id, {})
        ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
        photos = AD_PHOTOS_CACHE.get(ad_id, [])
        new_markup = make_ad_keyboard(
            ad_link, ad_id,
            photo_count=len(photos),
            seller_name=ad.get("seller"),
            is_blacklisted=False,
            is_fav=is_favorite(chat_id, ad_id),
            copy_msg=build_personalized_message(chat_id, ad),
            phone_number=ad.get("_resolved_phone")
        )
        if msg.get("text"):
            tg.edit_message_text(
                chat_id,
                msg_id,
                text="⚡ <b>Действия с объявлением:</b>",
                reply_markup=new_markup
            )
        else:
            tg.edit_message_reply_markup(chat_id, msg_id, reply_markup=new_markup)

    elif data == "cb:sw_menu":
        ack()
        USER_STATES.pop(chat_id, None)
        show_stop_words_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:sw_add":
        ack()
        USER_STATES[chat_id] = {"step": "WAIT_STOP_WORDS"}
        text = (
            "⛔ <b>Добавление стоп-слов</b>\n\n"
            "Отправьте слова, при наличии которых в названии или описании объявление будет скрыто.\n"
            "Можно через запятую (напр.: <code>аренда, чехол, запчасти, коробка</code>):"
        )
        markup = {"inline_keyboard": [[{"text": "‹ Назад", "callback_data": "cb:sw_menu"}]]}
        _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=msg_id)

    elif data == "cb:sw_del_menu":
        ack()
        sw_list = get_stop_words(chat_id)
        if not sw_list:
            show_stop_words_menu(tg, chat_id, edit_msg_id=msg_id)
            return
        buttons = []
        for w in sw_list:
            buttons.append([{"text": f"× {w}", "callback_data": f"cb:sw_del:{w}"}])
        buttons.append([{"text": "‹ Назад", "callback_data": "cb:sw_menu"}])
        _send_or_edit(tg, chat_id, "Выберите слово для удаления:", reply_markup={"inline_keyboard": buttons}, edit_msg_id=msg_id)

    elif data.startswith("cb:sw_del:"):
        word = data[len("cb:sw_del:"):]
        remove_stop_word(chat_id, word)
        save_local_backup()
        ack(text=f"Удалено: {word}")
        show_stop_words_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:sw_clear":
        cnt = clear_stop_words(chat_id)
        save_local_backup()
        ack(text=f"🧹 Удалено {cnt} стоп-слов!", show_alert=True)
        show_stop_words_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:tpl_menu":
        ack()
        USER_STATES.pop(chat_id, None)
        show_template_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:tpl_edit":
        ack()
        USER_STATES[chat_id] = {"step": "WAIT_TEMPLATE"}
        text = (
            "✏️ <b>Редактирование шаблона</b>\n\n"
            "Отправьте новым сообщением текст для первого сообщения продавцу\n"
            "(можно использовать <code>{name}</code>, <code>{item}</code>, <code>{price}</code>):"
        )
        markup = {"inline_keyboard": [[{"text": "‹ Назад", "callback_data": "cb:tpl_menu"}]]}
        _send_or_edit(tg, chat_id, text, reply_markup=markup, edit_msg_id=msg_id)

    elif data == "cb:tpl_reset":
        set_user_template(chat_id, DEFAULT_CONTACT_TEMPLATE)
        save_local_backup()
        ack(text="🔄 Шаблон сброшен к исходному!", show_alert=True)
        show_template_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:fast_toggle":
        next_fast = not get_user_fast_alert(chat_id)
        set_user_fast_alert(chat_id, next_fast)
        save_local_backup()
        ack(text="⚡ Молния включена!" if next_fast else "📸 Режим: сначала альбом фото")
        show_settings_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data == "cb:priority_menu":
        ack()
        USER_STATES.pop(chat_id, None)
        show_priority_mode_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data.startswith("cb:priority_set:"):
        mode = data.split(":")[-1]
        set_user_priority_mode(chat_id, mode)
        save_local_backup()
        ack(text=f"🔊 {PRIORITY_MODE_LABELS.get(get_user_priority_mode(chat_id), mode)}")
        show_priority_mode_menu(tg, chat_id, edit_msg_id=msg_id)

    elif data.startswith("cb:interval:"):
        sec = max(5, min(600, int(data.split(":")[-1])))
        config["check_interval_seconds"] = sec
        set_setting("check_interval_seconds", str(sec))
        save_local_backup()
        ack(text=f"⏱ Интервал: {sec} сек.")
        FORCE_CHECK_EVENT.set()
        try:
            config_path = os.path.join(BASE_DIR, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving interval: {e}")
        show_interval_options(tg, chat_id, sec, edit_msg_id=msg_id)


def process_text_message(tg: TelegramClient, chat_id: int, text: str, config: dict, user_info: Optional[Dict[str, Any]] = None):
    """Processes normal text messages from any user."""
    admin_chat_id = config.get("admin_chat_id", 0)

    # Register user and notify admin if new
    if user_info:
        check_and_notify_new_user(tg, user_info, config)

    # If no admin configured yet, the first user sending /start becomes admin
    if admin_chat_id == 0 and text.strip() == "/start":
        config["admin_chat_id"] = chat_id
        admin_chat_id = chat_id
        try:
            config_path = os.path.join(BASE_DIR, "config.json")
            if os.path.exists(config_path):
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving admin chat id: {e}")

    text = text.strip()

    # Commands / Menu Buttons
    if text in ["/start", "/menu", "🔙 В главное меню"]:
        USER_STATES.pop(chat_id, None)
        show_main_menu(tg, chat_id)
        return

    if text in ["/hide", "/close", "🔽 Скрыть", "Скрыть клавиатуру"]:
        tg.send_message(chat_id, "⌨️ Клавиатура скрыта. Чтобы открыть меню снова, отправьте /menu или /start.", reply_markup={"remove_keyboard": True})
        return

    if text in ["➕ Добавить поиск", "/add"]:
        prompt_add_search_options(tg, chat_id)
        return

    if text in ["📋 Мои поиски", "/list"]:
        show_my_searches(tg, chat_id)
        return

    if text in ["⭐️ Избранное", "⭐️ Избранное / Закладки", "/favorites", "/fav", "/favs"]:
        USER_STATES.pop(chat_id, None)
        show_favorites_menu(tg, chat_id)
        return

    if text in ["🌙 Ночной режим", "🌙 Тихий режим", "/silent", "/night"]:
        USER_STATES.pop(chat_id, None)
        show_silent_mode_menu(tg, chat_id)
        return

    if text in [
        "🛑 Остановить вывод",
        "Остановить вывод",
        "остановить вывод",
        "хватит",
        "/stop_output",
        "/abort"
    ]:
        STOP_OUTPUT_REQUESTED.add(chat_id)
        tg.send_message(chat_id, "🛑 Останавливаю вывод объявлений...")
        return

    if text in [
        "⏸ Стоп (пауза)",
        "⏸ Стоп",
        "Стоп",
        "стоп",
        "Пауза",
        "пауза",
        "⏸ Пауза",
        "/stop",
        "/pause",
        "стоп бот"
    ]:
        STOP_OUTPUT_REQUESTED.add(chat_id)
        USER_STATES.pop(chat_id, None)
        set_user_monitoring_paused(chat_id, True)
        save_local_backup()
        tg.send_message(chat_id, "⏸ <b>Мониторинг Kufar приостановлен.</b> Ваши фильтры и ЧС сохранены.")
        show_main_menu(tg, chat_id)
        return

    if text in [
        "▶️ Возобновить",
        "Возобновить",
        "возобновить",
        "Пуск",
        "пуск",
        "/resume",
        "/play",
        "старт мониторинга",
        "продолжить"
    ]:
        USER_STATES.pop(chat_id, None)
        set_user_monitoring_paused(chat_id, False)
        save_local_backup()
        FORCE_CHECK_EVENT.set()
        tg.send_message(chat_id, "▶️ <b>Мониторинг Kufar возобновлён!</b>")
        show_main_menu(tg, chat_id)
        return

    if text in ["🚫 Чёрный список", "🚫 Черный список", "/blacklist", "/bl"]:
        USER_STATES.pop(chat_id, None)
        show_blacklist_menu(tg, chat_id)
        return

    if text in ["⏱ Интервал", "/interval"]:
        show_interval_options(tg, chat_id, config.get("check_interval_seconds", 60))
        return

    if text in ["⛔ Стоп-слова", "/stop_words", "/stopwords"]:
        USER_STATES.pop(chat_id, None)
        show_stop_words_menu(tg, chat_id)
        return

    if text in ["💬 Мой шаблон", "/template", "/tpl"]:
        USER_STATES.pop(chat_id, None)
        show_template_menu(tg, chat_id)
        return

    if text in [
        "📦 Вывести всё",
        "📦 Вывести все",
        "Вывести всё",
        "Вывести все",
        "📦 Вывести все объявления",
        "⚡ Проверить сейчас",
        "/check",
        "/all",
        "/ads"
    ]:
        output_all_user_ads(tg, chat_id)
        return

    if text in ["/backup", "💾 Бэкап"]:
        sync_backup_to_telegram(tg, config, force_notify=True)
        return

    if text in ["/restore", "🔄 Восстановить"]:
        ok = restore_state_from_telegram(tg, config)
        if not ok:
            tg.send_message(chat_id, "ℹ️ Нет доступного бэкапа для восстановления или данные уже актуальны.")
        return

    if text in ["/clear_blacklist", "/reset_blacklist"]:
        cnt = clear_user_blacklist(chat_id)
        save_local_backup()
        tg.send_message(chat_id, f"✅ <b>Чёрный список очищен!</b> Удалено записей: {cnt}.", reply_markup=get_main_keyboard(chat_id))
        return

    if text in ["/app", "📱 Приложение", "🚀 Приложение"]:
        USER_STATES.pop(chat_id, None)
        webapp_url = get_webapp_url()
        if webapp_url:
            tg.send_message(
                chat_id,
                "<b>Kufar Hunter App</b>\n<i>Откройте интерактивное приложение для управления поисками, аналитики цен и фильтров:</i>",
                reply_markup={"inline_keyboard": [[{"text": "Открыть Mini App ↗", "web_app": {"url": webapp_url}}]]}
            )
        else:
            show_main_menu(tg, chat_id)
        return

    if text in ["⚙️ Настройки", "/settings"]:
        USER_STATES.pop(chat_id, None)
        show_settings_menu(tg, chat_id)
        return

    if text in ["📊 Статистика", "/status", "/stats"]:
        USER_STATES.pop(chat_id, None)
        show_stats_menu(tg, chat_id, config)
        return

    if text in ["❓ Помощь", "/help"]:
        USER_STATES.pop(chat_id, None)
        show_help_menu(tg, chat_id)
        return

    # Check if this is a direct Kufar link submission
    if "kufar.by" in text or text.startswith("http://") or text.startswith("https://"):
        USER_STATES.pop(chat_id, None)
        handle_link_submission(tg, chat_id, text)
        return

    # Handle active wizard steps
    state = USER_STATES.get(chat_id)
    if state:
        step = state.get("step")

        if step == "WAIT_URL":
            if "kufar.by" in text:
                USER_STATES.pop(chat_id, None)
                handle_link_submission(tg, chat_id, text)
            else:
                tg.send_message(chat_id, "⚠️ Это не похоже на ссылку с Kufar.by. Отправьте корректную ссылку:")
            return

        if step == "WAIT_SILENT_HOURS":
            val = text.strip()
            m = re.match(r'^([0-1]?[0-9]|2[0-3]):([0-5][0-9])\s*-\s*([0-1]?[0-9]|2[0-3]):([0-5][0-9])$', val)
            if m:
                clean_val = f"{int(m.group(1)):02d}:{m.group(2)}-{int(m.group(3)):02d}:{m.group(4)}"
                set_silent_mode(chat_id, clean_val)
                USER_STATES.pop(chat_id, None)
                save_local_backup()
                tg.send_message(
                    chat_id,
                    f"🌙 <b>Ночной режим успешно настроен на часы: {clean_val}!</b>\n"
                    f"В этот интервал времени объявления будут приходить полностью без звука.",
                    reply_markup=get_main_keyboard(chat_id)
                )
                show_silent_mode_menu(tg, chat_id)
            else:
                tg.send_message(
                    chat_id,
                    "⚠️ Неверный формат времени. Отправьте интервал в формате <code>ЧЧ:ММ-ЧЧ:ММ</code> (например: <code>23:00-08:00</code> или <code>01:30-09:30</code>):"
                )
            return

        if step == "WAIT_STOP_WORDS":
            raw_tokens = re.split(r'[,;\n]+', text)
            added_words = []
            for t in raw_tokens:
                clean = t.strip().lower()
                if clean and len(clean) >= 2:
                    if add_stop_word(chat_id, clean):
                        added_words.append(clean)
            USER_STATES.pop(chat_id, None)
            save_local_backup()
            if added_words:
                tg.send_message(
                    chat_id,
                    f"✅ <b>Добавлено стоп-слов: {len(added_words)}!</b>\n"
                    f"({', '.join(added_words)})\n\n"
                    f"Теперь объявления, содержащие эти слова, будут автоматически отсекаться.",
                    reply_markup=get_main_keyboard(chat_id)
                )
            else:
                tg.send_message(chat_id, "ℹ️ Эти слова уже есть в списке или введены некорректно.", reply_markup=get_main_keyboard(chat_id))
            show_stop_words_menu(tg, chat_id)
            return

        if step == "WAIT_TEMPLATE":
            new_tpl = text.strip()
            if len(new_tpl) >= 3:
                set_user_template(chat_id, new_tpl)
                USER_STATES.pop(chat_id, None)
                save_local_backup()
                tg.send_message(
                    chat_id,
                    f"✅ <b>Новый шаблон для связи сохранён!</b>\n\n<code>{html.escape(new_tpl)}</code>",
                    reply_markup=get_main_keyboard(chat_id)
                )
                show_template_menu(tg, chat_id)
            else:
                tg.send_message(chat_id, "⚠️ Текст шаблона слишком короткий. Отправьте текст сообщения:")
            return

        if step == "QUERY":
            state["data"]["query"] = text
            wizard_step_price(tg, chat_id)
            return

        if step == "PRICE":
            # parse price string
            parsed = parse_kufar_url_or_query(text)
            if parsed.get("prc"):
                state["data"]["prc"] = parsed["prc"]
            wizard_finish(tg, chat_id)
            return

    # Fallback: treat free text as query or link
    parsed_auto = parse_kufar_url_or_query(text)
    if parsed_auto.get("query"):
        handle_link_submission(tg, chat_id, text)
    else:
        tg.send_message(chat_id, "❓ Выберите команду в меню снизу или отправьте ссылку на поиск с Kufar.by.")


def telegram_listener(tg: TelegramClient, config: dict):
    """Long-polls for Telegram messages and inline callbacks."""
    last_update_id = None
    logger.info("Telegram updates polling listener started.")

    while not STOP_EVENT.is_set():
        try:
            updates = tg.get_updates(offset=last_update_id, timeout=20)
            if not updates:
                time.sleep(0.5)
                continue

            for u in updates:
                last_update_id = u["update_id"] + 1

                # Handle Inline Button Clicks
                if "callback_query" in u:
                    try:
                        process_callback_query(tg, u["callback_query"], config)
                    except Exception as e:
                        logger.error(f"Error handling callback query: {e}")
                    continue

                # Handle Text Messages
                msg = u.get("message")
                if not msg:
                    continue
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text")
                from_user = msg.get("from", {})

                # Handle document backup file uploads (.json)
                doc = msg.get("document")
                if doc and chat_id:
                    fname = doc.get("file_name", "")
                    admin_chat_id = config.get("admin_chat_id", 0)
                    if fname.endswith(".json") and chat_id == admin_chat_id:
                        file_info = tg.get_file(doc.get("file_id", ""))
                        if file_info and file_info.get("file_path"):
                            bytes_data = tg.download_file(file_info["file_path"])
                            if bytes_data:
                                try:
                                    state = json.loads(bytes_data.decode("utf-8"))
                                    stats = import_database_state(state)
                                    tg.send_message(
                                        chat_id,
                                        f"✅ <b>Файл бэкапа успешно импортирован!</b>\n"
                                        f"• Поисков добавлено: <b>{stats['queries']}</b>\n"
                                        f"• В чёрный список: <b>{stats['blacklisted']}</b>\n"
                                        f"• Пользователей: <b>{stats['users']}</b>",
                                        reply_markup=get_main_keyboard(chat_id)
                                    )
                                    sync_backup_to_telegram(tg, config)
                                except Exception as e:
                                    tg.send_message(chat_id, f"⚠️ Ошибка при чтении бэкапа: {e}")
                        continue

                if chat_id and text:
                    try:
                        process_text_message(tg, chat_id, text, config, user_info=from_user)
                    except Exception as e:
                        logger.error(f"Error handling text message: {e}")
        except Exception as e:
            logger.error(f"Error in Telegram listener: {e}")
            time.sleep(2.0)


def main():
    config = load_config()
    admin_id_env = os.getenv("ADMIN_CHAT_ID")
    if admin_id_env and admin_id_env.isdigit():
        config["admin_chat_id"] = int(admin_id_env)

    token = config.get("bot_token", "").strip()
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("❗ ОШИБКА: Не указан токен бота! Укажите переменную окружения BOT_TOKEN (в настройках Environment на Render) или в config.json.")
        sys.exit(1)

    init_db()
    tg = TelegramClient(token)

    global GLOBAL_TG, GLOBAL_CONFIG
    GLOBAL_TG = tg
    GLOBAL_CONFIG = config

    bot_info = tg.get_me()
    if not bot_info:
        print("❌ ОШИБКА: Не удалось авторизоваться в Telegram. Проверьте токен.")
        sys.exit(1)

    bot_username = bot_info.get("username", "Unknown")
    print(f"\n🚀 Бот @{bot_username} успешно авторизован!")

    # Register clean minimal slash commands menu in Telegram
    tg.set_my_commands([
        {"command": "start", "description": "Главное меню"},
        {"command": "app", "description": "Открыть Mini App"},
        {"command": "list", "description": "Мои поиски"},
        {"command": "pause", "description": "Пауза / Старт"},
        {"command": "help", "description": "Справка"}
    ])

    webapp_url = get_webapp_url()
    if webapp_url:
        tg.set_chat_menu_button(webapp_url, text="Mini App")
        print(f"Telegram Mini App включен: {webapp_url}")
    else:
        tg.set_chat_menu_button(None)
    admin_id = config.get("admin_chat_id")
    if admin_id:
        print(f"👑 Администратор для оповещений: chat_id={admin_id}")
    print("🌐 Режим работы: мультипользовательский (уведомления админу о новых пользователях и фильтрах).")

    # Restore state if local database has no searches and no blacklist (e.g. after Render redeploy)
    active = get_active_queries()
    total_bl = get_blacklist_count(admin_id) if admin_id else 0
    if len(active) == 0 and total_bl == 0:
        print("📦 База данных пуста. Восстанавливаем сохраненные фильтры и чёрный список...")
        restored = restore_state_from_telegram(tg, config)
        if restored:
            print("✅ Все сохраненные поиски и чёрный список успешно восстановлены!")
    else:
        print(f"📦 Загружено активных поисков: {len(active)}, в ЧС: {total_bl}")

    # Always purge any legacy starter test queries (e.g. PlayStation 5 Slim)
    cleanup_legacy_queries()
    
    # Save local backup only if there is data
    state = export_database_state()
    if state.get("queries") or state.get("blacklisted_ads") or state.get("blacklisted_sellers") or state.get("stop_words") or state.get("favorites"):
        save_local_backup()

    # Load persistent interval setting if saved in database
    saved_interval = get_setting("check_interval_seconds")
    if saved_interval and saved_interval.isdigit():
        config["check_interval_seconds"] = int(saved_interval)
        print(f"⏱ Восстановлен интервал проверки из базы: {saved_interval} сек.")

    # Start health server for Render
    port_env = os.getenv("PORT")
    if port_env and port_env.isdigit():
        start_health_server(int(port_env))
    elif os.getenv("ENABLE_HEALTH_SERVER"):
        start_health_server(8080)

    # Start self-pinger to prevent Render Free Tier from sleeping
    start_keep_alive_pinger()

    # Start Hunter background thread
    hunter_thread = threading.Thread(target=hunter_loop, args=(tg, config), daemon=True)
    hunter_thread.start()

    # Start independent Favorites tracker thread (never blocks ad sniping)
    favs_thread = threading.Thread(target=favorites_tracker_loop, args=(tg, config), daemon=True)
    favs_thread.start()

    # Start Telegram listener
    listener_thread = threading.Thread(target=telegram_listener, args=(tg, config), daemon=True)
    listener_thread.start()

    interval = config.get("check_interval_seconds", 60)
    print(f"📡 Охотник запущен (интервал: {interval} сек).")

    def handle_shutdown(signum, frame):
        logger.info(f"Received signal {signum}, gracefully terminating bot instance...")
        STOP_EVENT.set()
        FORCE_CHECK_EVENT.set()
        if BACKUP_DIRTY:
            try:
                sync_backup_to_telegram(tg, config, force_notify=False, in_place_only=True)
            except Exception:
                pass
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)
    except Exception:
        pass

    try:
        while not STOP_EVENT.is_set():
            if not listener_thread.is_alive():
                logger.warning("Listener thread died! Auto-restarting telegram_listener...")
                listener_thread = threading.Thread(target=telegram_listener, args=(tg, config), daemon=True)
                listener_thread.start()
            if not hunter_thread.is_alive():
                logger.warning("Hunter thread died! Auto-restarting hunter_loop...")
                hunter_thread = threading.Thread(target=hunter_loop, args=(tg, config), daemon=True)
                hunter_thread.start()
            if not favs_thread.is_alive():
                logger.warning("Favorites tracker thread died! Auto-restarting...")
                favs_thread = threading.Thread(target=favorites_tracker_loop, args=(tg, config), daemon=True)
                favs_thread.start()
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n🛑 Остановка бота...")
        STOP_EVENT.set()
        FORCE_CHECK_EVENT.set()
        time.sleep(0.5)
        print("Бот остановлен.")


if __name__ == "__main__":
    main()
