import cv2
import numpy as np
import requests
import threading
import queue
import time
import re
import html as html_parser
from datetime import datetime

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


def _run_decode_on_image(image_gray):
    """Internal helper to run barcode and QR decoders on a single-channel grayscale image."""
    if PYZBAR_AVAILABLE:
        try:
            return pyzbar.decode(image_gray)
        except Exception:
            pass

    # OpenCV Fallback
    decoded_codes = []
    if not hasattr(decode_frame, "barcode_detector"):
        decode_frame.barcode_detector = cv2.barcode.BarcodeDetector()
        decode_frame.qrcode_detector = cv2.QRCodeDetector()
        
    # 1. Scan for Barcodes
    try:
        retval, decoded_info, points, _ = decode_frame.barcode_detector.detectAndDecodeMulti(image_gray)
        if retval:
            if len(points.shape) == 2:
                points = np.expand_dims(points, axis=0)
            for info, pts in zip(decoded_info, points):
                info_stripped = info.strip()
                if info_stripped:
                    # Apply checksum validation filter for 1D codes in fallback mode to avoid misreads
                    if info_stripped.isdigit():
                        if len(info_stripped) == 13 and not is_valid_ean13(info_stripped):
                            continue  # Discard corrupt EAN-13 scan
                        elif len(info_stripped) == 12 and not is_valid_upc(info_stripped):
                            continue  # Discard corrupt UPC-A scan
                    decoded_codes.append(MockDecoded(info_stripped, pts))
    except Exception:
        pass
        
    # 2. Scan for QR Codes (no checksum validation needed for QR)
    try:
        retval, decoded_info, points, _ = decode_frame.qrcode_detector.detectAndDecodeMulti(image_gray)
        if retval:
            if len(points.shape) == 2:
                points = np.expand_dims(points, axis=0)
            for info, pts in zip(decoded_info, points):
                if info.strip():
                    decoded_codes.append(MockDecoded(info.strip(), pts))
    except Exception:
        pass
        
    return decoded_codes


def decode_frame(frame):
    """
    Decodes barcodes/QR codes from a frame using a multi-pass image processing pipeline.
    Enhances scanning stability under poor lighting, motion blur, and low resolution.
    """
    # Grayscale conversion
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    
    # PASS 1: Standard Grayscale Frame
    barcodes = _run_decode_on_image(gray)
    if barcodes:
        return barcodes
        
    # PASS 2: Binarization (Otsu's Thresholding for contrast)
    try:
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        barcodes = _run_decode_on_image(thresh)
        if barcodes:
            return barcodes
    except Exception:
        pass
        
    # PASS 3: Image Sharpening (fixes motion blur)
    try:
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
        sharpened = cv2.filter2D(gray, -1, kernel)
        barcodes = _run_decode_on_image(sharpened)
        if barcodes:
            return barcodes
    except Exception:
        pass

    # PASS 4: Upscaling (1.5x zoom for small/distant barcodes)
    try:
        scale = 1.5
        upscaled = cv2.resize(gray, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        barcodes = _run_decode_on_image(upscaled)
        if barcodes:
            # Map upscaled coordinates back to original frame size
            for bc in barcodes:
                if hasattr(bc, 'polygon') and bc.polygon:
                    for pt in bc.polygon:
                        pt.x = int(pt.x / scale)
                        pt.y = int(pt.y / scale)
                if hasattr(bc, 'rect') and bc.rect:
                    rx, ry, rw, rh = bc.rect
                    bc.rect = (int(rx / scale), int(ry / scale), int(rw / scale), int(rh / scale))
            return barcodes
    except Exception:
        pass
        
    return []


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


def draw_barcode_overlay(frame, barcode, status_info):
    """Draws a custom overlay around the scanned barcode and displays the product name."""
    status = status_info.get("status", "unknown")
    product_name = status_info.get("name", "")
    barcode_data = barcode.data.decode("utf-8")

    colors = {
        "pending": (0, 255, 255),    # Yellow
        "found": (0, 255, 0),        # Green
        "not_found": (0, 0, 255),    # Red
        "failed": (0, 165, 255),     # Orange
        "unknown": (255, 255, 255)   # White
    }
    color = colors.get(status, colors["unknown"])

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

    box_x = max(10, x)
    box_y = max(box_height + 10, y - 15)

    overlay = frame.copy()
    cv2.rectangle(overlay, (box_x, box_y - box_height), (box_x + box_width, box_y), (30, 30, 30), cv2.FILLED)
    alpha = 0.75
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

    cv2.rectangle(frame, (box_x, box_y - box_height), (box_x + box_width, box_y), color, 1)

    for i, label in enumerate(labels):
        text_y = box_y - box_height + padding + (i * line_spacing) + 10
        label_color = (255, 255, 255) if i == 0 else color
        cv2.putText(frame, label, (box_x + padding, text_y), font, font_scale, label_color, thickness, cv2.LINE_AA)


def main():
    print("====================================================")
    print("   Webcam Barcode & QR Code Scanner starting...     ")
    print("   7-Stage Cascading Lookups: OFF/OBF/OPF/OPPF/OL/UPC/Web ")
    print("====================================================")
    print(f"Decoder Engine: {'Pyzbar (Primary)' if PYZBAR_AVAILABLE else 'OpenCV (Fallback)'}")
    print("Press 'q' inside the webcam window to exit.\n")

    lookup_manager = BarcodeLookupManager()
    cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        print("[Critical Error] Could not access the webcam feed.")
        print("Please check connection or webcam permissions and try again.")
        lookup_manager.shutdown()
        return

    # Using 640x480 for faster real-time frame processing
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("[Error] Failed to retrieve frame from webcam. Retrying...")
                time.sleep(0.1)
                continue

            barcodes = decode_frame(frame)

            for barcode in barcodes:
                barcode_data = barcode.data.decode("utf-8")
                lookup_manager.request_lookup(barcode_data)
                status_info = lookup_manager.get_status(barcode_data)
                draw_barcode_overlay(frame, barcode, status_info)

            engine_str = "Pyzbar Engine" if PYZBAR_AVAILABLE else "OpenCV Engine"
            instruction_text = f"Press 'q' to Quit | {engine_str} active | Logs in scanned_products.txt"
            cv2.putText(frame, instruction_text, (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2, cv2.LINE_AA)

            cv2.imshow("Smart Barcode & QR Reader", frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                print("Exit signal received. Shutting down...")
                break

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
