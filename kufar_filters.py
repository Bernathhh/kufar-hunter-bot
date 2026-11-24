"""
Dynamic Kufar categories, subcategories, areas, and category-specific filter schema loader.
Parses live filter schemas directly from Kufar listings metadata with in-memory TTL caching,
rate-limit protection, and resilient fallbacks.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kufar_filters")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# All 22 Kufar top-level parent categories (prn)
PARENT_CATEGORIES: List[Tuple[str, str]] = [
    ("17000", "Телефоны и планшеты"),
    ("5000", "Электроника"),
    ("16000", "Компьютерная техника"),
    ("15000", "Бытовая техника"),
    ("2000", "Авто и запчасти"),
    ("1000", "Недвижимость"),
    ("21000", "Мебель"),
    ("3000", "Все для дома"),
    ("14000", "Ремонт и стройка"),
    ("10000", "Сад и огород"),
    ("8000", "Женский гардероб"),
    ("19000", "Мужской гардероб"),
    ("12000", "Все для детей и мам"),
    ("18000", "Красота и здоровье"),
    ("4000", "Хобби, спорт и туризм"),
    ("11000", "Животные"),
    ("13000", "Услуги"),
    ("20000", "Готовый бизнес и оборудование"),
    ("9000", "Свадьба и праздники"),
    ("6000", "Работа"),
    ("25000", "Путешествия"),
    ("7000", "Прочее"),
]

# Fallback subcategory tree (used if Kufar HTML metadata temporarily rate-limits)
SUBCATEGORIES_FALLBACK: Dict[str, List[Tuple[str, str]]] = {
    "17000": [
        ("17010", "Мобильные телефоны"),
        ("17050", "Планшеты"),
        ("17090", "Умные часы и фитнес-браслеты"),
        ("17100", "Наушники"),
        ("17030", "Аксессуары для телефонов"),
        ("17020", "Комплектующие для телефонов"),
        ("17070", "Электронные книги"),
    ],
    "5000": [
        ("5040", "Игры и приставки"),
        ("5020", "Аудиотехника"),
        ("5100", "Наушники"),
        ("5060", "ТВ и видеотехника"),
        ("5070", "Фототехника и оптика"),
        ("5110", "Безопасность и умный дом"),
    ],
    "16000": [
        ("16040", "Ноутбуки"),
        ("16020", "Компьютеры"),
        ("16010", "Комплектующие"),
        ("16030", "Мониторы"),
        ("16060", "Периферия и аксессуары"),
        ("16050", "Оргтехника"),
        ("16070", "Сетевое оборудование"),
    ],
    "15000": [
        ("15020", "Крупная техника для кухни"),
        ("15040", "Стиральные машины"),
        ("15030", "Пылесосы"),
        ("15010", "Техника для кухни"),
        ("15050", "Климатическая техника"),
    ],
    "2000": [
        ("2010", "Легковые авто"),
        ("2040", "Запчасти"),
        ("2075", "Колеса и шины"),
        ("2030", "Мототехника"),
    ],
    "1000": [
        ("1010", "Квартиры"),
        ("1020", "Дома, дачи, коттеджи"),
        ("1040", "Комнаты"),
        ("1080", "Участки"),
        ("1030", "Гаражи и стоянки"),
        ("1050", "Коммерческая недвижимость"),
    ],
    "21000": [
        ("21070", "Диваны"),
        ("21040", "Кровати и матрасы"),
        ("21150", "Шкафы"),
        ("21120", "Столы"),
        ("21130", "Стулья"),
        ("21050", "Кухни"),
    ],
    "4000": [
        ("4050", "Велоспорт"),
        ("4160", "Электротранспорт"),
        ("4020", "Спорттовары"),
        ("4070", "Музыкальные инструменты"),
        ("4030", "Антиквариат и коллекции"),
    ],
}

# Build reverse category_id -> parent_id index from fallbacks (augmented dynamically at runtime)
CAT_TO_PARENT_CACHE: Dict[str, str] = {}
CAT_NAME_CACHE: Dict[str, str] = {cid: cname for cid, cname in PARENT_CATEGORIES}
for _prn, _subs in SUBCATEGORIES_FALLBACK.items():
    for _cid, _cname in _subs:
        CAT_TO_PARENT_CACHE[_cid] = _prn
        CAT_NAME_CACHE[_cid] = _cname

# Fields never shown as dynamic category subfilters (either handled separately or internal to Kufar)
EXCLUDED_FILTER_KEYS = {
    "prn", "cat", "rgn", "ar", "prc", "cur", "rtr", "b2c",
    "rbs", "rprc", "mlr", "mle", "thph", "season", "so",
    "en", "crn", "cnvi", "usn", "ea", "drn", "cn", "ca",
    "wsl", "il", "drd", "vn", "cp", "sa", "trd", "ed",
    "crde", "othp", "nme", "sg",
}

COMMON_FILTER_KEYS = {"cnd", "cmp"}
EXTRA_BOOL_KEYS = {"dle", "sde", "hlv", "oph", "ovi", "pse", "trnt"}
CASCADE_TRIGGER_KEYS = {"prn", "cat", "rgn", "pb", "cte", "cbc", "cb", "crb", "re_type", "tp"}
SCHEMA_PARAM_IGNORE = {"query", "prc", "sort", "size", "ar", "cnd", "cmp"} | EXTRA_BOOL_KEYS

_SCHEMA_CACHE: Dict[Tuple[Tuple[str, str], ...], Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 6 * 3600
_FETCH_LOCK = threading.Lock()
_LAST_FETCH_TS = 0.0


def find_parent_category(cat_id: str) -> str:
    """Returns parent category ID (prn) for a subcategory ID if known."""
    cid = str(cat_id or "").strip()
    if not cid or cid == "all":
        return ""
    if cid in CAT_TO_PARENT_CACHE:
        return CAT_TO_PARENT_CACHE[cid]
    # Top-level category IDs themselves match prn
    if any(p[0] == cid for p in PARENT_CATEGORIES):
        return cid
    # Heuristic prefix fallback: e.g. 17050 -> 17000, 5040 -> 5000, 21120 -> 21000
    if len(cid) >= 4 and cid.isdigit():
        candidate = str((int(cid) // 1000) * 1000)
        if any(p[0] == candidate for p in PARENT_CATEGORIES):
            return candidate
    return ""


def get_category_label(cat_id: str) -> str:
    """Returns human-readable Russian name for any cached category/subcategory ID."""
    return CAT_NAME_CACHE.get(str(cat_id or ""), "")


def _clean_schema_request_params(params: Dict[str, Any]) -> Dict[str, str]:
    clean: Dict[str, str] = {}
    for k, v in (params or {}).items():
        ks = str(k).strip()
        if not ks or ks in SCHEMA_PARAM_IGNORE or ks in EXCLUDED_FILTER_KEYS - {"prn", "cat", "rgn"}:
            # Allow prn, cat, rgn + category cascade triggers
            if ks not in ("prn", "cat", "rgn"):
                continue
        if v is None:
            continue
        vs = str(v).strip()
        if not vs or vs.lower() == "all":
            continue
        # Only forward relevant schema-defining keys to avoid noisy cache misses
        if ks in ("prn", "cat", "rgn") or ks in CASCADE_TRIGGER_KEYS or ks.endswith("_brand") or len(ks) <= 5:
            if ks in SCHEMA_PARAM_IGNORE:
                continue
            clean[ks] = vs
    if "cat" in clean and "prn" in clean and clean["cat"] == clean["prn"]:
        clean.pop("cat", None)
    return clean


def _parse_next_data_state(url: str) -> Optional[Dict[str, Any]]:
    global _LAST_FETCH_TS
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://www.kufar.by/",
        }
    )
    for attempt in range(2):
        with _FETCH_LOCK:
            wait = 0.28 - (time.time() - _LAST_FETCH_TS)
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    _LAST_FETCH_TS = time.time()
                    html_text = resp.read().decode("utf-8", errors="ignore")
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
                if not m:
                    return None
                nd = json.loads(m.group(1))
                return nd.get("props", {}).get("initialState")
            except urllib.error.HTTPError as e:
                _LAST_FETCH_TS = time.time()
                if e.code == 429 and attempt == 0:
                    time.sleep(1.2)
                    continue
                logger.warning(f"Kufar schema HTTP {e.code} for {url}")
                return None
            except Exception as e:
                _LAST_FETCH_TS = time.time()
                logger.warning(f"Kufar schema fetch error for {url}: {e}")
                return None
    return None


def _normalize_filter_item(f: Dict[str, Any], parent_refs: set) -> Optional[Dict[str, Any]]:
    key = f.get("url_name")
    if not key or key in EXCLUDED_FILTER_KEYS:
        return None
    ui_comp = f.get("ui_component")
    if not isinstance(ui_comp, dict) or not ui_comp.get("type"):
        return None

    comp_type = ui_comp["type"]
    labels = f.get("labels") or {}
    label_ru = ((labels.get("name") or {}).get("ru") or f.get("name") or key).strip()
    if not label_ru:
        return None

    raw_vals = f.get("values") or []
    options: List[Dict[str, str]] = []
    for item in raw_vals:
        val_id = str(item.get("value") if item.get("value") is not None else "")
        opt_lbl = ((item.get("labels") or {}).get("ru") or "").strip()
        if not val_id or not opt_lbl or opt_lbl == "-":
            continue
        options.append({"value": val_id, "label": opt_lbl})

    if comp_type in ("checkbox", "new_checkbox"):
        kind = "bool"
    elif comp_type == "select_chips":
        kind = "chips"
    elif comp_type in ("input_range", "select_range"):
        kind = "range"
    elif comp_type == "multiselect_or" or f.get("multi"):
        kind = "multiselect"
    else:
        kind = "select"

    # Skip empty dropdowns/checkboxes with no usable values
    if kind in ("select", "multiselect", "chips") and not options:
        return None
    if kind == "bool" and not any(o["value"] == "1" for o in options):
        # Some boolean flags use 1 for Yes; skip malformed empty ones
        if not raw_vals:
            return None

    if key in COMMON_FILTER_KEYS:
        group = "common"
    elif key in EXTRA_BOOL_KEYS or (kind == "bool" and f.get("name", "").startswith(("only_", "delivery_", "safedeal_", "halva", "possible_"))):
        group = "extra"
    else:
        group = "primary"

    reload_on_change = (
        key in CASCADE_TRIGGER_KEYS
        or key in parent_refs
        or (f.get("name") or "") in parent_refs
    )

    rng = f.get("range")
    range_info = None
    if isinstance(rng, dict):
        range_info = {
            "lower": rng.get("lower"),
            "upper": rng.get("upper"),
            "default_lower": rng.get("default_lower"),
            "default_upper": rng.get("default_upper"),
            "step": rng.get("step", 1),
        }

    return {
        "key": key,
        "name": f.get("name") or key,
        "label": label_ru,
        "kind": kind,
        "group": group,
        "reload_on_change": reload_on_change,
        "options": options[:250],
        "range": range_info,
    }


def _fallback_schema(req_params: Dict[str, str]) -> Dict[str, Any]:
    prn = req_params.get("prn") or find_parent_category(req_params.get("cat", ""))
    subs = [{"id": cid, "name": cname} for cid, cname in SUBCATEGORIES_FALLBACK.get(prn, [])]
    common = [
        {
            "key": "cnd",
            "name": "condition",
            "label": "Состояние",
            "kind": "chips",
            "group": "common",
            "reload_on_change": False,
            "options": [{"value": "2", "label": "Новое"}, {"value": "1", "label": "Б/у"}],
            "range": None,
        },
        {
            "key": "cmp",
            "name": "company_ad",
            "label": "Продавец",
            "kind": "chips",
            "group": "common",
            "reload_on_change": False,
            "options": [{"value": "0", "label": "Частное лицо"}, {"value": "1", "label": "Компания"}],
            "range": None,
        },
    ]
    return {
        "ok": True,
        "cached": False,
        "fallback": True,
        "prn": prn,
        "cat": req_params.get("cat", ""),
        "parent_categories": [{"id": pid, "name": pname} for pid, pname in PARENT_CATEGORIES],
        "subcategories": subs,
        "areas": [],
        "fields": common,
    }


def fetch_kufar_filter_schema(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns full normalized category tree & adaptive filter schema for the selected Kufar parameters.
    Supports parent category (prn), subcategory (cat), region (rgn -> areas), and cascade filters
    such as phones_brand -> phones_model/memory or consoles_type -> consoles_brand/bluray.
    """
    req_params = _clean_schema_request_params(params)
    # Auto-infer prn if only cat was supplied
    if "cat" in req_params and "prn" not in req_params:
        inferred_prn = find_parent_category(req_params["cat"])
        if inferred_prn:
            req_params["prn"] = inferred_prn

    cache_key = tuple(sorted(req_params.items()))
    now = time.time()
    cached_entry = _SCHEMA_CACHE.get(cache_key)
    if cached_entry and (now - cached_entry[0]) < _CACHE_TTL_SECONDS:
        payload = dict(cached_entry[1])
        payload["cached"] = True
        return payload

    # Build Kufar listing URL (avoid passing redundant prn alongside cat if cat uniquely identifies category)
    url_params: Dict[str, str] = {}
    for k, v in req_params.items():
        if k == "prn" and req_params.get("cat"):
            continue
        url_params[k] = v

    qs = urllib.parse.urlencode(url_params)
    url = f"https://www.kufar.by/l?{qs}" if qs else "https://www.kufar.by/l"
    state = _parse_next_data_state(url)
    if not state:
        return _fallback_schema(req_params)

    page_filters = (state.get("filters") or {}).get("pageFilters") or []
    menu_links = ((state.get("menus") or {}).get("menuTree") or {}).get("links") or []

    inferred_prn = req_params.get("prn", "")
    # Extract exact parent from Kufar breadcrumb menuTree if available
    for link in menu_links:
        q = (link.get("href") or {}).get("query") or {}
        if q.get("prn"):
            inferred_prn = str(q["prn"])
            lname = (link.get("labels") or {}).get("ru")
            if lname:
                CAT_NAME_CACHE[inferred_prn] = lname
        if q.get("cat"):
            cid = str(q["cat"])
            lname = (link.get("labels") or {}).get("ru")
            if lname:
                CAT_NAME_CACHE[cid] = lname
            if link.get("parent"):
                CAT_TO_PARENT_CACHE[cid] = str(link["parent"])

    parent_categories: List[Dict[str, str]] = []
    subcategories: List[Dict[str, str]] = []
    areas: List[Dict[str, str]] = []
    parent_refs: set = set()

    for f in page_filters:
        pn = f.get("parent_nested")
        if isinstance(pn, str) and pn:
            parent_refs.add(pn)
        elif isinstance(pn, list):
            for x in pn:
                if x:
                    parent_refs.add(str(x))

    raw_fields: List[Dict[str, Any]] = []
    for f in page_filters:
        un = f.get("url_name")
        if un == "prn":
            for v in (f.get("values") or []):
                vid = str(v.get("value") or "")
                lbl = ((v.get("labels") or {}).get("ru") or "").strip()
                if vid and lbl:
                    parent_categories.append({"id": vid, "name": lbl})
                    CAT_NAME_CACHE[vid] = lbl
        elif un == "category" or un == "cat":
            # Only treat category list as subcategories when a parent category or subcategory is selected
            if req_params.get("prn") or req_params.get("cat") or inferred_prn:
                for v in (f.get("values") or []):
                    vid = str(v.get("value") or "")
                    lbl = ((v.get("labels") or {}).get("ru") or "").strip()
                    if vid and lbl:
                        subcategories.append({"id": vid, "name": lbl})
                        CAT_NAME_CACHE[vid] = lbl
                        if inferred_prn:
                            CAT_TO_PARENT_CACHE[vid] = inferred_prn
        elif un == "ar":
            for v in (f.get("values") or []):
                vid = str(v.get("value") or "")
                lbl = ((v.get("labels") or {}).get("ru") or "").strip()
                if vid and lbl:
                    areas.append({"id": vid, "name": lbl})
        else:
            norm = _normalize_filter_item(f, parent_refs)
            if norm:
                raw_fields.append(norm)

    if not parent_categories:
        parent_categories = [{"id": pid, "name": pname} for pid, pname in PARENT_CATEGORIES]

    # Sort fields: primary (select/multiselect/range first, then specific bools), common (cnd/cmp), extra
    group_order = {"primary": 0, "common": 1, "extra": 2}
    kind_order = {"select": 0, "multiselect": 1, "range": 2, "chips": 3, "bool": 4}
    raw_fields.sort(key=lambda x: (group_order.get(x["group"], 3), kind_order.get(x["kind"], 5)))

    result = {
        "ok": True,
        "cached": False,
        "fallback": False,
        "prn": inferred_prn,
        "cat": req_params.get("cat", ""),
        "parent_categories": parent_categories,
        "subcategories": subcategories,
        "areas": areas,
        "fields": raw_fields,
    }
    _SCHEMA_CACHE[cache_key] = (now, result)
    return result
