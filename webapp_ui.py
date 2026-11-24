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
    add_blacklisted_seller,
    add_favorite,
    add_query,
    add_stop_word,
    add_to_blacklist,
    clear_user_blacklist,
    clear_user_favorites,
    delete_query,
    get_active_queries,
    get_admin_users_overview,
    get_blacklisted_sellers,
    get_blacklist_count,
    get_price_history,
    get_query_seen_count,
    get_setting,
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
    record_price_history,
    register_or_update_user,
    remove_blacklisted_seller,
    remove_favorite,
    remove_stop_word,
    set_setting,
    set_silent_mode,
    set_user_monitoring_paused,
    set_user_template,
    toggle_query_active,
    update_favorite_crm,
    update_query,
)
from kufar_api import clean_kufar_api_params, compute_market_analysis, fetch_ads, parse_kufar_url_or_query
from kufar_filters import (
    PARENT_CATEGORIES,
    fetch_kufar_filter_schema,
    find_parent_category,
    get_category_label,
)

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


def _build_query_chips(
    p: Dict[str, Any],
    app_ctx: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None
) -> List[Dict[str, str]]:
    """Builds structured filter chips with semantic SVG icon keys (no emojis)."""
    chips: List[Dict[str, str]] = []
    cat_map = {k: _plain_label(v) for k, v in app_ctx.get("categories", [])}
    rgn_map = {k: _plain_label(v) for k, v in app_ctx.get("regions", [])}

    if p.get("query"):
        chips.append({"icon": "search", "text": str(p["query"])})
    cat_id = str(p.get("cat", ""))
    prn_id = str(p.get("prn", ""))
    if cat_id and cat_id != "all":
        lbl = get_category_label(cat_id) or cat_map.get(cat_id, f"Категория {cat_id}")
        chips.append({"icon": "grid", "text": lbl})
    elif prn_id and prn_id != "all":
        lbl = get_category_label(prn_id) or f"Раздел {prn_id}"
        chips.append({"icon": "grid", "text": lbl})
    cte_map = {"1": "Приставки", "2": "Игры", "3": "Аксессуары", "6": "Геймпады"}
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

    m = meta or {}
    # Human-readable labels for any dynamic Kufar subfilters (model, memory, Blu-ray, year, etc.)
    already_shown_keys = {"query", "cat", "prn", "rgn", "prc", "cnd", "cmp", "ar", "sort", "size", "lang", "brand"}
    if str(p.get("cte", "")) in cte_map:
        already_shown_keys.add("cte")
    if brand:
        already_shown_keys.update({"pb", "cbc"})
    filter_labels = m.get("filter_labels") or {}
    if isinstance(filter_labels, dict):
        for fk, flbl in filter_labels.items():
            if fk in already_shown_keys or not p.get(fk) or not flbl:
                continue
            chips.append({"icon": "tag", "text": str(flbl)})

    if m.get("private_only"):
        chips.append({"icon": "user", "text": "Только частники"})
    if int(m.get("only_bargain_pct") or 0) > 0:
        chips.append({"icon": "barChart", "text": f"Ниже рынка -{int(m['only_bargain_pct'])}%"})
    if m.get("stop_words"):
        chips.append({"icon": "shield", "text": f"Стоп: {len(m['stop_words'])}"})
    if m.get("plus_words"):
        chips.append({"icon": "sparkles", "text": f"Плюс: {len(m['plus_words'])}"})
    if m.get("silent_query"):
        chips.append({"icon": "moon", "text": "Без звука"})
    return chips or [{"icon": "grid", "text": "Все объявления"}]


def _format_time_ago(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo)
        sec = max(0, int((now - dt).total_seconds()))
        if sec < 60:
            return "только что"
        mins = sec // 60
        if mins < 60:
            return f"{mins} мин назад"
        hours = mins // 60
        if hours < 24:
            return f"{hours} ч назад"
        days = hours // 24
        return f"{days} дн назад"
    except Exception:
        return ""


def _parse_words_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        tokens = [str(x).strip().lower() for x in raw]
    else:
        tokens = [t.strip().lower() for t in re.split(r"[,;\n]+", str(raw or ""))]
    seen = set()
    res = []
    for t in tokens:
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            res.append(t)
    return res[:30]


def _merge_query_meta(existing_meta: Optional[Dict[str, Any]], body: Dict[str, Any]) -> Dict[str, Any]:
    meta: Dict[str, Any] = dict(existing_meta or {})
    if "local_stop_words" in body:
        sw = _parse_words_list(body.get("local_stop_words"))
        if sw:
            meta["stop_words"] = sw
        else:
            meta.pop("stop_words", None)
    if "plus_words" in body:
        pw = _parse_words_list(body.get("plus_words"))
        if pw:
            meta["plus_words"] = pw
        else:
            meta.pop("plus_words", None)
    if "only_bargain_pct" in body:
        try:
            pct = max(0, min(60, int(body.get("only_bargain_pct") or 0)))
        except Exception:
            pct = 0
        if pct > 0:
            meta["only_bargain_pct"] = pct
        else:
            meta.pop("only_bargain_pct", None)
    if "private_only" in body:
        if body.get("private_only"):
            meta["private_only"] = True
        else:
            meta.pop("private_only", None)
    if "silent_query" in body:
        if body.get("silent_query"):
            meta["silent_query"] = True
        else:
            meta.pop("silent_query", None)
    if "filter_labels" in body and isinstance(body.get("filter_labels"), dict):
        cleaned_labels = {
            str(k).strip(): str(v).strip()[:80]
            for k, v in body["filter_labels"].items()
            if str(k).strip() and str(v).strip()
        }
        if cleaned_labels:
            meta["filter_labels"] = cleaned_labels
        else:
            meta.pop("filter_labels", None)
    return meta


def _build_user_state_payload(chat_id: int, app_ctx: Dict[str, Any]) -> Dict[str, Any]:
    config = app_ctx["config"]
    total_checks, new_ads_found = app_ctx["get_counters"]()
    uptime = str(datetime.now() - app_ctx["start_time"]).split(".")[0]

    queries_raw = get_user_queries(chat_id)
    queries = []
    for q in queries_raw:
        meta = q.get("meta") or {}
        queries.append({
            "id": q["id"],
            "name": _plain_label(q["name"]),
            "params": q["params"],
            "prn": q["params"].get("prn") or find_parent_category(q["params"].get("cat", "")),
            "meta": meta,
            "chips": _build_query_chips(q["params"], app_ctx, meta=meta),
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
        get_events = app_ctx.get("get_api_events")
        stats.update({
            "uptime": uptime,
            "total_checks": total_checks,
            "new_ads_found": new_ads_found,
            "total_users": get_user_count(),
            "total_active_queries": len(get_active_queries()),
            "users_list": get_admin_users_overview(),
            "api_events": get_events() if callable(get_events) else [],
            "backup_freq": get_setting("backup_freq", "2") or "2",
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
        "parent_categories": [{"id": c[0], "name": c[1]} for c in PARENT_CATEGORIES],
        "regions": [{"id": r[0], "name": _plain_label(r[1])} for r in app_ctx.get("regions", [])],
    }


def _apply_dynamic_kufar_fields(params: Dict[str, Any], body: Dict[str, Any]):
    """Applies dynamic Kufar form parameters (kufar_params dict + region areas) onto params."""
    dyn = body.get("kufar_params")
    if isinstance(dyn, dict):
        # Clear any prior dynamic keys when full dynamic map is submitted, except base navigation/meta
        keep_base = {"query", "cat", "prn", "rgn", "ar", "prc", "sort", "size", "lang", "brand"}
        for existing_k in list(params.keys()):
            if existing_k not in keep_base:
                params.pop(existing_k, None)
        for k, v in dyn.items():
            ks = str(k).strip()
            if not ks or ks in ("prc", "query", "sort", "size", "lang"):
                continue
            vs = str(v or "").strip()
            if vs and vs.lower() != "all":
                params[ks] = vs
            else:
                params.pop(ks, None)


def _params_from_body(body: Dict[str, Any], app_ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    cat_map = {k: _plain_label(v) for k, v in app_ctx.get("categories", [])}
    url_raw = (body.get("url") or "").strip()
    if url_raw:
        params = parse_kufar_url_or_query(url_raw)
        cat_name = get_category_label(str(params.get("cat", ""))) or cat_map.get(str(params.get("cat", "")), "Куфар поиск")
        brand = params.get("brand") or ("Sony" if str(params.get("cbc")) == "31" else "")
        name = (body.get("name") or "").strip() or params.get("query") or (f"{cat_name} ({brand})" if brand else cat_name)
        return params, name

    params: Dict[str, Any] = {"sort": "lst.d", "size": "30"}
    kw = (body.get("query") or "").strip()
    if kw:
        params["query"] = kw

    prn_id = str(body.get("prn") or "").strip()
    cat_id = str(body.get("cat") or "").strip()
    if cat_id and cat_id != "all":
        params["cat"] = cat_id
    elif prn_id and prn_id != "all":
        params["prn"] = prn_id

    rgn_id = str(body.get("rgn") or "").strip()
    if rgn_id and rgn_id != "all":
        params["rgn"] = rgn_id
        ar_val = str(body.get("ar") or "").strip()
        if ar_val:
            params["ar"] = ar_val
        elif rgn_id == "7":
            params["ar"] = "v.or:22,23,24,25,26,27,28,29,30"

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

    _apply_dynamic_kufar_fields(params, body)

    auto_cat = get_category_label(cat_id) or get_category_label(prn_id) or cat_map.get(cat_id, "Куфар поиск")
    name = (body.get("name") or "").strip() or kw or auto_cat
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
        cat_name = get_category_label(str(params.get("cat", ""))) or cat_map.get(str(params.get("cat", "")), "Куфар поиск")
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

    if "prn" in body or "cat" in body:
        cat_id = str(body.get("cat") or "").strip()
        prn_id = str(body.get("prn") or "").strip()
        if cat_id and cat_id != "all":
            params["cat"] = cat_id
            params.pop("prn", None)
        elif prn_id and prn_id != "all":
            params.pop("cat", None)
            params["prn"] = prn_id
        else:
            params.pop("cat", None)
            params.pop("prn", None)

    if "rgn" in body:
        old_rgn = str(params.get("rgn", ""))
        rgn_id = str(body.get("rgn") or "").strip()
        if not rgn_id or rgn_id == "all":
            params.pop("rgn", None)
            params.pop("ar", None)
        else:
            params["rgn"] = rgn_id
            if "ar" in body:
                ar_val = str(body.get("ar") or "").strip()
                if ar_val:
                    params["ar"] = ar_val
                else:
                    params.pop("ar", None)
            elif rgn_id != old_rgn:
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

    if "kufar_params" in body:
        _apply_dynamic_kufar_fields(params, body)
    else:
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

  .app {
    max-width: 640px; margin: 0 auto;
    padding: calc(10px + env(safe-area-inset-top)) 12px calc(92px + env(safe-area-inset-bottom));
  }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 2px 2px 14px; gap: 8px;
  }
  .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .brand-mark {
    width: 38px; height: 38px; border-radius: 12px; flex-shrink: 0;
    background: linear-gradient(135deg, var(--accent), #6366f1);
    color: #fff;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 6px 18px color-mix(in srgb, var(--accent) 32%, transparent);
  }
  .brand-title { font-size: 16.5px; font-weight: 700; letter-spacing: -0.3px; white-space: nowrap; }
  .brand-sub { font-size: 11px; color: var(--hint); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .status-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 600; border: 1px solid var(--border);
    background: var(--card); color: var(--text); cursor: pointer;
    transition: transform .12s ease, border-color .15s; white-space: nowrap;
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

  .query-actions { display: grid; grid-template-columns: 1fr 1fr auto auto; gap: 6px; }
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
    padding: 9px 11px; font-size: 16px; font-family: inherit; outline: none;
    transition: border-color .15s;
  }
  .input:focus, select:focus, textarea:focus { border-color: var(--accent); }
  .toggle-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
  .toggle-pill {
    display: flex; align-items: center; justify-content: center; gap: 6px;
    padding: 9px 10px; border-radius: 11px; border: 1px solid var(--border);
    background: var(--surface); color: var(--hint); font-size: 12px; font-weight: 600;
    cursor: pointer; transition: all .15s;
  }
  .toggle-pill.active {
    background: var(--accent-soft); color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  }
  .crm-row { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
  .crm-pill {
    border: 1px solid var(--border); background: var(--surface); color: var(--hint);
    border-radius: 7px; padding: 3px 8px; font-size: 11px; font-weight: 600; cursor: pointer;
  }
  .crm-pill.active { background: var(--accent-soft); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
  .note-box {
    margin-top: 7px; padding: 6px 9px; border-radius: 9px;
    background: var(--surface); border: 1px solid var(--border);
    font-size: 12px; color: var(--text); word-break: break-word;
  }
  .badge-closed { background: var(--red-soft); color: var(--red); }
  .badge-plus { background: var(--accent-soft); color: var(--accent); }
  .spark-wrap { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); }
  .spark-head { display: flex; justify-content: space-between; font-size: 11px; color: var(--hint); margin-bottom: 4px; }
  .admin-row {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 12.5px;
  }
  .admin-row:last-child { border-bottom: none; padding-bottom: 0; }
  .log-item {
    font-size: 11.5px; padding: 6px 8px; border-radius: 8px; background: var(--surface);
    margin-bottom: 6px; display: flex; gap: 8px; align-items: baseline;
  }
  .log-time { color: var(--hint); font-weight: 700; flex-shrink: 0; }
  .log-err { color: var(--red); }
  .log-ok { color: var(--green); }
  .dyn-section {
    margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border);
  }
  .dyn-title {
    font-size: 11.5px; font-weight: 700; color: var(--accent); text-transform: uppercase;
    letter-spacing: 0.4px; margin-bottom: 6px; display: flex; align-items: center; gap: 6px;
  }
  .dyn-bools { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; margin-top: 8px; }
  .dyn-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  .picker-btn {
    width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 10px 11px; font-size: 14px; font-family: inherit;
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    cursor: pointer; text-align: left;
  }
  .picker-btn .val-txt {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
  }
  .picker-btn .val-placeholder { color: var(--hint); }
  .picker-list { max-height: 52dvh; overflow-y: auto; -webkit-overflow-scrolling: touch; margin-top: 10px; }
  .picker-row {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 11px 10px; border-radius: 10px; cursor: pointer;
    border-bottom: 1px solid var(--border); font-size: 14px;
  }
  .picker-row.selected { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  details.dyn-extra {
    margin-top: 12px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 9px 11px;
  }
  details.dyn-extra summary {
    cursor: pointer; font-size: 12.5px; font-weight: 600; color: var(--hint);
    list-style: none; display: flex; align-items: center; justify-content: space-between;
  }
  details.dyn-extra summary::-webkit-details-marker { display: none; }
  details.card-acc {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 13px 14px; margin-bottom: 10px;
  }
  details.card-acc summary {
    cursor: pointer; list-style: none; display: flex; align-items: center;
    justify-content: space-between; gap: 10px; font-weight: 700; font-size: 14.5px;
  }
  details.card-acc summary::-webkit-details-marker { display: none; }
  .acc-left { display: flex; align-items: center; gap: 8px; }
  .acc-badge { font-size: 11.5px; font-weight: 600; color: var(--hint); }
  .acc-body { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); }
  .market-bars { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
  .bar-row { display: grid; grid-template-columns: 112px 1fr 44px; align-items: center; gap: 8px; font-size: 11px; }
  .bar-track { height: 7px; border-radius: 999px; background: var(--surface-hover); overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--accent), #6366f1); }
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
    display: flex; align-items: stretch;
    padding: 7px 6px calc(7px + env(safe-area-inset-bottom));
    backdrop-filter: saturate(180%) blur(18px);
  }
  .nav-btn {
    background: none; border: none; color: var(--hint); flex: 1; min-width: 0;
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    padding: 4px 2px; font-size: 10.5px; font-weight: 600; cursor: pointer;
    position: relative; transition: color .15s;
  }
  .nav-btn.active { color: var(--accent); }
  .nav-badge {
    position: absolute; top: 1px; right: 20%; min-width: 16px; height: 16px;
    border-radius: 999px; background: var(--accent); color: var(--btn-text);
    font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center; padding: 0 4px;
  }

  .modal-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.62); backdrop-filter: blur(4px); z-index: 100;
    display: none; align-items: flex-end; justify-content: center;
  }
  .modal-bg.open { display: flex; }
  .modal {
    width: 100%; max-width: 640px; max-height: 88dvh; overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    background: var(--card); border-top-left-radius: 22px; border-top-right-radius: 22px;
    border-top: 1px solid var(--border);
    padding: 15px 14px calc(22px + env(safe-area-inset-bottom));
    animation: slideUp .18s ease;
  }
  @media (max-width: 390px) {
    .card { padding: 12px; }
    .query-actions { gap: 5px; }
    .btn-sm { padding: 7px 8px; font-size: 11.5px; min-height: 36px; white-space: nowrap; }
    .btn-icon { padding: 7px 8px; }
    .thumb-wrap { width: 76px; height: 76px; }
    .ad-card { padding: 10px; gap: 10px; }
    .feed-toolbar { grid-template-columns: 1fr auto; }
    .feed-toolbar #feedSearchInput { grid-column: 1 / -1; }
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

  <!-- TAB 3: SETTINGS & FILTERS (Clean Accordions) -->
  <div id="tab-settings" class="tab-panel">
    <details class="card-acc">
      <summary>
        <span class="acc-left"><span class="title-ic" id="icSwTitle"></span>Стоп-слова</span>
        <span class="acc-badge" id="accSwBadge">0</span>
      </summary>
      <div class="acc-body">
        <div class="query-meta" style="margin-bottom:8px">Объявления с этими словами в заголовке или описании скрываются автоматически.</div>
        <div id="stopWordsChips" class="chips"></div>
        <div style="display:flex;gap:8px">
          <input id="newStopWords" class="input" placeholder="аренда, чехол, ps4..." />
          <button class="btn btn-sm" onclick="addStopWords()">Добавить</button>
        </div>
      </div>
    </details>

    <div class="card" id="intervalCard" style="display:none">
      <div class="section-title"><span class="title-ic" id="icIntTitle"></span>Интервал проверки (Админ)</div>
      <div class="seg" id="intervalSeg">
        <button class="seg-btn" data-sec="15" onclick="setIntervalSec(15)">15 сек</button>
        <button class="seg-btn" data-sec="30" onclick="setIntervalSec(30)">30 сек</button>
        <button class="seg-btn" data-sec="60" onclick="setIntervalSec(60)">60 сек</button>
        <button class="seg-btn" data-sec="120" onclick="setIntervalSec(120)">120 сек</button>
      </div>
    </div>

    <details class="card-acc">
      <summary>
        <span class="acc-left"><span class="title-ic" id="icSilentTitle"></span>Ночной режим</span>
        <span class="acc-badge" id="accSilentBadge">Выкл</span>
      </summary>
      <div class="acc-body">
        <div class="query-top">
          <div class="query-meta" id="silentStatusText">Уведомления без звука в заданные часы</div>
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
    </details>

    <details class="card-acc">
      <summary>
        <span class="acc-left"><span class="title-ic" id="icTplTitle"></span>Шаблон ответа продавцу</span>
        <span class="acc-badge">Текст</span>
      </summary>
      <div class="acc-body">
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
    </details>

    <details class="card-acc">
      <summary>
        <span class="acc-left"><span class="title-ic" id="icBlTitle"></span>Чёрный список</span>
        <span class="acc-badge" id="accBlBadge">0</span>
      </summary>
      <div class="acc-body">
        <div class="section-head" style="margin:0 0 8px">
          <div class="query-meta" id="blSummary">Загрузка...</div>
          <button id="clearBlAdsBtn" class="btn btn-sm btn-danger" onclick="clearBlAds()">Очистить</button>
        </div>
        <div id="blSellersList" class="chips" style="margin-bottom:0"></div>
      </div>
    </details>
  </div>

  <!-- TAB 4: STATUS (ADMIN ONLY) -->
  <div id="tab-stats" class="tab-panel">
    <div class="stats-grid" id="statsGrid"></div>
    <div class="card">
      <div class="section-title"><span class="title-ic" id="icAlbumsTitle"></span>Управление сервером</div>
      <div class="query-meta" style="margin-top:6px">
        Внеочередной цикл поиска, вывод в чат и тихий автобэкап по расписанию (без спама в чате).
      </div>
      <span class="field-label" style="margin-top:10px">Автобэкап в Telegram (время Минск)</span>
      <div class="seg" id="backupSeg" style="grid-template-columns:repeat(5,1fr)">
        <button class="seg-btn" data-bf="1" onclick="setBackupFreq('1')">1x<br><span style="font-size:9.5px;opacity:0.75">09:00</span></button>
        <button class="seg-btn" data-bf="2" onclick="setBackupFreq('2')">2x<br><span style="font-size:9.5px;opacity:0.75">09, 21</span></button>
        <button class="seg-btn" data-bf="3" onclick="setBackupFreq('3')">3x<br><span style="font-size:9.5px;opacity:0.75">09..21</span></button>
        <button class="seg-btn" data-bf="4" onclick="setBackupFreq('4')">4x<br><span style="font-size:9.5px;opacity:0.75">03..21</span></button>
        <button class="seg-btn" data-bf="off" onclick="setBackupFreq('off')">Выкл<br><span style="font-size:9.5px;opacity:0.75">ручн.</span></button>
      </div>
      <div class="row-2" style="margin-top:12px">
        <button class="btn btn-ghost btn-block" onclick="triggerCheckNow()" id="btnStatsCheck"></button>
        <button class="btn btn-ghost btn-block" onclick="syncAdminBackup()" id="btnStatsBackup"></button>
      </div>
      <button class="btn btn-block" style="margin-top:8px" onclick="sendAllToChat()" id="btnStatsSend"></button>
    </div>
    <div class="card">
      <div class="section-title"><span class="title-ic" id="icAdminUsers"></span>Пользователи бота</div>
      <div id="adminUsersList" style="margin-top:8px"></div>
    </div>
    <div class="card">
      <div class="section-title"><span class="title-ic" id="icAdminLog"></span>Журнал Kufar API</div>
      <div id="adminApiLog" style="margin-top:10px"></div>
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
      <div class="row-2">
        <div>
          <span class="field-label">Раздел Kufar</span>
          <select id="addPrnSelect" class="input" onchange="onFormCategoryChange('add', 'prn')"></select>
        </div>
        <div>
          <span class="field-label">Подкатегория</span>
          <select id="addCatSelect" class="input" onchange="onFormCategoryChange('add', 'cat')"></select>
        </div>
      </div>

      <div class="row-2">
        <div>
          <span class="field-label">Регион</span>
          <select id="addRgnSelect" class="input" onchange="onFormCategoryChange('add', 'rgn')"></select>
        </div>
        <div id="addAreaWrap" style="display:none">
          <span class="field-label">Город / Район</span>
          <button type="button" id="addAreaBtn" class="picker-btn" onclick="openAreaPicker('add')"></button>
        </div>
      </div>

      <span class="field-label">Ключевые слова (необязательно)</span>
      <input id="addQueryInput" class="input" placeholder="ps5 slim, iphone 15 pro..." />

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

      <div id="addDynamicFilters" class="dyn-section" style="display:none"></div>
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
        <span class="field-label">Раздел Kufar</span>
        <select id="editPrnSelect" class="input" onchange="onFormCategoryChange('edit', 'prn')"></select>
      </div>
      <div>
        <span class="field-label">Подкатегория</span>
        <select id="editCatSelect" class="input" onchange="onFormCategoryChange('edit', 'cat')"></select>
      </div>
    </div>

    <div class="row-2">
      <div>
        <span class="field-label">Регион</span>
        <select id="editRgnSelect" class="input" onchange="onFormCategoryChange('edit', 'rgn')"></select>
      </div>
      <div id="editAreaWrap" style="display:none">
        <span class="field-label">Город / Район</span>
        <button type="button" id="editAreaBtn" class="picker-btn" onclick="openAreaPicker('edit')"></button>
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

    <div id="editDynamicFilters" class="dyn-section" style="display:none"></div>

    <details id="editProDetails" class="dyn-extra">
      <summary>
        <span id="editProSummary">Доп. правила (стоп-слова, выгода, ссылка)</span>
      </summary>
      <div style="margin-top:8px">
        <span class="field-label">Локальные стоп-слова только для этого поиска</span>
        <input id="editLocalStops" class="input" placeholder="чехол, коробка, обмен..." />

        <span class="field-label">Плюс-слова (приоритет и бейдж)</span>
        <input id="editPlusWords" class="input" placeholder="срочно, чек, торг, оригинал..." />

        <span class="field-label">Порог выгоды для уведомлений</span>
        <select id="editBargainSelect" class="input">
          <option value="0">Все подходящие объявления</option>
          <option value="10">Только ниже рынка на 10%+</option>
          <option value="15">Только ниже рынка на 15%+</option>
          <option value="20">Только ниже рынка на 20%+</option>
          <option value="25">Только ниже рынка на 25%+</option>
        </select>

        <div class="toggle-row">
          <button type="button" id="editPrivateBtn" class="toggle-pill" onclick="toggleEditMetaFlag('private')"></button>
          <button type="button" id="editSilentBtn" class="toggle-pill" onclick="toggleEditMetaFlag('silent')"></button>
        </div>

        <span class="field-label">Или заменить параметры ссылкой с Kufar.by</span>
        <input id="editUrlInput" class="input" placeholder="https://www.kufar.by/l/..." />
      </div>
    </details>

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

<!-- MULTI-SELECT PICKER MODAL -->
<div id="pickerModal" class="modal-bg" style="z-index:160" onclick="if(event.target===this)closeModal('pickerModal')">
  <div class="modal">
    <div class="modal-head">
      <div class="modal-title" id="pickerTitle">Выбор параметров</div>
      <button class="btn btn-sm btn-ghost btn-icon" onclick="closeModal('pickerModal')" id="btnClosePicker"></button>
    </div>
    <input id="pickerSearch" class="input" placeholder="Быстрый поиск..." oninput="renderPickerOptions()" />
    <div id="pickerList" class="picker-list"></div>
    <div class="row-2" style="margin-top:12px">
      <button class="btn btn-ghost btn-block" onclick="clearPickerSelection()">Сбросить</button>
      <button class="btn btn-block" onclick="applyPickerSelection()">Применить</button>
    </div>
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
  sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  cloud: '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
  userX: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="17" x2="22" y1="8" y2="13"/><line x1="22" x2="17" y1="8" y2="13"/>',
  note: '<path d="M16 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8Z"/><path d="M15 3v4a2 2 0 0 0 2 2h4"/>'
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
  document.getElementById('icAlbumsTitle').innerHTML = icon('layers', 'ic ic-sm');
  document.getElementById('icAdminUsers').innerHTML = icon('users', 'ic ic-sm');
  document.getElementById('icAdminLog').innerHTML = icon('activity', 'ic ic-sm');
  document.getElementById('btnStatsCheck').innerHTML = `${icon('refreshCw', 'ic ic-sm')} Проверить`;
  document.getElementById('btnStatsBackup').innerHTML = `${icon('cloud', 'ic ic-sm')} Бэкап Cloud`;
  document.getElementById('btnStatsSend').innerHTML = `${icon('send')} Вывести все свежие в чат`;
  document.getElementById('addModalTitle').innerHTML = `${icon('plus')} Новый поиск Kufar`;
  document.getElementById('editModalTitle').innerHTML = `${icon('edit')} Редактировать фильтр`;
  document.getElementById('btnCloseAdd').innerHTML = icon('x');
  document.getElementById('btnCloseEdit').innerHTML = icon('x');
  document.getElementById('btnCloseFeed').innerHTML = icon('x');
  document.getElementById('btnClosePicker').innerHTML = icon('x');
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
    qList.innerHTML = STATE.queries.map(q => {
      const chips = q.chips || [];
      const shownChips = chips.slice(0, 4);
      const extraChips = chips.length - shownChips.length;
      return `
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
          <div class="chips">${shownChips.map(renderChip).join('')}${extraChips > 0 ? `<span class="chip">+${extraChips} еще</span>` : ''}</div>
          <div class="query-actions">
            <button class="btn btn-sm btn-ghost" onclick="openFeed(${q.id})">${icon('eye', 'ic ic-sm')} Смотреть</button>
            <button class="btn btn-sm btn-ghost" onclick="sendQueryToChat(${q.id})">${icon('send', 'ic ic-sm')} В чат</button>
            <button class="btn btn-sm btn-ghost btn-icon" title="Редактировать" onclick="openEditModal(${q.id})">${icon('edit', 'ic ic-sm')}</button>
            <button class="btn btn-sm btn-danger btn-icon" title="Удалить" onclick="deleteQuery(${q.id}, '${esc(q.name)}')">${icon('trash', 'ic ic-sm')}</button>
          </div>
        </div>
      `;
    }).join('');
  }

  // Render favorites
  const fList = document.getElementById('favsList');
  document.getElementById('clearFavsBtn').style.display = STATE.favorites.length ? 'inline-flex' : 'none';
  const crmLabels = [
    ['watching', 'Наблюдаю'],
    ['messaged', 'Написал'],
    ['negotiating', 'Торгуюсь'],
    ['bought', 'Куплено'],
  ];
  const crmMap = Object.fromEntries(crmLabels);
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
            ${f.status && crmMap[f.status] ? `<span class="badge badge-plus">${esc(crmMap[f.status])}</span>` : ''}
            ${f.is_closed ? `<span class="badge badge-closed">Снято / продано</span>` : ''}
          </div>
          <div class="ad-sub">${icon('user', 'ic ic-sm')} ${esc(f.seller_name || 'Частное лицо')}</div>
          ${f.note ? `<div class="note-box">${esc(f.note)}</div>` : ''}
          <div id="fav-note-edit-${f.ad_id}" style="display:none;margin-top:8px">
            <div class="crm-row" style="margin-bottom:6px">
              ${crmLabels.map(([code, lbl]) => `
                <button type="button" class="crm-pill ${f.status === code ? 'active' : ''}" onclick="setFavStatus(${f.ad_id}, '${f.status === code ? '' : code}')">${lbl}</button>
              `).join('')}
            </div>
            <div style="display:flex;gap:6px">
              <input id="fav-note-inp-${f.ad_id}" class="input" value="${esc(f.note || '')}" placeholder="Заметка по сделке..." />
              <button class="btn btn-sm" onclick="saveFavNote(${f.ad_id})">ОК</button>
            </div>
          </div>
          <div class="ad-btns">
            <a class="btn btn-sm" href="${esc(f.ad_link)}" target="_blank">${icon('external', 'ic ic-sm')} Kufar</a>
            <button class="btn btn-sm btn-ghost" onclick="copyContact(${f.ad_id}, '${esc(f.seller_name||'')}', '${esc(f.title||'')}', '${fmtMoney(f.price_byn)}')">${icon('message', 'ic ic-sm')} Текст</button>
            <button class="btn btn-sm btn-ghost btn-icon" title="Статус и заметка" onclick="toggleFavNote(${f.ad_id})">${icon('note', 'ic ic-sm')}</button>
            <button class="btn btn-sm btn-danger btn-icon" onclick="removeFav(${f.ad_id})">${icon('x', 'ic ic-sm')}</button>
          </div>
        </div>
      </div>
    `).join('');
  }

  // Stop words
  const swEl = document.getElementById('stopWordsChips');
  document.getElementById('accSwBadge').textContent = STATE.stop_words.length ? `${STATE.stop_words.length} шт.` : 'Нет';
  swEl.innerHTML = STATE.stop_words.length
    ? STATE.stop_words.map(w => `<span class="chip">${esc(w)}<span class="x" onclick="removeStopWord('${esc(w)}')">${icon('x', 'ic ic-sm')}</span></span>`).join('')
    : `<span class="query-meta">Стоп-слов пока нет</span>`;

  // Interval
  document.querySelectorAll('#intervalSeg .seg-btn').forEach(b => {
    b.classList.toggle('active', Number(b.dataset.sec) === STATE.interval_seconds);
  });

  // Silent mode
  const isSilentOn = STATE.silent_mode && STATE.silent_mode !== 'off';
  document.getElementById('accSilentBadge').textContent = isSilentOn ? STATE.silent_mode : 'Выкл';
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
  const totalBl = (STATE.blacklisted_ads_count || 0) + (STATE.blacklisted_sellers ? STATE.blacklisted_sellers.length : 0);
  document.getElementById('accBlBadge').textContent = totalBl ? `${totalBl} скрыто` : 'Пусто';
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
    const uList = st.users_list || [];
    document.getElementById('adminUsersList').innerHTML = uList.length
      ? uList.map(u => {
          const uname = u.username ? '@' + esc(u.username) : esc(u.first_name || ('ID ' + u.chat_id));
          return `
            <div class="admin-row">
              <div>
                <div style="font-weight:700">${uname}</div>
                <div class="query-meta">ID: ${u.chat_id} · ${u.paused ? 'На паузе' : 'Активен'}</div>
              </div>
              <div style="text-align:right">
                <div style="font-weight:700">${u.queries_active}/${u.queries_total} фильтр.</div>
                <div class="query-meta" style="justify-content:flex-end">Избр: ${u.favorites_count}</div>
              </div>
            </div>
          `;
        }).join('')
      : `<div class="query-meta">Нет зарегистрированных пользователей</div>`;

    const evs = st.api_events || [];
    document.getElementById('adminApiLog').innerHTML = evs.length
      ? evs.slice(0, 12).map(ev => `
          <div class="log-item">
            <span class="log-time">${esc(ev.time)}</span>
            <span class="${ev.level === 'error' ? 'log-err' : (ev.level === 'ok' ? 'log-ok' : '')}">${esc(ev.message)}</span>
          </div>
        `).join('')
      : `<div class="query-meta">Событий пока нет (логи появятся после очередного цикла).</div>`;
    const bf = String(st.backup_freq || '2');
    document.querySelectorAll('#backupSeg .seg-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.bf === bf);
    });
  } else {
    document.getElementById('statsGrid').innerHTML = '';
    document.getElementById('adminUsersList').innerHTML = '';
    document.getElementById('adminApiLog').innerHTML = '';
  }

  const fillPrnAndRgn = (prnId, rgnId) => {
    const prnEl = document.getElementById(prnId);
    if (!prnEl.options.length) {
      prnEl.add(new Option('Все разделы', 'all'));
      (STATE.parent_categories || STATE.categories).forEach(c => prnEl.add(new Option(c.name, c.id)));
    }
    const rgnEl = document.getElementById(rgnId);
    if (!rgnEl.options.length) {
      rgnEl.add(new Option('Вся Беларусь', 'all'));
      STATE.regions.filter(r => r.id !== 'all').forEach(r => rgnEl.add(new Option(r.name, r.id)));
    }
  };
  fillPrnAndRgn('addPrnSelect', 'addRgnSelect');
  fillPrnAndRgn('editPrnSelect', 'editRgnSelect');
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

const SCHEMA_CACHE_CLIENT = {};
const FORM_STATE = {
  add: { prn: 'all', cat: 'all', rgn: 'all', ar: '', kufar_params: {}, schema: null },
  edit: { prn: 'all', cat: 'all', rgn: 'all', ar: '', kufar_params: {}, schema: null },
};
let PICKER_CTX = null;

function renderMarketBucketsHtml(a) {
  if (!a || !a.buckets || !a.buckets.length) return '';
  const maxCnt = Math.max(...a.buckets.map(b => b.count || 0), 1);
  return `
    <div class="market-bars">
      ${a.buckets.map(b => `
        <div class="bar-row">
          <span style="color:var(--hint);white-space:nowrap">${esc(b.label)}</span>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.round((b.count / maxCnt) * 100)}%"></div></div>
          <span style="text-align:right;font-weight:700">${b.count} шт</span>
        </div>
      `).join('')}
    </div>
  `;
}

function renderMarketSummaryHtml(a) {
  if (!a || !a.count) return '';
  const extraHint = a.above_budget_count > 0
    ? ` · выше лимита ещё ${a.above_budget_count}`
    : (a.unbounded ? ' · весь рынок без обрезки цены' : '');
  return `
    <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
      <div class="query-meta" style="margin-bottom:8px">${icon('barChart', 'ic ic-sm')} Рынок целиком: <b>${a.count}</b> релевант.${extraHint}</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center">
        <div><div style="font-weight:800">${fmtMoney(a.min)}</div><div class="query-meta" style="justify-content:center">Минимум</div></div>
        <div><div style="font-weight:800;color:var(--accent)">${fmtMoney(a.median)}</div><div class="query-meta" style="justify-content:center">Медиана</div></div>
        <div><div style="font-weight:800">${fmtMoney(a.max)}</div><div class="query-meta" style="justify-content:center">Максимум</div></div>
      </div>
      ${renderMarketBucketsHtml(a)}
    </div>
  `;
}

async function loadFormSchema(mode) {
  const st = FORM_STATE[mode];
  const reqParams = { ...st.kufar_params };
  if (st.prn && st.prn !== 'all') reqParams.prn = st.prn;
  if (st.cat && st.cat !== 'all') reqParams.cat = st.cat;
  if (st.rgn && st.rgn !== 'all') reqParams.rgn = st.rgn;

  const cacheKey = JSON.stringify(Object.keys(reqParams).sort().reduce((acc, k) => {
    if (!['prc', 'query', 'sort', 'size', 'ar', 'cnd', 'cmp'].includes(k)) acc[k] = reqParams[k];
    return acc;
  }, {}));

  const dynEl = document.getElementById(mode + 'DynamicFilters');
  if (dynEl && (st.cat !== 'all' || st.prn !== 'all')) {
    dynEl.style.display = 'block';
    if (!SCHEMA_CACHE_CLIENT[cacheKey]) {
      dynEl.innerHTML = `<div class="query-meta">Загружаю фильтры категории с Kufar...</div>`;
    }
  }

  let schema = SCHEMA_CACHE_CLIENT[cacheKey];
  if (!schema) {
    schema = await api('/api/kufar/schema', 'POST', { params: reqParams });
    SCHEMA_CACHE_CLIENT[cacheKey] = schema;
  }
  st.schema = schema;
  if (!st.prn || st.prn === 'all') {
    if (schema.prn) st.prn = schema.prn;
  }

  // Sync section & subcategory selects
  const prnEl = document.getElementById(mode + 'PrnSelect');
  if (prnEl) prnEl.value = st.prn || 'all';

  const catEl = document.getElementById(mode + 'CatSelect');
  if (catEl) {
    const subs = schema.subcategories || [];
    catEl.innerHTML = '';
    catEl.add(new Option(subs.length ? 'Весь раздел' : 'Все подкатегории', 'all'));
    subs.forEach(s => catEl.add(new Option(s.name, s.id)));
    catEl.value = (st.cat && Array.from(catEl.options).some(o => o.value === st.cat)) ? st.cat : 'all';
    if (st.cat !== 'all' && catEl.value === 'all' && st.cat) {
      // Preserve custom cat even if not in current subcategory list
      catEl.add(new Option('Категория #' + st.cat, st.cat));
      catEl.value = st.cat;
    }
  }

  // Sync area picker visibility & label
  const areas = schema.areas || [];
  const areaWrap = document.getElementById(mode + 'AreaWrap');
  if (areaWrap) {
    if (st.rgn && st.rgn !== 'all' && areas.length) {
      areaWrap.style.display = 'block';
      updateAreaButtonLabel(mode, areas);
    } else {
      areaWrap.style.display = 'none';
    }
  }

  renderDynamicFilters(mode);
}

function updateAreaButtonLabel(mode, areas) {
  const st = FORM_STATE[mode];
  const btn = document.getElementById(mode + 'AreaBtn');
  if (!btn) return;
  const selectedIds = parseMultiVal(st.ar);
  if (!selectedIds.length) {
    btn.innerHTML = `<span class="val-txt val-placeholder">Любой район</span>${icon('chevronRight', 'ic ic-sm')}`;
  } else {
    const map = Object.fromEntries((areas || []).map(a => [String(a.id), a.name]));
    const names = selectedIds.map(id => map[id] || id).join(', ');
    btn.innerHTML = `<span class="val-txt">${esc(names)}</span>${icon('chevronRight', 'ic ic-sm')}`;
  }
}

function parseMultiVal(raw) {
  if (!raw) return [];
  const s = String(raw);
  if (s.startsWith('v.or:')) return s.slice(5).split(',').filter(Boolean);
  return s.split(',').filter(Boolean);
}

function encodeMultiVal(ids, isSingle = false) {
  const arr = (ids || []).filter(Boolean);
  if (!arr.length) return '';
  if (isSingle || arr.length === 1) return arr.length === 1 ? arr[0] : 'v.or:' + arr.join(',');
  return 'v.or:' + arr.join(',');
}

function parseRangeClient(raw) {
  if (!raw || !String(raw).startsWith('r:')) return ['', ''];
  const parts = String(raw).slice(2).split(',');
  return [parts[0] && parts[0] !== '0' ? parts[0] : '', parts[1] && parts[1] !== '999999999' ? parts[1] : ''];
}

function renderDynamicFilters(mode) {
  const st = FORM_STATE[mode];
  const container = document.getElementById(mode + 'DynamicFilters');
  if (!container) return;
  const fields = (st.schema && st.schema.fields) || [];
  if (!fields.length) {
    container.style.display = 'none';
    container.innerHTML = '';
    return;
  }
  container.style.display = 'block';

  const primary = fields.filter(f => f.group === 'primary');
  const common = fields.filter(f => f.group === 'common');
  const extra = fields.filter(f => f.group === 'extra');

  const renderField = (f) => {
    const curVal = String(st.kufar_params[f.key] || '');
    if (f.kind === 'select') {
      const opts = f.options.map(o => `<option value="${esc(o.value)}" ${curVal === o.value ? 'selected' : ''}>${esc(o.label)}</option>`).join('');
      return `
        <div>
          <span class="field-label">${esc(f.label)}</span>
          <select class="input" onchange="onDynFieldChange('${mode}', '${esc(f.key)}', this.value, ${Boolean(f.reload_on_change)})">
            <option value="">Любой</option>
            ${opts}
          </select>
        </div>
      `;
    }
    if (f.kind === 'multiselect') {
      const selIds = parseMultiVal(curVal);
      const map = Object.fromEntries(f.options.map(o => [String(o.value), o.label]));
      const txt = selIds.length ? selIds.map(id => map[id] || id).join(', ') : 'Любой';
      return `
        <div>
          <span class="field-label">${esc(f.label)}</span>
          <button type="button" class="picker-btn" onclick="openDynPicker('${mode}', '${esc(f.key)}')">
            <span class="val-txt ${selIds.length ? '' : 'val-placeholder'}">${esc(txt)}</span>
            ${icon('chevronRight', 'ic ic-sm')}
          </button>
        </div>
      `;
    }
    if (f.kind === 'chips') {
      return `
        <div>
          <span class="field-label">${esc(f.label)}</span>
          <div class="dyn-chips">
            <button type="button" class="crm-pill ${!curVal ? 'active' : ''}" onclick="onDynFieldChange('${mode}', '${esc(f.key)}', '', false)">Все</button>
            ${f.options.map(o => `
              <button type="button" class="crm-pill ${curVal === o.value ? 'active' : ''}" onclick="onDynFieldChange('${mode}', '${esc(f.key)}', '${curVal === o.value ? '' : esc(o.value)}', false)">${esc(o.label)}</button>
            `).join('')}
          </div>
        </div>
      `;
    }
    if (f.kind === 'range') {
      const [rFrom, rTo] = parseRangeClient(curVal);
      const phFrom = f.range && f.range.lower !== null ? String(f.range.lower) : 'От';
      const phTo = f.range && f.range.upper !== null && f.range.upper < 10000000 ? String(f.range.upper) : 'До';
      return `
        <div>
          <span class="field-label">${esc(f.label)}</span>
          <div class="row-2">
            <input type="number" class="input" value="${esc(rFrom)}" placeholder="${esc(phFrom)}" oninput="onDynRangeChange('${mode}', '${esc(f.key)}', this.value, null)" />
            <input type="number" class="input" value="${esc(rTo)}" placeholder="${esc(phTo)}" oninput="onDynRangeChange('${mode}', '${esc(f.key)}', null, this.value)" />
          </div>
        </div>
      `;
    }
    return '';
  };

  const KEY_PRIORITY = new Set(['pb', 'phm', 'ppm', 'cte', 'cbc', 'cbu', 'clb', 'celr', 'rgd', 'crg', 'rms', 're_rooms']);
  const primaryRanked = [...primary].sort((a, b) => {
    const ap = KEY_PRIORITY.has(a.key) ? 0 : 1;
    const bp = KEY_PRIORITY.has(b.key) ? 0 : 1;
    return ap - bp;
  });
  const topFields = primaryRanked.slice(0, 3);
  const topKeys = new Set(topFields.map(f => f.key));
  const moreFields = [
    ...primaryRanked.slice(3),
    ...common,
    ...extra,
  ];

  const renderAnyField = (f) => {
    if (f.kind === 'bool') {
      const active = st.kufar_params[f.key] === '1';
      return `<button type="button" class="toggle-pill ${active ? 'active' : ''}" onclick="onDynBoolToggle('${mode}', '${esc(f.key)}', ${Boolean(f.reload_on_change)})">${active ? icon('check', 'ic ic-sm') : ''} ${esc(f.label)}</button>`;
    }
    return renderField(f);
  };

  let html = '';
  if (topFields.length) {
    html += `<div class="dyn-title">${icon('sliders', 'ic ic-sm')} Основные характеристики</div>`;
    const topNonBool = topFields.filter(f => f.kind !== 'bool');
    const topBool = topFields.filter(f => f.kind === 'bool');
    html += topNonBool.map(renderField).join('');
    if (topBool.length) {
      html += `<div class="dyn-bools">` + topBool.map(renderAnyField).join('') + `</div>`;
    }
  }
  if (moreFields.length) {
    const activeMoreCnt = moreFields.filter(f => Boolean(st.kufar_params[f.key])).length;
    const moreNonBool = moreFields.filter(f => f.kind !== 'bool');
    const moreBool = moreFields.filter(f => f.kind === 'bool');
    html += `
      <details class="dyn-extra" ${activeMoreCnt > 0 ? 'open' : ''}>
        <summary>
          <span>Все фильтры категории (${moreFields.length})${activeMoreCnt ? ' · выбрано ' + activeMoreCnt : ''}</span>
          ${icon('chevronRight', 'ic ic-sm')}
        </summary>
        <div style="margin-top:8px">
          ${moreNonBool.map(renderField).join('')}
          ${moreBool.length ? `<div class="dyn-bools" style="margin-top:10px">${moreBool.map(renderAnyField).join('')}</div>` : ''}
        </div>
      </details>
    `;
  }
  container.innerHTML = html;
}

async function onFormCategoryChange(mode, level) {
  const st = FORM_STATE[mode];
  if (level === 'prn') {
    st.prn = document.getElementById(mode + 'PrnSelect').value;
    st.cat = 'all';
    st.kufar_params = {};
  } else if (level === 'cat') {
    st.cat = document.getElementById(mode + 'CatSelect').value;
    st.kufar_params = {};
  } else if (level === 'rgn') {
    st.rgn = document.getElementById(mode + 'RgnSelect').value;
    st.ar = '';
  }
  await loadFormSchema(mode);
}

async function onDynFieldChange(mode, key, val, reload) {
  const st = FORM_STATE[mode];
  if (val) st.kufar_params[key] = val;
  else delete st.kufar_params[key];
  if (reload) {
    await loadFormSchema(mode);
  } else {
    renderDynamicFilters(mode);
  }
}

async function onDynBoolToggle(mode, key, reload) {
  haptic('light');
  const st = FORM_STATE[mode];
  if (st.kufar_params[key] === '1') delete st.kufar_params[key];
  else st.kufar_params[key] = '1';
  if (reload) await loadFormSchema(mode);
  else renderDynamicFilters(mode);
}

function onDynRangeChange(mode, key, newFrom, newTo) {
  const st = FORM_STATE[mode];
  const [curFrom, curTo] = parseRangeClient(st.kufar_params[key]);
  const f = newFrom !== null ? newFrom.trim() : curFrom;
  const t = newTo !== null ? newTo.trim() : curTo;
  if (!f && !t) {
    delete st.kufar_params[key];
  } else {
    const schemaField = (st.schema && st.schema.fields || []).find(x => x.key === key);
    const maxBound = (schemaField && schemaField.range && schemaField.range.upper) || 999999999;
    st.kufar_params[key] = `r:${f || 0},${t || maxBound}`;
  }
}

function openDynPicker(mode, key) {
  const st = FORM_STATE[mode];
  const field = (st.schema && st.schema.fields || []).find(x => x.key === key);
  if (!field) return;
  haptic('light');
  PICKER_CTX = {
    mode,
    key,
    isArea: false,
    title: field.label,
    options: field.options || [],
    selected: new Set(parseMultiVal(st.kufar_params[key])),
    reload: Boolean(field.reload_on_change),
  };
  document.getElementById('pickerTitle').textContent = field.label;
  document.getElementById('pickerSearch').value = '';
  renderPickerOptions();
  document.getElementById('pickerModal').classList.add('open');
}

function openAreaPicker(mode) {
  const st = FORM_STATE[mode];
  const areas = (st.schema && st.schema.areas) || [];
  if (!areas.length) return;
  haptic('light');
  PICKER_CTX = {
    mode,
    key: 'ar',
    isArea: true,
    title: 'Город / Район',
    options: areas.map(a => ({ value: String(a.id), label: a.name })),
    selected: new Set(parseMultiVal(st.ar)),
    reload: false,
  };
  document.getElementById('pickerTitle').textContent = 'Город / Район';
  document.getElementById('pickerSearch').value = '';
  renderPickerOptions();
  document.getElementById('pickerModal').classList.add('open');
}

function renderPickerOptions() {
  if (!PICKER_CTX) return;
  const q = (document.getElementById('pickerSearch').value || '').trim().toLowerCase();
  const opts = q
    ? PICKER_CTX.options.filter(o => o.label.toLowerCase().includes(q))
    : PICKER_CTX.options;
  const listEl = document.getElementById('pickerList');
  if (!opts.length) {
    listEl.innerHTML = `<div class="query-meta" style="padding:14px">Ничего не найдено</div>`;
    return;
  }
  listEl.innerHTML = opts.map(o => {
    const sel = PICKER_CTX.selected.has(String(o.value));
    return `
      <div class="picker-row ${sel ? 'selected' : ''}" onclick="togglePickerItem('${esc(o.value)}')">
        <span>${esc(o.label)}</span>
        <span>${sel ? icon('check', 'ic ic-sm') : ''}</span>
      </div>
    `;
  }).join('');
}

function togglePickerItem(val) {
  if (!PICKER_CTX) return;
  haptic('light');
  const s = String(val);
  if (PICKER_CTX.selected.has(s)) PICKER_CTX.selected.delete(s);
  else PICKER_CTX.selected.add(s);
  renderPickerOptions();
}

function clearPickerSelection() {
  if (!PICKER_CTX) return;
  PICKER_CTX.selected.clear();
  renderPickerOptions();
}

async function applyPickerSelection() {
  if (!PICKER_CTX) return;
  haptic('medium');
  const { mode, key, isArea, selected, reload } = PICKER_CTX;
  const encoded = encodeMultiVal(Array.from(selected));
  const st = FORM_STATE[mode];
  if (isArea) {
    st.ar = encoded;
    updateAreaButtonLabel(mode, (st.schema && st.schema.areas) || []);
  } else {
    if (encoded) st.kufar_params[key] = encoded;
    else delete st.kufar_params[key];
    if (reload) await loadFormSchema(mode);
    else renderDynamicFilters(mode);
  }
  closeModal('pickerModal');
}

function collectDynamicFilterLabels(mode) {
  const st = FORM_STATE[mode];
  const labels = {};
  const fields = (st.schema && st.schema.fields) || [];
  fields.forEach(f => {
    const v = st.kufar_params[f.key];
    if (!v) return;
    if (f.kind === 'bool' && v === '1') {
      labels[f.key] = `${f.label}: Да`;
    } else if (f.kind === 'range') {
      const [a, b] = parseRangeClient(v);
      if (a || b) labels[f.key] = `${f.label}: ${a || '...'}–${b || '...'}`;
    } else if (f.kind === 'select' || f.kind === 'chips') {
      const opt = (f.options || []).find(o => String(o.value) === String(v));
      if (opt) labels[f.key] = `${f.label}: ${opt.label}`;
    } else if (f.kind === 'multiselect') {
      const ids = parseMultiVal(v);
      const map = Object.fromEntries((f.options || []).map(o => [String(o.value), o.label]));
      const names = ids.map(id => map[id] || id);
      if (names.length) {
        labels[f.key] = names.length <= 2 ? names.join(', ') : `${f.label}: ${names.length} выбр.`;
      }
    }
  });
  return labels;
}

function openAddModal() {
  haptic('light');
  document.getElementById('previewBox').style.display = 'none';
  document.getElementById('addUrlInput').value = '';
  FORM_STATE.add = { prn: 'all', cat: 'all', rgn: 'all', ar: '', kufar_params: {}, schema: null };
  document.getElementById('addPrnSelect').value = 'all';
  document.getElementById('addRgnSelect').value = 'all';
  document.getElementById('addQueryInput').value = '';
  document.getElementById('addMinInput').value = '';
  document.getElementById('addMaxInput').value = '';
  document.getElementById('addModal').classList.add('open');
  if (ADD_MODE === 'form') loadFormSchema('add');
}
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function setAddMode(mode) {
  ADD_MODE = mode;
  document.getElementById('addLinkPane').style.display = mode === 'link' ? 'block' : 'none';
  document.getElementById('addFormPane').style.display = mode === 'form' ? 'block' : 'none';
  document.getElementById('modeLinkBtn').className = 'btn btn-sm' + (mode === 'link' ? '' : ' btn-ghost');
  document.getElementById('modeFormBtn').className = 'btn btn-sm' + (mode === 'form' ? '' : ' btn-ghost');
  document.getElementById('previewBox').style.display = 'none';
  if (mode === 'form') loadFormSchema('add');
}

function collectAddPayload() {
  if (ADD_MODE === 'link') {
    return { url: document.getElementById('addUrlInput').value.trim() };
  }
  const st = FORM_STATE.add;
  return {
    prn: st.prn,
    cat: st.cat,
    rgn: st.rgn,
    ar: st.ar,
    query: document.getElementById('addQueryInput').value.trim(),
    min_price: document.getElementById('addMinInput').value.trim(),
    max_price: document.getElementById('addMaxInput').value.trim(),
    kufar_params: st.kufar_params,
    filter_labels: collectDynamicFilterLabels('add'),
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
      <div class="query-meta">Под ваш бюджет сейчас на Kufar: <b>${res.count}</b></div>
      ${renderMarketSummaryHtml(res.market)}
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

let EDIT_PRIVATE_ONLY = false;
let EDIT_SILENT_QUERY = false;

function syncEditMetaButtons() {
  const pb = document.getElementById('editPrivateBtn');
  const sb = document.getElementById('editSilentBtn');
  pb.className = 'toggle-pill' + (EDIT_PRIVATE_ONLY ? ' active' : '');
  pb.innerHTML = `${icon('user', 'ic ic-sm')} Только частники`;
  sb.className = 'toggle-pill' + (EDIT_SILENT_QUERY ? ' active' : '');
  sb.innerHTML = `${icon('moon', 'ic ic-sm')} Без звука`;
}

function toggleEditMetaFlag(kind) {
  haptic('light');
  if (kind === 'private') EDIT_PRIVATE_ONLY = !EDIT_PRIVATE_ONLY;
  if (kind === 'silent') EDIT_SILENT_QUERY = !EDIT_SILENT_QUERY;
  syncEditMetaButtons();
}

function openEditModal(qid) {
  const q = STATE && STATE.queries.find(x => x.id === qid);
  if (!q) return;
  haptic('light');
  EDITING_QID = qid;
  const p = q.params || {};
  const m = q.meta || {};
  document.getElementById('editNameInput').value = q.name || '';
  document.getElementById('editQueryInput').value = p.query || '';
  document.getElementById('editRgnSelect').value = p.rgn || 'all';
  const [minP, maxP] = parsePrcClient(p.prc);
  document.getElementById('editMinInput').value = minP !== null ? String(minP) : '';
  document.getElementById('editMaxInput').value = maxP !== null ? String(maxP) : '';
  document.getElementById('editLocalStops').value = (m.stop_words || []).join(', ');
  document.getElementById('editPlusWords').value = (m.plus_words || []).join(', ');
  document.getElementById('editBargainSelect').value = String(m.only_bargain_pct || 0);
  EDIT_PRIVATE_ONLY = Boolean(m.private_only);
  EDIT_SILENT_QUERY = Boolean(m.silent_query);
  syncEditMetaButtons();
  const proActive = (m.stop_words && m.stop_words.length ? 1 : 0)
    + (m.plus_words && m.plus_words.length ? 1 : 0)
    + (Number(m.only_bargain_pct || 0) > 0 ? 1 : 0)
    + (EDIT_PRIVATE_ONLY ? 1 : 0)
    + (EDIT_SILENT_QUERY ? 1 : 0);
  const proDet = document.getElementById('editProDetails');
  if (proDet) {
    proDet.open = proActive > 0;
    document.getElementById('editProSummary').textContent = proActive
      ? `Доп. правила (активно: ${proActive})`
      : 'Доп. правила (стоп-слова, выгода, ссылка)';
  }
  document.getElementById('editUrlInput').value = '';
  document.getElementById('editPreviewBox').style.display = 'none';

  const keepBase = new Set(['query', 'cat', 'prn', 'rgn', 'ar', 'prc', 'sort', 'size', 'lang', 'brand']);
  const dynParams = {};
  Object.entries(p).forEach(([k, v]) => {
    if (!keepBase.has(k) && v !== null && v !== undefined && String(v) !== '') {
      dynParams[k] = String(v);
    }
  });
  FORM_STATE.edit = {
    prn: q.prn || p.prn || 'all',
    cat: p.cat || 'all',
    rgn: p.rgn || 'all',
    ar: p.ar || '',
    kufar_params: dynParams,
    schema: null,
  };
  document.getElementById('editPrnSelect').value = FORM_STATE.edit.prn;

  const infoEl = document.getElementById('editExtraInfo');
  infoEl.style.display = 'none';
  document.getElementById('editModal').classList.add('open');
  loadFormSchema('edit');
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
    local_stop_words: document.getElementById('editLocalStops').value,
    plus_words: document.getElementById('editPlusWords').value,
    only_bargain_pct: Number(document.getElementById('editBargainSelect').value || 0),
    private_only: EDIT_PRIVATE_ONLY,
    silent_query: EDIT_SILENT_QUERY,
  };
  if (url) {
    base.url = url;
    return base;
  }
  const st = FORM_STATE.edit;
  return {
    ...base,
    query: document.getElementById('editQueryInput').value.trim(),
    prn: st.prn,
    cat: st.cat,
    rgn: st.rgn,
    ar: st.ar,
    min_price: document.getElementById('editMinInput').value.trim(),
    max_price: document.getElementById('editMaxInput').value.trim(),
    kufar_params: st.kufar_params,
    filter_labels: collectDynamicFilterLabels('edit'),
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
      <div class="query-meta">Под ваш бюджет сейчас на Kufar: <b>${res.count}</b></div>
      ${renderMarketSummaryHtml(res.market)}
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

function renderPriceSparkline(history) {
  if (!history || !history.length) return '';
  if (history.length === 1) {
    return `<div class="spark-wrap"><div class="spark-head"><span>История цен (${esc(history[0].day)})</span><span>Медиана: ${fmtMoney(history[0].median_byn)}</span></div></div>`;
  }
  const vals = history.map(h => Number(h.median_byn || 0));
  const mn = Math.min(...vals);
  const mx = Math.max(...vals);
  const span = Math.max(1, mx - mn);
  const w = 260, h = 44, pad = 6;
  const pts = vals.map((v, i) => {
    const x = pad + (i * (w - pad * 2)) / Math.max(1, vals.length - 1);
    const y = h - pad - ((v - mn) * (h - pad * 2)) / span;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const firstDay = history[0].day.slice(5);
  const lastDay = history[history.length - 1].day.slice(5);
  return `
    <div class="spark-wrap">
      <div class="spark-head">
        <span>Динамика медианы (${firstDay} — ${lastDay})</span>
        <span>${fmtMoney(mn)} – ${fmtMoney(mx)}</span>
      </div>
      <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:44px;display:block">
        <polyline fill="none" stroke="var(--accent)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" points="${pts}" />
      </svg>
    </div>
  `;
}

async function hideSellerFromFeed(adId) {
  const ad = FEED_ADS_CACHE[adId];
  if (!ad || !ad.seller) return showToast('Имя продавца скрыто');
  if (!confirm(`Скрыть все объявления от «${ad.seller}»?`)) return;
  haptic('medium');
  await api('/api/blacklist/hide_seller', 'POST', { seller_name: ad.seller, account_id: ad.account_id || '' });
  FEED_ADS_LIST = FEED_ADS_LIST.filter(x => x.seller !== ad.seller);
  filterAndRenderFeed();
  await loadState();
  showToast('Продавец добавлен в ЧС');
}

function filterAndRenderFeed() {
  const searchVal = (document.getElementById('feedSearchInput').value || '').trim().toLowerCase();
  const sortMode = document.getElementById('feedSortSelect').value || 'default';
  let items = FEED_ADS_LIST.slice();
  if (searchVal) {
    items = items.filter(a => (a.subject || '').toLowerCase().includes(searchVal) || (a.area || '').toLowerCase().includes(searchVal) || (a.seller || '').toLowerCase().includes(searchVal));
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
    const plusBadge = (ad.matched_plus && ad.matched_plus.length)
      ? `<span class="badge badge-plus">${esc(ad.matched_plus[0])}</span>`
      : '';
    const urls = (ad.image_urls && ad.image_urls.length) ? ad.image_urls : (ad.image_url ? [ad.image_url] : []);
    const imgCount = urls.length;
    const sellerType = ad.company_ad ? 'Магазин' : 'Частник';
    const metaParts = [];
    if (ad.area || ad.region) metaParts.push(esc([ad.region, ad.area].filter(Boolean).join(', ')));
    if (ad.time_ago) metaParts.push(esc(ad.time_ago));
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
            ${plusBadge}
          </div>
          <div class="ad-sub">${icon('mapPin', 'ic ic-sm')} ${metaParts.join(' · ')}</div>
          <div class="ad-sub">${icon('user', 'ic ic-sm')} ${esc(ad.seller || 'Автор')} (${sellerType})</div>
          <div class="ad-btns">
            <a class="btn btn-sm" href="${esc(ad.ad_link)}" target="_blank">${icon('external', 'ic ic-sm')} Kufar</a>
            <button class="btn btn-sm btn-ghost" onclick="pushAdToChat(${ad.ad_id})">${icon('send', 'ic ic-sm')} В чат</button>
            <button class="btn btn-sm btn-ghost btn-icon" title="Текст продавцу" onclick="copyContactFromFeed(${ad.ad_id})">${icon('message', 'ic ic-sm')}</button>
            <button class="btn btn-sm btn-icon ${ad.is_fav ? 'btn-success' : 'btn-ghost'}" onclick="toggleFavFromFeed(${ad.ad_id}, this)">${icon('star', ad.is_fav ? 'ic ic-sm ic-fill' : 'ic ic-sm')}</button>
            ${ad.seller ? `<button class="btn btn-sm btn-danger btn-icon" title="Скрыть продавца" onclick="hideSellerFromFeed(${ad.ad_id})">${icon('userX', 'ic ic-sm')}</button>` : ''}
            <button class="btn btn-sm btn-danger btn-icon" title="Скрыть объявление" onclick="hideAdFromFeed(${ad.ad_id})">${icon('eyeOff', 'ic ic-sm')}</button>
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
      const sparkHtml = renderPriceSparkline(res.price_history || []);
      const extraMarket = a.above_budget_count > 0
        ? ` · выше лимита ещё ${a.above_budget_count}`
        : (a.unbounded ? ' · весь рынок целиком' : '');
      document.getElementById('feedAnalytics').innerHTML = `
        <div class="card" style="background:var(--bg);margin-bottom:12px">
          <div class="query-meta" style="margin-bottom:8px">${icon('barChart', 'ic ic-sm')} Рыночная аналитика: <b>${a.count}</b> объявл.${extraMarket}</div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center">
            <div><div style="font-weight:800">${fmtMoney(a.min)}</div><div class="query-meta" style="justify-content:center">Минимум</div></div>
            <div><div style="font-weight:800;color:var(--accent)">${fmtMoney(a.median)}</div><div class="query-meta" style="justify-content:center">Медиана</div></div>
            <div><div style="font-weight:800">${fmtMoney(a.max)}</div><div class="query-meta" style="justify-content:center">Максимум</div></div>
          </div>
          ${renderMarketBucketsHtml(a)}
          ${sparkHtml}
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

async function syncAdminBackup() {
  haptic('medium');
  await api('/api/admin/backup', 'POST', {});
  showToast('Бэкап тихо обновлён в Telegram Cloud');
}

async function setBackupFreq(freq) {
  haptic('light');
  await api('/api/admin/backup', 'POST', { backup_freq: freq });
  await loadState();
  showToast(freq === 'off' ? 'Автобэкап отключён' : `Автобэкап: ${freq}x в день (Минск)`);
}

async function setFavStatus(adId, status) {
  haptic('light');
  await api('/api/favorites/crm', 'POST', { ad_id: adId, status });
  await loadState();
}

function toggleFavNote(adId) {
  haptic('light');
  const el = document.getElementById('fav-note-edit-' + adId);
  if (!el) return;
  const open = el.style.display === 'none';
  el.style.display = open ? 'block' : 'none';
  if (open) {
    const inp = document.getElementById('fav-note-inp-' + adId);
    inp && inp.focus();
  }
}

async function saveFavNote(adId) {
  const inp = document.getElementById('fav-note-inp-' + adId);
  const note = inp ? inp.value.trim() : '';
  haptic('light');
  await api('/api/favorites/crm', 'POST', { ad_id: adId, note });
  await loadState();
  showToast('Заметка сохранена');
}

async function clearBlAds() {
  if (!confirm('Очистить список скрытых объявлений?')) return;
  await api('/api/blacklist/clear_ads', 'POST', {});
  await loadState();
  showToast('ЧС объявлений очищен');
}

window.addEventListener('DOMContentLoaded', async () => {
  initStaticIcons();
  const lb = document.getElementById('lightboxModal');
  let touchX = 0;
  lb.addEventListener('touchstart', e => { touchX = e.changedTouches[0].clientX; }, { passive: true });
  lb.addEventListener('touchend', e => {
    const dx = e.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 40) stepLightbox(dx < 0 ? 1 : -1);
  }, { passive: true });

  if (tg && tg.initData) {
    tg.ready();
    tg.expand();
    try { tg.disableVerticalSwipes && tg.disableVerticalSwipes(); } catch(e) {}
    try { tg.setHeaderColor && tg.setHeaderColor('bg_color'); } catch(e) {}
    try { tg.setBackgroundColor && tg.setBackgroundColor('bg_color'); } catch(e) {}
    document.getElementById('app').style.display = 'block';
    document.getElementById('bottomNav').style.display = 'flex';
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

                if path == "/api/kufar/schema":
                    schema = fetch_kufar_filter_schema(body.get("params") or body)
                    self._send_json(200, schema)
                    return

                if path == "/api/queries/preview":
                    params, name = _params_from_body(body, app_ctx)
                    if not params.get("query") and not params.get("cat") and not params.get("prn"):
                        self._send_json(400, {"ok": False, "error": "Укажите раздел, подкатегорию или ключевые слова"})
                        return
                    meta = _merge_query_meta({}, body)
                    ads = fetch_ads(params)
                    sw_list = get_stop_words(chat_id)
                    market = compute_market_analysis(
                        params,
                        user_stop_words=sw_list,
                        local_stop_words=meta.get("stop_words"),
                        exclude_company=bool(meta.get("private_only")),
                        ttl_seconds=120,
                    )
                    self._send_json(200, {
                        "ok": True,
                        "name": name,
                        "params": params,
                        "chips": _build_query_chips(params, app_ctx, meta=meta),
                        "count": len(ads),
                        "median": market.get("median"),
                        "market": market,
                    })
                    return

                if path == "/api/queries/add":
                    params, name = _params_from_body(body, app_ctx)
                    if not params.get("query") and not params.get("cat") and not params.get("prn"):
                        self._send_json(400, {"ok": False, "error": "Укажите раздел, подкатегорию или ключевые слова"})
                        return
                    meta = _merge_query_meta({}, body)
                    ads = fetch_ads(params)
                    qid = add_query(chat_id, name, params, meta=meta)
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

                if path == "/api/admin/backup":
                    admin_chat_id = config.get("admin_chat_id", 0)
                    if not (admin_chat_id and chat_id == admin_chat_id):
                        self._send_json(403, {"ok": False, "error": "Доступно только администратору"})
                        return
                    if "backup_freq" in body:
                        freq = str(body.get("backup_freq") or "2").strip()
                        if freq not in ("1", "2", "3", "4", "off"):
                            self._send_json(400, {"ok": False, "error": "Неверная частота автобэкапа"})
                            return
                        set_setting("backup_freq", freq)
                        app_ctx["sync_backup"]()
                        self._send_json(200, {"ok": True, "backup_freq": freq})
                        return
                    fn = app_ctx.get("sync_cloud_backup_now") or app_ctx["sync_backup"]
                    fn()
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
                    if not params.get("query") and not params.get("cat") and not params.get("prn"):
                        self._send_json(400, {"ok": False, "error": "Укажите раздел, подкатегорию или ключевые слова"})
                        return
                    meta = _merge_query_meta(existing.get("meta"), body)
                    final_name = new_name or existing["name"]
                    if body.get("preview_only"):
                        ads = fetch_ads(params)
                        sw_list = get_stop_words(chat_id)
                        market = compute_market_analysis(
                            params,
                            user_stop_words=sw_list,
                            local_stop_words=meta.get("stop_words"),
                            exclude_company=bool(meta.get("private_only")),
                            ttl_seconds=120,
                        )
                        self._send_json(200, {
                            "ok": True,
                            "name": final_name,
                            "params": params,
                            "meta": meta,
                            "chips": _build_query_chips(params, app_ctx, meta=meta),
                            "count": len(ads),
                            "median": market.get("median"),
                            "market": market,
                        })
                        return
                    if body.get("as_copy"):
                        ads = fetch_ads(params)
                        new_qid = add_query(chat_id, final_name, params, meta=meta)
                        if ads:
                            mark_ads_seen(new_qid, [a["ad_id"] for a in ads])
                        app_ctx["sync_backup"]()
                        app_ctx["trigger_force_check"]()
                        self._send_json(200, {"ok": True, "id": new_qid, "name": final_name, "copied": True})
                        return
                    update_query(qid, chat_id, name=final_name, params=params, meta=meta)
                    app_ctx["sync_backup"]()
                    app_ctx["trigger_force_check"]()
                    self._send_json(200, {
                        "ok": True,
                        "id": qid,
                        "name": final_name,
                        "params": params,
                        "meta": meta,
                        "chips": _build_query_chips(params, app_ctx, meta=meta),
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
                    q_meta = q.get("meta") or {}
                    api_params = dict(q["params"])
                    api_params["size"] = "50"
                    ads = fetch_ads(api_params)
                    min_b, max_b = _parse_prc_bounds(q["params"].get("prc"))
                    bl = get_user_blacklist(chat_id)
                    sw_list = get_stop_words(chat_id)

                    # Full market analytics WITHOUT user's prc budget ceiling (evaluates 2300, 2500 BYN etc.)
                    analytics = compute_market_analysis(
                        q["params"],
                        user_stop_words=sw_list,
                        local_stop_words=q_meta.get("stop_words"),
                        exclude_company=bool(q_meta.get("private_only")),
                        ttl_seconds=120,
                    )
                    med = analytics.get("median")
                    if med is not None and analytics.get("count"):
                        record_price_history(
                            qid, med, analytics.get("min"), analytics.get("max"), int(analytics["count"])
                        )

                    eval_meta = app_ctx.get("evaluate_query_meta")
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
                        matched_plus: List[str] = []
                        if callable(eval_meta):
                            allowed, matched_plus = eval_meta(a, q_meta, market_median=med)
                            if not allowed:
                                continue
                        b_pct = round(((p - med) / med) * 100) if (p is not None and med) else None
                        item = dict(a)
                        item["is_fav"] = is_favorite(chat_id, a["ad_id"])
                        item["bargain_pct"] = b_pct
                        item["time_ago"] = _format_time_ago(a.get("list_time", ""))
                        item["matched_plus"] = matched_plus
                        filtered.append(item)
                        if len(filtered) >= 30:
                            break

                    history = get_price_history(qid, days=14)
                    self._send_json(200, {
                        "ok": True,
                        "ads": filtered,
                        "analytics": analytics,
                        "price_history": history,
                    })
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

                if path == "/api/favorites/crm":
                    ad_id = int(body.get("ad_id", 0))
                    if not ad_id:
                        self._send_json(400, {"ok": False, "error": "ad_id required"})
                        return
                    status = body.get("status") if "status" in body else None
                    note = body.get("note") if "note" in body else None
                    update_favorite_crm(chat_id, ad_id, status=status, note=note)
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

                if path == "/api/blacklist/hide_seller":
                    sname = (body.get("seller_name") or "").strip()
                    acc_id = str(body.get("account_id") or "").strip() or None
                    if not sname:
                        self._send_json(400, {"ok": False, "error": "Имя продавца не указано"})
                        return
                    add_blacklisted_seller(chat_id, sname, account_id=acc_id)
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
