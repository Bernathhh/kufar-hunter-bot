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
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

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
    mark_ads_seen,
    get_query_seen_count
)
from kufar_api import parse_kufar_url_or_query, fetch_ads
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
        [{"text": "⚡ Проверить сейчас"}, {"text": "⏱ Интервал"}],
        [{"text": "📊 Статистика"}, {"text": "❓ Помощь"}]
    ],
    "resize_keyboard": True
}


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


def format_ad_caption(ad: Dict[str, Any], query_name: str) -> str:
    """Creates a clean, attractive HTML caption for the ad."""
    subject = html.escape(ad.get("subject", "Объявление"))
    link = ad.get("ad_link", "")
    price_byn = ad.get("price_byn")
    price_usd = ad.get("price_usd")
    timing = parse_ad_timing(ad.get("list_time", ""))

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

    seller = html.escape(ad.get("seller", "").strip()) or "Частное лицо"

    time_label = "Первично создано:" if timing["is_repost"] else "Опубликовано:"

    lines = [
        f"{timing['header']}\n",
        f"📌 <b>{subject}</b>",
        f"💰 <b>Цена:</b> {price_str}",
        f"🕒 <b>{time_label}</b> {timing['time_str']}",
        f"📍 <b>Локация:</b> {location}",
        f"👤 <b>Продавец:</b> {seller}",
        f"🎯 <i>Поиск: {html.escape(query_name)}</i>"
    ]
    if timing.get("note"):
        lines.append(timing["note"])

    lines.append(f"\n👉 <a href=\"{link}\">Открыть объявление на Kufar</a>")

    return "\n".join(lines)


def send_ad_notification(tg: TelegramClient, chat_id: int, ad: Dict[str, Any], query_name: str) -> bool:
    """Sends notification with photo if available, or text with link."""
    caption = format_ad_caption(ad, query_name)
    image_url = ad.get("image_url")

    markup = {
        "inline_keyboard": [
            [{"text": "🔗 Открыть на Kufar", "url": ad.get("ad_link", "https://kufar.by")}]
        ]
    }

    if image_url:
        ok = tg.send_photo(chat_id, image_url, caption=caption, reply_markup=markup)
        if ok:
            return True

    return tg.send_message(chat_id, caption, disable_web_page_preview=False, reply_markup=markup) is not None


def check_single_query(tg: TelegramClient, query: Dict[str, Any], chat_id_fallback: Optional[int] = None) -> int:
    """Checks Kufar for a specific query and sends notifications for new listings."""
    global NEW_ADS_FOUND
    qid = query["id"]
    chat_id = query.get("chat_id") or chat_id_fallback
    if not chat_id:
        logger.error(f"Cannot check query {qid}: chat_id is missing")
        return 0
    name = query["name"]
    params = query["params"]

    ads = fetch_ads(params)
    if not ads:
        return 0

    seen_count = get_query_seen_count(qid)

    # First run: seed database to avoid spamming historical ads
    if seen_count == 0:
        logger.info(f"Seeding historical ads for query '{name}' (count: {len(ads)})")
        all_ids = [a["ad_id"] for a in ads]
        mark_ads_seen(qid, all_ids)
        return 0

    new_ads = [a for a in ads if not is_ad_seen(qid, a["ad_id"])]
    if not new_ads:
        return 0

    logger.info(f"Found {len(new_ads)} new ads for query '{name}'!")

    for ad in reversed(new_ads):
        try:
            send_ad_notification(tg, chat_id, ad, name)
            mark_ads_seen(qid, [ad["ad_id"]])
            NEW_ADS_FOUND += 1
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"Error sending ad notification: {e}")

    return len(new_ads)


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

def show_main_menu(tg: TelegramClient, chat_id: int):
    """Sends greeting with main keyboard menu."""
    text = (
        "👋 <b>Главное меню Kufar Hunter</b>\n\n"
        "Бот настроен исключительно под ваш аккаунт и готов ловить объявления по точным фильтрам Куфара.\n\n"
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
                [{"text": "⚡ Проверить этот поиск", "callback_data": f"cb:check:{qid}"}],
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

    admin_chat_id = config.get("admin_chat_id", 0)
    if admin_chat_id != 0 and chat_id != admin_chat_id:
        tg.answer_callback_query(cb_id, text="⛔ Доступ закрыт", show_alert=True)
        return

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
        tg.send_message(chat_id, f"🗑 Поиск <b>[ID: {qid}]</b> удален.", reply_markup=MAIN_KEYBOARD)

    elif data.startswith("cb:check:"):
        qid = int(data.split(":")[-1])
        queries = get_user_queries(chat_id)
        target = next((q for q in queries if q["id"] == qid), None)
        if target:
            tg.send_message(chat_id, f"🔍 Проверяю «{html.escape(target['name'])}» на Куфаре...")
            try:
                count = check_single_query(tg, target, chat_id_fallback=chat_id)
                current_ads = fetch_ads(target["params"])
                total_on_site = len(current_ads)
                if count > 0:
                    tg.send_message(chat_id, f"🔥 Найдено и отправлено новых объявлений: <b>{count}</b>!")
                else:
                    tg.send_message(
                        chat_id,
                        f"✅ <b>Проверка «{html.escape(target['name'])}» завершена!</b>\n\n"
                        f"Новых объявлений с момента прошлой проверки не появлялось.\n"
                        f"📦 Всего на сайте по этому фильтру: <b>{total_on_site}</b> объявлений (все уже просмотрены).\n\n"
                        f"🔔 Как только кто-то выложит новое объявление — бот сразу пришлет его сюда с фото!"
                    )
            except Exception as e:
                logger.error(f"Error checking single query: {e}")
                tg.send_message(chat_id, f"⚠️ Ошибка при проверке: {e}")

    elif data.startswith("cb:interval:"):
        sec = int(data.split(":")[-1])
        config["check_interval_seconds"] = sec
        try:
            config_path = os.path.join(BASE_DIR, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving interval: {e}")
        tg.send_message(chat_id, f"⏱ <b>Интервал проверки успешно изменен на {sec} сек!</b>", reply_markup=MAIN_KEYBOARD)


def process_text_message(tg: TelegramClient, chat_id: int, text: str, config: dict):
    """Processes normal text messages from the user."""
    admin_chat_id = config.get("admin_chat_id", 0)

    # If no admin configured yet, the first user sending /start becomes admin
    if admin_chat_id == 0:
        if text.strip() == "/start":
            config["admin_chat_id"] = chat_id
            admin_chat_id = chat_id
            try:
                config_path = os.path.join(BASE_DIR, "config.json")
                if os.path.exists(config_path):
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(config, f, indent=4, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Error saving admin chat id: {e}")
        else:
            tg.send_message(chat_id, "⚠️ Бот еще не настроен. Отправьте /start для авторизации.")
            return

    # Strictly restrict to admin_chat_id
    if chat_id != admin_chat_id:
        tg.send_message(chat_id, "⛔ <b>Доступ ограничен.</b> Этот бот работает в приватном режиме.")
        return

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

    if text in ["⚡ Проверить сейчас", "/check"]:
        queries = get_user_queries(chat_id)
        if not queries:
            tg.send_message(chat_id, "📭 У вас пока нет активных поисков. Сначала добавьте поиск через кнопку «➕ Добавить поиск».")
            return

        tg.send_message(chat_id, f"🔍 Запускаю внеочередную проверку (фильтров: {len(queries)})...")
        total_new = 0
        for q in queries:
            try:
                total_new += check_single_query(tg, q, chat_id_fallback=chat_id)
            except Exception as e:
                logger.error(f"Error checking query {q.get('id')}: {e}")

        if total_new > 0:
            tg.send_message(chat_id, f"🔥 Проверка завершена! Найдено и отправлено новых объявлений: <b>{total_new}</b>.")
        else:
            tg.send_message(
                chat_id,
                "✅ <b>Проверка завершена!</b>\n\n"
                "Новых объявлений с момента прошлой проверки не появлялось.\n"
                "🔔 Бот продолжает мониторить сайт каждые 30 секунд в фоне и пришлет уведомление сразу же при публикации!"
            )
        return

    if text in ["📊 Статистика", "/status"]:
        uptime_str = str(datetime.now() - START_TIME).split(".")[0]
        active = len(get_active_queries())
        tg.send_message(
            chat_id,
            f"📊 <b>Статус Kufar Hunter Bot</b>\n\n"
            f"⏱ <b>Время работы:</b> {uptime_str}\n"
            f"🎯 <b>Активных поисков:</b> {active}\n"
            f"🔄 <b>Проверок выполнено:</b> {TOTAL_CHECKS}\n"
            f"📨 <b>Отправлено новых объявлений:</b> {NEW_ADS_FOUND}\n"
            f"⏳ <b>Интервал проверки:</b> {config.get('check_interval_seconds', 60)} сек.\n"
            f"🛡 <b>Приватный режим:</b> чат <code>{admin_chat_id}</code>"
        )
        return

    if text in ["❓ Помощь", "/help"]:
        help_text = (
            "📖 <b>Как пользоваться ботом:</b>\n\n"
            "1. Нажмите <b>«➕ Добавить поиск»</b>.\n"
            "2. Выберите <b>«🔗 Вставить ссылку с Куфара»</b>.\n"
            "3. Перейдите на <b>kufar.by</b>, выставьте нужные параметры (категорию, город, районы, цену, бренд и т.д.) и скопируйте ссылку.\n"
            "4. Отправьте ссылку сюда сообщением — бот перенесет все фильтры 1 в 1!\n\n"
            "Также можно отправить ссылку боту прямо в любой момент без нажатия кнопок."
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
                if chat_id and text:
                    process_text_message(tg, chat_id, text, config)
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

    bot_info = tg.get_me()
    if not bot_info:
        print("❌ ОШИБКА: Не удалось авторизоваться в Telegram. Проверьте токен.")
        sys.exit(1)

    bot_username = bot_info.get("username", "Unknown")
    print(f"\n🚀 Бот @{bot_username} успешно авторизован!")
    print(f"🔒 Приватный режим активирован: доступ разрешен ТОЛЬКО chat_id={config['admin_chat_id']}")

    # Start health server for Render
    port_env = os.getenv("PORT")
    if port_env and port_env.isdigit():
        start_health_server(int(port_env))
    elif os.getenv("ENABLE_HEALTH_SERVER"):
        start_health_server(8080)

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
