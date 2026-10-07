import argparse
import configparser
import csv
import json
import random
import re
import sqlite3
import textwrap
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

REQUIRED_KEYS = ["base_url", "wait_selector", "card", "title", "price"]


# ---------- config ----------
def load_config(path="selectors.ini", site="daraz"):
    if not Path(path).exists():
        raise SystemExit(f"Config file not found: {path}")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    if site not in cfg:
        raise SystemExit(f"Section [{site}] missing in {path}")
    sel = dict(cfg[site])
    missing = [k for k in REQUIRED_KEYS if not sel.get(k)]
    if missing:
        raise SystemExit(f"Missing keys in [{site}]: {', '.join(missing)}")
    return sel


# ---------- savers ----------
def save_json(products, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)


def save_csv(products, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=products[0].keys())
        writer.writeheader()
        writer.writerows(products)


def save_xlsx(products, path):
    pd.DataFrame(products).to_excel(path, index=False)


def save_xml(products, path):
    root = ET.Element("Products")
    for p in products:
        item = ET.SubElement(root, "Product")
        for key, value in p.items():
            ET.SubElement(item, key).text = "" if value is None else str(value)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def save_sqlite(products, path):
    with sqlite3.connect(path) as conn:
        pd.DataFrame(products).to_sql("products", conn, if_exists="replace", index=False)


SAVERS = {"json": save_json, "csv": save_csv, "xlsx": save_xlsx,
          "xml": save_xml, "sqlite": save_sqlite}


def save_all(products, base, formats):
    base = Path(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    for fmt in (f.strip().lower() for f in formats.split(",")):
        if fmt not in SAVERS:
            print(f"Unknown format '{fmt}', skipped")
            continue
        out = f"{base}.{'db' if fmt == 'sqlite' else fmt}"
        SAVERS[fmt](products, out)
        print(f"Saved {out}")


# ---------- parsing helpers ----------
def pick(parent, selector):
    """First match, or None if the selector is blank or nothing matches."""
    selector = (selector or "").strip()
    return parent.select_one(selector) if selector else None


def text_of(tag, default=""):
    return tag.get_text(strip=True) if tag else default


def get_image_url(img_tag, base_url):
    if img_tag is None:
        return None
    for attr in ("data-src", "data-lazy-src", "src"):
        url = img_tag.get(attr)
        if url and not url.startswith("data:"):
            return urljoin(base_url, url)
    return None


def clean_price(text):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    return float(m.group().replace(",", "")) if m else None


def clean_sold(text):
    m = re.search(r"([\d.,]+)\s*([KkMm]?)", text or "")
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(num * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))


def clean_discount(text):
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else 0


def extract_data(html, sel, seen, keyword):
    """Parse one page; return only products not already in `seen`."""
    products = []
    soup = BeautifulSoup(html, "html.parser")
    for div in soup.select(sel["card"]):
        title_tag = pick(div, sel["title"])
        if not title_tag:
            continue  # ad or broken card

        a = title_tag if title_tag.name == "a" else title_tag.find_parent("a")
        href = a.get("href", "") if a else ""
        link = urljoin(sel["base_url"], href) if href else ""

        key = link or text_of(title_tag)
        if key in seen:
            continue
        seen.add(key)

        sold_tag = pick(div, sel.get("sold"))
        sold_text = text_of(sold_tag.find("span") or sold_tag) if sold_tag else "0"

        products.append({
            "title": text_of(title_tag),
            "price": clean_price(text_of(pick(div, sel["price"]))),
            "discount_percent": clean_discount(text_of(pick(div, sel.get("discount")))),
            "sold": clean_sold(sold_text),
            "image": get_image_url(pick(div, sel.get("image")), sel["base_url"]),
            "url": link,
            "keyword": keyword,
            "scraped_at": datetime.now().isoformat(timespec="seconds"),
        })
    return products


# ---------- fetching ----------
def scroll_page(page, steps=8):
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, 1000)")
        page.wait_for_timeout(400)


def fetch_products(keyword, sel, pages="all", max_pages=50):
    all_products, seen = [], set()
    limit = max_pages if pages == "all" else int(pages)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for page_no in range(1, limit + 1):
            url = sel["base_url"].format(keyword=quote_plus(keyword), page=page_no)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector(sel["wait_selector"], timeout=30000)
            except PWTimeout:
                print(f"Page {page_no}: no products found (page title: {page.title()!r}). "
                      "The site may be blocking automation or the selectors changed.")
                break

            scroll_page(page)
            new = extract_data(page.content(), sel, seen, keyword)
            if not new:
                print(f"Page {page_no}: no new products, stopping")
                break

            all_products.extend(new)
            print(f"Page {page_no}: {len(new)} products ({len(all_products)} total)")
            time.sleep(random.uniform(3, 6))

        browser.close()
    return all_products


# ---------- main ----------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Daraz product web scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python3 daraz_scraper.py -k "laptops"                    # all pages (max 50)
              python3 daraz_scraper.py -k "laptops" -p 3               # first 3 pages
              python3 daraz_scraper.py -k "laptops" -o Output/laptops  # output name, no extension
              python3 daraz_scraper.py -k "laptops" -f json,xml        # formats (default json,csv)
              python3 daraz_scraper.py -k "laptops" -c selectors.ini   # config file path
        """))
    parser.add_argument("-k", "--keyword", help="Product to search")
    parser.add_argument("-p", "--page", default="all", help='Number of pages or "all"')
    parser.add_argument("-o", "--output", default="daraz_products", help="Output path (no extension)")
    parser.add_argument("-f", "--format", default="json,csv", help="csv,json,xlsx,xml,sqlite")
    parser.add_argument("-c", "--config", default="selectors.ini", help="Selectors config file")
    args = parser.parse_args()

    if not args.keyword:
        parser.print_help()
        raise SystemExit
    if args.page != "all" and not (args.page.isdigit() and int(args.page) > 0):
        parser.error("--page must be 'all' or a positive number")

    sel = load_config(args.config)
    products = fetch_products(args.keyword, sel, args.page)

    if not products:
        print("No products found. Check the selectors or run with headless=False.")
    else:
        print(f"Collected {len(products)} products")
        save_all(products, args.output, args.format)