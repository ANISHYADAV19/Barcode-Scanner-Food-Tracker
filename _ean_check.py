"""Temporary check: real EAN-13 1D barcode through the ROI scan window. Delete after running."""
import cv2
import numpy as np
import barcode_scanner as bs

L = {0: '0001101', 1: '0011001', 2: '0010011', 3: '0111101', 4: '0100011',
     5: '0110001', 6: '0101111', 7: '0111011', 8: '0110111', 9: '0001011'}
G = {0: '0100111', 1: '0110011', 2: '0011011', 3: '0100001', 4: '0011101',
     5: '0111001', 6: '0000101', 7: '0010001', 8: '0001001', 9: '0010111'}
R = {0: '1110010', 1: '1100110', 2: '1101100', 3: '1000010', 4: '1011100',
     5: '1001110', 6: '1010000', 7: '1101000', 8: '1001000', 9: '1110100'}
PARITY = ['LLLLLL', 'LLGLGG', 'LLGGLG', 'LLGGGL', 'LGLLGG',
          'LGGLLG', 'LGGGLL', 'LGLGLG', 'LGLGGL', 'LGGLGL']


def ean13_modules(code):
    """Returns the 95-module bit string for a valid EAN-13 code."""
    d = [int(c) for c in code]
    bits = '101'
    for i, p in enumerate(PARITY[d[0]]):
        bits += (L if p == 'L' else G)[d[1 + i]]
    bits += '01010'
    for i in range(6):
        bits += R[d[7 + i]]
    return bits + '101'


def render_ean13(code, module_w=3, height=120, quiet=10):
    """Renders an EAN-13 barcode as a white-background BGR image."""
    bits = ean13_modules(code)
    w = len(bits) * module_w + quiet * 2 * module_w
    img = np.full((height, w), 255, np.uint8)
    x = quiet * module_w
    for b in bits:
        if b == '1':
            img[:, x:x + module_w] = 0
        x += module_w
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


CODE = "3017624010701"          # Nutella, the code used in the README example
assert bs.is_valid_ean13(CODE), "test fixture itself is not a valid EAN-13"
print(f"fixture {CODE} passes the app's own EAN-13 checksum validator")

bars = render_ean13(CODE)
print("rendered barcode image:", bars.shape[1], "x", bars.shape[0])

roi = bs.get_roi_rect((720, 1280))
rx, ry, rw, rh = roi
print("ROI (x,y,w,h) =", roi)

# --- Case 1: barcode centred INSIDE the scan window ---
frame = np.full((720, 1280, 3), 255, np.uint8)
bh, bw = bars.shape[:2]
px, py = rx + (rw - bw) // 2, ry + (rh - bh) // 2
frame[py:py + bh, px:px + bw] = bars
found = bs.decode_frame(frame, roi)
codes = [b.data.decode() for b in found]
print("\n[inside] decoded:", codes)
assert CODE in codes, f"EAN-13 not decoded inside the scan window (got {codes})"
bx, by, bwid, bhei = found[0].rect
print(f"[inside] rect = ({bx},{by},{bwid},{bhei})  barcode drawn at ({px},{py},{bw},{bh})")
assert rx - 6 <= bx <= rx + rw + 6, "x not remapped into full-frame coords"
assert ry - 6 <= by <= ry + rh + 6, "y not remapped into full-frame coords"

# --- Case 2: same barcode OUTSIDE the window must be ignored ---
frame2 = np.full((720, 1280, 3), 255, np.uint8)
frame2[5:5 + bh, 5:5 + bw] = bars
outside = bs.decode_frame(frame2, roi)
print("\n[outside] decoded:", [b.data.decode() for b in outside])
assert outside == [], "barcode outside the window must be ignored"
full = bs.decode_frame(frame2, None)
print("[outside, full-frame mode] decoded:", [b.data.decode() for b in full])
assert CODE in [b.data.decode() for b in full], "full-frame mode failed on 1D barcode"

# --- Case 3: small / distant barcode relies on the upscale pass ---
small = cv2.resize(bars, (0, 0), fx=0.42, fy=0.42, interpolation=cv2.INTER_AREA)
sh, sw = small.shape[:2]
frame3 = np.full((720, 1280, 3), 255, np.uint8)
spx, spy = rx + (rw - sw) // 2, ry + (rh - sh) // 2
frame3[spy:spy + sh, spx:spx + sw] = small
small_found = [b.data.decode() for b in bs.decode_frame(frame3, roi)]
print(f"\n[small {sw}x{sh}] decoded:", small_found)

# --- Case 4: full render, as the user would see it ---
frame4 = np.full((720, 1280, 3), 255, np.uint8)
frame4[py:py + bh, px:px + bw] = bars
state = "found" if found else "idle"
bs.draw_scanner_window(frame4, roi, state, tick=20)
for b in found:
    bs.draw_barcode_overlay(frame4, b, {"status": "found", "name": "Nutella (Ferrero)"})
bs.draw_banner(frame4, f"[Q] Quit  [+/-] Box size  [F] Full frame  [R] Reset  |  OpenCV  |  BOX 70%x34%",
               0, bs.TOP_BANNER_H, (0, 255, 0))
bs.draw_banner(frame4, f"{CODE}  ->  Nutella (Ferrero)", 720 - bs.BOTTOM_BANNER_H,
               bs.BOTTOM_BANNER_H, bs.STATE_COLORS["found"])
cv2.imwrite("_ean_preview.png", frame4)
print("\nwrote _ean_preview.png")

print("\nEAN-13 PATH VERIFIED")
