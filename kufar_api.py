"""
Kufar API client and helper functions.
Supports direct API searching, parameter parsing, and parsing Kufar website URLs.
"""
import urllib.request
import urllib.parse
import json
import re
import logging
from typing import Dict, List, Optional, Any

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
        if "brand" in params:
            b_val = params["brand"].lower()
            params["brand"] = b_val.capitalize()
            if b_val in CONSOLES_BRAND_MAP:
                params["cbc"] = CONSOLES_BRAND_MAP[b_val]
        
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
                continue

            # 3. Check for category slug in path (e.g. pristavki-i-igry, pristavki, noutbuki, mebel)
            if part_lower in CATEGORY_SLUGS_MAP and "cat" not in params:
                params["cat"] = CATEGORY_SLUGS_MAP[part_lower]
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


def fetch_ads(params: Dict[str, Any], timeout: int = 10) -> List[Dict[str, Any]]:
    """
    Fetches listings matching params from Kufar API.
    Returns a cleaned list of ads.
    """
    query_params = dict(params)
    if "sort" not in query_params:
        query_params["sort"] = "lst.d"
    if "size" not in query_params:
        query_params["size"] = "30"

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
                return []
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Error fetching Kufar ads: {e}")
        return []

    ads_raw = payload.get("ads", [])
    results = []

    for ad in ads_raw:
        ad_id = ad.get("ad_id")
        if not ad_id:
            continue

        subject = ad.get("subject", "Без названия")
        ad_link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")
        list_time = ad.get("list_time", "")

        # Price parsing (kopecks to BYN / USD)
        price_byn_raw = ad.get("price_byn")
        price_usd_raw = ad.get("price_usd")
        
        price_byn = None
        price_usd = None
        if price_byn_raw and str(price_byn_raw).isdigit():
            price_byn = round(int(price_byn_raw) / 100, 2)
        if price_usd_raw and str(price_usd_raw).isdigit():
            price_usd = round(int(price_usd_raw) / 100, 2)

        # Region & Area extraction from ad_parameters
        region_name = ""
        area_name = ""
        for p in ad.get("ad_parameters", []):
            if p.get("p") == "region":
                region_name = p.get("vl", "")
            elif p.get("p") == "area":
                area_name = p.get("vl", "")

        # Seller name from account_parameters
        seller_name = ""
        for p in ad.get("account_parameters", []):
            if p.get("p") == "name":
                seller_name = p.get("v", "").strip()

        # Image URLs (up to 10 images)
        images = ad.get("images", [])
        image_urls = []
        if images and isinstance(images, list):
            for img in images[:10]:
                path = img.get("path")
                if path:
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

    return results


def fetch_ad_description(ad_id: int, timeout: int = 5) -> str:
    """Fetches the full item description from Kufar item page."""
    url = f"https://www.kufar.by/item/{ad_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.kufar.by/"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html_text = r.read().decode("utf-8", errors="ignore")

        # Extract description from Next.js payload
        m = re.search(r'\"id\":\"?' + str(ad_id) + r'\"?.*?\"description\":(\"[^\"]*\")', html_text)
        if not m:
            m = re.search(r'\"description\":(\"[^\"]*\")', html_text)
        if m:
            return json.loads(m.group(1)).strip()
    except Exception as e:
        logger.debug(f"Could not fetch description for {ad_id}: {e}")
    return ""


def fetch_ad_photos(ad_id: int, timeout: int = 5) -> List[str]:
    """Fetches full list of photo URLs from Kufar item page."""
    url = f"https://www.kufar.by/item/{ad_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.kufar.by/"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html_text = r.read().decode("utf-8", errors="ignore")
        paths = re.findall(r'\"path\":\"([^\"]+?\.(?:jpe?g|png|webp))\"', html_text)
        if paths:
            seen = set()
            urls = []
            for p in paths:
                if p not in seen:
                    seen.add(p)
                    urls.append(f"{IMAGE_BASE_URL}/{p}")
            return urls[:10]
    except Exception as e:
        logger.debug(f"Could not fetch photos for {ad_id}: {e}")
    return []


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
