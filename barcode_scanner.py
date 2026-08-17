import cv2
import numpy as np
import requests
import threading
import queue
import time
import re
import html as html_parser
from datetime import datetime

# --- Scanner viewfinder configuration ---
# Only the region inside the centred rectangle is decoded. A small, explicit
# search window makes aiming obvious for the user, keeps background clutter out
# of the decoder, and lets each frame be processed at a higher effective zoom.
ROI_W_RATIO = 0.70            # scan window width as a fraction of the frame
ROI_H_RATIO = 0.34            # scan window height as a fraction of the frame
ROI_MIN_W, ROI_MAX_W = 0.25, 0.96
ROI_MIN_H, ROI_MAX_H = 0.12, 0.90
DIM_ALPHA = 0.35              # brightness of the area outside the scan window
RESULT_HOLD_SECONDS = 6.0     # how long the last hit stays on the result bar
# Scale ladder for the decode pipeline, as target image widths in pixels.
# Counter-intuitively, OpenCV's BarcodeDetector reads a 1D symbol most reliably when
# it is only a couple of hundred pixels wide, and *fails* on the same symbol rendered
# larger - so the shrinking rungs come first and are what rescue a product held close
# enough to fill the scan window. The final enlarging rung is for small or distant
# codes, and is what pyzbar prefers when it is available.
DECODE_SCALE_WIDTHS = (620, 420, 300, 220, 160, 1000)
CAPTURE_WIDTH, CAPTURE_HEIGHT = 1280, 720
TOP_BANNER_H = 30             # height of the controls bar
BOTTOM_BANNER_H = 34          # height of the result bar


# Define MockDecoded structure for OpenCV fallback matching pyzbar interface
class MockDecoded:
    class Point:
        def __init__(self, x, y):
            self.x = int(x)
            self.y = int(y)

    def __init__(self, data_str, points):
        self.data = data_str.encode('utf-8')
        self.polygon = [self.Point(pt[0], pt[1]) for pt in points] if points is not None else []
        
        if points is not None and len(points) > 0:
            xs = [pt[0] for pt in points]
            ys = [pt[1] for pt in points]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            self.rect = (int(min_x), int(min_y), int(max_x - min_x), int(max_y - min_y))
        else:
            self.rect = (0, 0, 0, 0)

# Try importing pyzbar
PYZBAR_AVAILABLE = False
try:
    import os
    import sys
    if sys.platform == 'win32' and sys.version_info >= (3, 8):
        import importlib.util
        spec = importlib.util.find_spec('pyzbar')
        if spec and spec.submodule_search_locations:
            pyzbar_dir = spec.submodule_search_locations[0]
            if os.path.isdir(pyzbar_dir):
                os.add_dll_directory(pyzbar_dir)
                
    from pyzbar import pyzbar
    PYZBAR_AVAILABLE = True
except Exception as e:
    print(f"\n[Warning] Pyzbar library load failed: {e}")
    print("Falling back to OpenCV's built-in BarcodeDetector and QRCodeDetector.")
    print("For full pyzbar functionality, please ensure Microsoft Visual C++ 2013 Redistributable is installed.")
    print("-------------------------------------------------------------------------------------------------\n")


def is_valid_ean13(code):
    """Validates EAN-13 barcode checksum digit."""
    if not code.isdigit() or len(code) != 13:
        return False
    digits = [int(d) for d in code]
    odd_sum = sum(digits[i] for i in range(0, 12, 2))
    even_sum = sum(digits[i] for i in range(1, 12, 2))
    total = odd_sum + 3 * even_sum
    check_digit = (10 - (total % 10)) % 10
    return check_digit == digits[12]


def is_valid_upc(code):
    """Validates UPC-A (12-digit) barcode checksum digit."""
    if not code.isdigit() or len(code) != 12:
        return False
    digits = [int(d) for d in code]
    odd_sum = sum(digits[i] for i in range(0, 11, 2))
    even_sum = sum(digits[i] for i in range(1, 11, 2))
    total = 3 * odd_sum + even_sum
    check_digit = (10 - (total % 10)) % 10
    return check_digit == digits[11]


def get_roi_rect(frame_shape, w_ratio=ROI_W_RATIO, h_ratio=ROI_H_RATIO):
    """Returns the centred scan window as (x, y, w, h) for a given frame shape."""
    frame_h, frame_w = frame_shape[:2]
    roi_w = max(16, int(frame_w * w_ratio))
    roi_h = max(16, int(frame_h * h_ratio))
    roi_x = (frame_w - roi_w) // 2
    roi_y = (frame_h - roi_h) // 2
    return roi_x, roi_y, roi_w, roi_h


def _to_mutable(decoded):
    """
    Normalises any decoder result into a mutable MockDecoded.

    pyzbar returns immutable namedtuples, so its coordinates cannot be shifted or
    rescaled in place. Converting up front lets the ROI offset and upscale passes
    map their coordinates back onto the full camera frame.
    """
    if isinstance(decoded, MockDecoded):
        return decoded

    data_str = decoded.data.decode("utf-8", errors="replace")
    polygon = getattr(decoded, "polygon", None)

    if polygon:
        points = [(pt.x, pt.y) for pt in polygon]
    else:
        rect = decoded.rect
        left, top, width, height = rect[0], rect[1], rect[2], rect[3]
        points = [
            (left, top),
            (left + width, top),
            (left + width, top + height),
            (left, top + height),
        ]

    return MockDecoded(data_str, points)


def _remap_barcodes(barcodes, scale=1.0, offset_x=0, offset_y=0):
    """Maps decoded coordinates back onto the original frame (undo upscale, then add ROI offset)."""
    for barcode in barcodes:
        for pt in barcode.polygon:
            pt.x = int(pt.x / scale) + offset_x
            pt.y = int(pt.y / scale) + offset_y

        rx, ry, rw, rh = barcode.rect
        barcode.rect = (
            int(rx / scale) + offset_x,
            int(ry / scale) + offset_y,
            int(rw / scale),
            int(rh / scale),
        )
    return barcodes


def _ensure_detectors():
    """Lazily constructs the OpenCV detectors, tolerating builds without the barcode module."""
    if not hasattr(_ensure_detectors, "barcode"):
        try:
            _ensure_detectors.barcode = cv2.barcode.BarcodeDetector()
        except Exception:
            _ensure_detectors.barcode = None
        try:
            _ensure_detectors.qrcode = cv2.QRCodeDetector()
        except Exception:
            _ensure_detectors.qrcode = None
    return _ensure_detectors.barcode, _ensure_detectors.qrcode


def _decode_pyzbar(image_gray):
    """Decodes with pyzbar (handles 1D and QR in one call). Returns [] on any failure."""
    try:
        return [_to_mutable(d) for d in pyzbar.decode(image_gray)]
    except Exception:
        return []


def _decode_opencv_1d(image_gray):
    """
    Decodes 1D barcodes with OpenCV's BarcodeDetector.

    Cheap enough (a few milliseconds at any size) to run on every candidate image.
    Checksums are enforced here because this detector will happily return a
    corrupt read, and a wrong 13-digit code looks up a wrong product.
    """
    detector, _ = _ensure_detectors()
    if detector is None:
        return []

    decoded_codes = []
    try:
        retval, decoded_info, points, _ = detector.detectAndDecodeMulti(image_gray)
        if retval:
            if len(points.shape) == 2:
                points = np.expand_dims(points, axis=0)
            for info, pts in zip(decoded_info, points):
                info_stripped = info.strip()
                if not info_stripped:
                    continue
                if info_stripped.isdigit():
                    if len(info_stripped) == 13 and not is_valid_ean13(info_stripped):
                        continue  # Discard corrupt EAN-13 scan
                    if len(info_stripped) == 12 and not is_valid_upc(info_stripped):
                        continue  # Discard corrupt UPC-A scan
                decoded_codes.append(MockDecoded(info_stripped, pts))
    except Exception:
        pass
    return decoded_codes


def _decode_opencv_qr(image_gray):
    """Decodes QR codes with OpenCV's QRCodeDetector (no checksum needed - QR is self-correcting)."""
    _, detector = _ensure_detectors()
    if detector is None:
        return []

    decoded_codes = []
    try:
        retval, decoded_info, points, _ = detector.detectAndDecodeMulti(image_gray)
        if retval:
            if len(points.shape) == 2:
                points = np.expand_dims(points, axis=0)
            for info, pts in zip(decoded_info, points):
                if info.strip():
                    decoded_codes.append(MockDecoded(info.strip(), pts))
    except Exception:
        pass
    return decoded_codes


def _run_decode_on_image(image_gray):
    """Runs every available decoder over a single grayscale image."""
    if PYZBAR_AVAILABLE:
        found = _decode_pyzbar(image_gray)
        if found:
            return found
    return _decode_opencv_1d(image_gray) + _decode_opencv_qr(image_gray)


def _decode_candidates(gray):
    """
    Builds the (image, scale) variants to attempt, cheapest and most likely first.

    Every variant is prepared up front rather than lazily: thresholding, sharpening
    and resizing all cost well under a millisecond, which is negligible next to the
    detector calls that consume them.
    """
    candidates = [(gray, 1.0)]

    # Contrast fix for uneven lighting
    try:
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        candidates.append((thresh, 1.0))
    except Exception:
        pass

    # Sharpening fix for motion blur
    try:
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
        candidates.append((cv2.filter2D(gray, -1, kernel), 1.0))
    except Exception:
        pass

    # Scale ladder. See DECODE_SCALE_WIDTHS: the shrinking rungs come first because
    # they are what rescue a barcode held close enough to fill the scan window.
    width = float(gray.shape[1])
    for target in DECODE_SCALE_WIDTHS:
        scale = target / width
        if abs(scale - 1.0) < 0.08 or not (0.12 <= scale <= 3.0):
            continue  # too close to the native pass above, or an absurd resize
        try:
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            resized = cv2.resize(gray, (0, 0), fx=scale, fy=scale, interpolation=interp)
            candidates.append((resized, scale))
        except Exception:
            pass

    return candidates


def _decode_multipass(gray, tick=None):
    """
    Runs the decode pipeline over a grayscale image, returning at the first read.
    Coordinates come back in the coordinate space of the image passed in.

    Optimized using lazy candidate evaluation and frame staggering:
    - Original grayscale image is always checked on every frame.
    - Heavy operations (threshold/sharpening and various resizes) are staggered
      across alternating frames when a tick is provided.
    - If tick is None (e.g. for static tests), all candidates are processed.
    """
    # 1. Always try the original grayscale image first (fastest and most likely)
    found = _decode_pyzbar(gray) if PYZBAR_AVAILABLE else _decode_opencv_1d(gray)
    if found:
        return found

    # 2. Try threshold (even ticks or None) or sharpening (odd ticks or None)
    if tick is None or tick % 2 == 0:
        try:
            _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            found = _decode_pyzbar(thresh) if PYZBAR_AVAILABLE else _decode_opencv_1d(thresh)
            if found:
                return found
        except Exception:
            pass

    if tick is None or tick % 2 == 1:
        try:
            kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
            sharpened = cv2.filter2D(gray, -1, kernel)
            found = _decode_pyzbar(sharpened) if PYZBAR_AVAILABLE else _decode_opencv_1d(sharpened)
            if found:
                return found
        except Exception:
            pass

    # 3. Try scale ladder (staggered to reduce CPU load when no code is present)
    width = float(gray.shape[1])
    if tick is None:
        scales_to_try = DECODE_SCALE_WIDTHS
    elif tick % 2 == 0:
        scales_to_try = (620, 300, 160)
    else:
        scales_to_try = (420, 220, 1000)

    for target in scales_to_try:
        scale = target / width
        if abs(scale - 1.0) < 0.08 or not (0.12 <= scale <= 3.0):
            continue  # too close to the native pass, or an absurd resize
        try:
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            resized = cv2.resize(gray, (0, 0), fx=scale, fy=scale, interpolation=interp)
            found = _decode_pyzbar(resized) if PYZBAR_AVAILABLE else _decode_opencv_1d(resized)
            if found:
                return _remap_barcodes(found, scale=scale)
        except Exception:
            pass

    # 4. If using OpenCV fallback, check QR code on original gray frame
    if not PYZBAR_AVAILABLE:
        found = _decode_opencv_qr(gray)
        if found:
            return found

    return []


def decode_frame(frame, roi=None, tick=None):
    """
    Decodes barcodes/QR codes from a frame.

    When `roi` is given as (x, y, w, h), only that scan window is decoded and the
    results are shifted back into full-frame coordinates. Cropping keeps background
    clutter out of the decoder and makes every pass cheaper, so the scan window can
    be processed at a higher zoom than the whole frame could afford.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    if roi is None:
        return _decode_multipass(gray, tick=tick)

    frame_h, frame_w = gray.shape[:2]
    roi_x, roi_y, roi_w, roi_h = roi
    roi_x = max(0, min(roi_x, frame_w - 1))
    roi_y = max(0, min(roi_y, frame_h - 1))
    roi_w = max(1, min(roi_w, frame_w - roi_x))
    roi_h = max(1, min(roi_h, frame_h - roi_y))

    cropped = gray[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
    if cropped.size == 0:
        return []

    barcodes = _decode_multipass(cropped, tick=tick)
    return _remap_barcodes(barcodes, offset_x=roi_x, offset_y=roi_y)


class BarcodeLookupManager:
    def __init__(self, log_filename="scanned_products.txt"):
        self.log_filename = log_filename
        self.lookup_queue = queue.Queue()
        self.cache = {}  # Format: {barcode: {"status": str, "name": str, "timestamp": str}}
        self.lock = threading.Lock()
        
        # Start worker thread
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

    def request_lookup(self, barcode):
        """Requests background lookup of a barcode if it is not already in the cache."""
        with self.lock:
            if barcode not in self.cache:
                self.cache[barcode] = {
                    "status": "pending",
                    "name": "Looking up...",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                self.lookup_queue.put(barcode)

    def get_status(self, barcode):
        """Gets the lookup status and product name for a barcode."""
        with self.lock:
            return self.cache.get(barcode, {"status": "unknown", "name": ""})

    def _worker_loop(self):
        """Worker thread loop that fetches product data across multiple databases and web search fallbacks."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        })

        while True:
            barcode = self.lookup_queue.get()
            if barcode is None:
                break

            product_name = None
            source_database = None

            # Generate barcode variants to maximize lookup success (e.g. pad UPC to EAN-13, or unpad EAN-13 to UPC)
            barcodes_to_try = [barcode]
            if len(barcode) == 12:
                barcodes_to_try.append(f"0{barcode}")  # Try padded UPC
            elif len(barcode) == 13 and barcode.startswith("0"):
                barcodes_to_try.append(barcode[1:])    # Try stripped EAN

            # Query databases sequentially for each barcode variant
            for code_variant in barcodes_to_try:
                if product_name:
                    break

                # --- STAGE 1: Try Open Food Facts API (Groceries & Food products) ---
                try:
                    url = f"https://world.openfoodfacts.org/api/v2/product/{code_variant}.json?fields=product_name,brands,generic_name"
                    response = session.get(url, timeout=3)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("status") == 1:
                            product = data.get("product", {})
                            prod_name = product.get("product_name") or product.get("generic_name") or product.get("product_name_en")
                            if prod_name and prod_name.strip():
                                brand = product.get("brands")
                                product_name = f"{prod_name.strip()} ({brand.strip()})" if brand and brand.strip() else prod_name.strip()
                                source_database = "Open Food Facts"
                                break
                except Exception as e:
                    print(f"[Lookup Trace] Open Food Facts error for {code_variant}: {e}")

                # --- STAGE 2: Try Open Beauty Facts API (Cosmetics & Personal Care) ---
                if not product_name:
                    try:
                        url = f"https://world.openbeautyfacts.org/api/v2/product/{code_variant}.json?fields=product_name,brands,generic_name"
                        response = session.get(url, timeout=3)
                        if response.status_code == 200:
                            data = response.json()
                            if data.get("status") == 1:
                                product = data.get("product", {})
                                prod_name = product.get("product_name") or product.get("generic_name") or product.get("product_name_en")
                                if prod_name and prod_name.strip():
                                    brand = product.get("brands")
                                    product_name = f"{prod_name.strip()} ({brand.strip()})" if brand and brand.strip() else prod_name.strip()
                                    source_database = "Open Beauty Facts"
                                    break
                    except Exception as e:
                        print(f"[Lookup Trace] Open Beauty Facts error for {code_variant}: {e}")

                # --- STAGE 3: Try Open Pet Food Facts API (Pet Foods & Supplies) ---
                if not product_name:
                    try:
                        url = f"https://world.openpetfoodfacts.org/api/v2/product/{code_variant}.json?fields=product_name,brands,generic_name"
                        response = session.get(url, timeout=3)
                        if response.status_code == 200:
                            data = response.json()
                            if data.get("status") == 1:
                                product = data.get("product", {})
                                prod_name = product.get("product_name") or product.get("generic_name") or product.get("product_name_en")
                                if prod_name and prod_name.strip():
                                    brand = product.get("brands")
                                    product_name = f"{prod_name.strip()} ({brand.strip()})" if brand and brand.strip() else prod_name.strip()
                                    source_database = "Open Pet Food Facts"
                                    break
                    except Exception as e:
                        print(f"[Lookup Trace] Open Pet Food Facts error for {code_variant}: {e}")

                # --- STAGE 4: Try Open Products Facts API (Miscellaneous consumer goods) ---
                if not product_name:
                    try:
                        url = f"https://world.openproductsfacts.org/api/v2/product/{code_variant}.json?fields=product_name,brands,generic_name"
                        response = session.get(url, timeout=3)
                        if response.status_code == 200:
                            data = response.json()
                            if data.get("status") == 1:
                                product = data.get("product", {})
                                prod_name = product.get("product_name") or product.get("generic_name") or product.get("product_name_en")
                                if prod_name and prod_name.strip():
                                    brand = product.get("brands")
                                    product_name = f"{prod_name.strip()} ({brand.strip()})" if brand and brand.strip() else prod_name.strip()
                                    source_database = "Open Products Facts"
                                    break
                    except Exception as e:
                        print(f"[Lookup Trace] Open Products Facts error for {code_variant}: {e}")

                # --- STAGE 5: Try Open Library API (Books, ISBN barcode lookup) ---
                if not product_name:
                    try:
                        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{code_variant}&format=json&jscmd=data"
                        response = session.get(url, timeout=3)
                        if response.status_code == 200:
                            data = response.json()
                            key = f"ISBN:{code_variant}"
                            if key in data:
                                book = data[key]
                                title = book.get("title")
                                if title:
                                    authors_list = book.get("authors", [])
                                    authors = ", ".join(a.get("name") for a in authors_list if a.get("name"))
                                    product_name = f"{title} by {authors}" if authors else title
                                    source_database = "Open Library"
                                    break
                    except Exception as e:
                        print(f"[Lookup Trace] Open Library error for {code_variant}: {e}")

                # --- STAGE 6: Try UPCitemdb Trial API (General retail products: Electronics, Retail, Apparel) ---
                if not product_name:
                    try:
                        url = f"https://api.upcitemdb.com/prod/trial/lookup?upc={code_variant}"
                        response = session.get(url, timeout=3)
                        if response.status_code == 200:
                            data = response.json()
                            if data.get("code") == "OK" and data.get("total", 0) > 0:
                                items = data.get("items", [])
                                if items:
                                    item = items[0]
                                    title = item.get("title")
                                    brand = item.get("brand")
                                    if title and title.strip():
                                        product_name = f"{title.strip()} ({brand.strip()})" if brand and brand.strip() else title.strip()
                                        source_database = "UPCitemdb"
                                        break
                    except Exception as e:
                        print(f"[Lookup Trace] UPCitemdb error for {code_variant}: {e}")

                # --- STAGE 7: Try DuckDuckGo HTML Search Scraper (Global fallback for any indexed barcode) ---
                if not product_name:
                    try:
                        url = f"https://html.duckduckgo.com/html/?q={code_variant}"
                        response = session.get(url, timeout=4)
                        if response.status_code == 200:
                            titles = re.findall(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', response.text, re.DOTALL)
                            snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', response.text, re.DOTALL)
                            
                            ignore_keywords = [
                                "barcode lookup", "upc lookup", "ean lookup", "barcode search", 
                                "upc search", "ean search", "barcode database", "product database",
                                "search by barcode", "what is this barcode", "barcode detail", "lookup barcode",
                                "barcode locator", "ean-db"
                            ]
                            
                            candidates = []
                            
                            for title, snippet in zip(titles, snippets):
                                clean_title = re.sub(r'<[^>]+>', '', title).strip()
                                clean_title = html_parser.unescape(clean_title)
                                
                                clean_snippet = re.sub(r'<[^>]+>', '', snippet).strip()
                                clean_snippet = html_parser.unescape(clean_snippet)
                                
                                title_lower = clean_title.lower()
                                if any(kw in title_lower for kw in ignore_keywords):
                                    continue
                                    
                                suffixes_to_remove = [
                                    " - eBay", " | eBay", " - Amazon", " | Amazon", " - Walmart", " | Walmart",
                                    " - Flipkart", " | Flipkart", " - BigBasket", " | BigBasket"
                                ]
                                for suffix in suffixes_to_remove:
                                    if clean_title.endswith(suffix):
                                        clean_title = clean_title[:-len(suffix)]
                                    elif clean_title.lower().endswith(suffix.lower()):
                                        clean_title = clean_title[:-len(suffix)]
                                        
                                clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                                
                                if code_variant in clean_title or code_variant in clean_snippet:
                                    candidates.append((0, clean_title))
                                elif len(clean_title) > 5 and not clean_title.replace('.', '', 1).isdigit():
                                    candidates.append((1, clean_title))
                                    
                            if candidates:
                                candidates.sort(key=lambda x: x[0])
                                product_name = candidates[0][1]
                                source_database = "Web Search (DDG)"
                                break
                    except Exception as e:
                        print(f"[Lookup Trace] DuckDuckGo fallback error for {code_variant}: {e}")

            # --- Final Status Allocation ---
            if product_name:
                self._update_cache_and_log(barcode, "found", product_name, source_database)
            else:
                self._update_cache_and_log(barcode, "not_found", "Product Not Found", "None")

            self.lookup_queue.task_done()

    def _update_cache_and_log(self, barcode, status, name, source):
        """Helper to thread-safely update cache and write scanner log file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.cache[barcode] = {
                "status": status,
                "name": name,
                "timestamp": timestamp
            }
        
        log_entry = f"[{timestamp}] Barcode: {barcode} | Status: {status.upper()} | DbSource: {source} | Product: {name}\n"
        
        try:
            with open(self.log_filename, "a", encoding="utf-8") as f:
                f.write(log_entry)
            print(f"[Log Saved] {log_entry.strip()}")
        except IOError as e:
            print(f"[Error] Failed to write log to file: {e}")

    def shutdown(self):
        """Stops the worker thread."""
        self.lookup_queue.put(None)
        self.worker_thread.join(timeout=2)


STATE_COLORS = {
    "idle": (210, 210, 210),     # Light grey
    "pending": (0, 255, 255),    # Yellow
    "found": (0, 255, 0),        # Green
    "not_found": (0, 0, 255),    # Red
    "failed": (0, 165, 255),     # Orange
    "unknown": (255, 255, 255),  # White
}


def draw_barcode_overlay(frame, barcode, status_info):
    """Draws a custom overlay around the scanned barcode and displays the product name."""
    status = status_info.get("status", "unknown")
    product_name = status_info.get("name", "")
    barcode_data = barcode.data.decode("utf-8")

    color = STATE_COLORS.get(status, STATE_COLORS["unknown"])

    polygon = barcode.polygon
    if polygon and len(polygon) > 0:
        pts = np.array([[pt.x, pt.y] for pt in polygon], dtype=np.int32)
        pts = pts.reshape((-1, 1, 2))
        cv2.polylines(frame, [pts], isClosed=True, color=color, thickness=3)
        x, y = polygon[0].x, polygon[0].y
    else:
        x, y, w, h = barcode.rect
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 3)

    max_text_len = 35
    if len(product_name) > max_text_len:
        product_name = product_name[:max_text_len] + "..."

    labels = [
        f"Code: {barcode_data}",
        f"{product_name}"
    ]

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.5
    thickness = 1
    line_spacing = 18
    padding = 8
    
    text_widths = []
    for label in labels:
        (w, h), _ = cv2.getTextSize(label, font, font_scale, thickness)
        text_widths.append(w)
        
    box_width = max(text_widths) + (padding * 2)
    box_height = (len(labels) * line_spacing) + (padding * 2) - 4

    # Keep the badge fully on screen: a code near the right edge of the scan window
    # would otherwise push its label off-frame, and one near the top would collide
    # with the controls banner.
    frame_h, frame_w = frame.shape[:2]
    box_x = max(10, min(x, frame_w - box_width - 10))
    box_y = max(box_height + TOP_BANNER_H + 4, y - 15)
    box_y = min(box_y, frame_h - 10)

    overlay = frame.copy()
    cv2.rectangle(overlay, (box_x, box_y - box_height), (box_x + box_width, box_y), (30, 30, 30), cv2.FILLED)
    alpha = 0.75
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

    cv2.rectangle(frame, (box_x, box_y - box_height), (box_x + box_width, box_y), color, 1)

    for i, label in enumerate(labels):
        text_y = box_y - box_height + padding + (i * line_spacing) + 10
        label_color = (255, 255, 255) if i == 0 else color
        cv2.putText(frame, label, (box_x + padding, text_y), font, font_scale, label_color, thickness, cv2.LINE_AA)


def draw_scanner_window(frame, roi, state="idle", tick=0):
    """
    Draws the dedicated scan window: everything outside it is dimmed, the corners get
    bracket marks, and an animated sweep line shows the scanner is live. The rectangle
    marks exactly the area the decoder looks at, so aligning a product inside it is
    what makes a read succeed.
    """
    x, y, w, h = roi
    color = STATE_COLORS.get(state, STATE_COLORS["idle"])

    # Dim everything outside the scan window, then restore the window itself
    dimmed = cv2.convertScaleAbs(frame, alpha=DIM_ALPHA)
    dimmed[y:y + h, x:x + w] = frame[y:y + h, x:x + w]
    frame[:] = dimmed

    # Thin full guide rectangle
    cv2.rectangle(frame, (x, y), (x + w - 1, y + h - 1), color, 1)

    # Heavy corner brackets
    bracket = max(18, min(w, h) // 5)
    thickness = 4
    corners = (
        ((x, y), 1, 1),
        ((x + w - 1, y), -1, 1),
        ((x, y + h - 1), 1, -1),
        ((x + w - 1, y + h - 1), -1, -1),
    )
    for (cx, cy), dx, dy in corners:
        cv2.line(frame, (cx, cy), (cx + dx * bracket, cy), color, thickness)
        cv2.line(frame, (cx, cy), (cx, cy + dy * bracket), color, thickness)

    # Animated ping-pong sweep line while waiting for / resolving a code
    if state in ("idle", "pending"):
        period = 55
        phase = (tick % (period * 2)) / float(period)
        progress = phase if phase <= 1.0 else 2.0 - phase
        line_y = int(y + 6 + progress * max(1, h - 12))
        cv2.line(frame, (x + 5, line_y), (x + w - 6, line_y), (60, 60, 255), 2)

    # Caption under the window
    caption = "Align the barcode inside this box"
    (tw, _), _ = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
    caption_x = x + max(0, (w - tw) // 2)
    caption_y = min(frame.shape[0] - 8, y + h + 26)
    cv2.putText(frame, caption, (caption_x, caption_y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)


def _fit_text(text, max_width, font_scale=0.55, thickness=1):
    """Truncates text with an ellipsis so it fits inside max_width pixels."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    if cv2.getTextSize(text, font, font_scale, thickness)[0][0] <= max_width:
        return text

    trimmed = text
    while trimmed and cv2.getTextSize(trimmed + "...", font, font_scale, thickness)[0][0] > max_width:
        trimmed = trimmed[:-1]
    return trimmed + "..."


def draw_banner(frame, text, top, height, text_color, alpha=0.65):
    """Draws a translucent full-width bar with a single line of text."""
    frame_h, frame_w = frame.shape[:2]
    top = max(0, min(top, frame_h - height))

    overlay = frame.copy()
    cv2.rectangle(overlay, (0, top), (frame_w, top + height), (28, 28, 28), cv2.FILLED)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

    label = _fit_text(text, frame_w - 28)
    baseline = top + height - (height - 12) // 2 - 2
    cv2.putText(frame, label, (14, baseline),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, text_color, 1, cv2.LINE_AA)


def main():
    print("====================================================")
    print("   Webcam Barcode & QR Code Scanner starting...     ")
    print("   7-Stage Cascading Lookups: OFF/OBF/OPF/OPPF/OL/UPC/Web ")
    print("====================================================")
    print(f"Decoder Engine: {'Pyzbar (Primary)' if PYZBAR_AVAILABLE else 'OpenCV (Fallback)'}")
    print("Hold the product so its barcode sits inside the on-screen box.")
    print("Controls:  [Q] quit   [+/-] resize box   [F] toggle full-frame   [R] reset box\n")

    lookup_manager = BarcodeLookupManager()
    import sys
    if sys.platform == 'win32':
        # cv2.CAP_DSHOW prevents MSMF from padding the frame with black edges
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        print("[Critical Error] Could not access the webcam feed.")
        print("Please check connection or webcam permissions and try again.")
        lookup_manager.shutdown()
        return

    # Request 720p: only the scan window is decoded, so the extra detail is
    # affordable and it is what lets small printed barcodes resolve.
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
    print(f"Camera resolution: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")

    w_ratio, h_ratio = ROI_W_RATIO, ROI_H_RATIO
    scan_whole_frame = False
    tick = 0
    last_code = None
    last_seen = 0.0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("[Error] Failed to retrieve frame from webcam. Retrying...")
                time.sleep(0.1)
                continue

            tick += 1
            roi = None if scan_whole_frame else get_roi_rect(frame.shape, w_ratio, h_ratio)

            # Decode from the untouched frame, before any overlay is painted on it
            barcodes = decode_frame(frame, roi, tick)

            detections = []
            for barcode in barcodes:
                barcode_data = barcode.data.decode("utf-8")
                lookup_manager.request_lookup(barcode_data)
                detections.append((barcode, lookup_manager.get_status(barcode_data)))
                last_code = barcode_data
                last_seen = time.monotonic()

            # The scan window is tinted by the current hit so the box itself reports state
            state = detections[0][1].get("status", "unknown") if detections else "idle"
            if roi is not None:
                draw_scanner_window(frame, roi, state, tick)

            for barcode, status_info in detections:
                draw_barcode_overlay(frame, barcode, status_info)

            engine_str = "Pyzbar" if PYZBAR_AVAILABLE else "OpenCV"
            mode_str = "FULL FRAME" if scan_whole_frame else f"BOX {int(w_ratio * 100)}%x{int(h_ratio * 100)}%"
            draw_banner(frame, f"[Q] Quit  [+/-] Box size  [F] Full frame  [R] Reset  |  {engine_str}  |  {mode_str}",
                        0, TOP_BANNER_H, (0, 255, 0))

            # Keep the last hit on screen briefly so its details stay readable
            # after the product has been moved away from the window
            if last_code and (time.monotonic() - last_seen) < RESULT_HOLD_SECONDS:
                info = lookup_manager.get_status(last_code)
                result_text = f"{last_code}  ->  {info.get('name', '')}"
                result_color = STATE_COLORS.get(info.get("status", "unknown"), STATE_COLORS["unknown"])
            else:
                result_text = ("No code yet - point the camera at a barcode" if scan_whole_frame
                               else "No code yet - hold a barcode inside the box")
                result_color = STATE_COLORS["idle"]
            draw_banner(frame, result_text, frame.shape[0] - BOTTOM_BANNER_H, BOTTOM_BANNER_H, result_color)

            cv2.imshow("Smart Barcode & QR Reader", frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                print("Exit signal received. Shutting down...")
                break
            elif key in (ord('+'), ord('=')):
                w_ratio = min(ROI_MAX_W, w_ratio + 0.05)
                h_ratio = min(ROI_MAX_H, h_ratio + 0.04)
            elif key in (ord('-'), ord('_')):
                w_ratio = max(ROI_MIN_W, w_ratio - 0.05)
                h_ratio = max(ROI_MIN_H, h_ratio - 0.04)
            elif key == ord('f'):
                scan_whole_frame = not scan_whole_frame
            elif key == ord('r'):
                w_ratio, h_ratio = ROI_W_RATIO, ROI_H_RATIO

    except KeyboardInterrupt:
        print("\nInterrupted by user. Shutting down...")

    finally:
        cap.release()
        cv2.destroyAllWindows()
        lookup_manager.shutdown()
        print("Webcam released. All background tasks stopped.")
        print("Successfully scanned products log saved to 'scanned_products.txt'")


if __name__ == "__main__":
    main()
