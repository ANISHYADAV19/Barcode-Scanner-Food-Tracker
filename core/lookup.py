"""
Product lookup across seven data sources.

`lookup_product()` is a pure function: give it a barcode and a requests session and
it returns structured product data or None. It holds no threads and no state, so the
web API can call it directly per request while the desktop app wraps it in
`BarcodeLookupManager` for background, non-blocking lookups.

Cascade order (first hit wins):
  1.   Open Food Facts                                 -- full nutrition
  2.   Open Pet Food Facts                             -- full nutrition
  3.   Open Products Facts                             -- full nutrition (if present)
  4.   USDA FoodData Central                           -- full nutrition
"""

import html as html_parser
import os
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


def _try_usda_fdc(code, session):
    """
    Queries USDA FoodData Central search endpoint (using query=UPC).
    Requires a data.gov API key, falling back to public DEMO_KEY.
    """
    api_key = os.environ.get("USDA_API_KEY", "DEMO_KEY")
    url = f"https://api.nal.usda.gov/fdc/v1/foods/search?query={code}&pageSize=5&api_key={api_key}"
    response = session.get(url, timeout=API_TIMEOUT)
    if response.status_code != 200:
        return None

    data = response.json()
    foods = data.get("foods") or []
    if not foods:
        return None

    food = foods[0]
    description = (food.get("description") or "").strip()
    if not description or is_placeholder_name(description):
        return None

    brand = food.get("brandOwner") or food.get("brandName")
    return _result(code, description, brand, "USDA FoodData Central", nutrition.parse_usda_product(food))


def _build_stages(barcode):
    """Orders the cascade for this barcode."""
    return [
        ("Open Food Facts", lambda code, s: _try_off_family(code, s, "world.openfoodfacts.org", "Open Food Facts")),
        ("Open Pet Food Facts", lambda code, s: _try_off_family(code, s, "world.openpetfoodfacts.org", "Open Pet Food Facts")),
        ("Open Products Facts", lambda code, s: _try_off_family(code, s, "world.openproductsfacts.org", "Open Products Facts")),
        ("USDA FoodData Central", _try_usda_fdc),
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
