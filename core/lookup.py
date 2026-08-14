"""
Product lookup across seven data sources.

`lookup_product()` is a pure function: give it a barcode and a requests session and
it returns structured product data or None. It holds no threads and no state, so the
web API can call it directly per request while the desktop app wraps it in
`BarcodeLookupManager` for background, non-blocking lookups.

Cascade order (first hit wins):
  1-4. Open Food / Beauty / Pet Food / Products Facts  -- full nutrition
  5.   Open Library (ISBN)                             -- name only
  6.   UPCitemdb (general retail)                      -- name only
  7.   DuckDuckGo HTML search                          -- name only, best effort
"""

import html as html_parser
import queue
import re
import threading
from datetime import datetime

import requests

from core import nutrition

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)

# Open Food Facts database for retail food products.
OFF_FAMILY = (
    ("world.openfoodfacts.org", "Open Food Facts"),
)

API_TIMEOUT = 3
SEARCH_TIMEOUT = 4

# Search results whose titles look like barcode-lookup directories rather than
# actual products. Without this the web fallback "resolves" every unknown code to
# a database landing page.
IGNORE_KEYWORDS = (
    "barcode lookup", "upc lookup", "ean lookup", "barcode search",
    "upc search", "ean search", "barcode database", "product database",
    "search by barcode", "what is this barcode", "barcode detail", "lookup barcode",
    "barcode locator", "ean-db",
)

RETAILER_SUFFIXES = (
    " - eBay", " | eBay", " - Amazon", " | Amazon", " - Walmart", " | Walmart",
    " - Flipkart", " | Flipkart", " - BigBasket", " | BigBasket",
)

# Crowd-sourced placeholder names to reject outright, so a junk entry does not stop
# the cascade from reaching a source that actually knows the product.
PLACEHOLDER_NAMES = frozenset({
    "test", "testing", "tests", "unknown", "unnamed", "n/a", "na", "none", "null",
    "product", "produit", "-", "--", "?", "??", "x", "xx", "xxx",
})


def is_placeholder_name(name):
    """True when a returned product name is obvious junk rather than a real name."""
    if not name:
        return True
    cleaned = name.strip().lower()
    return len(cleaned) < 2 or cleaned in PLACEHOLDER_NAMES


def make_session():
    """Builds a requests session with the browser User-Agent the APIs expect."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def barcode_variants(barcode):
    """
    Yields all logical barcode forms worth trying to maximize database hits.
    
    Normalizes between UPC (12 digits), EAN-13 (13 digits), EAN-8 (8 digits),
    and GTIN-14 (14 digits) zero-padding formats.
    """
    cleaned = re.sub(r'\s+', '', barcode)
    if not cleaned:
        return []
        
    variants = [cleaned]
    
    # 12-digit UPC conversions
    if len(cleaned) == 12:
        variants.append(f"0{cleaned}")   # To EAN-13 (1 leading zero)
        variants.append(f"00{cleaned}")  # To GTIN-14 (2 leading zeros)
        
    # 13-digit EAN conversions
    elif len(cleaned) == 13:
        variants.append(f"0{cleaned}")   # To GTIN-14 (1 leading zero)
        if cleaned.startswith("0"):
            variants.append(cleaned[1:])  # Strip leading zero to check 12-digit UPC
            
    # 8-digit EAN conversions
    elif len(cleaned) == 8:
        variants.append(f"00000{cleaned}") # Zero-padded to EAN-13
        variants.append(f"0000{cleaned}")  # Zero-padded to UPC-12
        
    # Deduplicate while preserving lookup order
    seen = set()
    ordered_variants = []
    for var in variants:
        if var not in seen:
            seen.add(var)
            ordered_variants.append(var)
            
    return ordered_variants


def _compose(name, brand):
    """Formats the single-line label the desktop HUD shows."""
    if brand and brand.strip():
        return f"{name} ({brand.strip()})"
    return name


def _result(barcode, name, brand, source, extra=None):
    """Builds the structured lookup result, defaulting every nutrition column to None."""
    record = {
        "barcode": barcode,
        "name": name,
        "brand": (brand or "").strip() or None,
        "source": source,
        "display": _compose(name, brand),
        "quantity_label": None,
        "categories": None,
        "image_url": None,
        "serving_size": None,
        "nutriscore": None,
        "nova_group": None,
        "allergens": None,
    }
    for column in nutrition.NUTRIMENT_KEYS:
        record[column] = None

    if extra:
        record.update({k: v for k, v in extra.items() if k in record})

    record["has_nutrition"] = nutrition.has_nutrition(record)
    return record


def _try_off_family(code, session, host, label):
    """
    Queries one Open*Facts host.

    All four share an identical response envelope, so this one function replaces
    what used to be four copy-pasted stages -- and means nutrition parsing exists
    in exactly one place.
    """
    url = f"https://{host}/api/v2/product/{code}.json?fields={nutrition.OFF_FIELDS}"
    response = session.get(url, timeout=API_TIMEOUT)
    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("status") != 1:
        return None

    product = data.get("product") or {}
    name = nutrition.display_name(product)
    if not name or is_placeholder_name(name):
        return None

    return _result(code, name, product.get("brands"), label, nutrition.parse_off_product(product))





def _try_upcitemdb(code, session):
    """Queries the UPCitemdb trial endpoint (rate-limited, no key required)."""
    url = f"https://api.upcitemdb.com/prod/trial/lookup?upc={code}"
    response = session.get(url, timeout=API_TIMEOUT)
    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("code") != "OK" or not data.get("total"):
        return None

    items = data.get("items") or []
    if not items:
        return None

    item = items[0]
    title = (item.get("title") or "").strip()
    if not title:
        return None

    images = item.get("images") or []
    return _result(code, title, item.get("brand"), "UPCitemdb",
                   {"image_url": images[0] if images else None})


def _try_web_search(code, session):
    """
    Last-resort DuckDuckGo HTML scrape for regional or niche items.

    Best effort by nature: this parses result markup, so a layout change upstream
    makes it quietly stop finding names rather than fail loudly.
    """
    response = session.get(f"https://html.duckduckgo.com/html/?q={code}", timeout=SEARCH_TIMEOUT)
    if response.status_code != 200:
        return None

    titles = re.findall(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', response.text, re.DOTALL)
    snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', response.text, re.DOTALL)

    def strip_markup(raw):
        return html_parser.unescape(re.sub(r'<[^>]+>', '', raw).strip())

    candidates = []
    for raw_title, raw_snippet in zip(titles, snippets):
        title = strip_markup(raw_title)
        snippet = strip_markup(raw_snippet)

        if any(keyword in title.lower() for keyword in IGNORE_KEYWORDS):
            continue

        for suffix in RETAILER_SUFFIXES:
            if title.lower().endswith(suffix.lower()):
                title = title[:-len(suffix)]
        title = re.sub(r'\s+', ' ', title).strip()

        # Results that quote the barcode itself are far likelier to be the product
        if code in title or code in snippet:
            candidates.append((0, title))
        elif len(title) > 5 and not title.replace('.', '', 1).isdigit():
            candidates.append((1, title))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: pair[0])
    return _result(code, candidates[0][1], None, "Web Search (DDG)")


def _build_stages(barcode):
    """Orders the cascade for this barcode."""
    return [
        ("Open Food Facts", lambda code, s: _try_off_family(code, s, "world.openfoodfacts.org", "Open Food Facts")),
        ("UPCitemdb", _try_upcitemdb),
        ("Web Search (DDG)", _try_web_search),
    ]


def lookup_product(barcode, session=None, trace=False):
    """
    Resolves a barcode to structured product data, or None if no source knows it.

    Tries every barcode variant against every stage in cascade order. Each stage is
    isolated: a timeout or malformed response drops to the next source rather than
    failing the whole lookup.
    """
    session = session or make_session()
    stages = _build_stages(barcode)

    for code in barcode_variants(barcode):
        for label, stage in stages:
            try:
                result = stage(code, session)
            except Exception as exc:
                if trace:
                    print(f"[Lookup Trace] {label} error for {code}: {exc}")
                continue

            if result:
                # Report against the barcode that was scanned, not the variant that hit
                result["barcode"] = barcode
                return result

    return None


class BarcodeLookupManager:
    """
    Background lookup queue for the desktop app.

    Keeps the camera loop responsive: `request_lookup` returns immediately and a
    worker thread does the network calls, so the UI never blocks on HTTP. Results
    are cached in memory for the life of the process and appended to a log file.
    """

    def __init__(self, log_filename="scanned_products.txt"):
        self.log_filename = log_filename
        self.lookup_queue = queue.Queue()
        self.cache = {}
        self.lock = threading.Lock()

        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

    def request_lookup(self, barcode):
        """Queues a background lookup unless this barcode is already known."""
        with self.lock:
            if barcode not in self.cache:
                self.cache[barcode] = {
                    "status": "pending",
                    "name": "Looking up...",
                    "record": None,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.lookup_queue.put(barcode)

    def get_status(self, barcode):
        """Gets the lookup status and display name for a barcode."""
        with self.lock:
            return self.cache.get(barcode, {"status": "unknown", "name": "", "record": None})

    def _worker_loop(self):
        session = make_session()
        while True:
            barcode = self.lookup_queue.get()
            if barcode is None:
                break

            record = lookup_product(barcode, session, trace=True)
            if record:
                self._store(barcode, "found", record["display"], record)
            else:
                self._store(barcode, "not_found", "Product Not Found", None)

            self.lookup_queue.task_done()

    def _store(self, barcode, status, name, record):
        """Thread-safely updates the cache and appends to the scan log."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.cache[barcode] = {
                "status": status,
                "name": name,
                "record": record,
                "timestamp": timestamp,
            }

        source = record["source"] if record else "None"
        entry = (f"[{timestamp}] Barcode: {barcode} | Status: {status.upper()} | "
                 f"DbSource: {source} | Product: {name}\n")

        try:
            with open(self.log_filename, "a", encoding="utf-8") as handle:
                handle.write(entry)
            print(f"[Log Saved] {entry.strip()}")
        except IOError as exc:
            print(f"[Error] Failed to write log to file: {exc}")

    def shutdown(self):
        """Stops the worker thread."""
        self.lookup_queue.put(None)
        self.worker_thread.join(timeout=2)
