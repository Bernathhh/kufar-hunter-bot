"""
Telegram Mini App (WebApp) UI and JSON REST API handler for Kufar Hunter Bot.
Zero external dependencies — built purely with Python standard library + native Telegram WebApp JS SDK + inline SVG icons.
"""
import hashlib
import hmac
import http.server
import json
import logging
import re
import statistics
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from database import (
    DEFAULT_CONTACT_TEMPLATE,
    add_favorite,
    add_query,
    add_stop_word,
    add_to_blacklist,
    clear_user_blacklist,
    clear_user_favorites,
    delete_query,
    get_active_queries,
    get_blacklisted_sellers,
    get_blacklist_count,
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
    update_query,
)
from kufar_api import fetch_ads, parse_kufar_url_or_query

logger = logging.getLogger("kufar_webapp")


def _plain_label(label: str) -> str:
    """Strips decorative emoji prefixes from category/region labels for clean UI rendering."""
    cleaned = re.sub(r'^[^\wА-Яа-яЁёA-Za-z0-9]+', '', str(label or "")).strip()
    return cleaned or str(label or "")


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


def _build_query_chips(p: Dict[str, Any], app_ctx: Dict[str, Any]) -> List[Dict[str, str]]:
    """Builds structured filter chips with semantic SVG icon keys (no emojis)."""
    chips: List[Dict[str, str]] = []
    cat_map = {k: _plain_label(v) for k, v in app_ctx.get("categories", [])}
    rgn_map = {k: _plain_label(v) for k, v in app_ctx.get("regions", [])}

    if p.get("query"):
        chips.append({"icon": "search", "text": str(p["query"])})
    cat_id = str(p.get("cat", ""))
    if cat_id and cat_id != "all":
        chips.append({"icon": "grid", "text": cat_map.get(cat_id, f"Категория {cat_id}")})
    cte_map = {"1": "Приставки", "2": "Игры", "3": "Аксессуары"}
    if str(p.get("cte", "")) in cte_map:
        chips.append({"icon": "layers", "text": cte_map[str(p["cte"])]})
    pb_map = {"5": "Apple", "24": "Samsung", "35": "Xiaomi", "11": "Huawei", "12": "Honor"}
    cbc_map = {"31": "Sony", "13": "Xbox", "17": "Nintendo", "30": "Steam Deck"}
    brand = p.get("brand") or cbc_map.get(str(p.get("cbc", ""))) or pb_map.get(str(p.get("pb", "")))
    if brand:
        chips.append({"icon": "tag", "text": str(brand)})
    rgn_id = str(p.get("rgn", ""))
    if rgn_id and rgn_id != "all":
        chips.append({"icon": "mapPin", "text": rgn_map.get(rgn_id, f"Регион {rgn_id}")})
    min_p, max_p = _parse_prc_bounds(p.get("prc"))
    if min_p and max_p:
        chips.append({"icon": "wallet", "text": f"{int(min_p):,} – {int(max_p):,} BYN".replace(",", " ")})
    elif min_p:
        chips.append({"icon": "wallet", "text": f"от {int(min_p):,} BYN".replace(",", " ")})
    elif max_p:
        chips.append({"icon": "wallet", "text": f"до {int(max_p):,} BYN".replace(",", " ")})
    cnd_map = {"1": "Б/У", "2": "Новое"}
    if str(p.get("cnd", "")) in cnd_map:
        chips.append({"icon": "sparkles", "text": cnd_map[str(p["cnd"])]})
    cmp_map = {"0": "Частные", "1": "Компании"}
    if str(p.get("cmp", "")) in cmp_map:
        chips.append({"icon": "users", "text": cmp_map[str(p["cmp"])]})
    return chips or [{"icon": "grid", "text": "Все объявления"}]


def _build_user_state_payload(chat_id: int, app_ctx: Dict[str, Any]) -> Dict[str, Any]:
    config = app_ctx["config"]
    total_checks, new_ads_found = app_ctx["get_counters"]()
    uptime = str(datetime.now() - app_ctx["start_time"]).split(".")[0]

    queries_raw = get_user_queries(chat_id)
    queries = []
    for q in queries_raw:
        queries.append({
            "id": q["id"],
            "name": _plain_label(q["name"]),
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

    stats: Dict[str, Any] = {"is_admin": is_admin}
    if is_admin:
        stats.update({
            "uptime": uptime,
            "total_checks": total_checks,
            "new_ads_found": new_ads_found,
            "total_users": get_user_count(),
            "total_active_queries": len(get_active_queries()),
        })

    return {
        "ok": True,
        "chat_id": chat_id,
        "monitoring_paused": paused,
        "interval_seconds": int(config.get("check_interval_seconds", 60)) if is_admin else None,
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
        "categories": [{"id": c[0], "name": _plain_label(c[1])} for c in app_ctx.get("categories", [])],
        "regions": [{"id": r[0], "name": _plain_label(r[1])} for r in app_ctx.get("regions", [])],
    }


def _params_from_body(body: Dict[str, Any], app_ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    cat_map = {k: _plain_label(v) for k, v in app_ctx.get("categories", [])}
    url_raw = (body.get("url") or "").strip()
    if url_raw:
        params = parse_kufar_url_or_query(url_raw)
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

    name = kw or cat_map.get(cat_id, "Куфар поиск")
    return params, name


def _merge_edited_params(
    existing_params: Dict[str, Any],
    body: Dict[str, Any],
    app_ctx: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Merges edited filter fields into existing query params while preserving specialized Kufar keys."""
    cat_map = {k: _plain_label(v) for k, v in app_ctx.get("categories", [])}
    url_raw = (body.get("url") or "").strip()
    if url_raw:
        params = parse_kufar_url_or_query(url_raw)
        cat_name = cat_map.get(str(params.get("cat", "")), "Куфар поиск")
        brand = params.get("brand") or ("Sony" if str(params.get("cbc")) == "31" else "")
        auto_name = params.get("query") or (f"{cat_name} ({brand})" if brand else cat_name)
        name_val = (body.get("name") or "").strip() or auto_name
        return params, name_val

    params: Dict[str, Any] = dict(existing_params or {})
    params.setdefault("sort", "lst.d")
    params.setdefault("size", "30")

    if "query" in body:
        kw = str(body.get("query") or "").strip()
        if kw:
            params["query"] = kw
        else:
            params.pop("query", None)

    if "cat" in body:
        cat_id = str(body.get("cat") or "").strip()
        if not cat_id or cat_id == "all":
            params.pop("cat", None)
        else:
            params["cat"] = cat_id

    if "rgn" in body:
        old_rgn = str(params.get("rgn", ""))
        rgn_id = str(body.get("rgn") or "").strip()
        if not rgn_id or rgn_id == "all":
            params.pop("rgn", None)
            params.pop("ar", None)
        else:
            params["rgn"] = rgn_id
            if rgn_id != old_rgn:
                if rgn_id == "7":
                    params["ar"] = "v.or:22,23,24,25,26,27,28,29,30"
                else:
                    params.pop("ar", None)

    if "min_price" in body or "max_price" in body:
        min_raw = str(body.get("min_price") or "").strip()
        max_raw = str(body.get("max_price") or "").strip()
        min_p = int(float(min_raw)) if min_raw and min_raw.replace(".", "", 1).isdigit() else None
        max_p = int(float(max_raw)) if max_raw and max_raw.replace(".", "", 1).isdigit() else None
        if min_p is not None and max_p is not None and min_p > max_p:
            min_p, max_p = max_p, min_p
        if min_p is not None and max_p is not None:
            params["prc"] = f"r:{min_p * 100},{max_p * 100}"
        elif min_p is not None:
            params["prc"] = f"r:{min_p * 100},999999999"
        elif max_p is not None:
            params["prc"] = f"r:0,{max_p * 100}"
        else:
            params.pop("prc", None)

    if "cnd" in body:
        cnd = str(body.get("cnd") or "").strip()
        if cnd in ("1", "2"):
            params["cnd"] = cnd
        else:
            params.pop("cnd", None)

    if "cmp" in body:
        cmp_val = str(body.get("cmp") or "").strip()
        if cmp_val in ("0", "1"):
            params["cmp"] = cmp_val
        else:
            params.pop("cmp", None)

    if "sort" in body:
        sort_val = str(body.get("sort") or "").strip()
        if sort_val in ("lst.d", "prc.a", "prc.d"):
            params["sort"] = sort_val

    name_val = (body.get("name") or "").strip() or None
    return params, name_val


WEBAPP_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover" />
<title>Kufar Hunter</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #0b0d12);
    --card: var(--tg-theme-secondary-bg-color, #141821);
    --text: var(--tg-theme-text-color, #f1f5f9);
    --hint: var(--tg-theme-hint-color, #8492a6);
    --accent: var(--tg-theme-button-color, #3b82f6);
    --btn-text: var(--tg-theme-button-text-color, #ffffff);
    --surface: color-mix(in srgb, var(--text) 5%, transparent);
    --surface-hover: color-mix(in srgb, var(--text) 9%, transparent);
    --border: color-mix(in srgb, var(--text) 10%, transparent);
    --accent-soft: color-mix(in srgb, var(--accent) 16%, transparent);
    --green: #22c55e;
    --green-soft: rgba(34, 197, 94, 0.14);
    --red: #ef4444;
    --red-soft: rgba(239, 68, 68, 0.13);
    --amber: #f59e0b;
    --amber-soft: rgba(245, 158, 11, 0.14);
    --radius: 16px;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Segoe UI", Roboto, sans-serif;
    font-size: 14px; line-height: 1.45;
    font-variant-numeric: tabular-nums;
    overscroll-behavior-y: contain;
    -webkit-font-smoothing: antialiased;
  }
  svg.ic {
    width: 16px; height: 16px;
    stroke: currentColor; stroke-width: 1.85;
    stroke-linecap: round; stroke-linejoin: round;
    fill: none; flex-shrink: 0; display: inline-block;
  }
  svg.ic-sm { width: 13px; height: 13px; stroke-width: 2; }
  svg.ic-lg { width: 20px; height: 20px; stroke-width: 1.8; }
  svg.ic-xl { width: 26px; height: 26px; stroke-width: 1.65; }
  svg.ic-fill { fill: currentColor; }

  .app { max-width: 640px; margin: 0 auto; padding: 12px 14px 96px; }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 4px 2px 16px; gap: 10px;
  }
  .brand { display: flex; align-items: center; gap: 11px; }
  .brand-mark {
    width: 40px; height: 40px; border-radius: 13px;
    background: linear-gradient(135deg, var(--accent), #6366f1);
    color: #fff;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 6px 18px color-mix(in srgb, var(--accent) 32%, transparent);
  }
  .brand-title { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; }
  .brand-sub { font-size: 11.5px; color: var(--hint); margin-top: 1px; }

  .status-pill {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 7px 12px; border-radius: 999px;
    font-size: 12px; font-weight: 600; border: 1px solid var(--border);
    background: var(--card); color: var(--text); cursor: pointer;
    transition: transform .12s ease, border-color .15s;
  }
  .status-pill:active { transform: scale(0.96); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); }
  .dot.paused { background: var(--amber); box-shadow: 0 0 8px var(--amber); }

  .tab-panel { display: none; animation: fadeIn .16s ease; }
  .tab-panel.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  .section-head {
    display: flex; align-items: center; justify-content: space-between;
    margin: 4px 2px 12px; gap: 8px;
  }
  .section-title { font-size: 15px; font-weight: 700; letter-spacing: -0.2px; display: flex; align-items: center; gap: 7px; }
  .title-ic {
    width: 26px; height: 26px; border-radius: 8px;
    background: var(--accent-soft); color: var(--accent);
    display: inline-flex; align-items: center; justify-content: center;
  }

  .btn {
    border: none; border-radius: 12px; padding: 10px 14px;
    font-size: 13px; font-weight: 600; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    background: var(--accent); color: var(--btn-text);
    transition: opacity .15s, transform .1s, background .15s;
    text-decoration: none;
  }
  .btn:active { transform: scale(0.97); opacity: 0.9; }
  .btn-sm { padding: 7px 11px; font-size: 12px; border-radius: 10px; }
  .btn-icon { padding: 7px 9px; }
  .btn-ghost { background: var(--surface); color: var(--text); border: 1px solid var(--border); }
  .btn-danger { background: var(--red-soft); color: var(--red); border: 1px solid color-mix(in srgb, var(--red) 25%, transparent); }
  .btn-success { background: var(--green-soft); color: var(--green); border: 1px solid color-mix(in srgb, var(--green) 28%, transparent); }
  .btn-block { width: 100%; }

  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 14px; margin-bottom: 12px;
  }
  .query-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .query-name { font-size: 15px; font-weight: 700; letter-spacing: -0.2px; word-break: break-word; }
  .query-meta {
    font-size: 11.5px; color: var(--hint); margin-top: 3px;
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  }
  .state-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }

  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 11px 0 13px; }
  .chip {
    background: var(--surface); border: 1px solid var(--border);
    padding: 4px 9px; border-radius: 8px; font-size: 11.5px; font-weight: 500;
    color: var(--text); display: inline-flex; align-items: center; gap: 5px;
  }
  .chip svg.ic { color: var(--hint); }
  .chip .x {
    opacity: 0.65; cursor: pointer; display: inline-flex; align-items: center;
    padding: 2px; border-radius: 4px; margin-left: 2px;
  }
  .chip .x:hover { opacity: 1; color: var(--red); background: var(--red-soft); }

  .query-actions { display: grid; grid-template-columns: 1fr 1fr 1fr auto auto; gap: 6px; }
  .pill-mini-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .pill-mini {
    border: 1px solid var(--border); background: var(--surface); color: var(--text);
    border-radius: 8px; padding: 5px 9px; font-size: 11.5px; font-weight: 600;
    cursor: pointer; transition: background .15s, transform .1s;
  }
  .pill-mini:active { transform: scale(0.96); }
  .feed-toolbar { display: grid; grid-template-columns: 1fr auto auto; gap: 6px; margin-bottom: 10px; }
  .feed-toolbar .input, .feed-toolbar select { padding: 8px 10px; font-size: 12.5px; }
  .thumb-wrap.clickable { cursor: zoom-in; }
  .lightbox-bg {
    position: fixed; inset: 0; z-index: 200;
    background: rgba(5, 7, 11, 0.94); backdrop-filter: blur(8px);
    display: none; flex-direction: column; align-items: center; justify-content: center;
    padding: 16px;
  }
  .lightbox-bg.open { display: flex; }
  .lightbox-top {
    position: absolute; top: 14px; left: 16px; right: 16px;
    display: flex; align-items: center; justify-content: space-between; color: #fff;
  }
  .lightbox-counter { font-size: 13px; font-weight: 700; opacity: 0.9; }
  .lightbox-img {
    max-width: 100%; max-height: 76vh; border-radius: 14px; object-fit: contain;
    box-shadow: 0 12px 40px rgba(0,0,0,0.55);
  }
  .lightbox-nav {
    display: flex; align-items: center; justify-content: center; gap: 14px; margin-top: 14px;
  }
  .spin { animation: spin 0.8s linear infinite; }
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

  .switch {
    position: relative; width: 44px; height: 26px; flex-shrink: 0;
    background: var(--surface-hover); border: 1px solid var(--border);
    border-radius: 999px; cursor: pointer; transition: background .2s, border-color .2s;
  }
  .switch.on { background: var(--green); border-color: var(--green); }
  .switch::after {
    content: ""; position: absolute; top: 3px; left: 3px; width: 18px; height: 18px;
    border-radius: 50%; background: #fff; transition: transform .2s cubic-bezier(.3,1.3,.5,1);
    box-shadow: 0 2px 5px rgba(0,0,0,0.28);
  }
  .switch.on::after { transform: translateX(18px); }

  .seg { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 10px; }
  .seg-btn {
    padding: 9px 4px; border-radius: 11px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-size: 12px; font-weight: 600; cursor: pointer;
    transition: all .15s;
  }
  .seg-btn.active { background: var(--accent); color: var(--btn-text); border-color: transparent; }

  .input, select, textarea {
    width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 10px 12px; font-size: 13.5px; font-family: inherit; outline: none;
    transition: border-color .15s;
  }
  .input:focus, select:focus, textarea:focus { border-color: var(--accent); }
  .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .field-label { display: block; font-size: 12px; font-weight: 600; color: var(--hint); margin: 10px 0 5px; }

  .ad-card {
    display: flex; gap: 12px; padding: 12px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); margin-bottom: 10px;
  }
  .thumb-wrap {
    position: relative; width: 84px; height: 84px; border-radius: 12px;
    overflow: hidden; flex-shrink: 0; background: var(--surface);
    display: flex; align-items: center; justify-content: center; color: var(--hint);
  }
  .ad-thumb { width: 100%; height: 100%; object-fit: cover; display: block; }
  .photo-pill {
    position: absolute; bottom: 5px; right: 5px;
    background: rgba(11,13,18,0.78); backdrop-filter: blur(6px); color: #fff;
    padding: 2px 6px; border-radius: 6px; font-size: 10px; font-weight: 600;
    display: inline-flex; align-items: center; gap: 3px;
  }
  .ad-body { flex: 1; min-width: 0; }
  .ad-title {
    font-size: 13.5px; font-weight: 600; line-height: 1.3; margin-bottom: 4px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .ad-price-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .ad-price { font-size: 15px; font-weight: 800; color: var(--text); letter-spacing: -0.2px; }
  .ad-usd { font-size: 11.5px; color: var(--hint); font-weight: 500; }
  .ad-sub { font-size: 11.5px; color: var(--hint); margin-top: 3px; display: flex; align-items: center; gap: 4px; }
  .ad-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }

  .badge {
    display: inline-flex; align-items: center; gap: 3px;
    font-size: 10.5px; font-weight: 700; padding: 2px 7px; border-radius: 6px;
  }
  .badge-good { background: var(--green-soft); color: var(--green); }
  .badge-high { background: var(--amber-soft); color: var(--amber); }

  .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
  .stat-box {
    background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 13px;
    display: flex; flex-direction: column; justify-content: space-between;
  }
  .stat-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; color: var(--hint); }
  .stat-val { font-size: 21px; font-weight: 800; letter-spacing: -0.4px; }
  .stat-lbl { font-size: 11.5px; color: var(--hint); margin-top: 2px; }

  .nav {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 50;
    background: color-mix(in srgb, var(--card) 92%, transparent);
    border-top: 1px solid var(--border);
    display: grid; grid-template-columns: repeat(4, 1fr);
    padding: 7px 6px calc(7px + env(safe-area-inset-bottom));
    backdrop-filter: saturate(180%) blur(18px);
  }
  .nav-btn {
    background: none; border: none; color: var(--hint);
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    padding: 4px 2px; font-size: 10.5px; font-weight: 600; cursor: pointer;
    position: relative; transition: color .15s;
  }
  .nav-btn.active { color: var(--accent); }
  .nav-badge {
    position: absolute; top: 1px; right: 22%; min-width: 16px; height: 16px;
    border-radius: 999px; background: var(--accent); color: var(--btn-text);
    font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center; padding: 0 4px;
  }

  .modal-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.62); backdrop-filter: blur(4px); z-index: 100;
    display: none; align-items: flex-end; justify-content: center;
  }
  .modal-bg.open { display: flex; }
  .modal {
    width: 100%; max-width: 640px; max-height: 88vh; overflow-y: auto;
    background: var(--card); border-top-left-radius: 22px; border-top-right-radius: 22px;
    border-top: 1px solid var(--border);
    padding: 16px 16px calc(22px + env(safe-area-inset-bottom));
    animation: slideUp .18s ease;
  }
  @keyframes slideUp { from { transform: translateY(30px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
  .modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .modal-title { font-size: 16px; font-weight: 700; display: flex; align-items: center; gap: 8px; }

  .toast {
    position: fixed; top: 14px; left: 50%; transform: translateX(-50%) translateY(-80px);
    background: #181c24; color: #f8fafc; border: 1px solid rgba(255,255,255,0.12);
    padding: 9px 16px; border-radius: 999px;
    font-size: 12.5px; font-weight: 600; z-index: 200;
    box-shadow: 0 10px 28px rgba(0,0,0,0.38); transition: transform .22s ease;
    max-width: 90%; text-align: center;
  }
  .toast.show { transform: translateX(-50%) translateY(0); }

  .empty { text-align: center; padding: 38px 18px; color: var(--hint); }
  .empty-ic {
    width: 52px; height: 52px; border-radius: 16px; margin: 0 auto 12px;
    background: var(--surface); border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center; color: var(--hint);
  }
  .outside-box { max-width: 400px; margin: 72px auto; padding: 24px; text-align: center; }
</style>
</head>
<body>
<div id="toast" class="toast"></div>

<div id="outsideNotice" class="outside-box" style="display:none">
  <div class="brand-mark" style="width:56px;height:56px;margin:0 auto 16px;border-radius:18px" id="outsideLogo"></div>
  <h2 style="margin:0 0 8px;letter-spacing:-0.3px">Kufar Hunter</h2>
  <p style="color:var(--hint);margin:0">Откройте приложение через кнопку меню внутри Telegram-бота, чтобы управлять поисками, лентой и фильтрами.</p>
</div>

<div id="app" class="app" style="display:none">
  <div class="header">
    <div class="brand">
      <div class="brand-mark" id="brandLogo"></div>
      <div>
        <div class="brand-title">Kufar Hunter</div>
        <div class="brand-sub" id="subHeader">Загрузка...</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:6px">
      <button id="btnRefreshHeader" class="btn btn-sm btn-ghost btn-icon" title="Обновить" onclick="refreshState()"></button>
      <button id="monitorPill" class="status-pill" onclick="toggleGlobalMonitoring()">
        <span id="monitorDot" class="dot"></span>
        <span id="monitorLabel">Активен</span>
      </button>
    </div>
  </div>

  <!-- TAB 1: SEARCHES -->
  <div id="tab-searches" class="tab-panel active">
    <div class="section-head">
      <div class="section-title">Мои поиски</div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-sm btn-ghost" onclick="sendAllToChat()" id="btnAllToChat"></button>
        <button class="btn btn-sm" onclick="openAddModal()" id="btnOpenAdd"></button>
      </div>
    </div>
    <div id="queriesList"></div>
  </div>

  <!-- TAB 2: FAVORITES -->
  <div id="tab-favs" class="tab-panel">
    <div class="section-head">
      <div class="section-title">Избранное</div>
      <button id="clearFavsBtn" class="btn btn-sm btn-danger" onclick="clearFavorites()" style="display:none"></button>
    </div>
    <div id="favsList"></div>
  </div>

  <!-- TAB 3: SETTINGS & FILTERS -->
  <div id="tab-settings" class="tab-panel">
    <div class="card">
      <div class="section-title"><span class="title-ic" id="icSwTitle"></span>Стоп-слова</div>
      <div class="query-meta">Объявления с этими словами в заголовке или описании скрываются автоматически.</div>
      <div id="stopWordsChips" class="chips"></div>
      <div style="display:flex;gap:8px">
        <input id="newStopWords" class="input" placeholder="аренда, чехол, ps4..." />
        <button class="btn btn-sm" onclick="addStopWords()">Добавить</button>
      </div>
    </div>

    <div class="card" id="intervalCard" style="display:none">
      <div class="section-title"><span class="title-ic" id="icIntTitle"></span>Интервал проверки</div>
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
          <div class="section-title"><span class="title-ic" id="icSilentTitle"></span>Ночной режим</div>
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
      <div class="section-title"><span class="title-ic" id="icTplTitle"></span>Шаблон ответа продавцу</div>
      <div class="chips" style="margin-bottom:8px">
        <span class="chip" style="cursor:pointer" onclick="insertTplVar('{name}')">+ {name}</span>
        <span class="chip" style="cursor:pointer" onclick="insertTplVar('{item}')">+ {item}</span>
        <span class="chip" style="cursor:pointer" onclick="insertTplVar('{price}')">+ {price}</span>
      </div>
      <textarea id="templateInput" rows="3" class="input"></textarea>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn btn-sm btn-block" onclick="saveTemplate()">Сохранить</button>
        <button class="btn btn-sm btn-ghost" onclick="resetTemplate()">Сброс</button>
      </div>
    </div>

    <div class="card">
      <div class="section-head" style="margin:0 0 8px">
        <div class="section-title"><span class="title-ic" id="icBlTitle"></span>Чёрный список</div>
        <button id="clearBlAdsBtn" class="btn btn-sm btn-danger" onclick="clearBlAds()">Очистить</button>
      </div>
      <div class="query-meta" id="blSummary">Загрузка...</div>
      <div id="blSellersList" class="chips" style="margin-bottom:0"></div>
    </div>
  </div>

  <!-- TAB 4: STATUS -->
  <div id="tab-stats" class="tab-panel">
    <div class="stats-grid" id="statsGrid"></div>
    <div class="card">
      <div class="section-title"><span class="title-ic" id="icAlbumsTitle"></span>Фотоальбомы в чате</div>
      <div class="query-meta" style="margin-top:8px">
        Свежие объявления отправляются в чат нативным альбомом со всеми фотографиями, аналитикой цены относительно медианы и быстрым переходом к продавцу.
      </div>
      <div class="row-2" style="margin-top:14px">
        <button class="btn btn-ghost btn-block" onclick="triggerCheckNow()" id="btnStatsCheck"></button>
        <button class="btn btn-block" onclick="sendAllToChat()" id="btnStatsSend"></button>
      </div>
    </div>
  </div>
</div>

<!-- ADD SEARCH MODAL -->
<div id="addModal" class="modal-bg" onclick="if(event.target===this)closeModal('addModal')">
  <div class="modal">
    <div class="modal-head">
      <div class="modal-title" id="addModalTitle">Новый поиск</div>
      <button class="btn btn-sm btn-ghost btn-icon" onclick="closeModal('addModal')" id="btnCloseAdd"></button>
    </div>
    <div class="row-2" style="margin-bottom:10px">
      <button id="modeLinkBtn" class="btn btn-sm" onclick="setAddMode('link')"></button>
      <button id="modeFormBtn" class="btn btn-sm btn-ghost" onclick="setAddMode('form')"></button>
    </div>

    <div id="addLinkPane">
      <span class="field-label">Ссылка с Kufar.by (перенос фильтров 1 в 1)</span>
      <input id="addUrlInput" class="input" placeholder="https://www.kufar.by/l/r~minsk/..." />
    </div>

    <div id="addFormPane" style="display:none">
      <span class="field-label">Категория</span>
      <select id="addCatSelect" class="input"></select>
      <span class="field-label">Регион</span>
      <select id="addRgnSelect" class="input"></select>
      <span class="field-label">Ключевые слова (необязательно)</span>
      <input id="addQueryInput" class="input" placeholder="playstation 5, iphone 15..." />
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
      <button class="btn btn-ghost btn-block" onclick="previewNewQuery()" id="btnPreviewQuery"></button>
      <button class="btn btn-block" onclick="createNewQuery()" id="btnCreateQuery"></button>
    </div>
  </div>
</div>

<!-- EDIT FILTER MODAL -->
<div id="editModal" class="modal-bg" onclick="if(event.target===this)closeModal('editModal')">
  <div class="modal">
    <div class="modal-head">
      <div class="modal-title" id="editModalTitle">Редактировать фильтр</div>
      <button class="btn btn-sm btn-ghost btn-icon" onclick="closeModal('editModal')" id="btnCloseEdit"></button>
    </div>

    <span class="field-label">Название поиска</span>
    <input id="editNameInput" class="input" placeholder="Например: PS5 Минск" />

    <span class="field-label">Ключевые слова (необязательно)</span>
    <input id="editQueryInput" class="input" placeholder="playstation 5, slim, pro..." />

    <div class="row-2">
      <div>
        <span class="field-label">Категория</span>
        <select id="editCatSelect" class="input"></select>
      </div>
      <div>
        <span class="field-label">Регион</span>
        <select id="editRgnSelect" class="input"></select>
      </div>
    </div>

    <div class="row-2">
      <div>
        <span class="field-label">Цена от (BYN)</span>
        <input id="editMinInput" type="number" class="input" placeholder="0" />
      </div>
      <div>
        <span class="field-label">Цена до (BYN)</span>
        <input id="editMaxInput" type="number" class="input" placeholder="Любая" />
      </div>
    </div>
    <div class="pill-mini-row">
      <button type="button" class="pill-mini" onclick="adjustEditPrice(-100)">-100 к «до»</button>
      <button type="button" class="pill-mini" onclick="adjustEditPrice(100)">+100 к «до»</button>
      <button type="button" class="pill-mini" onclick="adjustEditPrice(250)">+250 к «до»</button>
      <button type="button" class="pill-mini" onclick="resetEditPrice()">Сбросить цену</button>
    </div>

    <div class="row-2">
      <div>
        <span class="field-label">Состояние</span>
        <select id="editCndSelect" class="input">
          <option value="">Любое</option>
          <option value="1">Б/У</option>
          <option value="2">Новое</option>
        </select>
      </div>
      <div>
        <span class="field-label">Продавец</span>
        <select id="editCmpSelect" class="input">
          <option value="">Все</option>
          <option value="0">Частные</option>
          <option value="1">Компании</option>
        </select>
      </div>
    </div>

    <span class="field-label">Или заменить параметры ссылкой с Kufar.by (опционально)</span>
    <input id="editUrlInput" class="input" placeholder="https://www.kufar.by/l/..." />

    <div id="editExtraInfo" class="query-meta" style="margin-top:10px;display:none"></div>
    <div id="editPreviewBox" class="card" style="display:none;margin-top:12px;background:var(--bg)"></div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px">
      <button class="btn btn-ghost btn-block" onclick="previewEditedQuery()" id="btnPreviewEdit"></button>
      <button class="btn btn-ghost btn-block" onclick="saveEditedQuery(true)" id="btnCopyEdit"></button>
    </div>
    <button class="btn btn-block" style="margin-top:8px" onclick="saveEditedQuery(false)" id="btnSaveEdit"></button>
  </div>
</div>

<!-- FEED & ANALYTICS MODAL -->
<div id="feedModal" class="modal-bg" onclick="if(event.target===this)closeModal('feedModal')">
  <div class="modal">
    <div class="modal-head">
      <div class="modal-title" id="feedTitle">Лента объявлений</div>
      <button class="btn btn-sm btn-ghost btn-icon" onclick="closeModal('feedModal')" id="btnCloseFeed"></button>
    </div>
    <div id="feedAnalytics"></div>
    <div id="feedToolbar" class="feed-toolbar" style="display:none">
      <input id="feedSearchInput" class="input" placeholder="Фильтр по названию..." oninput="filterAndRenderFeed()" />
      <select id="feedSortSelect" onchange="filterAndRenderFeed()">
        <option value="default">Сначала новые</option>
        <option value="price_asc">Дешевле</option>
        <option value="price_desc">Дороже</option>
        <option value="bargains">Выгодные</option>
      </select>
      <button class="btn btn-sm btn-ghost btn-icon" title="Копировать ссылку Kufar" onclick="copyFeedKufarUrl()" id="btnFeedCopyUrl"></button>
    </div>
    <div id="feedItems"></div>
  </div>
</div>

<!-- PHOTO LIGHTBOX MODAL -->
<div id="lightboxModal" class="lightbox-bg" onclick="if(event.target===this)closeModal('lightboxModal')">
  <div class="lightbox-top">
    <span class="lightbox-counter" id="lightboxCounter">1 / 1</span>
    <button class="btn btn-sm btn-ghost btn-icon" style="color:#fff;border-color:rgba(255,255,255,0.2)" onclick="closeModal('lightboxModal')" id="btnLightboxClose"></button>
  </div>
  <img id="lightboxImg" class="lightbox-img" src="" alt="" />
  <div class="lightbox-nav">
    <button class="btn btn-sm btn-ghost" style="color:#fff;border-color:rgba(255,255,255,0.2)" onclick="stepLightbox(-1)" id="btnLightboxPrev"></button>
    <button class="btn btn-sm btn-ghost" style="color:#fff;border-color:rgba(255,255,255,0.2)" onclick="stepLightbox(1)" id="btnLightboxNext"></button>
  </div>
</div>

<div id="bottomNav" class="nav" style="display:none">
  <button class="nav-btn active" data-tab="searches" onclick="switchTab('searches')">
    <span id="navIcSearches"></span><span>Поиски</span>
    <span id="badgeQueries" class="nav-badge" style="display:none">0</span>
  </button>
  <button class="nav-btn" data-tab="favs" onclick="switchTab('favs')">
    <span id="navIcFavs"></span><span>Избранное</span>
    <span id="badgeFavs" class="nav-badge" style="display:none">0</span>
  </button>
  <button class="nav-btn" data-tab="settings" onclick="switchTab('settings')">
    <span id="navIcSettings"></span><span>Фильтры</span>
  </button>
  <button id="navBtnStats" class="nav-btn" data-tab="stats" style="display:none" onclick="switchTab('stats')">
    <span id="navIcStats"></span><span>Статус</span>
  </button>
</div>

<script>
const SVG_PATHS = {
  radar: '<path d="M12 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0"/><path d="M16.24 7.76a6 6 0 0 1 0 8.49m-8.48 -.01a6 6 0 0 1 0 -8.49m11.31 -2.82a10 10 0 0 1 0 14.14m-14.14 0a10 10 0 0 1 0 -14.14"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/>',
  barChart: '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
  star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
  sliders: '<line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/><line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/><line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/><line x1="2" x2="6" y1="14" y2="14"/><line x1="10" x2="14" y1="8" y2="8"/><line x1="18" x2="22" y1="16" y2="16"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  message: '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
  ban: '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
  images: '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" x2="21" y1="14" y2="3"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
  mapPin: '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
  tag: '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5"/>',
  wallet: '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
  grid: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
  layers: '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
  user: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  activity: '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
  rocket: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/>',
  inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  refreshCw: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  chevronLeft: '<path d="m15 18-6-6 6-6"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>'
};

function icon(name, cls = 'ic') {
  return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${SVG_PATHS[name] || SVG_PATHS.tag}</svg>`;
}

function initStaticIcons() {
  document.getElementById('brandLogo').innerHTML = icon('radar', 'ic ic-lg');
  document.getElementById('outsideLogo').innerHTML = icon('radar', 'ic ic-xl');
  document.getElementById('btnRefreshHeader').innerHTML = icon('refreshCw', 'ic ic-sm');
  document.getElementById('btnAllToChat').innerHTML = `${icon('send', 'ic ic-sm')} В чат`;
  document.getElementById('btnOpenAdd').innerHTML = `${icon('plus', 'ic ic-sm')} Добавить`;
  document.getElementById('clearFavsBtn').innerHTML = `${icon('trash', 'ic ic-sm')} Очистить`;
  document.getElementById('icSwTitle').innerHTML = icon('shield', 'ic ic-sm');
  document.getElementById('icIntTitle').innerHTML = icon('clock', 'ic ic-sm');
  document.getElementById('icSilentTitle').innerHTML = icon('moon', 'ic ic-sm');
  document.getElementById('icTplTitle').innerHTML = icon('message', 'ic ic-sm');
  document.getElementById('icBlTitle').innerHTML = icon('ban', 'ic ic-sm');
  document.getElementById('icAlbumsTitle').innerHTML = icon('images', 'ic ic-sm');
  document.getElementById('btnStatsCheck').innerHTML = `${icon('refreshCw', 'ic ic-sm')} Проверить сейчас`;
  document.getElementById('btnStatsSend').innerHTML = `${icon('send')} В чат`;
  document.getElementById('addModalTitle').innerHTML = `${icon('plus')} Новый поиск Kufar`;
  document.getElementById('editModalTitle').innerHTML = `${icon('edit')} Редактировать фильтр`;
  document.getElementById('btnCloseAdd').innerHTML = icon('x');
  document.getElementById('btnCloseEdit').innerHTML = icon('x');
  document.getElementById('btnCloseFeed').innerHTML = icon('x');
  document.getElementById('btnFeedCopyUrl').innerHTML = icon('copy', 'ic ic-sm');
  document.getElementById('btnLightboxClose').innerHTML = icon('x');
  document.getElementById('btnLightboxPrev').innerHTML = `${icon('chevronLeft', 'ic ic-sm')} Назад`;
  document.getElementById('btnLightboxNext').innerHTML = `Далее ${icon('chevronRight', 'ic ic-sm')}`;
  document.getElementById('modeLinkBtn').innerHTML = `${icon('link', 'ic ic-sm')} По ссылке`;
  document.getElementById('modeFormBtn').innerHTML = `${icon('sliders', 'ic ic-sm')} Конструктор`;
  document.getElementById('btnPreviewQuery').innerHTML = `${icon('search', 'ic ic-sm')} Проверить`;
  document.getElementById('btnCreateQuery').innerHTML = `${icon('rocket', 'ic ic-sm')} Запустить`;
  document.getElementById('btnPreviewEdit').innerHTML = `${icon('search', 'ic ic-sm')} Проверить`;
  document.getElementById('btnCopyEdit').innerHTML = `${icon('copy', 'ic ic-sm')} Как копию`;
  document.getElementById('btnSaveEdit').innerHTML = `${icon('check', 'ic ic-sm')} Сохранить фильтр`;
  document.getElementById('navIcSearches').innerHTML = icon('radar', 'ic ic-lg');
  document.getElementById('navIcFavs').innerHTML = icon('star', 'ic ic-lg');
  document.getElementById('navIcSettings').innerHTML = icon('sliders', 'ic ic-lg');
  document.getElementById('navIcStats').innerHTML = icon('barChart', 'ic ic-lg');
}

const tg = window.Telegram && window.Telegram.WebApp;
let STATE = null;
let ADD_MODE = 'link';
let ACTIVE_FEED_Q = null;
let FEED_ADS_LIST = [];
let FEED_ADS_CACHE = {};
let EDITING_QID = null;
let LIGHTBOX_URLS = [];
let LIGHTBOX_IDX = 0;

function haptic(type = 'light') {
  try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(type); } catch(e) {}
}

function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
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
  if (tab === 'stats' && !(STATE && STATE.stats && STATE.stats.is_admin)) {
    tab = 'searches';
  }
  haptic('light');
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.querySelector(`.nav-btn[data-tab="${tab}"]`).classList.add('active');
}

function renderChip(c) {
  if (typeof c === 'string') return `<span class="chip">${esc(c)}</span>`;
  return `<span class="chip">${icon(c.icon || 'tag', 'ic ic-sm')}${esc(c.text)}</span>`;
}

function renderAll() {
  if (!STATE) return;
  const paused = STATE.monitoring_paused;
  const st = STATE.stats || {};
  const isAdmin = Boolean(st.is_admin);
  document.getElementById('monitorDot').className = 'dot' + (paused ? ' paused' : '');
  document.getElementById('monitorLabel').textContent = paused ? 'На паузе' : 'Активен';
  document.getElementById('subHeader').textContent = isAdmin
    ? `${paused ? 'Пауза' : 'Опрос каждые ' + STATE.interval_seconds + 'с'} · Поисков: ${STATE.queries.length}`
    : `${paused ? 'Мониторинг на паузе' : 'Мониторинг активен'} · Поисков: ${STATE.queries.length}`;

  document.getElementById('navBtnStats').style.display = isAdmin ? 'flex' : 'none';
  document.getElementById('intervalCard').style.display = isAdmin ? 'block' : 'none';
  if (!isAdmin && document.getElementById('tab-stats').classList.contains('active')) {
    switchTab('searches');
  }

  const bq = document.getElementById('badgeQueries');
  bq.textContent = STATE.queries.length;
  bq.style.display = STATE.queries.length ? 'flex' : 'none';
  const bf = document.getElementById('badgeFavs');
  bf.textContent = STATE.favorites.length;
  bf.style.display = STATE.favorites.length ? 'flex' : 'none';

  // Render queries
  const qList = document.getElementById('queriesList');
  if (!STATE.queries.length) {
    qList.innerHTML = `<div class="empty"><div class="empty-ic">${icon('radar', 'ic ic-xl')}</div>Пока нет поисков.<br>Нажмите <b>«Добавить»</b>, чтобы начать отслеживание.</div>`;
  } else {
    qList.innerHTML = STATE.queries.map(q => `
      <div class="card">
        <div class="query-top">
          <div>
            <div class="query-name">${esc(q.name)}</div>
            <div class="query-meta">
              <span class="state-dot" style="background:${q.is_active ? 'var(--green)' : 'var(--amber)'}"></span>
              <span>${q.is_active ? 'Активен' : 'Пауза'}</span> · <span>Просмотрено: ${q.seen_count}</span>
            </div>
          </div>
          <div class="switch ${q.is_active ? 'on' : ''}" onclick="toggleQuery(${q.id}, ${!q.is_active})"></div>
        </div>
        <div class="chips">${(q.chips || []).map(renderChip).join('')}</div>
        <div class="query-actions">
          <button class="btn btn-sm btn-ghost" onclick="openFeed(${q.id})">${icon('eye', 'ic ic-sm')} Лента</button>
          <button class="btn btn-sm btn-ghost" onclick="sendQueryToChat(${q.id})">${icon('send', 'ic ic-sm')} В чат</button>
          <button class="btn btn-sm btn-ghost" onclick="openFeed(${q.id}, true)">${icon('barChart', 'ic ic-sm')} Цены</button>
          <button class="btn btn-sm btn-ghost btn-icon" title="Редактировать" onclick="openEditModal(${q.id})">${icon('edit', 'ic ic-sm')}</button>
          <button class="btn btn-sm btn-danger btn-icon" title="Удалить" onclick="deleteQuery(${q.id}, '${esc(q.name)}')">${icon('trash', 'ic ic-sm')}</button>
        </div>
      </div>
    `).join('');
  }

  // Render favorites
  const fList = document.getElementById('favsList');
  document.getElementById('clearFavsBtn').style.display = STATE.favorites.length ? 'inline-flex' : 'none';
  if (!STATE.favorites.length) {
    fList.innerHTML = `<div class="empty"><div class="empty-ic">${icon('star', 'ic ic-xl')}</div>В Избранном пока пусто.<br>Отмечайте объявления прямо в чате или ленте.</div>`;
  } else {
    fList.innerHTML = STATE.favorites.map(f => `
      <div class="ad-card">
        <div class="thumb-wrap">
          ${f.image_url ? `<img class="ad-thumb" src="${esc(f.image_url)}" />` : icon('images', 'ic ic-lg')}
        </div>
        <div class="ad-body">
          <div class="ad-title">${esc(f.title || 'Объявление #' + f.ad_id)}</div>
          <div class="ad-price-row">
            <span class="ad-price">${fmtMoney(f.price_byn)}</span>
            ${f.price_usd ? `<span class="ad-usd">~$${Math.round(f.price_usd)}</span>` : ''}
          </div>
          <div class="ad-sub">${icon('user', 'ic ic-sm')} ${esc(f.seller_name || 'Частное лицо')}</div>
          <div class="ad-btns">
            <a class="btn btn-sm" href="${esc(f.ad_link)}" target="_blank">${icon('external', 'ic ic-sm')} Kufar</a>
            <button class="btn btn-sm btn-ghost" onclick="copyContact(${f.ad_id}, '${esc(f.seller_name||'')}', '${esc(f.title||'')}', '${fmtMoney(f.price_byn)}')">${icon('message', 'ic ic-sm')} Текст</button>
            <button class="btn btn-sm btn-danger btn-icon" onclick="removeFav(${f.ad_id})">${icon('x', 'ic ic-sm')}</button>
          </div>
        </div>
      </div>
    `).join('');
  }

  // Stop words
  const swEl = document.getElementById('stopWordsChips');
  swEl.innerHTML = STATE.stop_words.length
    ? STATE.stop_words.map(w => `<span class="chip">${esc(w)}<span class="x" onclick="removeStopWord('${esc(w)}')">${icon('x', 'ic ic-sm')}</span></span>`).join('')
    : `<span class="query-meta">Стоп-слов пока нет</span>`;

  // Interval
  document.querySelectorAll('#intervalSeg .seg-btn').forEach(b => {
    b.classList.toggle('active', Number(b.dataset.sec) === STATE.interval_seconds);
  });

  // Silent mode
  const isSilentOn = STATE.silent_mode && STATE.silent_mode !== 'off';
  document.getElementById('silentSwitch').className = 'switch' + (isSilentOn ? ' on' : '');
  document.getElementById('silentStatusText').textContent = isSilentOn
    ? `Включен: ${STATE.silent_mode}${STATE.in_silent_now ? ' (сейчас активно)' : ''}`
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
  document.getElementById('blSummary').textContent = `Скрытых объявлений: ${STATE.blacklisted_ads_count} · Продавцов в ЧС: ${STATE.blacklisted_sellers.length}`;
  document.getElementById('blSellersList').innerHTML = STATE.blacklisted_sellers.length
    ? STATE.blacklisted_sellers.map(s => `<span class="chip">${icon('user', 'ic ic-sm')}${esc(s)}<span class="x" onclick="unblockSeller('${esc(s)}')">${icon('undo', 'ic ic-sm')}</span></span>`).join('')
    : '';

  // Stats (Admin only)
  if (isAdmin) {
    document.getElementById('statsGrid').innerHTML = `
      <div class="stat-box">
        <div class="stat-top"><span class="stat-lbl">Аптайм сервера</span>${icon('clock')}</div>
        <div class="stat-val">${esc(st.uptime || '0:00')}</div>
      </div>
      <div class="stat-box">
        <div class="stat-top"><span class="stat-lbl">Проверок</span>${icon('activity')}</div>
        <div class="stat-val">${st.total_checks || 0}</div>
      </div>
      <div class="stat-box">
        <div class="stat-top"><span class="stat-lbl">Отправлено</span>${icon('send')}</div>
        <div class="stat-val">${st.new_ads_found || 0}</div>
      </div>
      <div class="stat-box">
        <div class="stat-top"><span class="stat-lbl">Поисков</span>${icon('radar')}</div>
        <div class="stat-val">${STATE.queries.length}</div>
      </div>
      <div class="stat-box">
        <div class="stat-top"><span class="stat-lbl">Пользователей</span>${icon('users')}</div>
        <div class="stat-val">${st.total_users || 1}</div>
      </div>
      <div class="stat-box">
        <div class="stat-top"><span class="stat-lbl">Активных фильтров</span>${icon('layers')}</div>
        <div class="stat-val">${st.total_active_queries || 0}</div>
      </div>
    `;
  } else {
    document.getElementById('statsGrid').innerHTML = '';
  }

  const catSel = document.getElementById('addCatSelect');
  if (!catSel.options.length) {
    STATE.categories.forEach(c => catSel.add(new Option(c.name, c.id)));
  }
  const rgnSel = document.getElementById('addRgnSelect');
  if (!rgnSel.options.length) {
    STATE.regions.forEach(r => rgnSel.add(new Option(r.name, r.id)));
  }
  const editCatSel = document.getElementById('editCatSelect');
  if (!editCatSel.options.length) {
    STATE.categories.forEach(c => editCatSel.add(new Option(c.name, c.id)));
  }
  const editRgnSel = document.getElementById('editRgnSelect');
  if (!editRgnSel.options.length) {
    STATE.regions.forEach(r => editRgnSel.add(new Option(r.name, r.id)));
  }
}

async function loadState() {
  const data = await api('/api/state');
  STATE = data;
  renderAll();
}

async function refreshState() {
  haptic('light');
  const btn = document.getElementById('btnRefreshHeader');
  const svg = btn && btn.querySelector('svg');
  if (svg) svg.classList.add('spin');
  try {
    await loadState();
    showToast('Данные обновлены');
  } finally {
    if (svg) svg.classList.remove('spin');
  }
}

async function triggerCheckNow() {
  haptic('medium');
  await api('/api/check_now', 'POST', {});
  showToast('Внеочередная проверка запущена');
}

async function toggleGlobalMonitoring() {
  haptic('medium');
  const next = !STATE.monitoring_paused;
  await api('/api/monitoring', 'POST', { paused: next });
  await loadState();
  showToast(next ? 'Мониторинг поставлен на паузу' : 'Мониторинг запущен');
}

async function toggleQuery(id, active) {
  haptic('light');
  await api('/api/queries/toggle', 'POST', { id, is_active: active });
  await loadState();
  showToast(active ? 'Поиск включён' : 'Поиск на паузе');
}

async function deleteQuery(id, name) {
  if (!confirm(`Удалить поиск «${name}»?`)) return;
  haptic('medium');
  await api('/api/queries/delete', 'POST', { id });
  await loadState();
  showToast('Поиск удалён');
}

async function sendQueryToChat(id) {
  haptic('light');
  await api('/api/queries/send_to_chat', 'POST', { id });
  showToast('Объявления с полными фотоальбомами отправляются в чат');
}

async function sendAllToChat() {
  haptic('light');
  await api('/api/queries/send_to_chat', 'POST', { id: 'all' });
  showToast('Вывод объявлений запущен в чате');
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
      <div class="chips" style="margin:8px 0">${res.chips.map(renderChip).join('')}</div>
      <div class="query-meta">Найдено на Kufar: <b>${res.count}</b>${res.median ? ' · Медиана: ' + fmtMoney(res.median) : ''}</div>
    `;
  } catch(e) { showToast(e.message); }
}

async function createNewQuery() {
  try {
    haptic('medium');
    const payload = collectAddPayload();
    await api('/api/queries/add', 'POST', payload);
    closeModal('addModal');
    await loadState();
    showToast('Поиск добавлен и запущен');
  } catch(e) { showToast(e.message); }
}

function parsePrcClient(prc) {
  if (!prc || !String(prc).startsWith('r:')) return [null, null];
  const parts = String(prc).slice(2).split(',');
  if (parts.length !== 2) return [null, null];
  const minP = (parts[0] && parts[0] !== '0') ? Math.round(Number(parts[0]) / 100) : null;
  const maxP = (parts[1] && parts[1] !== '999999999' && parts[1] !== '0') ? Math.round(Number(parts[1]) / 100) : null;
  return [Number.isFinite(minP) ? minP : null, Number.isFinite(maxP) ? maxP : null];
}

function openEditModal(qid) {
  const q = STATE && STATE.queries.find(x => x.id === qid);
  if (!q) return;
  haptic('light');
  EDITING_QID = qid;
  const p = q.params || {};
  document.getElementById('editNameInput').value = q.name || '';
  document.getElementById('editQueryInput').value = p.query || '';
  document.getElementById('editCatSelect').value = p.cat || 'all';
  document.getElementById('editRgnSelect').value = p.rgn || 'all';
  const [minP, maxP] = parsePrcClient(p.prc);
  document.getElementById('editMinInput').value = minP !== null ? String(minP) : '';
  document.getElementById('editMaxInput').value = maxP !== null ? String(maxP) : '';
  document.getElementById('editCndSelect').value = p.cnd || '';
  document.getElementById('editCmpSelect').value = p.cmp || '';
  document.getElementById('editUrlInput').value = '';
  document.getElementById('editPreviewBox').style.display = 'none';

  const specials = [];
  if (p.cbc === '31') specials.push('Sony');
  else if (p.cbc) specials.push('Бренд #' + p.cbc);
  if (p.brand) specials.push(p.brand);
  if (p.cte === '1') specials.push('Приставки');
  else if (p.cte === '2') specials.push('Игры');
  else if (p.cte === '3') specials.push('Аксессуары');
  if (p.pb) specials.push('Производитель #' + p.pb);
  const infoEl = document.getElementById('editExtraInfo');
  if (specials.length) {
    infoEl.style.display = 'flex';
    infoEl.innerHTML = `${icon('tag', 'ic ic-sm')} Сохранятся спецфильтры: ${esc(specials.join(', '))}`;
  } else {
    infoEl.style.display = 'none';
  }
  document.getElementById('editModal').classList.add('open');
}

function adjustEditPrice(delta) {
  haptic('light');
  const el = document.getElementById('editMaxInput');
  const cur = Number(el.value || 0);
  const next = Math.max(0, (Number.isFinite(cur) ? cur : 0) + delta);
  el.value = next > 0 ? String(next) : '';
}

function resetEditPrice() {
  haptic('light');
  document.getElementById('editMinInput').value = '';
  document.getElementById('editMaxInput').value = '';
}

function collectEditPayload() {
  const url = document.getElementById('editUrlInput').value.trim();
  const base = {
    id: EDITING_QID,
    name: document.getElementById('editNameInput').value.trim(),
  };
  if (url) {
    base.url = url;
    return base;
  }
  return {
    ...base,
    query: document.getElementById('editQueryInput').value.trim(),
    cat: document.getElementById('editCatSelect').value,
    rgn: document.getElementById('editRgnSelect').value,
    min_price: document.getElementById('editMinInput').value.trim(),
    max_price: document.getElementById('editMaxInput').value.trim(),
    cnd: document.getElementById('editCndSelect').value,
    cmp: document.getElementById('editCmpSelect').value,
  };
}

async function previewEditedQuery() {
  try {
    haptic('light');
    const payload = { ...collectEditPayload(), preview_only: true };
    const res = await api('/api/queries/update', 'POST', payload);
    const box = document.getElementById('editPreviewBox');
    box.style.display = 'block';
    box.innerHTML = `
      <div style="font-weight:700">${esc(res.name)}</div>
      <div class="chips" style="margin:8px 0">${res.chips.map(renderChip).join('')}</div>
      <div class="query-meta">Найдено на Kufar: <b>${res.count}</b>${res.median ? ' · Медиана: ' + fmtMoney(res.median) : ''}</div>
    `;
  } catch(e) { showToast(e.message); }
}

async function saveEditedQuery(asCopy = false) {
  try {
    haptic('medium');
    const payload = { ...collectEditPayload(), as_copy: !!asCopy };
    await api('/api/queries/update', 'POST', payload);
    closeModal('editModal');
    await loadState();
    showToast(asCopy ? 'Создана копия фильтра' : 'Фильтр обновлён');
  } catch(e) { showToast(e.message); }
}

function buildKufarUrlFromParams(p) {
  const sp = new URLSearchParams();
  Object.entries(p || {}).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '' && k !== 'size') {
      sp.set(k, String(v));
    }
  });
  return 'https://www.kufar.by/l?' + sp.toString();
}

function copyFeedKufarUrl() {
  if (!ACTIVE_FEED_Q) return;
  const url = buildKufarUrlFromParams(ACTIVE_FEED_Q.params);
  navigator.clipboard && navigator.clipboard.writeText(url);
  haptic('light');
  showToast('Ссылка поиска Kufar скопирована');
}

function openLightbox(urls, idx = 0) {
  if (!urls || !urls.length) return;
  haptic('light');
  LIGHTBOX_URLS = urls;
  LIGHTBOX_IDX = Math.max(0, Math.min(idx, urls.length - 1));
  renderLightbox();
  document.getElementById('lightboxModal').classList.add('open');
}

function stepLightbox(delta) {
  if (!LIGHTBOX_URLS.length) return;
  haptic('light');
  LIGHTBOX_IDX = (LIGHTBOX_IDX + delta + LIGHTBOX_URLS.length) % LIGHTBOX_URLS.length;
  renderLightbox();
}

function renderLightbox() {
  document.getElementById('lightboxImg').src = LIGHTBOX_URLS[LIGHTBOX_IDX];
  document.getElementById('lightboxCounter').textContent = `${LIGHTBOX_IDX + 1} / ${LIGHTBOX_URLS.length}`;
}

function copyContactFromFeed(adId) {
  const ad = FEED_ADS_CACHE[adId];
  if (!ad) return;
  copyContact(adId, ad.seller || 'продавец', ad.subject || 'товар', fmtMoney(ad.price_byn));
}

function filterAndRenderFeed() {
  const searchVal = (document.getElementById('feedSearchInput').value || '').trim().toLowerCase();
  const sortMode = document.getElementById('feedSortSelect').value || 'default';
  let items = FEED_ADS_LIST.slice();
  if (searchVal) {
    items = items.filter(a => (a.subject || '').toLowerCase().includes(searchVal) || (a.area || '').toLowerCase().includes(searchVal));
  }
  if (sortMode === 'price_asc') {
    items.sort((a, b) => (a.price_byn || 9e9) - (b.price_byn || 9e9));
  } else if (sortMode === 'price_desc') {
    items.sort((a, b) => (b.price_byn || 0) - (a.price_byn || 0));
  } else if (sortMode === 'bargains') {
    items = items.filter(a => a.bargain_pct !== null && a.bargain_pct <= -5)
      .sort((a, b) => (a.bargain_pct || 0) - (b.bargain_pct || 0));
  }
  if (!items.length) {
    document.getElementById('feedItems').innerHTML = `<div class="empty"><div class="empty-ic">${icon('inbox', 'ic ic-xl')}</div>По вашему фильтру ничего не найдено.</div>`;
    return;
  }
  document.getElementById('feedItems').innerHTML = items.map(ad => {
    let badge = '';
    if (ad.bargain_pct !== null && ad.bargain_pct <= -8) badge = `<span class="badge badge-good">${ad.bargain_pct}%</span>`;
    else if (ad.bargain_pct !== null && ad.bargain_pct >= 12) badge = `<span class="badge badge-high">+${ad.bargain_pct}%</span>`;
    const urls = (ad.image_urls && ad.image_urls.length) ? ad.image_urls : (ad.image_url ? [ad.image_url] : []);
    const imgCount = urls.length;
    return `
      <div class="ad-card" id="feed-ad-${ad.ad_id}">
        <div class="thumb-wrap ${imgCount ? 'clickable' : ''}" ${imgCount ? `onclick="openLightbox(FEED_ADS_CACHE[${ad.ad_id}].image_urls && FEED_ADS_CACHE[${ad.ad_id}].image_urls.length ? FEED_ADS_CACHE[${ad.ad_id}].image_urls : [FEED_ADS_CACHE[${ad.ad_id}].image_url], 0)"` : ''}>
          ${ad.image_url ? `<img class="ad-thumb" src="${esc(ad.image_url)}" />` : icon('images', 'ic ic-lg')}
          ${imgCount > 1 ? `<span class="photo-pill">${icon('images', 'ic ic-sm')}${imgCount}</span>` : ''}
        </div>
        <div class="ad-body">
          <div class="ad-title">${esc(ad.subject)}</div>
          <div class="ad-price-row">
            <span class="ad-price">${fmtMoney(ad.price_byn)}</span>
            ${badge}
          </div>
          <div class="ad-sub">${icon('mapPin', 'ic ic-sm')} ${esc(ad.region || '')}${ad.area ? ', ' + esc(ad.area) : ''}</div>
          <div class="ad-btns">
            <a class="btn btn-sm" href="${esc(ad.ad_link)}" target="_blank">${icon('external', 'ic ic-sm')} Kufar</a>
            <button class="btn btn-sm btn-ghost" onclick="pushAdToChat(${ad.ad_id})">${icon('send', 'ic ic-sm')} В чат</button>
            <button class="btn btn-sm btn-ghost btn-icon" title="Текст продавцу" onclick="copyContactFromFeed(${ad.ad_id})">${icon('message', 'ic ic-sm')}</button>
            <button class="btn btn-sm btn-icon ${ad.is_fav ? 'btn-success' : 'btn-ghost'}" onclick="toggleFavFromFeed(${ad.ad_id}, this)">${icon('star', ad.is_fav ? 'ic ic-sm ic-fill' : 'ic ic-sm')}</button>
            <button class="btn btn-sm btn-danger btn-icon" onclick="hideAdFromFeed(${ad.ad_id})">${icon('eyeOff', 'ic ic-sm')}</button>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function openFeed(qid, sortBargains = false) {
  haptic('light');
  const q = STATE.queries.find(x => x.id === qid);
  ACTIVE_FEED_Q = q;
  document.getElementById('feedTitle').innerHTML = `${icon('radar')} ${esc(q ? q.name : 'Лента')}`;
  document.getElementById('feedAnalytics').innerHTML = '';
  document.getElementById('feedToolbar').style.display = 'none';
  document.getElementById('feedSearchInput').value = '';
  document.getElementById('feedSortSelect').value = sortBargains ? 'bargains' : 'default';
  document.getElementById('feedItems').innerHTML = `<div class="empty">Загружаю объявления...</div>`;
  document.getElementById('feedModal').classList.add('open');

  try {
    const res = await api('/api/queries/feed', 'POST', { id: qid });
    if (res.analytics && res.analytics.count) {
      const a = res.analytics;
      document.getElementById('feedAnalytics').innerHTML = `
        <div class="card" style="background:var(--bg);margin-bottom:12px">
          <div class="query-meta" style="margin-bottom:8px">${icon('barChart', 'ic ic-sm')} Рыночная аналитика (${a.count} объявл.)</div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center">
            <div><div style="font-weight:800">${fmtMoney(a.min)}</div><div class="query-meta" style="justify-content:center">Минимум</div></div>
            <div><div style="font-weight:800;color:var(--accent)">${fmtMoney(a.median)}</div><div class="query-meta" style="justify-content:center">Медиана</div></div>
            <div><div style="font-weight:800">${fmtMoney(a.max)}</div><div class="query-meta" style="justify-content:center">Максимум</div></div>
          </div>
        </div>
      `;
    }
    FEED_ADS_CACHE = {};
    FEED_ADS_LIST = res.ads || [];
    if (!FEED_ADS_LIST.length) {
      document.getElementById('feedItems').innerHTML = `<div class="empty"><div class="empty-ic">${icon('inbox', 'ic ic-xl')}</div>Подходящих объявлений сейчас нет.</div>`;
      return;
    }
    FEED_ADS_LIST.forEach(ad => { FEED_ADS_CACHE[ad.ad_id] = ad; });
    document.getElementById('feedToolbar').style.display = 'grid';
    filterAndRenderFeed();
  } catch(e) {
    document.getElementById('feedItems').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

async function pushAdToChat(adId) {
  const ad = FEED_ADS_CACHE[adId];
  if (!ad) return;
  haptic('light');
  await api('/api/ads/send_to_chat', 'POST', { ad, query_name: ACTIVE_FEED_Q ? ACTIVE_FEED_Q.name : 'Поиск' });
  showToast('Фотоальбом и описание отправлены в чат');
}

async function toggleFavFromFeed(adId, btn) {
  const ad = FEED_ADS_CACHE[adId];
  if (!ad) return;
  haptic('light');
  const res = await api('/api/favorites/toggle', 'POST', { ad });
  ad.is_fav = res.is_fav;
  btn.innerHTML = icon('star', res.is_fav ? 'ic ic-sm ic-fill' : 'ic ic-sm');
  btn.className = 'btn btn-sm btn-icon ' + (res.is_fav ? 'btn-success' : 'btn-ghost');
  await loadState();
  showToast(res.is_fav ? 'Добавлено в Избранное' : 'Удалено из Избранного');
}

async function hideAdFromFeed(adId) {
  haptic('medium');
  await api('/api/blacklist/hide_ad', 'POST', { ad_id: adId });
  const el = document.getElementById('feed-ad-' + adId);
  if (el) el.remove();
  await loadState();
  showToast('Объявление скрыто в ЧС');
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
  showToast('Избранное очищено');
}

function copyContact(adId, seller, item, price) {
  let msg = STATE.template || '';
  msg = msg.replaceAll('{name}', seller || 'продавец').replaceAll('{item}', item || 'товар').replaceAll('{price}', price || '');
  navigator.clipboard && navigator.clipboard.writeText(msg);
  showToast('Текст скопирован в буфер');
}

async function addStopWords() {
  const inp = document.getElementById('newStopWords');
  const words = inp.value.trim();
  if (!words) return;
  await api('/api/stopwords/add', 'POST', { words });
  inp.value = '';
  await loadState();
  showToast('Стоп-слова добавлены');
}

async function removeStopWord(word) {
  await api('/api/stopwords/remove', 'POST', { word });
  await loadState();
}

async function setIntervalSec(sec) {
  haptic('light');
  await api('/api/settings', 'POST', { interval_seconds: sec });
  await loadState();
  showToast(`Интервал: ${sec} сек`);
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
  showToast(val === 'off' ? 'Тихий режим выключен' : `Тихий режим: ${val}`);
}

function insertTplVar(v) {
  const ta = document.getElementById('templateInput');
  ta.value = (ta.value ? ta.value + ' ' : '') + v;
  ta.focus();
}

async function saveTemplate() {
  const val = document.getElementById('templateInput').value.trim();
  if (val.length < 3) return showToast('Минимум 3 символа');
  await api('/api/settings', 'POST', { template: val });
  await loadState();
  showToast('Шаблон сохранён');
}

async function resetTemplate() {
  await api('/api/settings', 'POST', { reset_template: true });
  await loadState();
  showToast('Шаблон сброшен');
}

async function unblockSeller(name) {
  await api('/api/blacklist/unblock_seller', 'POST', { seller_name: name });
  await loadState();
  showToast(`Продавец «${name}» разблокирован`);
}

async function clearBlAds() {
  if (!confirm('Очистить список скрытых объявлений?')) return;
  await api('/api/blacklist/clear_ads', 'POST', {});
  await loadState();
  showToast('ЧС объявлений очищен');
}

window.addEventListener('DOMContentLoaded', async () => {
  initStaticIcons();
  if (tg && tg.initData) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor && tg.setHeaderColor('bg_color'); } catch(e) {}
    try { tg.setBackgroundColor && tg.setBackgroundColor('bg_color'); } catch(e) {}
    document.getElementById('app').style.display = 'block';
    document.getElementById('bottomNav').style.display = 'grid';
    try {
      await loadState();
    } catch (e) {
      showToast(e.message);
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

        def _send_json(self, code: int, payload: Dict[str, Any], head_only: bool = False):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _send_html(self, code: int, html_text: str, head_only: bool = False):
            body = html_text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
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

        def _handle_get_or_head(self, head_only: bool = False):
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
                }, head_only=head_only)
                return

            if path in ("/", "/webapp"):
                self._send_html(200, WEBAPP_HTML, head_only=head_only)
                return

            if path == "/api/state" and not head_only:
                chat_id = self._auth_chat_id()
                if chat_id is None:
                    return
                self._send_json(200, _build_user_state_payload(chat_id, app_ctx))
                return

            self._send_json(404, {"ok": False, "error": "Not found"}, head_only=head_only)

        def do_HEAD(self):
            self._handle_get_or_head(head_only=True)

        def do_GET(self):
            self._handle_get_or_head(head_only=False)

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

                if path == "/api/check_now":
                    admin_chat_id = config.get("admin_chat_id", 0)
                    if not (admin_chat_id and chat_id == admin_chat_id):
                        self._send_json(403, {"ok": False, "error": "Доступно только администратору"})
                        return
                    app_ctx["trigger_force_check"]()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/queries/update":
                    qid = int(body.get("id", 0))
                    user_queries = get_user_queries(chat_id)
                    existing = next((q for q in user_queries if q["id"] == qid), None)
                    if not existing:
                        self._send_json(404, {"ok": False, "error": "Поиск не найден"})
                        return
                    params, new_name = _merge_edited_params(existing["params"], body, app_ctx)
                    if not params.get("query") and (not params.get("cat") or params.get("cat") == "all"):
                        self._send_json(400, {"ok": False, "error": "Укажите категорию или ключевые слова"})
                        return
                    final_name = new_name or existing["name"]
                    if body.get("preview_only"):
                        ads = fetch_ads(params)
                        prices = [a["price_byn"] for a in ads if a.get("price_byn") and a["price_byn"] > 0]
                        med = round(float(statistics.median(prices)), 2) if len(prices) >= 3 else None
                        self._send_json(200, {
                            "ok": True,
                            "name": final_name,
                            "params": params,
                            "chips": _build_query_chips(params, app_ctx),
                            "count": len(ads),
                            "median": med,
                        })
                        return
                    if body.get("as_copy"):
                        ads = fetch_ads(params)
                        new_qid = add_query(chat_id, final_name, params)
                        if ads:
                            mark_ads_seen(new_qid, [a["ad_id"] for a in ads])
                        app_ctx["sync_backup"]()
                        app_ctx["trigger_force_check"]()
                        self._send_json(200, {"ok": True, "id": new_qid, "name": final_name, "copied": True})
                        return
                    update_query(qid, chat_id, name=final_name, params=params)
                    app_ctx["sync_backup"]()
                    app_ctx["trigger_force_check"]()
                    self._send_json(200, {
                        "ok": True,
                        "id": qid,
                        "name": final_name,
                        "params": params,
                        "chips": _build_query_chips(params, app_ctx),
                    })
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
                        admin_chat_id = config.get("admin_chat_id", 0)
                        if not (admin_chat_id and chat_id == admin_chat_id):
                            self._send_json(403, {"ok": False, "error": "Интервал сервера меняет только администратор"})
                            return
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
