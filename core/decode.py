"""
Barcode/QR decoding pipeline.

Shared by the desktop app (barcode_scanner.py) and the web API (web/main.py).
Contains no UI code: everything here takes images and returns decoded codes, so
both front ends can rely on exactly the same decode behaviour.
"""

import os
import sys

import cv2
import numpy as np

# --- Scan window geometry -------------------------------------------------
# Only the region inside the centred rectangle is decoded. A small, explicit
# search window makes aiming obvious for the user, keeps background clutter out
# of the decoder, and lets each frame be processed at a higher effective zoom.
# The web front end mirrors these ratios in CSS so its box matches the desktop one.
ROI_W_RATIO = 0.70            # scan window width as a fraction of the frame
ROI_H_RATIO = 0.34            # scan window height as a fraction of the frame
ROI_MIN_W, ROI_MAX_W = 0.25, 0.96
ROI_MIN_H, ROI_MAX_H = 0.12, 0.90

DECODE_TARGET_WIDTH = 1000    # upscale pass aims for this width in pixels


class MockDecoded:
    """
    Uniform decoder result.

    pyzbar and OpenCV report hits in different shapes, so both are normalised into
    this class and the rest of the codebase only ever sees one interface.
    """

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


# --- Decoder engine selection --------------------------------------------
PYZBAR_AVAILABLE = False
try:
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


def engine_name():
    """Human-readable name of the decoder actually in use."""
    return "Pyzbar" if PYZBAR_AVAILABLE else "OpenCV"


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


def _run_decode_on_image(image_gray):
    """Internal helper to run barcode and QR decoders on a single-channel grayscale image."""
    if PYZBAR_AVAILABLE:
        try:
            return [_to_mutable(d) for d in pyzbar.decode(image_gray)]
        except Exception:
            pass

    # OpenCV Fallback
    decoded_codes = []
    if not hasattr(_run_decode_on_image, "barcode_detector"):
        try:
            _run_decode_on_image.barcode_detector = cv2.barcode.BarcodeDetector()
        except Exception:
            _run_decode_on_image.barcode_detector = None
        _run_decode_on_image.qrcode_detector = cv2.QRCodeDetector()

    # 1. Scan for Barcodes (skipped when this OpenCV build ships no barcode module)
    if _run_decode_on_image.barcode_detector is not None:
        try:
            retval, decoded_info, points, _ = _run_decode_on_image.barcode_detector.detectAndDecodeMulti(image_gray)
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
        retval, decoded_info, points, _ = _run_decode_on_image.qrcode_detector.detectAndDecodeMulti(image_gray)
        if retval:
            if len(points.shape) == 2:
                points = np.expand_dims(points, axis=0)
            for info, pts in zip(decoded_info, points):
                if info.strip():
                    decoded_codes.append(MockDecoded(info.strip(), pts))
    except Exception:
        pass

    return decoded_codes


def _decode_multipass(gray):
    """
    Runs the multi-pass image processing pipeline over a grayscale image.
    Enhances scanning stability under poor lighting, motion blur, and low resolution.
    Coordinates are returned in the coordinate space of the image passed in.
    """
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

    # PASS 4: Upscaling (zoom for small/distant barcodes). The scale adapts to the
    # image width, so a tightly cropped scan window gets the zoom it needs.
    try:
        scale = min(3.0, max(1.5, DECODE_TARGET_WIDTH / float(gray.shape[1])))
        upscaled = cv2.resize(gray, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        barcodes = _run_decode_on_image(upscaled)
        if barcodes:
            return _remap_barcodes(barcodes, scale=scale)
    except Exception:
        pass

    return []


def decode_frame(frame, roi=None):
    """
    Decodes barcodes/QR codes from a frame.

    When `roi` is given as (x, y, w, h), only that scan window is decoded and the
    results are shifted back into full-frame coordinates. Cropping keeps background
    clutter out of the decoder and makes every pass cheaper, so the scan window can
    be processed at a higher zoom than the whole frame could afford.

    The web front end crops in the browser and passes roi=None, since the uploaded
    image is already the scan window.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

    if roi is None:
        return _decode_multipass(gray)

    frame_h, frame_w = gray.shape[:2]
    roi_x, roi_y, roi_w, roi_h = roi
    roi_x = max(0, min(roi_x, frame_w - 1))
    roi_y = max(0, min(roi_y, frame_h - 1))
    roi_w = max(1, min(roi_w, frame_w - roi_x))
    roi_h = max(1, min(roi_h, frame_h - roi_y))

    cropped = gray[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
    if cropped.size == 0:
        return []

    barcodes = _decode_multipass(cropped)
    return _remap_barcodes(barcodes, offset_x=roi_x, offset_y=roi_y)


def decode_jpeg_bytes(buf, roi=None):
    """
    Decodes an uploaded JPEG/PNG byte buffer.

    Used by the web API, where the browser has already cropped to the scan
    rectangle. Returns [] on an undecodable buffer rather than raising, so a
    corrupt upload is reported as "no code found" instead of a 500.
    """
    try:
        img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return []
    if img is None or img.size == 0:
        return []
    return decode_frame(img, roi)


def first_code(barcodes):
    """Returns the decoded text of the first hit, or None."""
    for b in barcodes:
        text = b.data.decode("utf-8", errors="replace").strip()
        if text:
            return text
    return None
