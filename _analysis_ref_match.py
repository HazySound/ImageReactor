"""
Reference Template Matching 분석 스크립트.
- Cruyff Sans Condensed Medium 폰트로 0-9 + 콤마 reference 렌더링
- capture1.png의 등수/점수 ROI 추출
- 각 컴포넌트와 reference의 NCC 매칭 점수 측정
- 분포 보고: 정답 ref와의 점수 vs 다른 ref와의 점수 마진
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
CAPTURE2  = r"D:\박시몬\ImageReactor\ImageReactor\777을7로.png"
BASE_DIR  = r"D:\박시몬\ImageReactor\ImageReactor"

# ---------- 폰트 reference 렌더링 ----------

def render_glyph(char, font_size, padding=4):
    font = ImageFont.truetype(FONT_PATH, font_size)
    bbox = font.getbbox(char)
    x0, y0, x1, y1 = bbox
    w = max(1, x1 - x0)
    h = max(1, y1 - y0)
    img = Image.new("L", (w + padding*2, h + padding*2), 0)
    draw = ImageDraw.Draw(img)
    draw.text((-x0 + padding, -y0 + padding), char, fill=255, font=font)
    arr = np.array(img)
    ys, xs = np.where(arr > 32)
    if len(xs) == 0:
        return arr
    return arr[ys.min():ys.max()+1, xs.min():xs.max()+1]


# ---------- 한글 경로 안전 이미지 IO ----------

def imread_unicode(path):
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)

def imwrite_unicode(path, img):
    ext = "." + path.rsplit(".", 1)[1]
    ok, buf = cv2.imencode(ext, img)
    if ok:
        buf.tofile(path)


# ---------- ocr_v2와 동일 로직 ----------

def white_mask(bgr, v_min=200, s_max=60):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    s = hsv[..., 1]; v = hsv[..., 2]
    return ((v >= v_min) & (s <= s_max)).astype(np.uint8) * 255

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
    if not comps:
        return []
    sc = sorted(comps, key=lambda c: -c[3])
    hs = [c[3] for c in sc]
    cut = len(sc)
    for i in range(len(hs)-1):
        if hs[i+1] <= 0:
            cut = i+1; break
        if hs[i]/hs[i+1] >= ratio:
            cut = i+1; break
    return sc[:max(cut, 1)]


# ---------- hole count (8 vs 0/6/9 결정적 구분) ----------

def count_holes(comp_mask_local):
    """컴포넌트 mask(0/255) 안의 내부 hole 개수.
    8 → 2, 0/6/9 → 1, 그 외 → 0 (이상적).
    padding 추가 후 invert → connected components → outer 1개 제외.
    """
    padded = cv2.copyMakeBorder(comp_mask_local, 1, 1, 1, 1,
                                cv2.BORDER_CONSTANT, value=0)
    inv = 255 - padded
    n, _ = cv2.connectedComponents(inv, connectivity=4)
    # n labels = (배경 outer 1개) + (내부 hole H개) + label 0 자체는 카운트 안 함
    # connectedComponents는 0(검은영역 라벨 시작 1부터)? 실제로 inv에서 흰영역(원래 검은영역)이 라벨링됨.
    # 결과: 외부 배경(연결된 가장 큰 영역) 1개 + 내부 hole 개. n - 1 - 1 = n - 2
    return max(0, n - 2)


# ---------- ref hole count (각 글자의 이론 hole) ----------

def compute_ref_holes():
    out = {}
    for c in "0123456789,":
        m = refs_raw[c]
        out[c] = count_holes(m)
    return out


# ---------- canonical letterbox ----------

def to_canonical(arr, size=64):
    h, w = arr.shape
    scale = size / max(h, w)
    new_h, new_w = max(1, int(h * scale)), max(1, int(w * scale))
    resized = cv2.resize(arr, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((size, size), dtype=np.uint8)
    y_off = (size - new_h) // 2
    x_off = (size - new_w) // 2
    canvas[y_off:y_off+new_h, x_off:x_off+new_w] = resized
    return canvas


# ---------- main ----------

char_list = "0123456789,"
REF_SIZE = 64

# reference 렌더링
refs_raw = {c: render_glyph(c, font_size=100) for c in char_list}
refs_canon = {c: to_canonical(refs_raw[c], size=REF_SIZE) for c in char_list}

print("=== Reference 렌더링 + hole count ===")
ref_holes = compute_ref_holes()
for c in char_list:
    arr = refs_raw[c]
    print(f"  {c!r}: raw_shape={arr.shape}  canon shape={refs_canon[c].shape}  holes={ref_holes[c]}")

# Reference 자체 self-similarity matrix (각 ref vs 다른 ref, 폰트만 가지고 confusable 검증)
print("\n=== REFERENCE Self-similarity (NCC matrix) ===")
print("    " + "  ".join([f"{c:>4}" for c in char_list]))
for c1 in char_list:
    row = []
    for c2 in char_list:
        r = cv2.matchTemplate(refs_canon[c1], refs_canon[c2], cv2.TM_CCOEFF_NORMED)
        row.append(float(r[0, 0]))
    print(f"  {c1}: " + "  ".join([f"{v:>4.2f}" for v in row]))

# 1 vs 7 / 6 vs 8 / 0 vs 9 같은 confusable 짝 마진 보고
print("\n  >> 주요 confusable 짝 마진:")
def pair_score(a, b):
    r = cv2.matchTemplate(refs_canon[a], refs_canon[b], cv2.TM_CCOEFF_NORMED)
    return float(r[0, 0])
for a, b in [("1","7"), ("1","4"), ("4","7"), ("6","8"), ("6","9"), ("0","9"), ("3","8"), ("5","6")]:
    print(f"    {a} vs {b}: {pair_score(a,b):.3f}  (hole_a={ref_holes[a]} hole_b={ref_holes[b]})")

# 캡처 로드
capture = imread_unicode(CAPTURE)
H, W = capture.shape[:2]
print(f"\ncapture shape: {W}x{H}")

base_w, base_h = 2560, 1440
sx = W / base_w
sy = H / base_h
def scale_roi(roi):
    x, y, w, h = roi
    return (int(x*sx), int(y*sy), int(w*sx), int(h*sy))

rank_roi = scale_roi([1021, 485, 344, 108])
pts_roi  = scale_roi([484, 488, 267, 101])
print(f"rank_roi: {rank_roi}")
print(f"pts_roi:  {pts_roi}")

def crop(img, roi):
    x, y, w, h = roi
    return img[y:y+h, x:x+w]

rank_crop = crop(capture, rank_roi)
pts_crop  = crop(capture, pts_roi)
imwrite_unicode(rf"{BASE_DIR}\_dbg_rank_crop.png", rank_crop)
imwrite_unicode(rf"{BASE_DIR}\_dbg_pts_crop.png", pts_crop)

mask_rank, comps_rank_all = extract_components(rank_crop)
mask_pts, comps_pts_all   = extract_components(pts_crop)

kept_rank = filter_height_gap(comps_rank_all)
kept_pts  = filter_height_gap(comps_pts_all)

print(f"\n[ranked ROI] all={len(comps_rank_all)} kept(height_gap)={len(kept_rank)}")
print(f"  ALL  heights: {sorted([c[3] for c in comps_rank_all], reverse=True)}")
print(f"  kept heights: {sorted([c[3] for c in kept_rank], reverse=True)}")
print(f"\n[points ROI] all={len(comps_pts_all)} kept(height_gap)={len(kept_pts)}")
print(f"  ALL  heights: {sorted([c[3] for c in comps_pts_all], reverse=True)}")
print(f"  kept heights: {sorted([c[3] for c in kept_pts], reverse=True)}")


def match_all(comp_box, full_mask):
    x, y, w, h, _ = comp_box
    bw = full_mask[y:y+h, x:x+w]
    bw_canon = to_canonical(bw, size=REF_SIZE)
    holes = count_holes(bw)
    scores = []
    for ch in char_list:
        r = cv2.matchTemplate(bw_canon, refs_canon[ch], cv2.TM_CCOEFF_NORMED)
        scores.append((ch, float(r[0, 0])))
    scores.sort(key=lambda x: -x[1])
    # hole 보정: NCC top 후보 중 hole 일치 후보 선별
    matching = [(ch, sc) for ch, sc in scores if ref_holes.get(ch, -1) == holes]
    return holes, scores, matching

def fmt_scores(scores):
    return ", ".join([f"{ch}={sc:.3f}" for ch, sc in scores[:5]])

print("\n=== RANK 컴포넌트 매칭 (정답: '268') ===")
sorted_rank = sorted(kept_rank, key=lambda c: c[0])
for i, c in enumerate(sorted_rank):
    holes, s, matching = match_all(c, mask_rank)
    pick_ncc  = s[0][0]
    pick_hole = matching[0][0] if matching else "(none)"
    print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3} holes={holes}: "
          f"TOP5 [{fmt_scores(s)}]")
    print(f"    → NCC pick={pick_ncc}   hole-filter pick={pick_hole}   "
          f"(hole 일치 후보 마진: top={matching[0][1]:.3f} 2nd={matching[1][1]:.3f}"
          if len(matching) >= 2 else
          f"    → NCC pick={pick_ncc}   hole-filter pick={pick_hole}   "
          f"(hole 일치 후보: {len(matching)}개)")

print("\n=== PTS 컴포넌트 매칭 - ALL components 콤마 포함 (정답: '4,393') ===")
sorted_pts = sorted(comps_pts_all, key=lambda c: c[0])
for i, c in enumerate(sorted_pts):
    holes, s, matching = match_all(c, mask_pts)
    pick_ncc  = s[0][0]
    pick_hole = matching[0][0] if matching else "(none)"
    print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3} holes={holes}: "
          f"TOP5 [{fmt_scores(s)}]")
    print(f"    -> NCC pick={pick_ncc}   hole-filter pick={pick_hole}   "
          f"hole-matching후보={[c2 for c2, _ in matching[:5]]}")


# === CAPTURE2 분석 (777을7로.png — 1, 7 confusable 검증용) ===
print("\n" + "="*60)
print("=== CAPTURE2: 777을7로.png — 7과 1 confusable 검증 ===")
print("="*60)

capture2 = imread_unicode(CAPTURE2)
if capture2 is not None:
    H2, W2 = capture2.shape[:2]
    print(f"capture2 shape: {W2}x{H2}")
    # 이 캡처는 OCR 테스트 화면 — 게임이 GUI 패널 내부에 표시됨.
    # 게임 영역 좌표를 시각으로 추정해서 ROI 별도 지정.
    # 첨부 이미지 기준으로 게임 화면 안의 점수 '4,321'과 등수 '777' 영역 좌표를 찾는다.
    # 전체 캡처에서 게임 화면 영역을 먼저 잘라낸 뒤, base 비율 적용해보기.

    # 게임 패널 추정 (이미지 분석으로 추정값):
    # 캡처 안 게임 화면 좌상단 ~ 우하단을 시각으로 잡음.
    # 캡처는 1198x896 정도이고, 게임 화면은 거의 풀 영역.
    # base 2560x1440 비율 그대로 적용.
    sx2 = W2 / 2560
    sy2 = H2 / 1440
    def scale_roi2(roi):
        x, y, w, h = roi
        return (int(x*sx2), int(y*sy2), int(w*sx2), int(h*sy2))
    rank_roi2 = scale_roi2([1021, 485, 344, 108])
    pts_roi2  = scale_roi2([484, 488, 267, 101])
    print(f"  rank_roi2: {rank_roi2}")
    print(f"  pts_roi2:  {pts_roi2}")

    rank_crop2 = crop(capture2, rank_roi2)
    pts_crop2  = crop(capture2, pts_roi2)
    imwrite_unicode(rf"{BASE_DIR}\_dbg_rank_crop2.png", rank_crop2)
    imwrite_unicode(rf"{BASE_DIR}\_dbg_pts_crop2.png", pts_crop2)

    mask_rank2, comps_rank2_all = extract_components(rank_crop2)
    mask_pts2, comps_pts2_all   = extract_components(pts_crop2)

    kept_rank2 = filter_height_gap(comps_rank2_all)
    kept_pts2  = filter_height_gap(comps_pts2_all)
    print(f"\n[rank2] all={len(comps_rank2_all)} kept={len(kept_rank2)} "
          f"heights={sorted([c[3] for c in kept_rank2], reverse=True)}")
    print(f"[pts2]  all={len(comps_pts2_all)}  kept={len(kept_pts2)} "
          f"heights={sorted([c[3] for c in kept_pts2], reverse=True)}")

    print("\n=== RANK2 매칭 (정답: '777') ===")
    sorted_rank2 = sorted(kept_rank2, key=lambda c: c[0])
    for i, c in enumerate(sorted_rank2):
        holes, s, matching = match_all(c, mask_rank2)
        print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3} holes={holes}: "
              f"TOP5 [{fmt_scores(s)}]")
        print(f"    -> NCC pick={s[0][0]}  hole-filter pick="
              f"{matching[0][0] if matching else '(none)'}")

    print("\n=== PTS2 매칭 ALL 콤마 포함 (정답: '4,321') ===")
    sorted_pts2 = sorted(comps_pts2_all, key=lambda c: c[0])
    for i, c in enumerate(sorted_pts2):
        holes, s, matching = match_all(c, mask_pts2)
        print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3} holes={holes}: "
              f"TOP5 [{fmt_scores(s)}]")
        print(f"    -> NCC pick={s[0][0]}  hole-filter pick="
              f"{matching[0][0] if matching else '(none)'}")
else:
    print("capture2 로드 실패")


# === 이미지 4 분석 (1등캡처.jpg) — 1 confusable 검증 ===
print("\n" + "="*60)
print("=== 이미지4: 1등캡처.jpg — 1 confusable 검증 ===")
print("="*60)
CAPTURE4 = rf"{BASE_DIR}\1등캡처.jpg"
capture4 = imread_unicode(CAPTURE4)
if capture4 is not None:
    H4, W4 = capture4.shape[:2]
    print(f"capture4 shape: {W4}x{H4}")
    sx4 = W4 / 2560
    sy4 = H4 / 1440
    def scale_roi4(roi):
        x, y, w, h = roi
        return (int(x*sx4), int(y*sy4), int(w*sx4), int(h*sy4))
    rank_roi4 = scale_roi4([1021, 485, 344, 108])
    pts_roi4  = scale_roi4([484, 488, 267, 101])
    print(f"  rank_roi4: {rank_roi4}")
    print(f"  pts_roi4:  {pts_roi4}")
    rank_crop4 = crop(capture4, rank_roi4)
    pts_crop4  = crop(capture4, pts_roi4)
    imwrite_unicode(rf"{BASE_DIR}\_dbg_rank_crop4.png", rank_crop4)
    imwrite_unicode(rf"{BASE_DIR}\_dbg_pts_crop4.png", pts_crop4)

    mask_rank4, comps_rank4_all = extract_components(rank_crop4)
    mask_pts4, comps_pts4_all   = extract_components(pts_crop4)
    kept_rank4 = filter_height_gap(comps_rank4_all)
    print(f"[rank4] all={len(comps_rank4_all)} kept={len(kept_rank4)} "
          f"heights={sorted([c[3] for c in kept_rank4], reverse=True)}")

    print("\n=== RANK4 매칭 (정답: '1' — 1과 7 confusable 직접 검증) ===")
    sorted_rank4 = sorted(kept_rank4, key=lambda c: c[0])
    for i, c in enumerate(sorted_rank4):
        holes, s, matching = match_all(c, mask_rank4)
        print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3} holes={holes}: "
              f"TOP5 [{fmt_scores(s)}]")
        print(f"    -> NCC pick={s[0][0]}  hole-filter pick="
              f"{matching[0][0] if matching else '(none)'}")

    print("\n=== PTS4 매칭 ALL (정답: '4,552') ===")
    sorted_pts4 = sorted(comps_pts4_all, key=lambda c: c[0])
    for i, c in enumerate(sorted_pts4):
        holes, s, matching = match_all(c, mask_pts4)
        print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3} holes={holes}: "
              f"TOP5 [{fmt_scores(s)}]")
        print(f"    -> NCC pick={s[0][0]}  hole-filter pick="
              f"{matching[0][0] if matching else '(none)'}")
else:
    print("capture4 로드 실패")


# === 이미지 1 분석 (11등.jpg) — 11 confusable 직접 검증 ===
print("\n" + "="*60)
print("=== 이미지1: 11등.jpg — '11' 두 자리 1 confusable 검증 ===")
print("="*60)
CAPTURE1B = rf"{BASE_DIR}\11등.jpg"
capture1b = imread_unicode(CAPTURE1B)
if capture1b is not None:
    H1, W1 = capture1b.shape[:2]
    print(f"capture1b shape: {W1}x{H1}")
    sx1 = W1 / 2560
    sy1 = H1 / 1440
    def scale_roi1(roi):
        x, y, w, h = roi
        return (int(x*sx1), int(y*sy1), int(w*sx1), int(h*sy1))
    rank_roi1 = scale_roi1([1021, 485, 344, 108])
    pts_roi1  = scale_roi1([484, 488, 267, 101])
    rank_crop1b = crop(capture1b, rank_roi1)
    pts_crop1b  = crop(capture1b, pts_roi1)
    imwrite_unicode(rf"{BASE_DIR}\_dbg_rank_crop1b.png", rank_crop1b)
    imwrite_unicode(rf"{BASE_DIR}\_dbg_pts_crop1b.png", pts_crop1b)

    mask_rank1b, comps_rank1b_all = extract_components(rank_crop1b)
    kept_rank1b = filter_height_gap(comps_rank1b_all)
    print(f"[rank1b] all={len(comps_rank1b_all)} kept={len(kept_rank1b)} "
          f"heights={sorted([c[3] for c in kept_rank1b], reverse=True)}")

    print("\n=== RANK1b 매칭 (정답: '11') ===")
    sorted_rank1b = sorted(kept_rank1b, key=lambda c: c[0])
    for i, c in enumerate(sorted_rank1b):
        holes, s, matching = match_all(c, mask_rank1b)
        print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3} holes={holes}: "
              f"TOP5 [{fmt_scores(s)}]")
        print(f"    -> NCC pick={s[0][0]}  hole-filter pick="
              f"{matching[0][0] if matching else '(none)'}")

    mask_pts1b, comps_pts1b_all = extract_components(pts_crop1b)
    print("\n=== PTS1b 매칭 ALL (정답: '4,444') ===")
    sorted_pts1b = sorted(comps_pts1b_all, key=lambda c: c[0])
    for i, c in enumerate(sorted_pts1b):
        holes, s, matching = match_all(c, mask_pts1b)
        print(f"  Comp[{i}] x={c[0]:>3} w={c[2]:>3} h={c[3]:>3} holes={holes}: "
              f"TOP5 [{fmt_scores(s)}]")
        print(f"    -> NCC pick={s[0][0]}  hole-filter pick="
              f"{matching[0][0] if matching else '(none)'}")
else:
    print("capture1b 로드 실패")
