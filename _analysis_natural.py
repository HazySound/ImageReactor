"""
자연 비율 NCC 매칭 재측정 (letterbox 제거).
- ref를 컴포넌트 height에 맞춰 자연 폭으로 렌더링
- 컴포넌트와 ref 둘 다 같은 (H, W) 캔버스에 중앙 정렬 후 1:1 NCC
"""
import sys, io
if (sys.stdout.encoding or "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import cv2
import numpy as np
from PIL import Image, ImageFont, ImageDraw

FONT_PATH = r"D:\박시몬\ImageReactor\ImageReactor\CruyffSansCondensed-Medium.ttf"
CAPTURE   = r"D:\박시몬\ImageReactor\ImageReactor\capture1.png"
BASE_DIR  = r"D:\박시몬\ImageReactor\ImageReactor"

def imread_unicode(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)

def imwrite_unicode(path, img):
    ext = "." + path.rsplit(".", 1)[1]
    ok, buf = cv2.imencode(ext, img)
    if ok: buf.tofile(path)

def white_mask(bgr, v_min=200, s_max=60):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return ((hsv[..., 2] >= v_min) & (hsv[..., 1] <= s_max)).astype(np.uint8) * 255

def extract_components(bgr, min_h=5):
    mask = white_mask(bgr)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, w, h, a = (int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP]),
                         int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT]),
                         int(stats[i, cv2.CC_STAT_AREA]))
        if h >= min_h and w >= 2:
            comps.append((x, y, w, h, a))
    return mask, comps

def filter_height_gap(comps, ratio=1.5):
    if not comps: return []
    sc = sorted(comps, key=lambda c: -c[3])
    hs = [c[3] for c in sc]
    cut = len(sc)
    for i in range(len(hs)-1):
        if hs[i+1] <= 0: cut = i+1; break
        if hs[i]/hs[i+1] >= ratio: cut = i+1; break
    return sc[:max(cut, 1)]


# ---------- 자연 비율 ref 렌더링 ----------

def render_at_height(char, target_height):
    """글자를 정확히 target_height 픽셀로 자연 비율 렌더링.
    폭은 폰트가 그린 그대로 유지(stretch 없음).
    """
    # 큰 사이즈로 그린 뒤 tight crop → target_h로 비율 유지 리사이즈
    font_size = max(20, target_height * 2)
    font = ImageFont.truetype(FONT_PATH, font_size)
    bbox = font.getbbox(char)
    x0, y0, x1, y1 = bbox
    w = max(1, x1 - x0); h = max(1, y1 - y0)
    img = Image.new("L", (w + 8, h + 8), 0)
    draw = ImageDraw.Draw(img)
    draw.text((-x0 + 4, -y0 + 4), char, fill=255, font=font)
    arr = np.array(img)
    ys, xs = np.where(arr > 32)
    if len(xs) == 0: return None
    cropped = arr[ys.min():ys.max()+1, xs.min():xs.max()+1]
    ch, cw = cropped.shape
    scale = target_height / ch
    new_w = max(1, int(round(cw * scale)))
    return cv2.resize(cropped, (new_w, target_height), interpolation=cv2.INTER_AREA)


def match_natural(comp_bw, refs_at_h):
    """컴포넌트(BW)와 같은 height의 ref들 매칭. 폭이 다르면 큰 폭에 맞춰 둘 다 패딩 후 1:1 NCC."""
    ch, cw = comp_bw.shape
    scores = []
    for c, ref in refs_at_h.items():
        if ref is None: continue
        rh, rw = ref.shape
        H, W = max(ch, rh), max(cw, rw)
        c_canvas = np.zeros((H, W), dtype=np.uint8)
        r_canvas = np.zeros((H, W), dtype=np.uint8)
        # 글자를 좌측 정렬 + 수직 중앙. (좌측 정렬은 baseline 영향 줄임)
        c_canvas[(H-ch)//2:(H-ch)//2+ch, 0:cw] = comp_bw
        r_canvas[(H-rh)//2:(H-rh)//2+rh, 0:rw] = ref
        r = cv2.matchTemplate(c_canvas, r_canvas, cv2.TM_CCOEFF_NORMED)
        scores.append((c, float(r[0, 0])))
    scores.sort(key=lambda x: -x[1])
    return scores


# ---------- Reference self-similarity (자연 비율로 다시) ----------

char_list = "0123456789,"

print("=== Reference Self-similarity (자연 비율, target_h=72) ===")
target_h = 72  # capture1.png의 등수 컴포넌트 높이와 비슷
refs_natural = {c: render_at_height(c, target_h) for c in char_list}
for c in char_list:
    r = refs_natural[c]
    print(f"  ref {c!r}: shape={r.shape if r is not None else None}")

print()
print("    " + "  ".join([f"{c:>5}" for c in char_list]))
for c1 in char_list:
    row = []
    for c2 in char_list:
        scores = match_natural(refs_natural[c1], {c2: refs_natural[c2]})
        row.append(scores[0][1])
    print(f"  {c1}: " + "  ".join([f"{v:>5.2f}" for v in row]))

print("\n  >> 주요 confusable 짝 마진 (자연 비율):")
def pair_score(a, b):
    s = match_natural(refs_natural[a], {b: refs_natural[b]})
    return s[0][1]
for a, b in [("1","7"), ("1","4"), ("4","7"), ("6","8"), ("6","9"), ("0","9"),
             ("3","8"), ("5","6"), ("0","6"), ("0","8")]:
    print(f"    {a} vs {b}: {pair_score(a,b):.3f}")


# ---------- Capture1 실측 ----------

capture = imread_unicode(CAPTURE)
H, W = capture.shape[:2]
sx = W / 2560
sy = H / 1440

def scale_roi(roi):
    x, y, w, h = roi
    return (int(x*sx), int(y*sy), int(w*sx), int(h*sy))

def crop(img, roi):
    x, y, w, h = roi
    return img[y:y+h, x:x+w]

rank_roi = scale_roi([1021, 485, 344, 108])
pts_roi  = scale_roi([484, 488, 267, 101])

rank_crop = crop(capture, rank_roi)
pts_crop  = crop(capture, pts_roi)
mask_rank, comps_rank_all = extract_components(rank_crop)
mask_pts, comps_pts_all   = extract_components(pts_crop)
kept_rank = filter_height_gap(comps_rank_all)


def comp_bw(box, full_mask):
    x, y, w, h, _ = box
    return full_mask[y:y+h, x:x+w]


def match_to_refs(comp_box, full_mask):
    bw = comp_bw(comp_box, full_mask)
    ch = bw.shape[0]
    # ref를 컴포넌트 height에 맞춰 동적 렌더링
    refs_at_h = {c: render_at_height(c, ch) for c in char_list}
    return match_natural(bw, refs_at_h)


def fmt(scores, k=5):
    return ", ".join([f"{c}={s:.3f}" for c, s in scores[:k]])


print("\n\n=== Capture1 RANK 자연비율 매칭 (정답: '268') ===")
sorted_rank = sorted(kept_rank, key=lambda c: c[0])
for i, c in enumerate(sorted_rank):
    s = match_to_refs(c, mask_rank)
    pick = s[0][0]
    margin = s[0][1] - s[1][1]
    print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3}: TOP5 [{fmt(s)}]")
    print(f"    -> pick={pick}  top={s[0][1]:.3f}  margin={margin:.3f}")

print("\n=== Capture1 PTS 자연비율 매칭 (정답: '4,393') ===")
sorted_pts = sorted(comps_pts_all, key=lambda c: c[0])
for i, c in enumerate(sorted_pts):
    s = match_to_refs(c, mask_pts)
    pick = s[0][0]
    margin = s[0][1] - s[1][1]
    print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3}: TOP5 [{fmt(s)}]")
    print(f"    -> pick={pick}  top={s[0][1]:.3f}  margin={margin:.3f}")
