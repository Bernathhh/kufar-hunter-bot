"""
Kufar API client and helper functions.
Supports direct API searching, parameter parsing, and parsing Kufar website URLs.
"""
import urllib.request
import urllib.parse
import json
import re
import logging
import statistics
import time
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("kufar_api")

API_SEARCH_URL = "https://cre-api.kufar.by/ads-search/v1/engine/v1/search/rendered-paginated"
IMAGE_BASE_URL = "https://rms.kufar.by/v1/gallery"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

REGIONS_MAP = {
    "brest": "1",
    "brest-obl": "1",
    "brestskaya-oblast": "1",
    "gomel": "2",
    "gomel-obl": "2",
    "gomelskaya-oblast": "2",
    "grodno": "3",
    "grodno-obl": "3",
    "grodnenskaya-oblast": "3",
    "mogilev": "4",
    "mogilev-obl": "4",
    "mogilevskaya-oblast": "4",
    "minsk-obl": "5",
    "minskaya-oblast": "5",
    "vitebsk": "6",
    "vitebsk-obl": "6",
    "vitebskaya-oblast": "6",
    "minsk": "7",
}

CATEGORY_SLUGS_MAP = {
    # Gaming & Consoles (5040)
    "igry-i-pristavki": "5040",
    "pristavki-i-igry": "5040",
    "pristavki": "5040",
    "igrovye-pristavki": "5040",
    "igry-dlya-pristavok": "5040",
    "konsoli": "5040",
    "gejmpady-i-aksessuary": "5040",

    # Electronics (5000)
    "elektronika": "5000",
    "audiotehnika": "5020",
    "audio-tehnika": "5020",
    "akustika-i-kolonki": "5020",
    "tv-i-videotehnika": "5060",
    "tv-i-video": "5060",
    "televizory": "5060",
    "fototehnika-i-optika": "5070",
    "fotoapparaty": "5070",

    # Computers & Laptops (16000)
    "kompyuternaya-tehnika": "16000",
    "noutbuki": "16040",
    "kompyutery": "16020",
    "kompyutery-sistemnye-bloki": "16020",
    "sistemnye-bloki": "16020",
    "monitory": "16030",
    "komplektuyushchie": "16010",
    "videokarty": "16010",
    "processory": "16010",
    "materinskie-platy": "16010",
    "nakopiteli-ssd-hdd": "16010",
    "periferiya-i-aksessuary": "16060",
    "klaviatury-i-myshi": "16060",
    "orgtehnika": "16050",
    "printery-i-mfo": "16050",
    "setevoe-oborudovanie": "16070",

    # Phones & Tablets (17000)
    "telefony-i-planshety": "17000",
    "mobilnye-telefony": "17010",
    "smartfony": "17010",
    "planshety": "17050",
    "umnye-chasy": "17090",
    "umnye-chasy-i-fitnes-braslety": "17090",
    "naushniki": "17100",
    "aksessuary-dlya-telefonov": "17030",
    "komplektuyushchie-dlya-telefonov": "17020",
    "elektronnye-knigi": "17070",

    # Appliances (15000)
    "bytovaya-tehnika": "15000",
    "tehnika-dlya-kuhni": "15010",
    "krupnaya-tehnika-dlya-kuhni": "15020",
    "holodilniki": "15020",
    "stiralnye-mashiny": "15040",
    "pylesosy": "15030",
    "klimaticheskaya-tehnika": "15050",

    # Auto & Transport (2000)
    "avto-i-zapchasti": "2000",
    "avto": "2000",
    "legkovye-avto": "2010",
    "avtomobili": "2010",
    "zapchasti": "2040",
    "mototehnika": "2030",
    "motocikly": "2030",
    "shiny-diski": "2075",
    "kolesa-i-shiny": "2075",

    # Real Estate (1000)
    "nedvizhimost": "1000",
    "kvartiry": "1010",
    "snjat-kvartiru": "1010",
    "kupit-kvartiru": "1010",
    "komnaty": "1040",
    "doma-dachi-kottedzhi": "1020",
    "garazhi-i-stoyanki": "1030",
    "uchastki": "1080",
    "kommercheskaya-nedvizhimost": "1050",

    # Furniture (21000)
    "mebel": "21000",
    "stoly": "21120",
    "stoly-i-obedennye-gruppy": "21120",
    "stulya": "21130",
    "divany": "21070",
    "myagkaya-mebel": "21070",
    "shkafy": "21150",
    "krovati-matrasy": "21040",
    "kuhni": "21050",
    "komody": "21030",

    # Clothing & Wardrobe (8000, 19000)
    "zhenskij-garderob": "8000",
    "muzhskoj-garderob": "19000",
    "zhenskaya-odezhda": "8080",
    "muzhskaya-odezhda": "19010",
    "zhenskaya-obuv": "8100",
    "muzhskaya-obuv": "19020",

    # Sports & Hobbies (4000)
    "hobbi-sport-i-turizm": "4000",
    "sporttovary": "4020",
    "velosport": "4050",
    "velosipedy": "4050",
    "elektrotransport": "4160",
    "samokaty": "4160",
    "antikvariat": "4030",
    "muzykalnye-instrumenty": "4070",

    # Renovation & Tools (14000)
    "remont-i-strojka": "14000",
    "stroitelnyj-instrument": "14010",
    "elektroinstrument": "14010",

    # Kids (12000)
    "vse-dlya-detej-i-mam": "12000",
    "detskie-kolyaski": "12040",
    "detskaya-odezhda": "12010",

    # Animals (11000)
    "zhivotnye": "11000",
    "sobaki": "11010",
    "koshki": "11010",

    # Services (13000)
    "uslugi": "13000",
    "arenda-i-prokat": "13300",
}

CONSOLES_BRAND_MAP = {
    "sony": "31",
    "microsoft": "13",
    "xbox": "13",
    "nintendo": "17",
    "steam-deck": "30",
    "steamdeck": "30",
    "sega": "27",
    "anbernic": "36",
    "lenovo": "14",
    "msi": "16",
    "defender": "1",
}

PHONES_BRAND_MAP = {
    "apple": "5",
    "iphone": "5",
    "samsung": "24",
    "xiaomi": "35",
    "redmi": "35",
    "huawei": "11",
    "honor": "12",
    "google": "9",
    "pixel": "9",
    "oneplus": "20",
    "poco": "35",
    "realme": "22",
}

# Internal bot keys that must NEVER be passed in the HTTP query string to Kufar search API
# (Kufar strictly returns HTTP 422 Unprocessable Entity if unrecognized query parameters are sent)
BOT_INTERNAL_KEYS = {
    "brand",
    "name",
    "chat_id",
    "id",
    "qid",
    "initial_ads",
    "is_active",
    "created_at",
    "updated_at",
    "seen_count",
    "user_id",
    "min_price",
    "max_price",
    "stop_words",
    "plus_words",
    "only_bargain_pct",
    "private_only",
    "silent_query",
    "meta",
    "ad_id",
    "category_name",
    "region_name",
}


def clean_kufar_api_params(params: Dict[str, Any]) -> Dict[str, str]:
    """
    Cleans parameters dictionary before sending it to Kufar Search API.
    Strips internal bot metadata fields that Kufar's API strictly rejects with HTTP 422.
    """
    clean: Dict[str, str] = {}
    for k, v in params.items():
        if k in BOT_INTERNAL_KEYS or str(k).startswith("_"):
            continue
        if v is None:
            continue
        val_str = str(v)
        if not val_str.strip() and k != "query":
            continue
        if k in ("cat", "rgn") and val_str.strip().lower() == "all":
            continue
        clean[str(k)] = val_str
    return clean

def parse_kufar_url_or_query(input_str: str) -> Dict[str, Any]:
    """
    Parses either a full Kufar URL (e.g. https://www.kufar.by/l/r~minsk/pristavki-i-igry?prc=r:150000,220000)
    or a natural search query string with optional price range (e.g. 'iphone 13 800 1500', 'ps5 1500 2200').
    """
    input_str = input_str.strip()
    
    # Check if this is a URL
    if input_str.startswith("http://") or input_str.startswith("https://") or "kufar.by" in input_str:
        if not input_str.startswith("http"):
            input_str = "https://" + input_str
        
        parsed = urllib.parse.urlparse(input_str)
        query_dict = urllib.parse.parse_qs(parsed.query)
        
        # Flatten query parameters and filter out web-only analytics/tracking params
        IGNORE_PARAMS = {"r_pagetype", "searchid", "block_name", "page", "rank", "total_rank", "elementtype"}
        params = {}
        for k, v in query_dict.items():
            k_lower = k.lower()
            if k_lower.startswith("utm_") or k_lower.startswith("r_") or k_lower in IGNORE_PARAMS:
                continue
            params[k] = v[0]

        # Alias query parameters
        if "q" in params and "query" not in params:
            params["query"] = params.pop("q")
        if "ot" in params and "query" not in params:
            params["query"] = params.pop("ot")
        if "search" in params and "query" not in params:
            params["query"] = params.pop("search")
        if "category" in params and "cat" not in params:
            params["cat"] = params.pop("category")
        if "pr" in params and "brand" not in params:
            b_val = params.pop("pr").lower()
            params["brand"] = b_val.capitalize()
            if b_val in CONSOLES_BRAND_MAP:
                params["cbc"] = CONSOLES_BRAND_MAP[b_val]
            elif b_val in PHONES_BRAND_MAP:
                params["pb"] = PHONES_BRAND_MAP[b_val]
        if "brand" in params:
            b_val = params["brand"].lower()
            params["brand"] = b_val.capitalize()
            if b_val in CONSOLES_BRAND_MAP:
                params["cbc"] = CONSOLES_BRAND_MAP[b_val]
            elif b_val in PHONES_BRAND_MAP:
                params["pb"] = PHONES_BRAND_MAP[b_val]
        
        # Parse path for region, category, and brand slugs
        # e.g. /l/r~minsk/pristavki/pr~sony or /l/minsk/noutbuki
        path_parts = [p for p in parsed.path.split("/") if p and p != "l"]
        for part in path_parts:
            part_lower = part.lower().strip()

            # 1. Check for region prefix or exact region name (e.g. r~minsk or minsk)
            region_slug = part_lower[2:] if part_lower.startswith("r~") else part_lower
            if region_slug in REGIONS_MAP and "rgn" not in params:
                params["rgn"] = REGIONS_MAP[region_slug]
                continue

            # 2. Check for brand / manufacturer in path (e.g. pr~sony, mt~apple, cm~asus)
            if part_lower.startswith("pr~") or part_lower.startswith("mt~") or part_lower.startswith("cm~"):
                brand_slug = part_lower[3:].strip()
                brand_name = brand_slug.title()
                if brand_slug in ("sony", "xbox", "msi", "hp", "bmw"):
                    brand_name = brand_slug.upper() if brand_slug in ("msi", "hp", "bmw") else brand_slug.capitalize()
                params["brand"] = brand_name
                
                # If this is consoles category or slug has consoles brands:
                if brand_slug in CONSOLES_BRAND_MAP:
                    params["cbc"] = CONSOLES_BRAND_MAP[brand_slug]
                elif brand_slug in PHONES_BRAND_MAP:
                    params["pb"] = PHONES_BRAND_MAP[brand_slug]
                continue

            # 3. Check for category slug in path (e.g. pristavki-i-igry, pristavki, noutbuki, mebel)
            if part_lower in CATEGORY_SLUGS_MAP and "cat" not in params:
                params["cat"] = CATEGORY_SLUGS_MAP[part_lower]
                if part_lower in ("pristavki", "igrovye-pristavki", "konsoli") and "cte" not in params:
                    params["cte"] = "1"
                elif part_lower == "igry-dlya-pristavok" and "cte" not in params:
                    params["cte"] = "2"
                elif part_lower == "gejmpady-i-aksessuary" and "cte" not in params:
                    params["cte"] = "3"
                continue

            # 4. Check for SEO landing URL, e.g. /l/kupit-playstation-5
            if part_lower.startswith("kupit-") and "query" not in params:
                raw_kw = part_lower[len("kupit-"):].replace("-", " ")
                # Strip out common noise like 'pristavku' if followed by brand
                cleaned_kw = re.sub(r'^(?:pristavku|noutbuk|telefon|avtomobil)\s+', '', raw_kw, flags=re.IGNORECASE)
                params["query"] = cleaned_kw.strip() if cleaned_kw.strip() else raw_kw.strip()
                continue
        
        # Ensure newest first and default page size
        if "sort" not in params:
            params["sort"] = "lst.d"
        if "size" not in params:
            params["size"] = "30"
            
        return params

    # Otherwise, it's text. Smart price parser to avoid false positives on model numbers (e.g. 'rtx 4070', 'iphone 13')
    text = input_str
    min_price = None
    max_price = None

    # Check for 'от 500' / 'до 1500'
    m_ot = re.search(r'\b(?:от|min)\s*[:=]?\s*(\d+)\b', text, re.I)
    if m_ot:
        min_price = int(m_ot.group(1))
        text = text[:m_ot.start()] + " " + text[m_ot.end():]

    m_do = re.search(r'\b(?:до|max)\s*[:=]?\s*(\d+)\b', text, re.I)
    if m_do:
        max_price = int(m_do.group(1))
        text = text[:m_do.start()] + " " + text[m_do.end():]

    # Check for '500-1500' or '500..1500'
    m_range = re.search(r'\b(\d+)\s*(?:-|\.\.)\s*(\d+)\b', text)
    if m_range and min_price is None and max_price is None:
        min_price = int(m_range.group(1))
        max_price = int(m_range.group(2))
        text = text[:m_range.start()] + " " + text[m_range.end():]

    # Check for two trailing numbers: 'iphone 13 800 1500'
    if min_price is None and max_price is None:
        parts = text.split()
        if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
            min_price = int(parts[-2])
            max_price = int(parts[-1])
            text = " ".join(parts[:-2])

    query_str = " ".join(text.split())
    params = {
        "query": query_str,
        "sort": "lst.d",
        "size": "30"
    }

    # Convert prices to kopecks (x100) for Kufar's API
    if min_price is not None and max_price is not None:
        params["prc"] = f"r:{min_price * 100},{max_price * 100}"
    elif min_price is not None:
        params["prc"] = f"r:{min_price * 100},999999999"
    elif max_price is not None:
        params["prc"] = f"r:0,{max_price * 100}"

    return params


_MARKET_CACHE: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
_ACCESSORY_CATS = {"17020", "17030", "16060", "2040", "13300"}
_NOISE_RE = re.compile(
    r"(?:аренд[аеуы]|прокат\b|наклейк|чехол|чехл|стекл[оа]|коробк[аеуи]|"
    r"подставк|кабель|зарядк|скупк|выкуп|ремонт\b|обменяю)"
)


def fetch_ads_with_total(params: Dict[str, Any], timeout: int = 10) -> Tuple[List[Dict[str, Any]], int]:
    """Fetches listings from Kufar Search API along with total matching count reported by Kufar."""
    query_params = clean_kufar_api_params(params)
    if "sort" not in query_params:
        query_params["sort"] = "lst.d"
    if "size" not in query_params:
        query_params["size"] = "30"
    query_params.setdefault("lang", "ru")

    encoded_query = urllib.parse.urlencode(query_params)
    full_url = f"{API_SEARCH_URL}?{encoded_query}"

    req = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://www.kufar.by/"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                logger.error(f"Kufar API error status: {response.status}")
                return [], 0
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.error(f"Error fetching Kufar ads: HTTP Error {e.code}: {e.reason} (URL: {full_url})")
        return [], 0
    except Exception as e:
        logger.error(f"Error fetching Kufar ads: {e} (URL: {full_url})")
        return [], 0

    ads_raw = payload.get("ads", [])
    total_reported = int(payload.get("total") or len(ads_raw))
    results = []

    for ad in ads_raw:
        ad_id = ad.get("ad_id")
        if not ad_id:
            continue

        subject = ad.get("subject", "Без названия")
        ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
        list_time = ad.get("list_time", "")

        price_byn_raw = ad.get("price_byn")
        price_usd_raw = ad.get("price_usd")

        price_byn = None
        price_usd = None
        if price_byn_raw and str(price_byn_raw).isdigit():
            price_byn = round(int(price_byn_raw) / 100, 2)
        if price_usd_raw and str(price_usd_raw).isdigit():
            price_usd = round(int(price_usd_raw) / 100, 2)

        region_name = ""
        area_name = ""
        for p in ad.get("ad_parameters", []):
            if p.get("p") == "region":
                region_name = p.get("vl", "")
            elif p.get("p") == "area":
                area_name = p.get("vl", "")

        seller_name = ""
        for p in ad.get("account_parameters", []):
            if p.get("p") == "name":
                seller_name = p.get("v", "").strip()

        images = ad.get("images", [])
        image_urls = []
        seen_paths = set()
        if images and isinstance(images, list):
            for img in images[:20]:
                path = img.get("path")
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    image_urls.append(f"{IMAGE_BASE_URL}/{path}")

        image_url = image_urls[0] if image_urls else None
        phone_hidden = ad.get("phone_hidden", True)

        results.append({
            "ad_id": ad_id,
            "subject": subject,
            "ad_link": ad_link,
            "price_byn": price_byn,
            "price_usd": price_usd,
            "list_time": list_time,
            "region": region_name,
            "area": area_name,
            "seller": seller_name,
            "image_url": image_url,
            "image_urls": image_urls,
            "phone_hidden": phone_hidden,
            "company_ad": ad.get("company_ad", False),
            "account_id": ad.get("account_id")
        })

    return results, total_reported


def fetch_ads(params: Dict[str, Any], timeout: int = 10) -> List[Dict[str, Any]]:
    """
    Fetches listings matching params from Kufar API.
    Returns a cleaned list of ads.
    """
    ads, _ = fetch_ads_with_total(params, timeout=timeout)
    return ads


def compute_market_analysis(
    params: Dict[str, Any],
    user_stop_words: Optional[List[str]] = None,
    local_stop_words: Optional[List[str]] = None,
    exclude_company: bool = False,
    ttl_seconds: int = 300,
) -> Dict[str, Any]:
    """
    Analyzes the FULL market price distribution across Kufar for the same item/category filters
    WITHOUT the user's min/max price ceiling (prc), so expensive/normal market listings (e.g. 2300/2500 BYN)
    are properly accounted for when evaluating bargains and computing medians.
    """
    market_params = clean_kufar_api_params(params)
    user_prc = market_params.pop("prc", None)
    market_params["size"] = "100"
    market_params["sort"] = "lst.d"

    user_min: Optional[float] = None
    user_max: Optional[float] = None
    if user_prc and str(user_prc).startswith("r:"):
        parts = str(user_prc)[2:].split(",")
        if len(parts) == 2:
            try:
                user_min = float(parts[0]) / 100.0 if parts[0] not in ("", "0") else None
                user_max = float(parts[1]) / 100.0 if parts[1] not in ("", "0", "999999999") else None
            except Exception:
                pass

    all_stops = sorted({
        str(w).strip().lower()
        for w in (list(user_stop_words or []) + list(local_stop_words or []))
        if str(w).strip()
    })
    cache_key = (
        tuple(sorted(market_params.items())),
        tuple(all_stops),
        bool(exclude_company),
    )
    now = time.time()
    cached = _MARKET_CACHE.get(cache_key)
    if cached and (now - cached[0]) < ttl_seconds:
        res = dict(cached[1])
        if user_max is not None and res.get("_prices"):
            res["above_budget_count"] = sum(1 for p in res["_prices"] if p > user_max)
        else:
            res["above_budget_count"] = 0
        res.pop("_prices", None)
        return res

    ads, total_on_kufar = fetch_ads_with_total(market_params)
    cat_id = str(market_params.get("cat", ""))
    cte_id = str(market_params.get("cte", ""))
    kw_lower = str(market_params.get("query") or "").lower()
    is_accessory_search = (
        cat_id in _ACCESSORY_CATS
        or cte_id in ("2", "3", "6")
        or bool(_NOISE_RE.search(kw_lower))
    )

    candidate_prices: List[float] = []
    for ad in ads:
        if exclude_company and ad.get("company_ad"):
            continue
        p = ad.get("price_byn")
        if p is None or p <= 0:
            continue
        subj = (ad.get("subject") or "").lower()
        if all_stops and any(sw in subj for sw in all_stops):
            continue
        if not is_accessory_search and _NOISE_RE.search(subj):
            continue
        candidate_prices.append(float(p))

    # Light outlier filter for tiny accessories/cables if query-based high-ticket search wasn't narrowed by cte/subcat
    clean_prices = list(candidate_prices)
    if len(candidate_prices) >= 6 and not is_accessory_search:
        s = sorted(candidate_prices)
        q75 = s[int(0.75 * (len(s) - 1))]
        if q75 >= 250:
            floor = q75 * 0.22
            filtered = [p for p in s if p >= floor]
            if len(filtered) >= 3:
                clean_prices = filtered

    if not clean_prices:
        return {
            "count": 0,
            "total_on_kufar": total_on_kufar,
            "min": None,
            "median": None,
            "avg": None,
            "max": None,
            "p25": None,
            "p75": None,
            "above_budget_count": 0,
            "unbounded": bool(user_prc),
            "buckets": [],
        }

    s_prices = sorted(clean_prices)
    n = len(s_prices)
    med = round(float(statistics.median(s_prices)), 2)
    avg = round(float(sum(s_prices) / n), 2)
    p25 = round(float(s_prices[max(0, int(0.25 * (n - 1)))]), 2)
    p75 = round(float(s_prices[min(n - 1, int(0.75 * (n - 1)))]), 2)
    mn = round(float(s_prices[0]), 2)
    mx = round(float(s_prices[-1]), 2)

    # Build 4 clear price range buckets
    buckets: List[Dict[str, Any]] = []
    if n >= 4 and mx > mn:
        step_raw = (mx - mn) / 4.0
        # Round step cleanly (to nearest 10/50/100 depending on magnitude)
        if step_raw >= 100:
            step = max(50, round(step_raw / 50) * 50)
        elif step_raw >= 20:
            step = max(10, round(step_raw / 10) * 10)
        else:
            step = max(1, round(step_raw))
        start = int(mn // step) * step if step >= 10 else int(mn)
        edges = [start, start + step, start + step * 2, start + step * 3, int(mx) + 1]
        for idx in range(4):
            lo, hi = edges[idx], edges[idx + 1]
            if idx < 3:
                cnt = sum(1 for p in s_prices if lo <= p < hi)
                disp_lo = max(int(mn), int(lo)) if idx == 0 else int(lo)
                lbl = f"{disp_lo:,}–{int(hi):,}".replace(",", " ")
            else:
                cnt = sum(1 for p in s_prices if p >= lo)
                lbl = f"от {int(lo):,}".replace(",", " ")
            if cnt > 0:
                buckets.append({"label": lbl + " BYN", "count": cnt, "pct": round((cnt / n) * 100)})

    payload = {
        "count": n,
        "total_on_kufar": total_on_kufar,
        "min": mn,
        "median": med,
        "avg": avg,
        "max": mx,
        "p25": p25,
        "p75": p75,
        "unbounded": bool(user_prc),
        "buckets": buckets,
        "_prices": s_prices,
    }
    _MARKET_CACHE[cache_key] = (now, dict(payload))
    if len(_MARKET_CACHE) > 200:
        oldest = sorted(_MARKET_CACHE.items(), key=lambda kv: kv[1][0])[:50]
        for k, _ in oldest:
            _MARKET_CACHE.pop(k, None)

    out = dict(payload)
    out["above_budget_count"] = sum(1 for p in s_prices if user_max is not None and p > user_max)
    out.pop("_prices", None)
    return out


def fetch_ad_details(ad_id: int, timeout: int = 6) -> tuple:
    """
    Fetches full (description, gallery_image_urls) from a Kufar item page in one request
    using Next.js __NEXT_DATA__ payload, with safe escaped-string fallback.
    """
    url = f"https://www.kufar.by/item/{ad_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.kufar.by/"
        }
    )
    description = ""
    gallery_urls: List[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html_text = r.read().decode("utf-8", errors="ignore")

        m_next = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
        if m_next:
            try:
                nd = json.loads(m_next.group(1))
                ad_view = nd.get("props", {}).get("initialState", {}).get("adView", {}).get("data", {})
                if isinstance(ad_view, dict):
                    raw_desc = ad_view.get("description")
                    if isinstance(raw_desc, str):
                        description = raw_desc.strip()
                    imgs = ad_view.get("images", {})
                    raw_list = imgs.get("gallery", []) if isinstance(imgs, dict) else (imgs if isinstance(imgs, list) else [])
                    seen = set()
                    for item in raw_list[:20]:
                        u = item if isinstance(item, str) else (item.get("url") or (f"{IMAGE_BASE_URL}/{item['path']}" if isinstance(item, dict) and item.get("path") else None))
                        if u and u not in seen:
                            seen.add(u)
                            gallery_urls.append(u)
            except Exception as e:
                logger.debug(f"__NEXT_DATA__ parse error for {ad_id}: {e}")

        if not description:
            m = re.search(r'\"id\":\"?' + str(ad_id) + r'\"?.*?\"description\":(\"(?:\\.|[^\"\\])*\")', html_text)
            if not m:
                m = re.search(r'\"description\":(\"(?:\\.|[^\"\\])*\")', html_text)
            if m:
                description = json.loads(m.group(1)).strip()
    except Exception as e:
        logger.debug(f"Could not fetch details for {ad_id}: {e}")
    return description, gallery_urls


def fetch_ad_description(ad_id: int, timeout: int = 5) -> str:
    """Fetches the full item description from Kufar item page."""
    desc, _ = fetch_ad_details(ad_id, timeout=timeout)
    return desc


def fetch_ad_photos(ad_id: int, timeout: int = 5) -> List[str]:
    """Fetches full list of photo URLs from Kufar item page."""
    _, photos = fetch_ad_details(ad_id, timeout=timeout)
    return photos


def fetch_ad_live_state(ad_id: int, timeout: int = 6) -> Dict[str, Any]:
    """
    Checks live status and current price of an ad on Kufar.
    Returns dict with keys: checked (bool), closed (bool), price_byn, price_usd.
    Never marks closed=True on transient network timeouts.
    """
    url = f"https://www.kufar.by/item/{ad_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.kufar.by/",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final_url = r.geturl() or ""
            if "/item/" not in final_url:
                return {"checked": True, "closed": True, "price_byn": None, "price_usd": None}
            html_text = r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return {"checked": True, "closed": True, "price_byn": None, "price_usd": None}
        return {"checked": False, "closed": False, "price_byn": None, "price_usd": None}
    except Exception:
        return {"checked": False, "closed": False, "price_byn": None, "price_usd": None}

    price_byn: Optional[float] = None
    price_usd: Optional[float] = None
    m_next = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if m_next:
        try:
            nd = json.loads(m_next.group(1))
            ad_view = nd.get("props", {}).get("initialState", {}).get("adView", {}).get("data", {})
            if not ad_view or ad_view.get("is_active") is False or ad_view.get("status") in ("removed", "sold", "inactive"):
                return {"checked": True, "closed": True, "price_byn": None, "price_usd": None}
            pb = ad_view.get("price_byn")
            pu = ad_view.get("price_usd")
            if pb and str(pb).isdigit():
                price_byn = round(int(pb) / 100, 2)
            if pu and str(pu).isdigit():
                price_usd = round(int(pu) / 100, 2)
        except Exception:
            pass

    if "Объявление не найдено" in html_text or "Объявление удалено" in html_text:
        return {"checked": True, "closed": True, "price_byn": None, "price_usd": None}

    return {"checked": True, "closed": False, "price_byn": price_byn, "price_usd": price_usd}


def extract_phone_from_text(text: str) -> Optional[str]:
    """Finds Belarusian phone numbers in text."""
    if not text:
        return None
    # Matches: +375 29 123-45-67, 80291234567, 375 (33) 123 45 67
    pattern = r'(?:\+?375|80)\s*\(?(?:25|29|33|44|17)\)?\s*\d{3}[-\s]?\d{2}[-\s]?\d{2}'
    m = re.search(pattern, text)
    if m:
        return m.group(0).strip()
    return None


def fetch_ad_phone_with_token(ad_id: int, bearer_token: str, timeout: int = 5) -> Optional[str]:
    """Fetches phone from Kufar API if authorized user token is provided."""
    if not bearer_token:
        return None
    url = f"https://cre-api.kufar.by/ads-search/v1/engine/v1/item/{ad_id}/phone"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("phone")
    except Exception:
        return None
