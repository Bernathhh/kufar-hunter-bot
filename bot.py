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
import html
import json
import http.server
import socketserver
import re
import statistics
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
    cleanup_legacy_queries
)
from kufar_api import (
    parse_kufar_url_or_query,
    fetch_ads,
    fetch_ad_description,
    fetch_ad_photos,
    extract_phone_from_text,
    fetch_ad_phone_with_token
)
from telegram_api import TelegramClient

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

# Timestamp of last saved backup
LAST_BACKUP_TIME: Optional[datetime] = None
BACKUP_FILE = os.path.join(os.path.dirname(__file__), "backup_state.json")

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

CATEGORIES = [
    ("5040", "🎮 Приставки и игры"),
    ("17010", "📱 Мобильные телефоны"),
    ("16000", "💻 Компьютеры и электроника"),
    ("2000", "🚗 Авто и запчасти"),
    ("1000", "🏠 Недвижимость"),
    ("5000", "🔌 Бытовая техника"),
    ("4000", "🛋 Мебель и интерьер"),
    ("all", "📦 Любая категория")
]

REGIONS = [
    ("7", "🏙 Минск"),
    ("5", "🌲 Минская обл."),
    ("1", "🏰 Брестская обл."),
    ("2", "🏭 Гомельская обл."),
    ("3", "⛪ Гродненская обл."),
    ("4", "🏰 Могилевская обл."),
    ("6", "❄️ Витебская обл."),
    ("all", "🇧🇾 Вся Беларусь")
]

# Persistent Reply Keyboard Menu
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "➕ Добавить поиск"}, {"text": "📋 Мои поиски"}],
        [{"text": "📦 Вывести всё"}, {"text": "⏱ Интервал"}],
        [{"text": "⛔ Стоп-слова"}, {"text": "💬 Мой шаблон"}],
        [{"text": "📊 Статистика"}, {"text": "❓ Помощь"}]
    ],
    "resize_keyboard": True
}


def make_ad_keyboard(
    ad_link: str,
    ad_id: int,
    photo_count: int = 0,
    seller_name: Optional[str] = None,
    is_blacklisted: bool = False
) -> Dict[str, Any]:
    """Builds inline action keyboard attached directly to ad cards."""
    buttons = []
    
    # 1. Open on Kufar
    buttons.append([{"text": "🔗 Открыть на Kufar", "url": ad_link}])

    # 2. Quick message template
    buttons.append([{"text": "💬 Написать продавцу (шаблон)", "callback_data": f"cb:contact:{ad_id}"}])
    
    # 3. Show all photos button if multiple photos exist
    if photo_count > 1:
        buttons.append([{"text": f"📸 Показать все фото ({photo_count} шт.)", "callback_data": f"cb:photos:{ad_id}"}])

    # 4. Blacklist ad & Blacklist seller
    bl_row = []
    if is_blacklisted:
        bl_row.append({"text": "↩️ Вернуть из ЧС", "callback_data": f"cb:unbl:{ad_id}"})
    else:
        bl_row.append({"text": "🚫 В ЧС (скрыть)", "callback_data": f"cb:bl:{ad_id}"})

    if seller_name:
        clean_s = seller_name.strip()
        short_s = clean_s[:13] + "…" if len(clean_s) > 14 else clean_s
        bl_row.append({"text": f"👤 В ЧС: {short_s}", "callback_data": f"cb:bl_seller:{ad_id}"})
    else:
        bl_row.append({"text": "👤 В ЧС продавца", "callback_data": f"cb:bl_seller:{ad_id}"})

    buttons.append(bl_row)

    return {"inline_keyboard": buttons}


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
    """Returns eye-catching market price badge comparing ad price to market median."""
    if price_byn is None or median_byn is None or median_byn <= 0:
        return None
    diff_ratio = (price_byn - median_byn) / median_byn
    med_str = f"{median_byn:,.0f} BYN".replace(",", " ")
    if diff_ratio <= -0.18:
        pct = round(abs(diff_ratio) * 100)
        return f"💎 <b>ВЫГОДНО: на {pct}% ниже рынка!</b> (медиана ~{med_str})"
    elif diff_ratio <= -0.07:
        pct = round(abs(diff_ratio) * 100)
        return f"🔥 <b>Хорошая цена: -{pct}% от рынка</b> (медиана ~{med_str})"
    elif diff_ratio >= 0.20:
        pct = round(diff_ratio * 100)
        return f"⚠️ <b>Выше рынка на {pct}%</b> (медиана ~{med_str})"
    elif diff_ratio >= 0.08:
        pct = round(diff_ratio * 100)
        return f"🏷 <b>Чуть выше рынка (+{pct}%)</b> (медиана ~{med_str})"
    else:
        return f"⚖️ <b>В рынке</b> (медиана ~{med_str})"


def extract_smart_badges(subject: str, description: Optional[str]) -> List[str]:
    """
    Extracts key selling points, equipment and nuances from title and description.
    (e.g. 2 gamepads, disc edition, warranty, full box, included games, regional version, jailbreak, defects).
    """
    text = (subject + " " + (description or "")).lower()
    badges = []

    # 1. Controllers / Gamepads
    if re.search(r'(2\s*(геймпада|джойстика|контроллера|dualsense|dualshock)|два\s*(геймпада|джойстика|dualsense)|пара\s*(геймпадов|джойстиков)|\+2-й\s*геймпад|\+второй\s*(геймпад|джойстик))', text):
        badges.append("🎮 2 геймпада")
    elif re.search(r'(3\s*(геймпада|джойстика|контроллера)|три\s*(геймпада|джойстика))', text):
        badges.append("🎮 3 геймпада")
    elif re.search(r'(геймпад|джойстик|dualsense|dualshock)', text):
        badges.append("🎮 Геймпад в комплекте")

    # 2. Disc drive vs Digital
    if re.search(r'(с\s+дисковод|дисковод|disc\s*edition|disk\s*edition|blu-?ray|дисковая\s+версия)', text) and not re.search(r'(без\s+дисковод|дисковод\s+отдельно)', text):
        badges.append("📀 С дисководом")
    elif re.search(r'(digital\s*edition|диджитал|без\s+дисковод|digital\s*версия|без\s+привода)', text):
        badges.append("📱 Digital (без дисковода)")

    # 3. Box / Complete kit
    if re.search(r'(полный\s+комплект|родная\s+коробка|заводская\s+коробка|в\s+коробке|коробка\s+есть|есть\s+коробка|full\s*set)', text):
        badges.append("📦 Полный комплект / Коробка")

    # 4. Games included
    if re.search(r'(\d+\s*(диска|дисков|игры|игр)|диски\s+в\s+комплекте|игры\s+в\s+комплекте|отдам\s+(диски|игры)|игры\s+на\s+аккаунт|аккаунт\s+с\s+играми)', text):
        badges.append("💿 Игры в комплекте")

    # 5. Warranty & Receipt
    if re.search(r'(на\s+гаранти|официальная\s+гарантия|гарантийный\s+талон|чек\s+о\s+покупке|есть\s+чек|чек\s+и\s+гарантия|действует\s+гарантия|куплен\s+в\s+(5\s*элемент|электросил|21\s*век))', text):
        badges.append("🧾 На гарантии / Чек")

    # 6. Region / Version
    if re.search(r'(японк|япония|cfi-?\d+00|japan|\(jp\))', text):
        badges.append("🇯🇵 Версия: Япония")
    elif re.search(r'(европ|европейка|евротест|cfi-?\d+16|europe|\(eu\))', text):
        badges.append("🇪🇺 Версия: Европа")
    elif re.search(r'(китай|китаянка|бэкап|cfi-?\d+09|china|\(cn\))', text):
        badges.append("🇨🇳 Версия: Китай (бэкап)")

    # 7. Jailbreak / Firmare
    if re.search(r'(прошит|прошивка|jailbreak|hen|goldhen)', text):
        badges.append("⚡ Прошита")

    # 8. Defects / Nuances warning (filtering out "без дефектов", "не шумит", etc.)
    has_defect_word = re.search(r'(дефект|нюанс|не\s+работает|дрифт|дрифтит|шумит|греется|царапин|трещин|скол|глючит)', text)
    if has_defect_word:
        is_negated = re.search(r'(без\s+(дефектов|нюансов|царапин|сколов|проблем|глюков)|не\s+(шумит|греется|глючит|дрифтит)|отличное\s+состояние|в\s+идеале)', text)
        if not is_negated:
            badges.append("⚠️ Есть нюансы/дефекты")

    return badges


class HealthHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Kufar Hunter Bot is running")

    def log_message(self, format, *args):
        pass


def start_health_server(port: int):
    try:
        server = socketserver.TCPServer(("0.0.0.0", port), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Health check HTTP server running on port {port}")
    except Exception as e:
        logger.warning(f"Could not start health check HTTP server on port {port}: {e}")


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
    """Formats Kufar search parameters into clear, human-readable Russian text."""
    lines = []
    
    if p.get("query"):
        lines.append(f"🔍 <b>Ключевые слова:</b> <code>{html.escape(p['query'])}</code>")
    else:
        lines.append("🔍 <b>Ключевые слова:</b> <i>все товары в категории</i>")

    cat_id = str(p.get("cat", ""))
    cat_name = dict(CATEGORIES).get(cat_id, f"Код {cat_id}" if cat_id else "Все категории")
    lines.append(f"📂 <b>Категория:</b> {cat_name}")

    rgn_id = str(p.get("rgn", ""))
    rgn_name = dict(REGIONS).get(rgn_id, f"Регион {rgn_id}" if rgn_id else "Вся Беларусь")
    lines.append(f"🏙 <b>Регион:</b> {rgn_name}")

    if p.get("ar"):
        if "22,23,24,25,26,27,28,29,30" in p["ar"]:
            lines.append("📍 <b>Районы:</b> Все 9 районов Минска")
        else:
            lines.append(f"📍 <b>Районы:</b> <code>{html.escape(p['ar'])}</code>")

    if p.get("prc"):
        prc_str = p["prc"].replace("r:", "")
        parts = prc_str.split(",")
        if len(parts) == 2:
            try:
                min_p = int(parts[0]) // 100 if parts[0] else 0
                max_p = int(parts[1]) // 100 if parts[1] else 0
                if min_p and max_p and max_p < 9000000:
                    lines.append(f"💰 <b>Цена:</b> от <b>{min_p:,}</b> до <b>{max_p:,} BYN</b>".replace(",", " "))
                elif max_p and max_p < 9000000:
                    lines.append(f"💰 <b>Цена:</b> до <b>{max_p:,} BYN</b>".replace(",", " "))
                elif min_p:
                    lines.append(f"💰 <b>Цена:</b> от <b>{min_p:,} BYN</b>".replace(",", " "))
            except Exception:
                lines.append(f"💰 <b>Цена:</b> <code>{html.escape(p['prc'])}</code>")
    else:
        lines.append("💰 <b>Цена:</b> Любая")

    lines.append("⚡ <b>Сортировка:</b> Сначала новые (lst.d)")
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
            header = "🔄 <b>Обновление / Поднято из архива!</b>"
            note = "\nℹ️ <i>Продавец поднял объявление, изменил цену или активировал из архива.</i>"
        else:
            header = "🔥 <b>Свежее объявление на Куфаре!</b>"
            note = ""

        return {
            "header": header,
            "time_str": full_time,
            "is_repost": is_repost,
            "note": note
        }
    except Exception:
        return {"header": "🔥 <b>Новое объявление на Куфаре!</b>", "time_str": iso_str, "is_repost": False, "note": ""}


def resolve_phone_string(ad: Dict[str, Any], description: str) -> str:
    """Detects phone number from description or checks authorization/visibility."""
    # 1. Search in description text
    phone_in_text = extract_phone_from_text(description)
    if phone_in_text:
        return f"<code>{html.escape(phone_in_text)}</code> (из описания)"

    # 2. Check if optional Kufar Bearer token is provided
    kufar_token = os.getenv("KUFAR_BEARER_TOKEN")
    if kufar_token:
        direct_phone = fetch_ad_phone_with_token(ad["ad_id"], kufar_token)
        if direct_phone:
            return f"<code>{html.escape(direct_phone)}</code>"

    # 3. Fallback based on Kufar's phone_hidden flag
    link = ad.get("ad_link", "")
    if ad.get("phone_hidden") is False:
        return f"<a href=\"{link}\">Есть в объявлении (кнопка «Позвонить»)</a>"
    else:
        return "<i>Скрыт продавцом (только сообщения)</i>"


def format_ad_caption(
    ad: Dict[str, Any],
    query_name: str,
    description: str = "",
    update_type: str = "new",
    old_price_byn: Optional[float] = None,
    new_price_byn: Optional[float] = None,
    market_median: Optional[float] = None
) -> str:
    """Creates a clean, attractive HTML caption for the ad, including price updates, seller badge and market stats."""
    subject = html.escape(ad.get("subject", "Объявление"))
    link = ad.get("ad_link", "")
    price_byn = ad.get("price_byn")
    price_usd = ad.get("price_usd")
    timing = parse_ad_timing(ad.get("list_time", ""))
    phone_str = resolve_phone_string(ad, description)

    if price_byn is not None:
        price_str = f"<b>{price_byn:,.2f} BYN</b>".replace(",", " ")
        if price_usd is not None:
            price_str += f" (~${price_usd:,.0f})".replace(",", " ")
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
        seller_label = f"🏪 {html.escape(raw_seller or 'Магазин / Компания')} <i>(Магазин / ИП)</i>"
    else:
        seller_label = f"👤 {html.escape(raw_seller or 'Частное лицо')} <i>(Частное лицо)</i>"

    time_label = "Первично создано:" if timing["is_repost"] else "Опубликовано:"

    # Format header and price depending on whether this is a price update
    note_line = None
    if update_type == "price_change" and old_price_byn is not None and new_price_byn is not None:
        diff_byn = old_price_byn - new_price_byn
        if diff_byn > 0:
            # Price decreased (discount / bargain!)
            pct = round((diff_byn / old_price_byn) * 100) if old_price_byn > 0 else 0
            header_text = f"📉 <b>Цена снижена! (-{diff_byn:,.0f} BYN)</b>\n"
            price_line = (
                f"💰 <b>Новая цена:</b> <b>{new_price_byn:,.2f} BYN</b>"
                + (f" (~${price_usd:,.0f})" if price_usd is not None else "")
                + f"\n🏷 <b>Старая цена:</b> <s>{old_price_byn:,.2f} BYN</s>"
                + f"\n🔻 <b>Скидка:</b> -{pct}% (дешевле на {diff_byn:,.2f} BYN!)"
            )
            note_line = "💡 <i>Продавец снизил цену этого объявления на Куфаре.</i>"
        else:
            # Price increased (seller raised the price!)
            raise_byn = abs(diff_byn)
            pct = round((raise_byn / old_price_byn) * 100) if old_price_byn > 0 else 0
            header_text = f"📈 <b>Внимание: продавец поднял цену! (+{raise_byn:,.0f} BYN)</b>\n"
            price_line = (
                f"💰 <b>Новая цена:</b> <b>{new_price_byn:,.2f} BYN</b>"
                + (f" (~${price_usd:,.0f})" if price_usd is not None else "")
                + f"\n🏷 <b>Прежняя цена:</b> <s>{old_price_byn:,.2f} BYN</s>"
                + f"\n🔺 <b>Повышение:</b> +{raise_byn:,.2f} BYN (+{pct}%)"
            )
            note_line = "⚠️ <i>Продавец только что повысил цену на это объявление на Куфаре!</i>"
    else:
        header_text = f"{timing['header']}\n"
        price_line = f"💰 <b>Цена:</b> {price_str}"
        note_line = timing.get("note")

    # Bargain badge (Индикатор «Сладкая цена»)
    effective_price = new_price_byn if (update_type == "price_change" and new_price_byn is not None) else price_byn
    bargain_badge = get_bargain_badge(effective_price, market_median)
    if bargain_badge:
        price_line += f"\n📊 {bargain_badge}"

    lines = [
        header_text,
        f"📌 <b>{subject}</b>",
        price_line,
        f"🕒 <b>{time_label}</b> {timing['time_str']}",
        f"📍 <b>Локация:</b> {location}",
        f"<b>Продавец:</b> {seller_label}",
        f"📞 <b>Телефон:</b> {phone_str}",
        f"🎯 <i>Поиск: {html.escape(query_name)}</i>"
    ]
    if note_line:
        lines.append(note_line)

    # Smart description badges (Умная выжимка описания)
    smart_badges = extract_smart_badges(ad.get("subject", ""), description)
    if smart_badges:
        lines.append(f"\n✨ <b>Комплект & Особенности:</b>\n" + " • ".join(smart_badges))

    if description:
        clean_desc = html.escape(description.strip())
        if len(clean_desc) > 320:
            clean_desc = clean_desc[:320] + "..."
        lines.append(f"\n📝 <b>Описание:</b>\n<i>{clean_desc}</i>")

    return "\n".join(lines)


def send_ad_notification(
    tg: TelegramClient,
    chat_id: int,
    ad: Dict[str, Any],
    query_name: str,
    update_type: str = "new",
    old_price_byn: Optional[float] = None,
    new_price_byn: Optional[float] = None,
    market_median: Optional[float] = None
) -> bool:
    """
    Sends unified notification with photo, caption and action buttons attached directly.
    Supports both new listings and price change updates.
    - Single unified message: Photo + Full description + Inline buttons directly under it.
    - If multiple photos: includes «📸 Показать все фото» button to view the full gallery.
    - Text fallback if no photos available.
    """
    ad_id = ad["ad_id"]
    if is_ad_blacklisted(chat_id, ad_id):
        logger.info(f"Ad {ad_id} is blacklisted by chat {chat_id}, skipping")
        return False

    # Check seller blacklist
    seller_name = ad.get("seller", "")
    account_id = ad.get("account_id")
    if is_seller_blacklisted(chat_id, seller_name, account_id):
        logger.info(f"Seller '{seller_name}' is blacklisted by chat {chat_id}, skipping ad {ad_id}")
        return False

    description = ad.get("description")
    if description is None:
        description = fetch_ad_description(ad_id)
        ad["description"] = description

    # Check stop-words (negative keywords)
    stop_words = get_stop_words(chat_id)
    if stop_words:
        check_text = f"{ad.get('subject', '')} {description or ''}".lower()
        for sw in stop_words:
            if sw in check_text:
                logger.info(f"Ad {ad_id} matches stop-word '{sw}', skipping")
                return False

    # Cache ad data for quick interactive callbacks (seller blacklist, contact template)
    AD_DATA_CACHE[ad_id] = ad
    if len(AD_DATA_CACHE) > 1000:
        for old_k in list(AD_DATA_CACHE.keys())[:200]:
            AD_DATA_CACHE.pop(old_k, None)

    caption = format_ad_caption(
        ad, query_name,
        description=description,
        update_type=update_type,
        old_price_byn=old_price_byn,
        new_price_byn=new_price_byn,
        market_median=market_median
    )
    image_urls = ad.get("image_urls") or ([ad["image_url"]] if ad.get("image_url") else [])
    ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")

    if image_urls:
        AD_PHOTOS_CACHE[ad_id] = image_urls

    markup = make_ad_keyboard(
        ad_link,
        ad_id,
        photo_count=len(image_urls),
        seller_name=seller_name
    )

    # Prune old cache entries to prevent memory growth
    if len(AD_MESSAGES_MAP) > 1000:
        for old_k in list(AD_MESSAGES_MAP.keys())[:200]:
            AD_MESSAGES_MAP.pop(old_k, None)

    # Send unified card: Photo + Description + Action buttons attached directly
    if image_urls:
        photo_caption = caption if len(caption) <= 1000 else caption[:996] + "..."
        res_photo = tg.send_photo(chat_id, image_urls[0], caption=photo_caption, reply_markup=markup)
        if res_photo and isinstance(res_photo, dict) and "message_id" in res_photo:
            AD_MESSAGES_MAP[(chat_id, ad_id)] = [res_photo["message_id"]]
            return True

    # Fallback: Text only with buttons attached directly
    res_msg = tg.send_message(chat_id, caption, disable_web_page_preview=False, reply_markup=markup)
    if res_msg and isinstance(res_msg, dict) and "message_id" in res_msg:
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
    name = query["name"]
    params = query["params"]

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
    market_median = calculate_market_stats(ads)
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

            # Only notify if within user's budget
            if is_within_budget:
                try:
                    if send_ad_notification(tg, chat_id, ad, name, update_type="new", market_median=market_median):
                        sent_count += 1
                        NEW_ADS_FOUND += 1
                        time.sleep(1.0)
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
                        # Price INCREASED! (e.g. 2100 -> 2500 BYN)
                        # Notify if it was previously within budget (user received it before)
                        if max_budget is None or last_byn <= max_budget:
                            should_notify = True
                            logger.info(f"Price RAISED for ad {ad_id}: {last_byn} -> {curr_byn} BYN (+{diff})!")
                    else:
                        # Price DECREASED! (e.g. 2500 -> 1950 BYN or 2100 -> 1900 BYN)
                        # Notify if new price is within user's budget
                        if max_budget is None or curr_byn <= max_budget:
                            should_notify = True
                            logger.info(f"Price DROPPED for ad {ad_id}: {last_byn} -> {curr_byn} BYN ({diff})!")

                    if should_notify:
                        try:
                            if send_ad_notification(
                                tg, chat_id, ad, name,
                                update_type="price_change",
                                old_price_byn=last_byn,
                                new_price_byn=curr_byn,
                                market_median=market_median
                            ):
                                sent_count += 1
                                NEW_ADS_FOUND += 1
                                time.sleep(1.0)
                        except Exception as e:
                            logger.error(f"Error sending price update notification: {e}")

            elif last_byn is None and curr_byn is not None:
                # Was previously without price recorded in DB, silently update price
                mark_ad_seen(qid, ad_id, price_byn=curr_byn, price_usd=curr_usd)

    return sent_count


def hunter_loop(tg: TelegramClient, config: dict):
    """Background monitoring loop."""
    global TOTAL_CHECKS
    logger.info("Kufar Hunter loop started.")

    while not STOP_EVENT.is_set():
        interval = config.get("check_interval_seconds", 60)
        try:
            queries = get_active_queries()
            if queries:
                logger.debug(f"Running check for {len(queries)} active queries...")
                for q in queries:
                    if STOP_EVENT.is_set():
                        break
                    try:
                        check_single_query(tg, q)
                    except Exception as e:
                        logger.error(f"Error checking query '{q['name']}': {e}")
                    time.sleep(2.0)
                TOTAL_CHECKS += 1
        except Exception as e:
            logger.error(f"Unexpected error in hunter loop: {e}")

        FORCE_CHECK_EVENT.wait(timeout=interval)
        FORCE_CHECK_EVENT.clear()


# ==================== UI & COMMAND HANDLERS ====================

def save_local_backup():
    """Silently saves current state to backup_state.json and syncs to Telegram Cloud."""
    global LAST_BACKUP_TIME
    try:
        state = export_database_state()
        has_data = bool(state.get("queries") or state.get("blacklisted_ads") or state.get("blacklisted_sellers") or state.get("stop_words"))
        if has_data:
            with open(BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            LAST_BACKUP_TIME = datetime.now()
        
        # Trigger silent background cloud sync to Telegram
        if GLOBAL_TG and GLOBAL_CONFIG:
            sync_backup_to_telegram(GLOBAL_TG, GLOBAL_CONFIG, force_notify=False)
    except Exception as e:
        logger.error(f"Error saving local backup: {e}")


def sync_backup_to_telegram(tg: TelegramClient, config: dict, force_notify: bool = False):
    """
    Saves state locally to backup_state.json and silently syncs to Telegram Cloud.
    Updates the pinned backup message silently in-place without triggering notifications.
    """
    global LAST_BACKUP_TIME
    admin_chat_id = config.get("admin_chat_id", 0)
    if not admin_chat_id:
        return

    try:
        state = export_database_state()
        has_data = bool(state.get("queries") or state.get("blacklisted_ads") or state.get("blacklisted_sellers") or state.get("stop_words"))
        if has_data:
            with open(BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            LAST_BACKUP_TIME = datetime.now()

        raw_json = json.dumps(state, ensure_ascii=False)
        updated_time = datetime.now().strftime("%d.%m.%Y в %H:%M:%S")
        q_count = len(state.get("queries", []))
        bl_count = len(state.get("blacklisted_ads", []))
        bl_sellers = len(state.get("blacklisted_sellers", []))
        sw_count = len(state.get("stop_words", []))

        backup_text = (
            f"💾 <b>Kufar Hunter — Синхронизация данных</b>\n"
            f"<i>Хранит ваши фильтры, ЧС и настройки. Авто-обновление.</i>\n\n"
            f"• 🎯 Поисков: <b>{q_count}</b>\n"
            f"• 🚫 В ЧС объявлений: <b>{bl_count}</b>\n"
            f"• 👤 В ЧС продавцов: <b>{bl_sellers}</b>\n"
            f"• ⛔ Стоп-слов: <b>{sw_count}</b>\n"
            f"🕒 <i>Синхронизировано: {updated_time}</i>\n\n"
            f"#KUFAR_BACKUP_START\n"
            f"<pre><code class=\"language-json\">{html.escape(raw_json)}</code></pre>\n"
            f"#KUFAR_BACKUP_END"
        )

        chat_info = tg.get_chat(admin_chat_id)
        pinned = chat_info.get("pinned_message") if chat_info else None

        if not has_data and not force_notify:
            # If current local state is completely empty, do NOT overwrite an existing cloud backup
            if pinned and ("#KUFAR_BACKUP_START" in pinned.get("text", "") or "<!--KUFAR_BACKUP_START-->" in pinned.get("text", "") or pinned.get("document")):
                logger.warning("Local state is empty while cloud backup exists; skipping overwrite to prevent data loss.")
                return

        if len(backup_text) <= 4000:
            if pinned and ("#KUFAR_BACKUP_START" in pinned.get("text", "") or "<!--KUFAR_BACKUP_START-->" in pinned.get("text", "")):
                # Silent in-place edit! Zero notification sound, zero new messages!
                tg.edit_message_text(admin_chat_id, pinned["message_id"], backup_text, parse_mode="HTML")
            else:
                msg = tg.send_message(admin_chat_id, backup_text, disable_notification=True)
                if msg and isinstance(msg, dict) and "message_id" in msg:
                    tg.pin_chat_message(admin_chat_id, msg["message_id"], disable_notification=True)
        else:
            caption = (
                f"💾 <b>Kufar Hunter — Резервная копия</b>\n\n"
                f"• Поисков: {q_count}, В ЧС: {bl_count}, Продавцов: {bl_sellers}, Стоп-слов: {sw_count}\n"
                f"🕒 {updated_time}\n#KUFAR_BACKUP_DOC"
            )
            old_mid = pinned.get("message_id") if pinned else None
            doc_msg = tg.send_document(
                admin_chat_id,
                "kufar_backup.json",
                raw_json.encode("utf-8"),
                caption=caption
            )
            if doc_msg and isinstance(doc_msg, dict) and "message_id" in doc_msg:
                tg.pin_chat_message(admin_chat_id, doc_msg["message_id"], disable_notification=True)
                if old_mid and old_mid != doc_msg["message_id"]:
                    try:
                        tg.delete_message(admin_chat_id, old_mid)
                    except Exception:
                        pass

        if force_notify:
            tg.send_message(
                admin_chat_id,
                f"✅ <b>Резервная копия сохранена в Telegram!</b>\n"
                f"🎯 Поисков: {q_count}\n"
                f"🚫 В ЧС объявлений: {bl_count}\n"
                f"👤 В ЧС продавцов: {bl_sellers}\n"
                f"⛔ Стоп-слов: {sw_count}"
            )
    except Exception as e:
        logger.error(f"Error syncing backup to Telegram: {e}")


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


def output_all_user_ads(tg: TelegramClient, chat_id: int):
    """Outputs current listings for all active user searches, respecting blacklisted items."""
    queries = get_user_queries(chat_id)
    if not queries:
        text = (
            "📭 <b>У вас пока нет активных поисков.</b>\n\n"
            "Нажмите кнопку <b>«➕ Добавить поиск»</b>, чтобы создать первый фильтр!"
        )
        tg.send_message(chat_id, text, reply_markup=MAIN_KEYBOARD)
        return

    tg.send_message(
        chat_id,
        f"🔍 <b>Загружаю актуальные объявления по вашим фильтрам...</b>\n"
        f"<i>Фильтров: {len(queries)}. Пожалуйста, подождите...</i>"
    )

    bl = get_user_blacklist(chat_id)
    seen_in_run = set()
    total_shown = 0

    for q in queries:
        q_name = q["name"]
        params = q["params"]
        try:
            ads = fetch_ads(params)
        except Exception as e:
            logger.error(f"Error fetching ads for query {q.get('id')}: {e}")
            continue

        q_median = calculate_market_stats(ads)
        valid_ads = [
            a for a in ads
            if a["ad_id"] not in bl
            and a["ad_id"] not in seen_in_run
            and not is_seller_blacklisted(chat_id, a.get("seller", ""), a.get("account_id"))
        ]
        # Show top 10 most recent listings per query
        for ad in valid_ads[:10]:
            seen_in_run.add(ad["ad_id"])
            mark_ad_seen(q["id"], ad["ad_id"], price_byn=ad.get("price_byn"), price_usd=ad.get("price_usd"))
            try:
                if send_ad_notification(tg, chat_id, ad, q_name, market_median=q_median):
                    total_shown += 1
                    time.sleep(0.8)
            except Exception as e:
                logger.error(f"Error displaying ad {ad.get('ad_id')}: {e}")

    if total_shown == 0:
        tg.send_message(
            chat_id,
            "📭 <b>На Куфаре пока нет подходящих объявлений</b> (или все имеющиеся добавлены вами в чёрный список).",
            reply_markup=MAIN_KEYBOARD
        )
    else:
        tg.send_message(
            chat_id,
            f"✅ <b>Вывод завершён! Всего показано: {total_shown} объявлений.</b>\n\n"
            f"💡 <i>Если объявление вам не подходит, нажмите кнопку «🚫 Не интересно», чтобы оно больше никогда не показывалось.</i>",
            reply_markup=MAIN_KEYBOARD
        )


def output_ads_for_query(tg: TelegramClient, chat_id: int, qid: int):
    """Outputs current listings for a specific search query."""
    queries = get_user_queries(chat_id)
    target = next((q for q in queries if q["id"] == qid), None)
    if not target:
        tg.send_message(chat_id, "⚠️ Этот поиск не найден или был удалён.", reply_markup=MAIN_KEYBOARD)
        return

    q_name = target["name"]
    params = target["params"]

    tg.send_message(
        chat_id,
        f"🔍 <b>Загружаю актуальные объявления для «{html.escape(q_name)}»...</b>"
    )

    bl = get_user_blacklist(chat_id)
    try:
        ads = fetch_ads(params)
    except Exception as e:
        logger.error(f"Error fetching ads for query {qid}: {e}")
        tg.send_message(chat_id, f"⚠️ Ошибка загрузки объявлений: {e}", reply_markup=MAIN_KEYBOARD)
        return

    q_median = calculate_market_stats(ads)
    valid_ads = [
        a for a in ads
        if a["ad_id"] not in bl
        and not is_seller_blacklisted(chat_id, a.get("seller", ""), a.get("account_id"))
    ]
    total_shown = 0

    for ad in valid_ads[:10]:
        mark_ad_seen(qid, ad["ad_id"], price_byn=ad.get("price_byn"), price_usd=ad.get("price_usd"))
        try:
            if send_ad_notification(tg, chat_id, ad, q_name, market_median=q_median):
                total_shown += 1
                time.sleep(0.8)
        except Exception as e:
            logger.error(f"Error displaying ad {ad.get('ad_id')}: {e}")

    if total_shown == 0:
        tg.send_message(
            chat_id,
            f"📭 По поиску «{html.escape(q_name)}» нет подходящих объявлений (или все добавлены в ЧС).",
            reply_markup=MAIN_KEYBOARD
        )
    else:
        tg.send_message(
            chat_id,
            f"✅ <b>Показано {total_shown} объявлений по поиску «{html.escape(q_name)}».</b>",
            reply_markup=MAIN_KEYBOARD
        )


def show_main_menu(tg: TelegramClient, chat_id: int):
    """Sends greeting with main keyboard menu."""
    text = (
        "👋 <b>Главное меню Kufar Hunter</b>\n\n"
        "Бот непрерывно мониторит Kufar и мгновенно присылает новые объявления по вашим точным фильтрам.\n\n"
        "Выберите действие на кнопках ниже 👇"
    )
    tg.send_message(chat_id, text, reply_markup=MAIN_KEYBOARD)


def prompt_add_search_options(tg: TelegramClient, chat_id: int):
    """Shows options to add search: by link or by step-by-step wizard."""
    text = (
        "➕ <b>Как вы хотите настроить фильтры?</b>\n\n"
        "1️⃣ <b>Вставить ссылку с Куфара (Рекомендуется)</b> — настройте любые тонкие фильтры на сайте kufar.by в браузере (категория, состояние, районы, бренд, цена), скопируйте ссылку и отправьте боту. Он скопирует все фильтры 1 в 1!\n\n"
        "2️⃣ <b>Конструктор по кнопкам</b> — пошагово выбрать категорию, регион, цену и ключевые слова прямо в Telegram."
    )
    markup = {
        "inline_keyboard": [
            [{"text": "🔗 Вставить ссылку с Куфара (1 в 1)", "callback_data": "cb:add_link"}],
            [{"text": "🛠 Пошаговый конструктор", "callback_data": "cb:wiz_start"}],
            [{"text": "🔙 В главное меню", "callback_data": "cb:menu"}]
        ]
    }
    tg.send_message(chat_id, text, reply_markup=markup)


def show_my_searches(tg: TelegramClient, chat_id: int):
    """Lists all active searches with interactive management buttons."""
    queries = get_user_queries(chat_id)
    if not queries:
        text = (
            "📭 <b>У вас пока нет активных поисков.</b>\n\n"
            "Нажмите кнопку <b>«➕ Добавить поиск»</b>, чтобы создать первый фильтр!"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "➕ Добавить поиск", "callback_data": "cb:add_link"}]
            ]
        }
        tg.send_message(chat_id, text, reply_markup=markup)
        return

    tg.send_message(chat_id, f"📋 <b>Ваши активные поиски (всего: {len(queries)}):</b>")

    for q in queries:
        qid = q["id"]
        name = q["name"]
        params = q["params"]
        seen = get_query_seen_count(qid)

        text = (
            f"📌 <b>Поиск [ID: {qid}]: {html.escape(name)}</b>\n\n"
            f"{format_params_human(params)}\n\n"
            f"📦 <i>Просмотрено объявлений: {seen}</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "📦 Показать объявления", "callback_data": f"cb:show_ads:{qid}"}],
                [{"text": "🗑 Удалить этот поиск", "callback_data": f"cb:del:{qid}"}]
            ]
        }
        tg.send_message(chat_id, text, reply_markup=markup)


def show_interval_options(tg: TelegramClient, chat_id: int, current: int):
    """Shows speed/interval options."""
    text = (
        f"⏱ <b>Скорость проверки Куфара</b>\n\n"
        f"Текущий интервал: <b>{current} сек.</b>\n"
        f"Выберите, как часто бот должен опрашивать сайт:"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "⚡ 15 сек", "callback_data": "cb:interval:15"},
                {"text": "🚀 30 сек", "callback_data": "cb:interval:30"}
            ],
            [
                {"text": "⏱ 60 сек", "callback_data": "cb:interval:60"},
                {"text": "⏳ 120 сек", "callback_data": "cb:interval:120"}
            ],
            [{"text": "🔙 Назад", "callback_data": "cb:menu"}]
        ]
    }
    tg.send_message(chat_id, text, reply_markup=markup)


def show_stop_words_menu(tg: TelegramClient, chat_id: int):
    """Displays current stop words and interactive management buttons."""
    sw_list = get_stop_words(chat_id)
    if sw_list:
        words_str = ", ".join(f"<code>{html.escape(w)}</code>" for w in sw_list)
        text = (
            f"⛔ <b>Ваши стоп-слова (минус-слова):</b>\n\n"
            f"{words_str}\n\n"
            f"💡 <i>Объявления, содержащие эти слова в названии или описании, будут автоматически скрываться.</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "➕ Добавить стоп-слова", "callback_data": "cb:sw_add"}],
                [{"text": "🗑 Удалить конкретное слово", "callback_data": "cb:sw_del_menu"}],
                [{"text": "🧹 Очистить все слова", "callback_data": "cb:sw_clear"}],
                [{"text": "🔙 В главное меню", "callback_data": "cb:menu"}]
            ]
        }
    else:
        text = (
            "⛔ <b>Стоп-слова (минус-слова)</b>\n\n"
            "У вас пока не добавлено ни одного стоп-слова.\n\n"
            "Вы можете добавить слова, которые вам не интересны (например: <code>аренда, чехол, запчасти, коробка, обмен</code>), "
            "и бот будет автоматически скрывать такие объявления!"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "➕ Добавить стоп-слова", "callback_data": "cb:sw_add"}],
                [{"text": "🔙 В главное меню", "callback_data": "cb:menu"}]
            ]
        }
    tg.send_message(chat_id, text, reply_markup=markup)


def show_template_menu(tg: TelegramClient, chat_id: int):
    """Displays current quick contact message template."""
    tpl = get_user_template(chat_id)
    text = (
        "💬 <b>Настраиваемый шаблон для связи с продавцом</b>\n\n"
        "В карточке каждого объявления есть кнопка <b>«💬 Написать продавцу (шаблон)»</b>. "
        "При нажатии бот выдает готовый текст сообщения в виде блока, который можно скопировать в один клик и отправить продавцу на Куфаре!\n\n"
        f"<b>Текущий шаблон:</b>\n<code>{html.escape(tpl)}</code>"
    )
    markup = {
        "inline_keyboard": [
            [{"text": "✏️ Изменить текст шаблона", "callback_data": "cb:tpl_edit"}],
            [{"text": "🔄 Сбросить по умолчанию", "callback_data": "cb:tpl_reset"}],
            [{"text": "🔙 В главное меню", "callback_data": "cb:menu"}]
        ]
    }
    tg.send_message(chat_id, text, reply_markup=markup)


def handle_link_submission(tg: TelegramClient, chat_id: int, url: str):
    """Parses a Kufar URL submitted by the user and asks for confirmation."""
    params = parse_kufar_url_or_query(url)
    ads = fetch_ads(params)
    count = len(ads)

    query_name = params.get("query")
    if not query_name:
        cat_id = params.get("cat")
        cat_name = dict(CATEGORIES).get(str(cat_id), "Куфар поиск")
        query_name = cat_name

    pending_id = f"p_{int(time.time())}"
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
            [{"text": "✅ Подтвердить и запустить", "callback_data": f"cb:confirm:{pending_id}"}],
            [{"text": "❌ Отмена", "callback_data": f"cb:cancel:{pending_id}"}]
        ]
    }
    tg.send_message(chat_id, text, reply_markup=markup)


# ==================== WIZARD STEPS ====================

def wizard_step_category(tg: TelegramClient, chat_id: int):
    """Step 1: Category selection."""
    USER_STATES[chat_id] = {"step": "CAT", "data": {}}
    text = "🛠 <b>Шаг 1 из 4: Выберите категорию товаров:</b>"
    buttons = []
    row = []
    for cid, cname in CATEGORIES:
        row.append({"text": cname, "callback_data": f"cb:wiz_cat:{cid}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "❌ Отмена", "callback_data": "cb:menu"}])

    tg.send_message(chat_id, text, reply_markup={"inline_keyboard": buttons})


def wizard_step_region(tg: TelegramClient, chat_id: int):
    """Step 2: Region selection."""
    USER_STATES[chat_id]["step"] = "RGN"
    text = "🏙 <b>Шаг 2 из 4: Выберите регион поиска:</b>"
    buttons = []
    row = []
    for rid, rname in REGIONS:
        row.append({"text": rname, "callback_data": f"cb:wiz_rgn:{rid}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "❌ Отмена", "callback_data": "cb:menu"}])

    tg.send_message(chat_id, text, reply_markup={"inline_keyboard": buttons})


def wizard_step_query(tg: TelegramClient, chat_id: int):
    """Step 3: Keyword prompt."""
    USER_STATES[chat_id]["step"] = "QUERY"
    text = (
        "🔍 <b>Шаг 3 из 4: Введите ключевые слова для поиска</b>\n\n"
        "Например: <code>ps5 slim</code>, <code>rtx 4070</code> или <code>iphone 13</code>.\n"
        "Или нажмите кнопку «Пропустить», если хотите отслеживать всю категорию без фильтра по словам."
    )
    markup = {
        "inline_keyboard": [
            [{"text": "⏩ Пропустить ключевые слова", "callback_data": "cb:wiz_skip_query"}],
            [{"text": "❌ Отмена", "callback_data": "cb:menu"}]
        ]
    }
    tg.send_message(chat_id, text, reply_markup=markup)


def wizard_step_price(tg: TelegramClient, chat_id: int):
    """Step 4: Price bounds prompt."""
    USER_STATES[chat_id]["step"] = "PRICE"
    text = (
        "💰 <b>Шаг 4 из 4: Укажите цену в BYN</b>\n\n"
        "Напишите в ответ диапазоном или пределом:\n"
        "• <code>1000 2100</code> (от 1000 до 2100 BYN)\n"
        "• <code>до 1500</code> (максимум 1500 BYN)\n"
        "• <code>от 800</code> (минимум 800 BYN)\n\n"
        "Или нажмите кнопку «Любая цена», если фильтр цен не нужен."
    )
    markup = {
        "inline_keyboard": [
            [{"text": "⏩ Любая цена", "callback_data": "cb:wiz_skip_price"}],
            [{"text": "❌ Отмена", "callback_data": "cb:menu"}]
        ]
    }
    tg.send_message(chat_id, text, reply_markup=markup)


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

    pending_id = f"p_{int(time.time())}"
    PENDING_QUERIES[pending_id] = {
        "chat_id": chat_id,
        "name": name,
        "params": params,
        "initial_ads": [a["ad_id"] for a in ads]
    }

    text = (
        f"✅ <b>Фильтр успешно сформирован!</b>\n\n"
        f"🏷 <b>Название:</b> {html.escape(name)}\n\n"
        f"{format_params_human(params)}\n\n"
        f"📊 <b>Найдено сейчас на сайте:</b> {count} объявлений\n\n"
        f"Запустить мониторинг?"
    )
    markup = {
        "inline_keyboard": [
            [{"text": "🚀 Запустить отслеживание", "callback_data": f"cb:confirm:{pending_id}"}],
            [{"text": "❌ Отмена", "callback_data": f"cb:cancel:{pending_id}"}]
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

    tg.answer_callback_query(cb_id)

    if data == "cb:menu":
        USER_STATES.pop(chat_id, None)
        show_main_menu(tg, chat_id)

    elif data == "cb:add_link":
        USER_STATES[chat_id] = {"step": "WAIT_URL", "data": {}}
        text = (
            "🔗 <b>Отправьте ссылку на поиск с Kufar.by</b>\n\n"
            "Настройте любые фильтры на сайте (категория, районы, цена, состояние и т.д.), скопируйте ссылку из адресной строки браузера и отправьте её мне сюда сообщением 👇"
        )
        markup = {"inline_keyboard": [[{"text": "🔙 Отмена", "callback_data": "cb:menu"}]]}
        tg.send_message(chat_id, text, reply_markup=markup)

    elif data == "cb:wiz_start":
        wizard_step_category(tg, chat_id)

    elif data.startswith("cb:wiz_cat:"):
        cat_id = data.split(":")[-1]
        if chat_id in USER_STATES:
            USER_STATES[chat_id]["data"]["cat"] = cat_id
        wizard_step_region(tg, chat_id)

    elif data.startswith("cb:wiz_rgn:"):
        rgn_id = data.split(":")[-1]
        if chat_id in USER_STATES:
            USER_STATES[chat_id]["data"]["rgn"] = rgn_id
        wizard_step_query(tg, chat_id)

    elif data == "cb:wiz_skip_query":
        wizard_step_price(tg, chat_id)

    elif data == "cb:wiz_skip_price":
        wizard_finish(tg, chat_id)

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

        # Sync cloud backup to Telegram so search is never lost on Render deploy
        sync_backup_to_telegram(tg, config)

        tg.send_message(
            chat_id,
            f"🎉 <b>Поиск «{html.escape(item['name'])}» успешно активирован!</b> [ID: {qid}]\n\n"
            f"🔔 Теперь я буду непрерывно мониторить Kufar и <b>мгновенно</b> присылать новые объявления!",
            reply_markup=MAIN_KEYBOARD
        )

    elif data.startswith("cb:cancel:"):
        pending_id = data.split(":")[-1]
        PENDING_QUERIES.pop(pending_id, None)
        if msg_id:
            tg.delete_message(chat_id, msg_id)
        tg.send_message(chat_id, "❌ Настройка поиска отменена.", reply_markup=MAIN_KEYBOARD)

    elif data.startswith("cb:del:"):
        qid = int(data.split(":")[-1])
        delete_query(qid, chat_id=chat_id)
        sync_backup_to_telegram(tg, config)
        tg.send_message(chat_id, f"🗑 Поиск <b>[ID: {qid}]</b> удален.", reply_markup=MAIN_KEYBOARD)
        admin_chat_id = config.get("admin_chat_id", 0)
        if admin_chat_id and chat_id != admin_chat_id:
            try:
                tg.send_message(admin_chat_id, f"🗑 <b>Пользователь {chat_id} удалил свой поиск [ID: {qid}].</b>")
            except Exception:
                pass

    elif data.startswith("cb:show_ads:") or data.startswith("cb:check:"):
        qid = int(data.split(":")[-1])
        output_ads_for_query(tg, chat_id, qid)

    elif data.startswith("cb:photos:"):
        ad_id = int(data.split(":")[-1])
        photos = AD_PHOTOS_CACHE.get(ad_id)
        if not photos:
            photos = fetch_ad_photos(ad_id)
            if photos:
                AD_PHOTOS_CACHE[ad_id] = photos

        if photos and len(photos) >= 2:
            tg.answer_callback_query(cb_id, text=f"📸 Отправляю {len(photos)} фото...")
            media = [{"type": "photo", "media": photos[0], "caption": f"📸 <b>Все фотографии к объявлению #{ad_id}</b>", "parse_mode": "HTML"}]
            for u in photos[1:10]:
                media.append({"type": "photo", "media": u})
            res_album = tg.send_media_group(chat_id, media)
            if res_album and isinstance(res_album, list):
                mids = AD_MESSAGES_MAP.get((chat_id, ad_id), [])
                for m in res_album:
                    if isinstance(m, dict) and "message_id" in m:
                        mids.append(m["message_id"])
                AD_MESSAGES_MAP[(chat_id, ad_id)] = mids
        elif photos:
            tg.answer_callback_query(cb_id, text="В этом объявлении только 1 фото")
        else:
            tg.answer_callback_query(cb_id, text="Не удалось загрузить дополнительные фото")

    elif data.startswith("cb:contact:"):
        ad_id = int(data.split(":")[-1])
        ad = AD_DATA_CACHE.get(ad_id, {})
        ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
        tpl = get_user_template(chat_id)
        text = (
            f"💬 <b>Шаблон первого сообщения продавцу:</b>\n\n"
            f"Нажмите на текст ниже, чтобы скопировать его в буфер обмена одним кликом:\n\n"
            f"<code>{html.escape(tpl)}</code>\n\n"
            f"Затем откройте чат с продавцом на Куфаре и вставьте сообщение 👇"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "💬 Открыть диалог на Kufar", "url": ad_link}]
            ]
        }
        tg.send_message(chat_id, text, reply_markup=markup)

    elif data.startswith("cb:bl:"):
        ad_id = int(data.split(":")[-1])
        add_to_blacklist(chat_id, ad_id)
        save_local_backup()
        tg.answer_callback_query(cb_id, text="🚫 Объявление добавлено в ЧС и удалено!", show_alert=False)

        # Delete all messages related to this ad (album photos + description card)
        mids = AD_MESSAGES_MAP.pop((chat_id, ad_id), [])
        if msg_id and msg_id not in mids:
            mids.append(msg_id)
        for m in mids:
            try:
                tg.delete_message(chat_id, m)
            except Exception as e:
                logger.debug(f"Could not delete message {m}: {e}")

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
        tg.answer_callback_query(
            cb_id,
            text=f"👤 Продавец {disp_name} добавлен в ЧС! Все его объявления скрыты.",
            show_alert=True
        )

        # Delete all messages related to this ad (album photos + description card)
        mids = AD_MESSAGES_MAP.pop((chat_id, ad_id), [])
        if msg_id and msg_id not in mids:
            mids.append(msg_id)
        for m in mids:
            try:
                tg.delete_message(chat_id, m)
            except Exception as e:
                logger.debug(f"Could not delete message {m}: {e}")

    elif data.startswith("cb:unbl:"):
        ad_id = int(data.split(":")[-1])
        remove_from_blacklist(chat_id, ad_id)
        save_local_backup()
        tg.answer_callback_query(cb_id, text="✅ Объявление убрано из ЧС", show_alert=False)
        ad_link = f"https://www.kufar.by/item/{ad_id}"
        ad = AD_DATA_CACHE.get(ad_id, {})
        new_markup = make_ad_keyboard(ad_link, ad_id, seller_name=ad.get("seller"), is_blacklisted=False)
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
        USER_STATES.pop(chat_id, None)
        show_stop_words_menu(tg, chat_id)

    elif data == "cb:sw_add":
        USER_STATES[chat_id] = {"step": "WAIT_STOP_WORDS"}
        text = (
            "⛔ <b>Добавление стоп-слов</b>\n\n"
            "Отправьте слова, при наличии которых в названии или описании объявление будет скрыто.\n"
            "Можно отправлять через запятую или пробел (например: <code>аренда, чехол, запчасти, коробка, обмен</code>):"
        )
        markup = {"inline_keyboard": [[{"text": "🔙 Отмена", "callback_data": "cb:sw_menu"}]]}
        tg.send_message(chat_id, text, reply_markup=markup)

    elif data == "cb:sw_del_menu":
        sw_list = get_stop_words(chat_id)
        if not sw_list:
            tg.send_message(chat_id, "ℹ️ Список стоп-слов пуст.", reply_markup=MAIN_KEYBOARD)
            return
        buttons = []
        for w in sw_list:
            buttons.append([{"text": f"❌ {w}", "callback_data": f"cb:sw_del:{w}"}])
        buttons.append([{"text": "🔙 Назад", "callback_data": "cb:sw_menu"}])
        tg.send_message(chat_id, "Выберите слово, которое хотите удалить:", reply_markup={"inline_keyboard": buttons})

    elif data.startswith("cb:sw_del:"):
        word = data[len("cb:sw_del:"):]
        remove_stop_word(chat_id, word)
        save_local_backup()
        tg.answer_callback_query(cb_id, text=f"Удалено: {word}")
        show_stop_words_menu(tg, chat_id)

    elif data == "cb:sw_clear":
        cnt = clear_stop_words(chat_id)
        save_local_backup()
        tg.answer_callback_query(cb_id, text=f"🧹 Удалено {cnt} стоп-слов!", show_alert=True)
        show_stop_words_menu(tg, chat_id)

    elif data == "cb:tpl_menu":
        USER_STATES.pop(chat_id, None)
        show_template_menu(tg, chat_id)

    elif data == "cb:tpl_edit":
        USER_STATES[chat_id] = {"step": "WAIT_TEMPLATE"}
        text = (
            "✏️ <b>Редактирование шаблона</b>\n\n"
            "Отправьте мне новым сообщением текст, который вы хотите отправлять продавцам первым делом:"
        )
        markup = {"inline_keyboard": [[{"text": "🔙 Отмена", "callback_data": "cb:tpl_menu"}]]}
        tg.send_message(chat_id, text, reply_markup=markup)

    elif data == "cb:tpl_reset":
        set_user_template(chat_id, DEFAULT_CONTACT_TEMPLATE)
        save_local_backup()
        tg.answer_callback_query(cb_id, text="🔄 Шаблон сброшен к исходному!", show_alert=True)
        show_template_menu(tg, chat_id)

    elif data.startswith("cb:interval:"):
        sec = int(data.split(":")[-1])
        config["check_interval_seconds"] = sec
        set_setting("check_interval_seconds", str(sec))
        save_local_backup()
        FORCE_CHECK_EVENT.set()
        try:
            config_path = os.path.join(BASE_DIR, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving interval: {e}")
        tg.send_message(chat_id, f"⏱ <b>Интервал проверки успешно изменен на {sec} сек!</b>\nЗначение сохранено в базу данных.", reply_markup=MAIN_KEYBOARD)


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
    if text in ["/start", "🔙 В главное меню"]:
        USER_STATES.pop(chat_id, None)
        show_main_menu(tg, chat_id)
        return

    if text in ["➕ Добавить поиск", "/add"]:
        prompt_add_search_options(tg, chat_id)
        return

    if text in ["📋 Мои поиски", "/list"]:
        show_my_searches(tg, chat_id)
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
        tg.send_message(chat_id, f"✅ <b>Чёрный список очищен!</b> Удалено записей: {cnt}.", reply_markup=MAIN_KEYBOARD)
        return

    if text in ["📊 Статистика", "/status"]:
        uptime_str = str(datetime.now() - START_TIME).split(".")[0]
        user_queries = len(get_user_queries(chat_id))
        bl_count = get_blacklist_count(chat_id)
        sw_count = len(get_stop_words(chat_id))
        bl_sellers_count = len(get_blacklisted_sellers(chat_id))
        backup_str = LAST_BACKUP_TIME.strftime('%d.%m.%Y в %H:%M:%S') if LAST_BACKUP_TIME else "сохранена"
        status_text = (
            f"📊 <b>Статус Kufar Hunter Bot</b>\n\n"
            f"⏱ <b>Время работы:</b> {uptime_str}\n"
            f"🎯 <b>Ваших активных поисков:</b> {user_queries}\n"
            f"⛔ <b>Стоп-слов настроено:</b> {sw_count}\n"
            f"👤 <b>Продавцов в ЧС:</b> {bl_sellers_count}\n"
            f"🚫 <b>Объявлений в ЧС:</b> {bl_count}\n"
            f"💾 <b>Резервная копия:</b> {backup_str}\n"
            f"🔄 <b>Проверок выполнено:</b> {TOTAL_CHECKS}\n"
            f"⏳ <b>Интервал проверки:</b> {config.get('check_interval_seconds', 60)} сек."
        )
        if chat_id == admin_chat_id:
            total_users = get_user_count()
            total_active_q = len(get_active_queries())
            status_text += (
                f"\n\n👑 <b>Панель администратора:</b>\n"
                f"👥 <b>Пользователей в боте:</b> {total_users}\n"
                f"🎯 <b>Всего активных фильтров:</b> {total_active_q}\n"
                f"📨 <b>Отправлено новых объявлений:</b> {NEW_ADS_FOUND}"
            )
        tg.send_message(chat_id, status_text, reply_markup=MAIN_KEYBOARD)
        return

    if text in ["❓ Помощь", "/help"]:
        help_text = (
            "📖 <b>Как пользоваться ботом:</b>\n\n"
            "1. Нажмите <b>«➕ Добавить поиск»</b>.\n"
            "2. Выберите <b>«🔗 Вставить ссылку с Куфара»</b>.\n"
            "3. Перейдите на <b>kufar.by</b>, выставьте нужные параметры (категорию, город, районы, цену, бренд и т.д.) и скопируйте ссылку.\n"
            "4. Отправьте ссылку сюда сообщением — бот перенесет все фильтры 1 в 1!\n\n"
            "📦 <b>«Вывести всё»</b> — выводит актуальные объявления по вашим сохранённым фильтрам.\n"
            "🚫 <b>«Не интересно (в ЧС)»</b> — скрывает ненужное объявление, чтобы оно больше никогда не показывалось."
        )
        tg.send_message(chat_id, help_text, reply_markup=MAIN_KEYBOARD)
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
                    reply_markup=MAIN_KEYBOARD
                )
            else:
                tg.send_message(chat_id, "ℹ️ Эти слова уже есть в списке или введены некорректно.", reply_markup=MAIN_KEYBOARD)
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
                    reply_markup=MAIN_KEYBOARD
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
            for u in updates:
                last_update_id = u["update_id"] + 1

                # Handle Inline Button Clicks
                if "callback_query" in u:
                    process_callback_query(tg, u["callback_query"], config)
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
                                        reply_markup=MAIN_KEYBOARD
                                    )
                                    sync_backup_to_telegram(tg, config)
                                except Exception as e:
                                    tg.send_message(chat_id, f"⚠️ Ошибка при чтении бэкапа: {e}")
                        continue

                if chat_id and text:
                    process_text_message(tg, chat_id, text, config, user_info=from_user)
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
    if state.get("queries") or state.get("blacklisted_ads") or state.get("blacklisted_sellers") or state.get("stop_words"):
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

    # Start Telegram listener
    listener_thread = threading.Thread(target=telegram_listener, args=(tg, config), daemon=True)
    listener_thread.start()

    interval = config.get("check_interval_seconds", 60)
    print(f"📡 Охотник запущен (интервал: {interval} сек).")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Остановка бота...")
        STOP_EVENT.set()
        FORCE_CHECK_EVENT.set()
        time.sleep(1)
        print("Бот остановлен.")


if __name__ == "__main__":
    main()
