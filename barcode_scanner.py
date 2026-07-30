import cv2
import numpy as np
import requests
import threading
import queue
import time
from datetime import datetime

# Define MockDecoded structure for OpenCV fallback matching pyzbar interface
class MockDecoded:
    class Point:
        def __init__(self, x, y):
            self.x = int(x)
            self.y = int(y)

    def __init__(self, data_str, points):
        self.data = data_str.encode('utf-8')
        
        # points is expected to be a numpy array of shape (4, 2) or list of coordinates
        self.polygon = [self.Point(pt[0], pt[1]) for pt in points] if points is not None else []
        
        # Calculate rect from points
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
    # Add pyzbar folder to DLL directory searches on Windows (Python 3.8+)
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

def decode_frame(frame):
    """Decodes barcodes/QR codes from a frame, using Pyzbar or falling back to OpenCV."""
    if PYZBAR_AVAILABLE:
        try:
            return pyzbar.decode(frame)
        except Exception as e:
            # If pyzbar fails at runtime for some reason, print warning and fallback
            print(f"[Error] pyzbar.decode runtime error: {e}. Switching to OpenCV detectors.")
            pass

    # OpenCV Fallback
    decoded_codes = []
    
    # Initialize static detectors on first call
    if not hasattr(decode_frame, "barcode_detector"):
        decode_frame.barcode_detector = cv2.barcode.BarcodeDetector()
        decode_frame.qrcode_detector = cv2.QRCodeDetector()
        
    # 1. Scan for Barcodes
    try:
        retval, decoded_info, points, _ = decode_frame.barcode_detector.detectAndDecodeMulti(frame)
        if retval:
            # If a single code is returned, points might not be nested. Normalize zip.
            if len(points.shape) == 2:
                points = np.expand_dims(points, axis=0)
            for info, pts in zip(decoded_info, points):
                if info.strip():
                    decoded_codes.append(MockDecoded(info, pts))
    except Exception:
        pass
        
    # 2. Scan for QR Codes
    try:
        retval, decoded_info, points, _ = decode_frame.qrcode_detector.detectAndDecodeMulti(frame)
        if retval:
            if len(points.shape) == 2:
                points = np.expand_dims(points, axis=0)
            for info, pts in zip(decoded_info, points):
                if info.strip():
                    decoded_codes.append(MockDecoded(info, pts))
    except Exception:
        pass
        
    return decoded_codes


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
        """Worker thread loop that fetches product data and logs it."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": "BarcodeScannerApp/1.0 (Python OpenCV/pyzbar; contact: appdeveloper@example.com)"
        })

        while True:
            barcode = self.lookup_queue.get()
            if barcode is None:
                break

            url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json?fields=product_name,brands,generic_name"
            
            try:
                response = session.get(url, timeout=5)
                
                if response.status_code == 200:
                    data = response.json()
                    status = data.get("status")
                    
                    if status == 1:
                        product = data.get("product", {})
                        prod_name = product.get("product_name") or product.get("generic_name") or "Unknown Product"
                        brand = product.get("brands")
                        
                        full_name = f"{prod_name} ({brand})" if brand else prod_name
                        full_name = full_name.strip()
                        if not full_name:
                            full_name = "Unnamed Product"
                            
                        self._update_cache_and_log(barcode, "found", full_name)
                    else:
                        self._update_cache_and_log(barcode, "not_found", "Product Not Found")
                else:
                    self._update_cache_and_log(barcode, "failed", f"API Error ({response.status_code})")
            
            except requests.RequestException as e:
                print(f"[Error] Network lookup failed for barcode {barcode}: {e}")
                self._update_cache_and_log(barcode, "failed", "Network Error")
            
            finally:
                self.lookup_queue.task_done()

    def _update_cache_and_log(self, barcode, status, name):
        """Helper to thread-safely update cache and write scanner log file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.cache[barcode] = {
                "status": status,
                "name": name,
                "timestamp": timestamp
            }
        
        log_entry = f"[{timestamp}] Barcode: {barcode} | Status: {status.upper()} | Product: {name}\n"
        
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
    print("   Lookup API: Open Food Facts (No Key Required)    ")
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

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("[Error] Failed to retrieve frame from webcam. Retrying...")
                time.sleep(0.1)
                continue

            # Decode barcodes/QR codes from the frame
            barcodes = decode_frame(frame)

            for barcode in barcodes:
                barcode_data = barcode.data.decode("utf-8")
                lookup_manager.request_lookup(barcode_data)
                status_info = lookup_manager.get_status(barcode_data)
                draw_barcode_overlay(frame, barcode, status_info)

            # Display instruction text on the window
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
