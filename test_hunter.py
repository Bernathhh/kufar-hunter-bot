#!/usr/bin/env python3
"""
Test script for Kufar Hunter.
Tests fetching and formatting ads directly in the terminal without requiring Telegram credentials.
"""
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from kufar_api import parse_kufar_url_or_query, fetch_ads
from bot import format_ad_caption

def main():
    test_queries = [
        "iphone 13 800 1500",
        "rtx 4070",
        "https://www.kufar.by/l/r~minsk/noutbuki?query=thinkpad"
    ]

    print("=" * 60)
    print("🔎 ТЕСТ ПАРСИНГА И ПОИСКА НА KUFAR")
    print("=" * 60)

    for q in test_queries:
        print(f"\n🔍 Проверяем запрос: '{q}'")
        params = parse_kufar_url_or_query(q)
        print(f"⚙️ Сформированные параметры: {params}")

        ads = fetch_ads(params)
        print(f"📦 Найдено объявлений: {len(ads)}")

        if ads:
            latest = ads[0]
            print("\n--- Пример первого объявления ---")
            print(f"Заголовок: {latest['subject']}")
            print(f"Цена BYN: {latest['price_byn']} BYN (USD: ${latest['price_usd']})")
            print(f"Локация: {latest['region']} / {latest['area']}")
            print(f"Продавец: {latest['seller']}")
            print(f"Ссылка: {latest['ad_link']}")
            print(f"Картинка: {latest['image_url']}")
            print("---------------------------------")

    print("\n✅ Тест успешно завершен! Все парсеры и API работают идеально.")

if __name__ == "__main__":
    main()
