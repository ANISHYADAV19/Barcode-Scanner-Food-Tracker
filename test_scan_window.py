"""
Verification for the dedicated scan window, using real synthetic EAN-13 barcodes.

Renders valid EAN-13 symbols from scratch (no extra dependencies) and pushes them
through the same decode path the live camera loop uses. This covers the app's actual
job -- reading 1D product barcodes -- rather than only QR codes.

Run:  venv/Scripts/python.exe test_scan_window.py
"""
import cv2
import numpy as np
import barcode_scanner as bs

# --- EAN-13 symbol encoding tables ---
L_CODE = ["0001101", "0011001", "0010011", "0111101", "0100011",
          "0110001", "0101111", "0111011", "0110111", "0001011"]
G_CODE = ["0100111", "0110011", "0011011", "0100001", "0011101",
          "0111001", "0000101", "0010001", "0001001", "0010111"]
R_CODE = ["1110010", "1100110", "1101100", "1000010", "1011100",
          "1001110", "1010000", "1000100", "1001000", "1110100"]
PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
          "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def ean13_bits(code):
    """Builds the 95-module bit string for a valid EAN-13 code."""
    assert bs.is_valid_ean13(code), f"{code} is not a valid EAN-13"
    digits = [int(d) for d in code]
    bits = "101"                                    # start guard
    for i, d in enumerate(digits[1:7]):             # left group, parity per digit 1
        bits += L_CODE[d] if PARITY[digits[0]][i] == "L" else G_CODE[d]
    bits += "01010"                                 # centre guard
    for d in digits[7:]:                            # right group
        bits += R_CODE[d]
    bits += "101"                                   # end guard
    assert len(bits) == 95, len(bits)
    return bits


def render_ean13(code, module=5, height=170, quiet=10):
    """Renders an EAN-13 barcode as a BGR image with a proper quiet zone."""
    bits = ean13_bits(code)
    width = (len(bits) + quiet * 2) * module
    img = np.full((height, width), 255, np.uint8)
    for i, bit in enumerate(bits):
        if bit == "1":
            x0 = (quiet + i) * module
            img[:, x0:x0 + module] = 0
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def blank(h=720, w=1280):
    return np.full((h, w, 3), 255, np.uint8)


def place(frame, patch, x, y):
    ph, pw = patch.shape[:2]
    frame[y:y + ph, x:x + pw] = patch
    return x, y, pw, ph


# Real-world product codes (Nutella, Coca-Cola, Clean Code ISBN) plus a synthetic one
CODES = ["3017624010701", "5449000000996", "9780132350884", "1234567890128"]
FAILURES = []

roi = bs.get_roi_rect((720, 1280), bs.ROI_W_RATIO, bs.ROI_H_RATIO)
rx, ry, rw, rh = roi
print(f"ROI (x,y,w,h) = {roi}")

# --- Case 1: real EAN-13 barcodes centred INSIDE the scan window ---
print("\n[1] EAN-13 decode inside the scan window")
for code in CODES:
    patch = render_ean13(code)
    frame = blank()
    px = rx + (rw - patch.shape[1]) // 2
    py = ry + (rh - patch.shape[0]) // 2
    place(frame, patch, px, py)

    found = bs.decode_frame(frame, roi)
    values = [b.data.decode() for b in found]
    ok = code in values
    print(f"    {code} -> {values or '(nothing)'}  {'OK' if ok else 'FAIL'}")
    if not ok:
        FAILURES.append(f"EAN-13 {code} not decoded inside the window (got {values})")
        continue

    # Coordinates must be remapped into full-frame space, landing on the symbol
    hit = next(b for b in found if b.data.decode() == code)
    bx, by, bw_, bh_ = hit.rect
    if not (px - 12 <= bx <= px + patch.shape[1] and py - 12 <= by <= py + patch.shape[0]):
        FAILURES.append(f"EAN-13 {code} rect {hit.rect} outside the symbol at ({px},{py})")

# --- Case 2: the same barcode OUTSIDE the window must be ignored ---
print("\n[2] barcode outside the window is ignored")
code = CODES[0]
patch = render_ean13(code)
frame = blank()
place(frame, patch, 20, 20)                      # top-left corner, clear of the ROI
outside = [b.data.decode() for b in bs.decode_frame(frame, roi)]
print(f"    windowed decode -> {outside or '(nothing)'}  {'OK' if not outside else 'FAIL'}")
if outside:
    FAILURES.append(f"code outside the window was still decoded: {outside}")

full = [b.data.decode() for b in bs.decode_frame(frame, None)]
print(f"    full-frame decode -> {full or '(nothing)'}  {'OK' if code in full else 'FAIL'}")
if code not in full:
    FAILURES.append(f"full-frame mode failed to decode {code} (got {full})")

# --- Case 3: checksum validation rejects a corrupt code ---
print("\n[3] checksum guards")
bad = "3017624010702"                            # last digit wrong
checks = [
    (bs.is_valid_ean13("3017624010701"), True, "valid EAN-13 accepted"),
    (bs.is_valid_ean13(bad), False, "corrupt EAN-13 rejected"),
    (bs.is_valid_upc("036000291452"), True, "valid UPC-A accepted"),
    (bs.is_valid_upc("036000291453"), False, "corrupt UPC-A rejected"),
]
for got, want, label in checks:
    print(f"    {label}: {'OK' if got == want else 'FAIL'}")
    if got != want:
        FAILURES.append(label)

# --- Case 4: overlays render, dimming only outside the window ---
print("\n[4] overlay rendering")
frame = blank()
patch = render_ean13(CODES[0])
px = rx + (rw - patch.shape[1]) // 2
py = ry + (rh - patch.shape[0]) // 2
place(frame, patch, px, py)
found = bs.decode_frame(frame, roi)
bs.draw_scanner_window(frame, roi, "found", tick=37)
bs.draw_banner(frame, "x" * 400, 0, bs.TOP_BANNER_H, (0, 255, 0))
bs.draw_banner(frame, "code -> product", frame.shape[0] - bs.BOTTOM_BANNER_H,
               bs.BOTTOM_BANNER_H, (0, 255, 0))
for b in found:
    bs.draw_barcode_overlay(frame, b, {"status": "found", "name": "Nutella (Ferrero)"})

outside_px = int(frame[400, 5][0])
inside_px = int(frame[ry + 8, rx + 8][0])
print(f"    outside pixel {outside_px} (dimmed from 255): {'OK' if outside_px < 200 else 'FAIL'}")
print(f"    inside pixel {inside_px} (kept bright):      {'OK' if inside_px > 200 else 'FAIL'}")
if outside_px >= 200:
    FAILURES.append("area outside the window was not dimmed")
if inside_px <= 200:
    FAILURES.append("area inside the window was dimmed")
cv2.imwrite("_scan_preview.png", frame)
print("    wrote _scan_preview.png")

# --- Case 5: badge stays on screen for codes at every frame edge ---
print("\n[5] badge clamped at frame edges")
for cx, cy in [(0, 0), (1180, 0), (0, 660), (1180, 660), (600, 350)]:
    f = blank()
    fake = bs.MockDecoded("3017624010701",
                          [(cx, cy), (cx + 100, cy), (cx + 100, cy + 60), (cx, cy + 60)])
    bs.draw_barcode_overlay(f, fake, {"status": "found", "name": "A" * 60})
print("    5 edge positions drawn without error: OK")

# --- Case 6: degenerate ROIs and a full animation sweep must not crash ---
print("\n[6] degenerate ROIs and animation")
for r in [(0, 0, 1, 1), (1270, 710, 50, 50), (0, 0, 1280, 720), (-20, -20, 100, 100)]:
    bs.decode_frame(frame, r)
for t in range(240):
    bs.draw_scanner_window(blank(), roi, "idle", tick=t)
print("    degenerate ROIs + 240 animation ticks: OK")

# --- Case 7: every resize step the +/- keys can reach stays usable ---
print("\n[7] all reachable box sizes decode")
patch = render_ean13(CODES[0])
w_ratio, h_ratio = bs.ROI_W_RATIO, bs.ROI_H_RATIO
steps = 0
while w_ratio <= bs.ROI_MAX_W:
    r = bs.get_roi_rect((720, 1280), w_ratio, h_ratio)
    if r[2] >= patch.shape[1] and r[3] >= patch.shape[0]:
        f = blank()
        place(f, patch, r[0] + (r[2] - patch.shape[1]) // 2,
              r[1] + (r[3] - patch.shape[0]) // 2)
        vals = [b.data.decode() for b in bs.decode_frame(f, r)]
        if CODES[0] not in vals:
            FAILURES.append(f"box {int(w_ratio*100)}%x{int(h_ratio*100)}% failed to decode")
        steps += 1
    w_ratio = round(w_ratio + 0.05, 2)
    h_ratio = round(min(bs.ROI_MAX_H, h_ratio + 0.04), 2)
print(f"    {steps} box sizes that fit the symbol all decoded: "
      f"{'OK' if not FAILURES else 'see failures'}")

# --- Summary ---
print("\n" + "=" * 58)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f_ in FAILURES:
        print(f"  - {f_}")
    raise SystemExit(1)
print("ALL SCAN-WINDOW TESTS PASSED")
print(f"Decoder engine used: {'Pyzbar' if bs.PYZBAR_AVAILABLE else 'OpenCV fallback'}")
