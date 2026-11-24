"""
Telegram Mini App (WebApp) UI and JSON REST API handler for Kufar Hunter Bot.
Zero external dependencies — built purely with Python standard library + native Telegram WebApp JS SDK.
"""
import hashlib
import hmac
import http.server
import json
import logging
import os
import statistics
import time
import urllib.parse
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from database import (
    DEFAULT_CONTACT_TEMPLATE,
    add_blacklisted_seller,
    add_favorite,
    add_query,
    add_stop_word,
    add_to_blacklist,
    clear_stop_words,
    clear_user_blacklist,
    clear_user_favorites,
    delete_query,
    get_active_queries,
    get_blacklisted_sellers,
    get_blacklist_count,
    get_favorites_count,
    get_query_seen_count,
    get_silent_mode,
    get_stop_words,
    get_user_blacklist,
    get_user_count,
    get_user_favorites,
    get_user_queries,
    get_user_template,
    is_favorite,
    is_in_silent_hours,
    is_seller_blacklisted,
    is_user_monitoring_paused,
    mark_ads_seen,
    register_or_update_user,
    remove_blacklisted_seller,
    remove_favorite,
    remove_stop_word,
    set_setting,
    set_silent_mode,
    set_user_monitoring_paused,
    set_user_template,
    toggle_query_active,
)
from kufar_api import fetch_ads, parse_kufar_url_or_query

logger = logging.getLogger("kufar_webapp")


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 86400
) -> Optional[Dict[str, Any]]:
    """
    Validates Telegram WebApp initData using HMAC-SHA256 per official Telegram specification.
    Returns parsed user dict on success, or None if invalid/expired.
    """
    if not init_data or not bot_token:
        return None
    try:
        parsed_pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data_dict: Dict[str, str] = {}
        hash_val = ""
        for k, v in parsed_pairs:
            if k == "hash":
                hash_val = v
            else:
                data_dict[k] = v

        if not hash_val:
            return None

        auth_date_str = data_dict.get("auth_date", "0")
        if max_age_seconds > 0 and auth_date_str.isdigit():
            age = time.time() - int(auth_date_str)
            if age > max_age_seconds or age < -300:
                return None

        data_check_string = "\n".join(f"{k}={data_dict[k]}" for k in sorted(data_dict.keys()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed_hash, hash_val):
            return None

        user_raw = data_dict.get("user")
        if not user_raw:
            return None
        user_obj = json.loads(user_raw)
        if not isinstance(user_obj, dict) or not user_obj.get("id"):
            return None
        return user_obj
    except Exception as e:
        logger.debug(f"initData verification failed: {e}")
        return None


def _parse_prc_bounds(prc_param: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    if not prc_param or not str(prc_param).startswith("r:"):
        return None, None
    parts = str(prc_param)[2:].split(",")
    if len(parts) != 2:
        return None, None
    try:
        min_p = float(parts[0]) / 100.0 if parts[0] and parts[0] not in ("0", "") else None
        max_p = float(parts[1]) / 100.0 if parts[1] and parts[1] not in ("999999999", "0", "") else None
        return min_p, max_p
    except Exception:
        return None, None


def _build_query_chips(p: Dict[str, Any], app_ctx: Dict[str, Any]) -> List[str]:
    chips: List[str] = []
    cat_map = dict(app_ctx.get("categories", []))
    rgn_map = dict(app_ctx.get("regions", []))

    if p.get("query"):
        chips.append(f"🔍 {p['query']}")
    cat_id = str(p.get("cat", ""))
    if cat_id and cat_id != "all":
        chips.append(cat_map.get(cat_id, f"Кат. {cat_id}"))
    cte_map = {"1": "Приставки", "2": "Игры", "3": "Аксессуары"}
    if str(p.get("cte", "")) in cte_map:
        chips.append(cte_map[str(p["cte"])])
    pb_map = {"5": "Apple", "24": "Samsung", "35": "Xiaomi", "11": "Huawei", "12": "Honor"}
    cbc_map = {"31": "Sony", "13": "Xbox", "17": "Nintendo", "30": "Steam Deck"}
    brand = p.get("brand") or cbc_map.get(str(p.get("cbc", ""))) or pb_map.get(str(p.get("pb", "")))
    if brand:
        chips.append(f"🏷 {brand}")
    rgn_id = str(p.get("rgn", ""))
    if rgn_id and rgn_id != "all":
        chips.append(rgn_map.get(rgn_id, f"Рег. {rgn_id}"))
    min_p, max_p = _parse_prc_bounds(p.get("prc"))
    if min_p and max_p:
        chips.append(f"💰 {int(min_p):,}–{int(max_p):,} BYN".replace(",", " "))
    elif min_p:
        chips.append(f"💰 от {int(min_p):,} BYN".replace(",", " "))
    elif max_p:
        chips.append(f"💰 до {int(max_p):,} BYN".replace(",", " "))
    return chips or ["📦 Все объявления"]


def _build_user_state_payload(chat_id: int, app_ctx: Dict[str, Any]) -> Dict[str, Any]:
    config = app_ctx["config"]
    total_checks, new_ads_found = app_ctx["get_counters"]()
    uptime = str(datetime.now() - app_ctx["start_time"]).split(".")[0]

    queries_raw = get_user_queries(chat_id)
    queries = []
    for q in queries_raw:
        queries.append({
            "id": q["id"],
            "name": q["name"],
            "params": q["params"],
            "chips": _build_query_chips(q["params"], app_ctx),
            "is_active": bool(q.get("is_active", True)),
            "seen_count": get_query_seen_count(q["id"]),
            "created_at": str(q.get("created_at", "")),
        })

    favs = get_user_favorites(chat_id)
    bl_sellers = sorted(get_blacklisted_sellers(chat_id))
    bl_ads_count = get_blacklist_count(chat_id)
    stop_words = get_stop_words(chat_id)
    silent_mode = get_silent_mode(chat_id)
    paused = is_user_monitoring_paused(chat_id)
    template = get_user_template(chat_id)

    admin_chat_id = config.get("admin_chat_id", 0)
    is_admin = bool(admin_chat_id and chat_id == admin_chat_id)

    stats: Dict[str, Any] = {
        "uptime": uptime,
        "total_checks": total_checks,
        "new_ads_found": new_ads_found,
        "is_admin": is_admin,
    }
    if is_admin:
        stats["total_users"] = get_user_count()
        stats["total_active_queries"] = len(get_active_queries())

    return {
        "ok": True,
        "chat_id": chat_id,
        "monitoring_paused": paused,
        "interval_seconds": int(config.get("check_interval_seconds", 60)),
        "silent_mode": silent_mode,
        "in_silent_now": is_in_silent_hours(chat_id),
        "template": template,
        "default_template": DEFAULT_CONTACT_TEMPLATE,
        "queries": queries,
        "favorites": favs,
        "stop_words": stop_words,
        "blacklisted_sellers": bl_sellers,
        "blacklisted_ads_count": bl_ads_count,
        "stats": stats,
        "categories": [{"id": c[0], "name": c[1]} for c in app_ctx.get("categories", [])],
        "regions": [{"id": r[0], "name": r[1]} for r in app_ctx.get("regions", [])],
    }


def _params_from_body(body: Dict[str, Any], app_ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    url_raw = (body.get("url") or "").strip()
    if url_raw:
        params = parse_kufar_url_or_query(url_raw)
        cat_map = dict(app_ctx.get("categories", []))
        cat_name = cat_map.get(str(params.get("cat", "")), "Куфар поиск")
        brand = params.get("brand") or ("Sony" if str(params.get("cbc")) == "31" else "")
        name = params.get("query") or (f"{cat_name} ({brand})" if brand else cat_name)
        return params, name

    params = {"sort": "lst.d", "size": "30"}
    kw = (body.get("query") or "").strip()
    if kw:
        params["query"] = kw
    cat_id = (body.get("cat") or "").strip()
    if cat_id and cat_id != "all":
        params["cat"] = cat_id
    rgn_id = (body.get("rgn") or "").strip()
    if rgn_id and rgn_id != "all":
        params["rgn"] = rgn_id
        if rgn_id == "7":
            params["ar"] = "v.or:22,23,24,25,26,27,28,29,30"

    min_raw = str(body.get("min_price") or "").strip()
    max_raw = str(body.get("max_price") or "").strip()
    min_p = int(float(min_raw)) if min_raw and min_raw.replace(".", "", 1).isdigit() else None
    max_p = int(float(max_raw)) if max_raw and max_raw.replace(".", "", 1).isdigit() else None
    if min_p is not None and max_p is not None:
        params["prc"] = f"r:{min_p * 100},{max_p * 100}"
    elif min_p is not None:
        params["prc"] = f"r:{min_p * 100},999999999"
    elif max_p is not None:
        params["prc"] = f"r:0,{max_p * 100}"

    cat_map = dict(app_ctx.get("categories", []))
    name = kw or cat_map.get(cat_id, "Куфар поиск")
    return params, name


WEBAPP_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover" />
<title>Kufar Hunter</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #0f1117);
    --card: var(--tg-theme-secondary-bg-color, #191d27);
    --card-elev: rgba(255,255,255,0.045);
    --border: rgba(128,140,160,0.16);
    --text: var(--tg-theme-text-color, #f3f5f9);
    --hint: var(--tg-theme-hint-color, #8e97aa);
    --accent: var(--tg-theme-button-color, #2ea6ff);
    --btn-text: var(--tg-theme-button-text-color, #ffffff);
    --green: #22c55e;
    --red: #ef4444;
    --amber: #f59e0b;
    --radius: 16px;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    font-size: 14px; line-height: 1.45;
    overscroll-behavior-y: contain;
  }
  .app { max-width: 640px; margin: 0 auto; padding: 12px 14px 90px; }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 2px 14px; gap: 10px;
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-icon {
    width: 38px; height: 38px; border-radius: 12px;
    background: linear-gradient(135deg, var(--accent), #6366f1);
    display: flex; align-items: center; justify-content: center;
    font-size: 20px; box-shadow: 0 4px 14px rgba(46,166,255,0.28);
  }
  .brand-title { font-size: 17px; font-weight: 700; letter-spacing: -0.2px; }
  .brand-sub { font-size: 11px; color: var(--hint); }
  .status-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 12px; border-radius: 999px;
    font-size: 12px; font-weight: 600; border: 1px solid var(--border);
    background: var(--card); color: var(--text); cursor: pointer;
    transition: transform .12s ease, opacity .15s;
  }
  .status-pill:active { transform: scale(0.96); }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); }
  .dot.paused { background: var(--amber); box-shadow: 0 0 8px var(--amber); }

  .tab-panel { display: none; animation: fadeIn .16s ease; }
  .tab-panel.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  .section-head {
    display: flex; align-items: center; justify-content: space-between;
    margin: 6px 2px 10px;
  }
  .section-title { font-size: 15px; font-weight: 700; }
  .btn {
    border: none; border-radius: 12px; padding: 10px 14px;
    font-size: 13px; font-weight: 600; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    background: var(--accent); color: var(--btn-text);
    transition: opacity .15s, transform .1s;
  }
  .btn:active { transform: scale(0.97); opacity: 0.9; }
  .btn-sm { padding: 7px 11px; font-size: 12px; border-radius: 10px; }
  .btn-ghost { background: var(--card-elev); color: var(--text); border: 1px solid var(--border); }
  .btn-danger { background: rgba(239,68,68,0.14); color: #f87171; border: 1px solid rgba(239,68,68,0.24); }
  .btn-success { background: rgba(34,197,94,0.14); color: #4ade80; border: 1px solid rgba(34,197,94,0.25); }
  .btn-block { width: 100%; }

  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 14px; margin-bottom: 12px;
  }
  .query-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
  .query-name { font-size: 15px; font-weight: 700; word-break: break-word; }
  .query-meta { font-size: 11.5px; color: var(--hint); margin-top: 2px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 12px; }
  .chip {
    background: var(--card-elev); border: 1px solid var(--border);
    padding: 4px 9px; border-radius: 8px; font-size: 11.5px; color: var(--text);
    display: inline-flex; align-items: center; gap: 5px;
  }
  .chip .x { opacity: 0.65; cursor: pointer; font-weight: 700; padding-left: 2px; }
  .chip .x:hover { opacity: 1; color: var(--red); }
  .query-actions { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 6px; }

  .switch {
    position: relative; width: 42px; height: 24px; flex-shrink: 0;
    background: rgba(128,140,160,0.3); border-radius: 999px; cursor: pointer; transition: background .2s;
  }
  .switch.on { background: var(--green); }
  .switch::after {
    content: ""; position: absolute; top: 3px; left: 3px; width: 18px; height: 18px;
    border-radius: 50%; background: #fff; transition: transform .2s; box-shadow: 0 1px 4px rgba(0,0,0,0.3);
  }
  .switch.on::after { transform: translateX(18px); }

  .seg { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 8px; }
  .seg-btn {
    padding: 8px 4px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--card-elev); color: var(--text); font-size: 12px; font-weight: 600; cursor: pointer;
  }
  .seg-btn.active { background: var(--accent); color: var(--btn-text); border-color: transparent; }

  .input, select, textarea {
    width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 10px 12px; font-size: 13.5px; font-family: inherit; outline: none;
  }
  .input:focus, select:focus, textarea:focus { border-color: var(--accent); }
  .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .field-label { display: block; font-size: 12px; font-weight: 600; color: var(--hint); margin: 10px 0 5px; }

  .ad-card {
    display: flex; gap: 12px; padding: 12px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); margin-bottom: 10px;
  }
  .ad-thumb {
    width: 82px; height: 82px; border-radius: 12px; object-fit: cover; flex-shrink: 0;
    background: var(--card-elev); display: flex; align-items: center; justify-content: center; font-size: 24px;
  }
  .ad-body { flex: 1; min-width: 0; }
  .ad-title { font-size: 13.5px; font-weight: 600; line-height: 1.3; margin-bottom: 4px; }
  .ad-price { font-size: 14.5px; font-weight: 800; color: var(--accent); }
  .ad-sub { font-size: 11.5px; color: var(--hint); margin-top: 3px; }
  .ad-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .badge {
    display: inline-block; font-size: 10.5px; font-weight: 700;
    padding: 2px 7px; border-radius: 6px; margin-left: 4px;
  }
  .badge-good { background: rgba(34,197,94,0.16); color: #4ade80; }
  .badge-high { background: rgba(245,158,11,0.16); color: #fbbf24; }

  .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
  .stat-box { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 12px; }
  .stat-val { font-size: 20px; font-weight: 800; letter-spacing: -0.3px; }
  .stat-lbl { font-size: 11.5px; color: var(--hint); margin-top: 2px; }

  .nav {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 50;
    background: var(--card); border-top: 1px solid var(--border);
    display: grid; grid-template-columns: repeat(4, 1fr);
    padding: 6px 6px calc(6px + env(safe-area-inset-bottom));
    backdrop-filter: blur(12px);
  }
  .nav-btn {
    background: none; border: none; color: var(--hint);
    display: flex; flex-direction: column; align-items: center; gap: 2px;
    padding: 4px 2px; font-size: 10.5px; font-weight: 600; cursor: pointer;
    position: relative;
  }
  .nav-btn .ico { font-size: 19px; line-height: 1.1; }
  .nav-btn.active { color: var(--accent); }
  .nav-badge {
    position: absolute; top: 1px; right: 20%; min-width: 16px; height: 16px;
    border-radius: 999px; background: var(--accent); color: var(--btn-text);
    font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center; padding: 0 4px;
  }

  .modal-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.62); z-index: 100;
    display: none; align-items: flex-end; justify-content: center;
  }
  .modal-bg.open { display: flex; }
  .modal {
    width: 100%; max-width: 640px; max-height: 88vh; overflow-y: auto;
    background: var(--card); border-top-left-radius: 20px; border-top-right-radius: 20px;
    border-top: 1px solid var(--border);
    padding: 16px 16px calc(22px + env(safe-area-inset-bottom));
    animation: slideUp .18s ease;
  }
  @keyframes slideUp { from { transform: translateY(30px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
  .modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .modal-title { font-size: 16px; font-weight: 700; }

  .toast {
    position: fixed; top: 14px; left: 50%; transform: translateX(-50%) translateY(-80px);
    background: #1f2937; color: #fff; padding: 9px 16px; border-radius: 999px;
    font-size: 12.5px; font-weight: 600; z-index: 200;
    box-shadow: 0 8px 24px rgba(0,0,0,0.35); transition: transform .22s ease;
    max-width: 90%; text-align: center;
  }
  .toast.show { transform: translateX(-50%) translateY(0); }

  .empty { text-align: center; padding: 34px 16px; color: var(--hint); }
  .empty .e-ico { font-size: 40px; margin-bottom: 8px; }
  .outside-box { max-width: 420px; margin: 60px auto; padding: 24px; text-align: center; }
</style>
</head>
<body>
<div id="toast" class="toast"></div>

<div id="outsideNotice" class="outside-box" style="display:none">
  <div style="font-size:48px;margin-bottom:10px">🏹</div>
  <h2 style="margin:0 0 8px">Kufar Hunter Mini App</h2>
  <p style="color:var(--hint)">Откройте это приложение прямо внутри Telegram-бота, чтобы управлять поисками, фильтрами и избранным.</p>
</div>

<div id="app" class="app" style="display:none">
  <div class="header">
    <div class="brand">
      <div class="brand-icon">🏹</div>
      <div>
        <div class="brand-title">Kufar Hunter</div>
        <div class="brand-sub" id="subHeader">Загрузка...</div>
      </div>
    </div>
    <button id="monitorPill" class="status-pill" onclick="toggleGlobalMonitoring()">
      <span id="monitorDot" class="dot"></span>
      <span id="monitorLabel">Активен</span>
    </button>
  </div>

  <!-- TAB 1: SEARCHES -->
  <div id="tab-searches" class="tab-panel active">
    <div class="section-head">
      <div class="section-title">Мои поиски</div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-sm btn-ghost" onclick="sendAllToChat()">📦 В чат</button>
        <button class="btn btn-sm" onclick="openAddModal()">➕ Добавить</button>
      </div>
    </div>
    <div id="queriesList"></div>
  </div>

  <!-- TAB 2: FAVORITES -->
  <div id="tab-favs" class="tab-panel">
    <div class="section-head">
      <div class="section-title">Избранное</div>
      <button id="clearFavsBtn" class="btn btn-sm btn-danger" onclick="clearFavorites()" style="display:none">🧹 Очистить</button>
    </div>
    <div id="favsList"></div>
  </div>

  <!-- TAB 3: SETTINGS & FILTERS -->
  <div id="tab-settings" class="tab-panel">
    <div class="card">
      <div class="section-title">⛔ Стоп-слова (минус-слова)</div>
      <div class="query-meta">Объявления с этими словами автоматически скрываются.</div>
      <div id="stopWordsChips" class="chips"></div>
      <div style="display:flex;gap:8px">
        <input id="newStopWords" class="input" placeholder="напр. аренда, чехол, ps4..." />
        <button class="btn btn-sm" onclick="addStopWords()">Добавить</button>
      </div>
    </div>

    <div class="card">
      <div class="section-title">⏱ Интервал проверки Kufar</div>
      <div class="seg" id="intervalSeg">
        <button class="seg-btn" data-sec="15" onclick="setIntervalSec(15)">15 сек</button>
        <button class="seg-btn" data-sec="30" onclick="setIntervalSec(30)">30 сек</button>
        <button class="seg-btn" data-sec="60" onclick="setIntervalSec(60)">60 сек</button>
        <button class="seg-btn" data-sec="120" onclick="setIntervalSec(120)">120 сек</button>
      </div>
    </div>

    <div class="card">
      <div class="query-top">
        <div>
          <div class="section-title">🌙 Тихий (ночной) режим</div>
          <div class="query-meta" id="silentStatusText">Уведомления без звука в заданные часы</div>
        </div>
        <div id="silentSwitch" class="switch" onclick="toggleSilentPreset()"></div>
      </div>
      <div class="row-2" style="margin-top:10px">
        <div>
          <span class="field-label" style="margin-top:0">С (Минск)</span>
          <input id="silentFrom" type="time" class="input" value="23:00" />
        </div>
        <div>
          <span class="field-label" style="margin-top:0">До (Минск)</span>
          <input id="silentTo" type="time" class="input" value="08:00" />
        </div>
      </div>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn btn-sm btn-block" onclick="saveSilentCustom()">Сохранить часы</button>
        <button class="btn btn-sm btn-ghost" onclick="saveSilentVal('off')">Выкл</button>
      </div>
    </div>

    <div class="card">
      <div class="section-title">💬 Шаблон сообщения продавцу</div>
      <div class="chips" style="margin-bottom:8px">
        <span class="chip" style="cursor:pointer" onclick="insertTplVar('{name}')">+ {name}</span>
        <span class="chip" style="cursor:pointer" onclick="insertTplVar('{item}')">+ {item}</span>
        <span class="chip" style="cursor:pointer" onclick="insertTplVar('{price}')">+ {price}</span>
      </div>
      <textarea id="templateInput" rows="3" class="input"></textarea>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn btn-sm btn-block" onclick="saveTemplate()">Сохранить шаблон</button>
        <button class="btn btn-sm btn-ghost" onclick="resetTemplate()">Сброс</button>
      </div>
    </div>

    <div class="card">
      <div class="section-head" style="margin:0 0 8px">
        <div class="section-title">🚫 Чёрный список</div>
        <button id="clearBlAdsBtn" class="btn btn-sm btn-danger" onclick="clearBlAds()">Очистить объявл.</button>
      </div>
      <div class="query-meta" id="blSummary">Загрузка...</div>
      <div id="blSellersList" class="chips" style="margin-bottom:0"></div>
    </div>
  </div>

  <!-- TAB 4: STATUS -->
  <div id="tab-stats" class="tab-panel">
    <div class="stats-grid" id="statsGrid"></div>
    <div class="card">
      <div class="section-title">📸 Фотоальбомы в чате</div>
      <div class="query-meta" style="margin-top:6px">
        Все найденные объявления мгновенно приходят в Telegram-чат <b>полным альбомом со всеми фотографиями</b>, оценкой цены относительно рынка и кнопками быстрого ответа.
      </div>
      <button class="btn btn-block" style="margin-top:12px" onclick="sendAllToChat()">📦 Вывести свежие объявления в чат</button>
    </div>
  </div>
</div>

<!-- ADD SEARCH MODAL -->
<div id="addModal" class="modal-bg" onclick="if(event.target===this)closeModal('addModal')">
  <div class="modal">
    <div class="modal-head">
      <div class="modal-title">➕ Новый поиск Kufar</div>
      <button class="btn btn-sm btn-ghost" onclick="closeModal('addModal')">✕</button>
    </div>
    <div class="row-2" style="margin-bottom:10px">
      <button id="modeLinkBtn" class="btn btn-sm" onclick="setAddMode('link')">🔗 По ссылке</button>
      <button id="modeFormBtn" class="btn btn-sm btn-ghost" onclick="setAddMode('form')">🛠 Конструктор</button>
    </div>

    <div id="addLinkPane">
      <span class="field-label">Ссылка с Kufar.by (все фильтры 1 в 1)</span>
      <input id="addUrlInput" class="input" placeholder="https://www.kufar.by/l/r~minsk/..." />
    </div>

    <div id="addFormPane" style="display:none">
      <span class="field-label">Категория</span>
      <select id="addCatSelect" class="input"></select>
      <span class="field-label">Регион</span>
      <select id="addRgnSelect" class="input"></select>
      <span class="field-label">Ключевые слова (необязательно)</span>
      <input id="addQueryInput" class="input" placeholder="напр. playstation 5, iphone 15..." />
      <div class="row-2">
        <div>
          <span class="field-label">Цена от (BYN)</span>
          <input id="addMinInput" type="number" class="input" placeholder="0" />
        </div>
        <div>
          <span class="field-label">Цена до (BYN)</span>
          <input id="addMaxInput" type="number" class="input" placeholder="Любая" />
        </div>
      </div>
    </div>

    <div id="previewBox" class="card" style="display:none;margin-top:12px;background:var(--bg)"></div>

    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn btn-ghost btn-block" onclick="previewNewQuery()">🔎 Проверить</button>
      <button class="btn btn-block" onclick="createNewQuery()">🚀 Запустить</button>
    </div>
  </div>
</div>

<!-- FEED & ANALYTICS MODAL -->
<div id="feedModal" class="modal-bg" onclick="if(event.target===this)closeModal('feedModal')">
  <div class="modal">
    <div class="modal-head">
      <div class="modal-title" id="feedTitle">Лента объявлений</div>
      <button class="btn btn-sm btn-ghost" onclick="closeModal('feedModal')">✕</button>
    </div>
    <div id="feedAnalytics"></div>
    <div id="feedItems"></div>
  </div>
</div>

<div id="bottomNav" class="nav" style="display:none">
  <button class="nav-btn active" data-tab="searches" onclick="switchTab('searches')">
    <span class="ico">🎯</span><span>Поиски</span>
    <span id="badgeQueries" class="nav-badge" style="display:none">0</span>
  </button>
  <button class="nav-btn" data-tab="favs" onclick="switchTab('favs')">
    <span class="ico">⭐️</span><span>Избранное</span>
    <span id="badgeFavs" class="nav-badge" style="display:none">0</span>
  </button>
  <button class="nav-btn" data-tab="settings" onclick="switchTab('settings')">
    <span class="ico">🛡</span><span>Фильтры</span>
  </button>
  <button class="nav-btn" data-tab="stats" onclick="switchTab('stats')">
    <span class="ico">📊</span><span>Статус</span>
  </button>
</div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
let STATE = null;
let ADD_MODE = 'link';
let ACTIVE_FEED_Q = null;
let FEED_ADS_CACHE = {};

function haptic(type = 'light') {
  try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(type); } catch(e) {}
}

function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.remove('show'), 2500);
}

async function api(path, method = 'GET', body = null) {
  const headers = { 'X-Telegram-Init-Data': (tg && tg.initData) || '' };
  const opts = { method, headers };
  if (body !== null) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  const data = await resp.json();
  if (!resp.ok || !data.ok) {
    throw new Error(data.error || 'Ошибка запроса');
  }
  return data;
}

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtMoney(n) {
  if (n === null || n === undefined) return 'Договорная';
  return Number(n).toLocaleString('ru-RU', { maximumFractionDigits: 0 }) + ' BYN';
}

function switchTab(tab) {
  haptic('light');
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.querySelector(`.nav-btn[data-tab="${tab}"]`).classList.add('active');
}

function renderAll() {
  if (!STATE) return;
  const paused = STATE.monitoring_paused;
  document.getElementById('monitorDot').className = 'dot' + (paused ? ' paused' : '');
  document.getElementById('monitorLabel').textContent = paused ? 'На паузе' : 'Активен';
  document.getElementById('subHeader').textContent = `${paused ? '⏸ Пауза' : '🟢 Проверка каждые ' + STATE.interval_seconds + 'с'} • Поисков: ${STATE.queries.length}`;

  const bq = document.getElementById('badgeQueries');
  bq.textContent = STATE.queries.length;
  bq.style.display = STATE.queries.length ? 'flex' : 'none';
  const bf = document.getElementById('badgeFavs');
  bf.textContent = STATE.favorites.length;
  bf.style.display = STATE.favorites.length ? 'flex' : 'none';

  // Render queries
  const qList = document.getElementById('queriesList');
  if (!STATE.queries.length) {
    qList.innerHTML = `<div class="empty"><div class="e-ico">🎯</div>Пока нет поисков.<br>Нажмите <b>«➕ Добавить»</b>, чтобы начать отслеживание!</div>`;
  } else {
    qList.innerHTML = STATE.queries.map(q => `
      <div class="card">
        <div class="query-top">
          <div>
            <div class="query-name">${esc(q.name)}</div>
            <div class="query-meta">${q.is_active ? '🟢 Активен' : '⏸ Пауза'} • Просмотрено: ${q.seen_count}</div>
          </div>
          <div class="switch ${q.is_active ? 'on' : ''}" onclick="toggleQuery(${q.id}, ${!q.is_active})"></div>
        </div>
        <div class="chips">${(q.chips || []).map(c => `<span class="chip">${esc(c)}</span>`).join('')}</div>
        <div class="query-actions">
          <button class="btn btn-sm btn-ghost" onclick="openFeed(${q.id})">👀 Лента</button>
          <button class="btn btn-sm btn-ghost" onclick="sendQueryToChat(${q.id})">📸 В чат</button>
          <button class="btn btn-sm btn-ghost" onclick="openFeed(${q.id}, true)">📊 Цены</button>
          <button class="btn btn-sm btn-danger" onclick="deleteQuery(${q.id}, '${esc(q.name)}')">🗑</button>
        </div>
      </div>
    `).join('');
  }

  // Render favorites
  const fList = document.getElementById('favsList');
  document.getElementById('clearFavsBtn').style.display = STATE.favorites.length ? 'inline-flex' : 'none';
  if (!STATE.favorites.length) {
    fList.innerHTML = `<div class="empty"><div class="e-ico">⭐️</div>В Избранном пока пусто.<br>Отмечайте понравившиеся объявления прямо в чате или ленте!</div>`;
  } else {
    fList.innerHTML = STATE.favorites.map(f => `
      <div class="ad-card">
        ${f.image_url ? `<img class="ad-thumb" src="${esc(f.image_url)}" />` : `<div class="ad-thumb">📦</div>`}
        <div class="ad-body">
          <div class="ad-title">${esc(f.title || 'Объявление #' + f.ad_id)}</div>
          <div class="ad-price">${fmtMoney(f.price_byn)}${f.price_usd ? ` <span style="font-size:11px;color:var(--hint);font-weight:500">~$${Math.round(f.price_usd)}</span>` : ''}</div>
          <div class="ad-sub">👤 ${esc(f.seller_name || 'Частное лицо')}</div>
          <div class="ad-btns">
            <a class="btn btn-sm" style="text-decoration:none" href="${esc(f.ad_link)}" target="_blank">Kufar ↗</a>
            <button class="btn btn-sm btn-ghost" onclick="copyContact(${f.ad_id}, '${esc(f.seller_name||'')}', '${esc(f.title||'')}', '${fmtMoney(f.price_byn)}')">💬 Написать</button>
            <button class="btn btn-sm btn-danger" onclick="removeFav(${f.ad_id})">✕</button>
          </div>
        </div>
      </div>
    `).join('');
  }

  // Stop words
  const swEl = document.getElementById('stopWordsChips');
  swEl.innerHTML = STATE.stop_words.length
    ? STATE.stop_words.map(w => `<span class="chip">${esc(w)} <span class="x" onclick="removeStopWord('${esc(w)}')">✕</span></span>`).join('')
    : `<span class="query-meta">Стоп-слов пока нет</span>`;

  // Interval
  document.querySelectorAll('#intervalSeg .seg-btn').forEach(b => {
    b.classList.toggle('active', Number(b.dataset.sec) === STATE.interval_seconds);
  });

  // Silent mode
  const isSilentOn = STATE.silent_mode && STATE.silent_mode !== 'off';
  document.getElementById('silentSwitch').className = 'switch' + (isSilentOn ? ' on' : '');
  document.getElementById('silentStatusText').textContent = isSilentOn
    ? `Включен: ${STATE.silent_mode}${STATE.in_silent_now ? ' (сейчас тихо 🌙)' : ''}`
    : 'Выключен (звук круглосуточно)';
  if (isSilentOn && STATE.silent_mode.includes('-')) {
    const [a, b] = STATE.silent_mode.split('-');
    document.getElementById('silentFrom').value = a.trim();
    document.getElementById('silentTo').value = b.trim();
  }

  // Template
  document.getElementById('templateInput').value = STATE.template || '';

  // Blacklist
  document.getElementById('clearBlAdsBtn').style.display = STATE.blacklisted_ads_count ? 'inline-flex' : 'none';
  document.getElementById('blSummary').textContent = `Скрытых объявлений: ${STATE.blacklisted_ads_count} • Продавцов в ЧС: ${STATE.blacklisted_sellers.length}`;
  document.getElementById('blSellersList').innerHTML = STATE.blacklisted_sellers.length
    ? STATE.blacklisted_sellers.map(s => `<span class="chip">👤 ${esc(s)} <span class="x" onclick="unblockSeller('${esc(s)}')">↩️</span></span>`).join('')
    : '';

  // Stats
  const st = STATE.stats || {};
  let statsHtml = `
    <div class="stat-box"><div class="stat-val">${esc(st.uptime || '0:00')}</div><div class="stat-lbl">⏱ Аптайм сервера</div></div>
    <div class="stat-box"><div class="stat-val">${st.total_checks || 0}</div><div class="stat-lbl">🔄 Выполнено проверок</div></div>
    <div class="stat-box"><div class="stat-val">${st.new_ads_found || 0}</div><div class="stat-lbl">📨 Отправлено объявлений</div></div>
    <div class="stat-box"><div class="stat-val">${STATE.queries.length}</div><div class="stat-lbl">🎯 Ваших фильтров</div></div>
  `;
  if (st.is_admin) {
    statsHtml += `
      <div class="stat-box"><div class="stat-val">${st.total_users || 1}</div><div class="stat-lbl">👑 Всего пользователей</div></div>
      <div class="stat-box"><div class="stat-val">${st.total_active_queries || 0}</div><div class="stat-lbl">📡 Активных фильтров</div></div>
    `;
  }
  document.getElementById('statsGrid').innerHTML = statsHtml;

  // Populate selects in Add modal
  const catSel = document.getElementById('addCatSelect');
  if (!catSel.options.length) {
    STATE.categories.forEach(c => catSel.add(new Option(c.name, c.id)));
  }
  const rgnSel = document.getElementById('addRgnSelect');
  if (!rgnSel.options.length) {
    STATE.regions.forEach(r => rgnSel.add(new Option(r.name, r.id)));
  }
}

async function loadState() {
  const data = await api('/api/state');
  STATE = data;
  renderAll();
}

async function toggleGlobalMonitoring() {
  haptic('medium');
  const next = !STATE.monitoring_paused;
  await api('/api/monitoring', 'POST', { paused: next });
  await loadState();
  showToast(next ? '⏸ Мониторинг на паузе' : '🟢 Мониторинг запущен');
}

async function toggleQuery(id, active) {
  haptic('light');
  await api('/api/queries/toggle', 'POST', { id, is_active: active });
  await loadState();
  showToast(active ? '🟢 Поиск включён' : '⏸ Поиск на паузе');
}

async function deleteQuery(id, name) {
  if (!confirm(`Удалить поиск «${name}»?`)) return;
  haptic('medium');
  await api('/api/queries/delete', 'POST', { id });
  await loadState();
  showToast('🗑 Поиск удалён');
}

async function sendQueryToChat(id) {
  haptic('light');
  await api('/api/queries/send_to_chat', 'POST', { id });
  showToast('📸 Объявления со всеми фото отправляются в чат!');
}

async function sendAllToChat() {
  haptic('light');
  await api('/api/queries/send_to_chat', 'POST', { id: 'all' });
  showToast('📸 Вывод всех фотоальбомов запущен в чате!');
}

function openAddModal() {
  haptic('light');
  document.getElementById('previewBox').style.display = 'none';
  document.getElementById('addUrlInput').value = '';
  document.getElementById('addModal').classList.add('open');
}
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function setAddMode(mode) {
  ADD_MODE = mode;
  document.getElementById('addLinkPane').style.display = mode === 'link' ? 'block' : 'none';
  document.getElementById('addFormPane').style.display = mode === 'form' ? 'block' : 'none';
  document.getElementById('modeLinkBtn').className = 'btn btn-sm' + (mode === 'link' ? '' : ' btn-ghost');
  document.getElementById('modeFormBtn').className = 'btn btn-sm' + (mode === 'form' ? '' : ' btn-ghost');
  document.getElementById('previewBox').style.display = 'none';
}

function collectAddPayload() {
  if (ADD_MODE === 'link') {
    return { url: document.getElementById('addUrlInput').value.trim() };
  }
  return {
    cat: document.getElementById('addCatSelect').value,
    rgn: document.getElementById('addRgnSelect').value,
    query: document.getElementById('addQueryInput').value.trim(),
    min_price: document.getElementById('addMinInput').value.trim(),
    max_price: document.getElementById('addMaxInput').value.trim(),
  };
}

async function previewNewQuery() {
  try {
    haptic('light');
    const payload = collectAddPayload();
    const res = await api('/api/queries/preview', 'POST', payload);
    const box = document.getElementById('previewBox');
    box.style.display = 'block';
    box.innerHTML = `
      <div style="font-weight:700">${esc(res.name)}</div>
      <div class="chips" style="margin:6px 0">${res.chips.map(c=>`<span class="chip">${esc(c)}</span>`).join('')}</div>
      <div class="query-meta">Найдено сейчас на Kufar: <b>${res.count}</b> объявл.${res.median ? ' • Медиана: ' + fmtMoney(res.median) : ''}</div>
    `;
  } catch(e) { showToast('⚠️ ' + e.message); }
}

async function createNewQuery() {
  try {
    haptic('medium');
    const payload = collectAddPayload();
    await api('/api/queries/add', 'POST', payload);
    closeModal('addModal');
    await loadState();
    showToast('🚀 Поиск добавлен и запущен!');
  } catch(e) { showToast('⚠️ ' + e.message); }
}

async function openFeed(qid, focusStats = false) {
  haptic('light');
  const q = STATE.queries.find(x => x.id === qid);
  ACTIVE_FEED_Q = q;
  document.getElementById('feedTitle').textContent = q ? q.name : 'Лента';
  document.getElementById('feedAnalytics').innerHTML = '';
  document.getElementById('feedItems').innerHTML = `<div class="empty">🔍 Загружаю объявления с Куфара...</div>`;
  document.getElementById('feedModal').classList.add('open');

  try {
    const res = await api('/api/queries/feed', 'POST', { id: qid });
    if (res.analytics && res.analytics.count) {
      const a = res.analytics;
      document.getElementById('feedAnalytics').innerHTML = `
        <div class="card" style="background:var(--bg);margin-bottom:12px">
          <div class="query-meta" style="margin-bottom:6px">📊 Аналитика цен (${a.count} объявл.):</div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center">
            <div><div style="font-weight:800">${fmtMoney(a.min)}</div><div class="query-meta">Минимум</div></div>
            <div><div style="font-weight:800;color:var(--accent)">${fmtMoney(a.median)}</div><div class="query-meta">Медиана</div></div>
            <div><div style="font-weight:800">${fmtMoney(a.max)}</div><div class="query-meta">Максимум</div></div>
          </div>
        </div>
      `;
    }
    FEED_ADS_CACHE = {};
    if (!res.ads.length) {
      document.getElementById('feedItems').innerHTML = `<div class="empty"><div class="e-ico">📭</div>Подходящих объявлений сейчас нет.</div>`;
      return;
    }
    res.ads.forEach(ad => { FEED_ADS_CACHE[ad.ad_id] = ad; });
    document.getElementById('feedItems').innerHTML = res.ads.map(ad => {
      let badge = '';
      if (ad.bargain_pct !== null && ad.bargain_pct <= -8) badge = `<span class="badge badge-good">${ad.bargain_pct}%</span>`;
      else if (ad.bargain_pct !== null && ad.bargain_pct >= 12) badge = `<span class="badge badge-high">+${ad.bargain_pct}%</span>`;
      const imgCount = (ad.image_urls || []).length;
      return `
        <div class="ad-card" id="feed-ad-${ad.ad_id}">
          ${ad.image_url ? `<img class="ad-thumb" src="${esc(ad.image_url)}" />` : `<div class="ad-thumb">📦</div>`}
          <div class="ad-body">
            <div class="ad-title">${esc(ad.subject)}</div>
            <div class="ad-price">${fmtMoney(ad.price_byn)}${badge}</div>
            <div class="ad-sub">📍 ${esc(ad.region || '')}${ad.area ? ', ' + esc(ad.area) : ''} • 📸 ${imgCount} фото</div>
            <div class="ad-btns">
              <a class="btn btn-sm" style="text-decoration:none" href="${esc(ad.ad_link)}" target="_blank">Kufar ↗</a>
              <button class="btn btn-sm btn-ghost" onclick="pushAdToChat(${ad.ad_id})">📸 В чат</button>
              <button class="btn btn-sm ${ad.is_fav ? 'btn-success' : 'btn-ghost'}" onclick="toggleFavFromFeed(${ad.ad_id}, this)">${ad.is_fav ? '⭐️' : '☆'}</button>
              <button class="btn btn-sm btn-danger" onclick="hideAdFromFeed(${ad.ad_id})">🚫</button>
            </div>
          </div>
        </div>
      `;
    }).join('');
  } catch(e) {
    document.getElementById('feedItems').innerHTML = `<div class="empty">⚠️ ${esc(e.message)}</div>`;
  }
}

async function pushAdToChat(adId) {
  const ad = FEED_ADS_CACHE[adId];
  if (!ad) return;
  haptic('light');
  await api('/api/ads/send_to_chat', 'POST', { ad, query_name: ACTIVE_FEED_Q ? ACTIVE_FEED_Q.name : 'Поиск' });
  showToast('📸 Все фото и описание отправлены в чат!');
}

async function toggleFavFromFeed(adId, btn) {
  const ad = FEED_ADS_CACHE[adId];
  if (!ad) return;
  haptic('light');
  const res = await api('/api/favorites/toggle', 'POST', { ad });
  ad.is_fav = res.is_fav;
  btn.textContent = res.is_fav ? '⭐️' : '☆';
  btn.className = 'btn btn-sm ' + (res.is_fav ? 'btn-success' : 'btn-ghost');
  await loadState();
  showToast(res.is_fav ? '⭐️ Добавлено в Избранное' : 'Удалено из Избранного');
}

async function hideAdFromFeed(adId) {
  haptic('medium');
  await api('/api/blacklist/hide_ad', 'POST', { ad_id: adId });
  const el = document.getElementById('feed-ad-' + adId);
  if (el) el.remove();
  await loadState();
  showToast('🚫 Объявление скрыто в ЧС');
}

async function removeFav(adId) {
  await api('/api/favorites/toggle', 'POST', { ad: { ad_id: adId } });
  await loadState();
  showToast('Удалено из Избранного');
}

async function clearFavorites() {
  if (!confirm('Очистить всё Избранное?')) return;
  await api('/api/favorites/clear', 'POST', {});
  await loadState();
  showToast('🧹 Избранное очищено');
}

function copyContact(adId, seller, item, price) {
  let msg = STATE.template || '';
  msg = msg.replaceAll('{name}', seller || 'продавец').replaceAll('{item}', item || 'товар').replaceAll('{price}', price || '');
  navigator.clipboard && navigator.clipboard.writeText(msg);
  showToast('💬 Текст скопирован в буфер!');
}

async function addStopWords() {
  const inp = document.getElementById('newStopWords');
  const words = inp.value.trim();
  if (!words) return;
  await api('/api/stopwords/add', 'POST', { words });
  inp.value = '';
  await loadState();
  showToast('⛔ Стоп-слова добавлены');
}

async function removeStopWord(word) {
  await api('/api/stopwords/remove', 'POST', { word });
  await loadState();
}

async function setIntervalSec(sec) {
  haptic('light');
  await api('/api/settings', 'POST', { interval_seconds: sec });
  await loadState();
  showToast(`⏱ Интервал: ${sec} сек`);
}

async function toggleSilentPreset() {
  const isOn = STATE.silent_mode && STATE.silent_mode !== 'off';
  const val = isOn ? 'off' : `${document.getElementById('silentFrom').value || '23:00'}-${document.getElementById('silentTo').value || '08:00'}`;
  await saveSilentVal(val);
}

async function saveSilentCustom() {
  const a = document.getElementById('silentFrom').value || '23:00';
  const b = document.getElementById('silentTo').value || '08:00';
  await saveSilentVal(`${a}-${b}`);
}

async function saveSilentVal(val) {
  haptic('light');
  await api('/api/settings', 'POST', { silent_mode: val });
  await loadState();
  showToast(val === 'off' ? '☀️ Тихий режим выключен' : `🌙 Тихий режим: ${val}`);
}

function insertTplVar(v) {
  const ta = document.getElementById('templateInput');
  ta.value = (ta.value ? ta.value + ' ' : '') + v;
  ta.focus();
}

async function saveTemplate() {
  const val = document.getElementById('templateInput').value.trim();
  if (val.length < 3) return showToast('⚠️ Минимум 3 символа');
  await api('/api/settings', 'POST', { template: val });
  await loadState();
  showToast('💬 Шаблон сохранён');
}

async function resetTemplate() {
  await api('/api/settings', 'POST', { reset_template: true });
  await loadState();
  showToast('🔄 Шаблон сброшен');
}

async function unblockSeller(name) {
  await api('/api/blacklist/unblock_seller', 'POST', { seller_name: name });
  await loadState();
  showToast(`↩️ Продавец «${name}» разблокирован`);
}

async function clearBlAds() {
  if (!confirm('Очистить список скрытых объявлений?')) return;
  await api('/api/blacklist/clear_ads', 'POST', {});
  await loadState();
  showToast('🧹 ЧС объявлений очищен');
}

window.addEventListener('DOMContentLoaded', async () => {
  if (tg && tg.initData) {
    tg.ready();
    tg.expand();
    document.getElementById('app').style.display = 'block';
    document.getElementById('bottomNav').style.display = 'grid';
    try {
      await loadState();
    } catch (e) {
      showToast('⚠️ ' + e.message);
    }
  } else {
    document.getElementById('outsideNotice').style.display = 'block';
  }
});
</script>
</body>
</html>
"""


def create_http_handler(app_ctx: Dict[str, Any]):
    """Builds HTTP request handler class bound to shared bot runtime context."""

    class KufarWebAppHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, code: int, payload: Dict[str, Any]):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, code: int, html_text: str):
            body = html_text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 256 * 1024:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def _auth_chat_id(self) -> Optional[int]:
            init_data = self.headers.get("X-Telegram-Init-Data", "")
            bot_token = app_ctx["config"].get("bot_token", "")
            user = verify_telegram_init_data(init_data, bot_token)
            if not user:
                self._send_json(401, {"ok": False, "error": "Требуется авторизация через Telegram Mini App"})
                return None
            chat_id = int(user["id"])
            register_or_update_user(
                chat_id,
                username=user.get("username"),
                first_name=user.get("first_name", ""),
                last_name=user.get("last_name", ""),
            )
            return chat_id

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                total_checks, _ = app_ctx["get_counters"]()
                uptime = str(datetime.now() - app_ctx["start_time"]).split(".")[0]
                self._send_json(200, {
                    "status": "online",
                    "bot": "Kufar Hunter Bot",
                    "uptime": uptime,
                    "total_checks": total_checks,
                    "active_queries": len(get_active_queries()),
                    "timestamp": datetime.now().isoformat(),
                })
                return

            if path in ("/", "/webapp"):
                self._send_html(200, WEBAPP_HTML)
                return

            if path == "/api/state":
                chat_id = self._auth_chat_id()
                if chat_id is None:
                    return
                self._send_json(200, _build_user_state_payload(chat_id, app_ctx))
                return

            self._send_json(404, {"ok": False, "error": "Not found"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            chat_id = self._auth_chat_id()
            if chat_id is None:
                return

            body = self._read_json_body()
            config = app_ctx["config"]

            try:
                if path == "/api/monitoring":
                    paused = bool(body.get("paused", False))
                    set_user_monitoring_paused(chat_id, paused)
                    app_ctx["sync_backup"]()
                    if not paused:
                        app_ctx["trigger_force_check"]()
                    self._send_json(200, {"ok": True, "monitoring_paused": paused})
                    return

                if path == "/api/queries/preview":
                    params, name = _params_from_body(body, app_ctx)
                    if not params.get("query") and (not params.get("cat") or params.get("cat") == "all"):
                        self._send_json(400, {"ok": False, "error": "Укажите категорию или ключевые слова"})
                        return
                    ads = fetch_ads(params)
                    prices = [a["price_byn"] for a in ads if a.get("price_byn") and a["price_byn"] > 0]
                    med = round(float(statistics.median(prices)), 2) if len(prices) >= 3 else None
                    self._send_json(200, {
                        "ok": True,
                        "name": name,
                        "params": params,
                        "chips": _build_query_chips(params, app_ctx),
                        "count": len(ads),
                        "median": med,
                    })
                    return

                if path == "/api/queries/add":
                    params, name = _params_from_body(body, app_ctx)
                    if not params.get("query") and (not params.get("cat") or params.get("cat") == "all"):
                        self._send_json(400, {"ok": False, "error": "Укажите категорию или ключевые слова"})
                        return
                    ads = fetch_ads(params)
                    qid = add_query(chat_id, name, params)
                    if ads:
                        mark_ads_seen(qid, [a["ad_id"] for a in ads])
                    app_ctx["sync_backup"]()
                    app_ctx["trigger_force_check"]()
                    self._send_json(200, {"ok": True, "id": qid, "name": name})
                    return

                if path == "/api/queries/toggle":
                    qid = int(body.get("id", 0))
                    is_active = bool(body.get("is_active", True))
                    toggle_query_active(qid, is_active, chat_id=chat_id)
                    app_ctx["sync_backup"]()
                    if is_active:
                        app_ctx["trigger_force_check"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/queries/delete":
                    qid = int(body.get("id", 0))
                    delete_query(qid, chat_id=chat_id)
                    app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/queries/send_to_chat":
                    raw_id = body.get("id")
                    if raw_id == "all":
                        app_ctx["send_all_ads_to_chat"](chat_id)
                    else:
                        app_ctx["send_query_ads_to_chat"](chat_id, int(raw_id))
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/queries/feed":
                    qid = int(body.get("id", 0))
                    q = next((x for x in get_user_queries(chat_id) if x["id"] == qid), None)
                    if not q:
                        self._send_json(404, {"ok": False, "error": "Поиск не найден"})
                        return
                    api_params = dict(q["params"])
                    api_params["size"] = "50"
                    ads = fetch_ads(api_params)
                    min_b, max_b = _parse_prc_bounds(q["params"].get("prc"))
                    bl = get_user_blacklist(chat_id)
                    sw_list = get_stop_words(chat_id)
                    prices = [a["price_byn"] for a in ads if a.get("price_byn") and a["price_byn"] > 0]
                    med = round(float(statistics.median(prices)), 2) if len(prices) >= 3 else None

                    filtered = []
                    for a in ads:
                        if a["ad_id"] in bl:
                            continue
                        if is_seller_blacklisted(chat_id, a.get("seller", ""), a.get("account_id")):
                            continue
                        p = a.get("price_byn")
                        if p is not None:
                            if min_b is not None and p < min_b:
                                continue
                            if max_b is not None and p > max_b:
                                continue
                        if sw_list and any(sw in (a.get("subject") or "").lower() for sw in sw_list):
                            continue
                        b_pct = round(((p - med) / med) * 100) if (p is not None and med) else None
                        item = dict(a)
                        item["is_fav"] = is_favorite(chat_id, a["ad_id"])
                        item["bargain_pct"] = b_pct
                        filtered.append(item)
                        if len(filtered) >= 30:
                            break

                    analytics = {
                        "count": len(prices),
                        "min": min(prices) if prices else None,
                        "median": med,
                        "max": max(prices) if prices else None,
                    }
                    self._send_json(200, {"ok": True, "ads": filtered, "analytics": analytics})
                    return

                if path == "/api/ads/send_to_chat":
                    ad = body.get("ad") or {}
                    q_name = body.get("query_name") or "Поиск"
                    if not ad.get("ad_id"):
                        self._send_json(400, {"ok": False, "error": "Неверные данные объявления"})
                        return
                    ok = app_ctx["send_single_ad_to_chat"](chat_id, ad, q_name, None)
                    self._send_json(200, {"ok": bool(ok)})
                    return

                if path == "/api/favorites/toggle":
                    ad = body.get("ad") or {}
                    ad_id = int(ad.get("ad_id", 0))
                    if not ad_id:
                        self._send_json(400, {"ok": False, "error": "ad_id required"})
                        return
                    if is_favorite(chat_id, ad_id):
                        remove_favorite(chat_id, ad_id)
                        now_fav = False
                    else:
                        add_favorite(
                            chat_id,
                            ad_id,
                            title=ad.get("subject", ""),
                            price_byn=ad.get("price_byn"),
                            price_usd=ad.get("price_usd"),
                            ad_link=ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}"),
                            seller_name=ad.get("seller", ""),
                            image_url=ad.get("image_url", ""),
                        )
                        now_fav = True
                    app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True, "is_fav": now_fav})
                    return

                if path == "/api/favorites/clear":
                    clear_user_favorites(chat_id)
                    app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/blacklist/hide_ad":
                    ad_id = int(body.get("ad_id", 0))
                    if ad_id:
                        add_to_blacklist(chat_id, ad_id)
                        app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/blacklist/unblock_seller":
                    sname = (body.get("seller_name") or "").strip()
                    if sname:
                        remove_blacklisted_seller(chat_id, sname)
                        app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/blacklist/clear_ads":
                    clear_user_blacklist(chat_id)
                    app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/stopwords/add":
                    import re
                    raw_words = body.get("words") or ""
                    for token in re.split(r"[,;\n]+", raw_words):
                        clean = token.strip().lower()
                        if len(clean) >= 2:
                            add_stop_word(chat_id, clean)
                    app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/stopwords/remove":
                    word = (body.get("word") or "").strip()
                    if word:
                        remove_stop_word(chat_id, word)
                        app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/settings":
                    if "interval_seconds" in body:
                        sec = max(10, min(600, int(body["interval_seconds"])))
                        config["check_interval_seconds"] = sec
                        set_setting("check_interval_seconds", str(sec))
                        app_ctx["trigger_force_check"]()
                    if "silent_mode" in body:
                        set_silent_mode(chat_id, str(body["silent_mode"]).strip() or "off")
                    if body.get("reset_template"):
                        set_user_template(chat_id, DEFAULT_CONTACT_TEMPLATE)
                    elif "template" in body:
                        tpl = str(body["template"]).strip()
                        if len(tpl) >= 3:
                            set_user_template(chat_id, tpl)
                    app_ctx["sync_backup"]()
                    self._send_json(200, {"ok": True})
                    return

                self._send_json(404, {"ok": False, "error": "Unknown API endpoint"})
            except Exception as e:
                logger.error(f"WebApp API error on {path}: {e}")
                self._send_json(500, {"ok": False, "error": str(e)})

    return KufarWebAppHandler
