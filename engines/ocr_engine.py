#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocr_engine.py - 스캔 PDF / 글자깨짐 페이지 OCR (Tesseract + OpenCV)
======================================================================
pdf_engine 에서 사용합니다. 단독 실행으로 한 페이지 OCR 결과를 확인할 수 있습니다:

    .venv\\Scripts\\python.exe -m engines.ocr_engine <PDF> --page 3 --debug

기능
- 스캔 페이지 전체 OCR: 400 dpi 렌더링 → 기울기 자동 보정(deskew) → Tesseract LSTM
- 표 격자 자동 인식: 가로/세로 괘선을 형태학 연산으로 찾아 셀 단위로 글자를 묶음
  (셀 경계를 넘지 않도록 번역문 배치에 사용)
- 신뢰도 낮은 줄 재인식: 해당 줄만 잘라 이진화/반전/확대 후 단일 줄 모드(psm 7)로 다시 OCR,
  더 좋은 결과를 채택
- 그림(래스터 이미지) 속 글자 OCR: 희소 텍스트 모드(psm 11)로 라벨/범례 인식
- 글자 배경색/글자색/굵기/크기 추정: 번역문을 덮어쓸 때 원본과 같은 색으로 패치
"""
from __future__ import annotations

import os
import math
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Optional

try:
    import numpy as np
    import cv2
except ImportError:  # pragma: no cover
    np = None  # type: ignore
    cv2 = None  # type: ignore

try:
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore

OCR_VERSION = "21"  # OCR 로직이 바뀌면 올려서 캐시를 무효화

DEFAULT_BULLET_RE = re.compile(
    r"^\s*(?:[•●▪■◦○‣·*\-–—]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\d+(?:\.\d+)+\.?)\s+"
)
NOISE_RE = re.compile(r"^[\W_]+$")

TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
]


# ----------------------------------------------------------------------------
# 데이터 구조
# ----------------------------------------------------------------------------
@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float
    grp: int = -1  # Tesseract 의 (블록, 문단, 줄) 번호 → 중복 인식 판별용

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Line:
    words: list
    marker: bool = False   # 줄 맨 앞의 글머리표/로고 조각을 떼어낸 줄 (새 문단의 시작)

    @property
    def x0(self):
        return min(w.x0 for w in self.words)

    @property
    def x1(self):
        return max(w.x1 for w in self.words)

    @property
    def y0(self):
        return min(w.y0 for w in self.words)

    @property
    def y1(self):
        return max(w.y1 for w in self.words)

    @property
    def h(self):
        return median([w.h for w in self.words])

    @property
    def text(self):
        return " ".join(w.text for w in sorted(self.words, key=lambda w: w.x0))

    @property
    def conf(self):
        tot = sum(len(w.text) for w in self.words) or 1
        return sum(w.conf * len(w.text) for w in self.words) / tot


@dataclass
class OcrBlock:
    """OCR로 찾은 텍스트 덩어리 1개 (번역 단위). 좌표는 PDF pt(페이지 좌표)."""
    rect: tuple
    text: str
    conf: float
    size: float
    origin_y: float
    align: str = "left"
    bold: bool = False
    fg: tuple = (0.0, 0.0, 0.0)
    bg: tuple = (1.0, 1.0, 1.0)
    cell: Optional[tuple] = None
    source: str = "ocr-page"   # ocr-page | ocr-figure
    n_lines: int = 1
    lines: list = field(default_factory=list)  # [(x0,y0,x1,y1,text), ...]
    busy: bool = False         # 사진/무늬 배경 위의 짧은 글자 → 번역하지 않음(덧씌우면 그림이 망가짐). 제목 판단에는 사용
    uls: list = field(default_factory=list)  # 줄 전체 밑줄 [(x0, y0, x1, y1), ...] pt → 번역문 폭에 맞춰 다시 그림

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "OcrBlock":
        d = dict(d)
        d["rect"] = tuple(d["rect"])
        d["fg"] = tuple(d["fg"])
        d["bg"] = tuple(d["bg"])
        if d.get("cell"):
            d["cell"] = tuple(d["cell"])
        return cls(**d)


# ----------------------------------------------------------------------------
# 이미지 처리 유틸
# ----------------------------------------------------------------------------
def pixmap_to_rgb(pix) -> "np.ndarray":
    """PyMuPDF Pixmap → numpy RGB (H, W, 3) uint8."""
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    elif pix.n == 1:
        arr = np.repeat(arr, 3, axis=2)
    return np.ascontiguousarray(arr)


def estimate_skew(gray: "np.ndarray", max_angle: float = 3.0) -> float:
    """수평 투영 프로파일 분산이 최대가 되는 각도(도) 추정. 양수 = 반시계."""
    h, w = gray.shape
    scale = 1200.0 / max(w, 1)
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if bw.mean() < 1.0:  # 거의 빈 페이지
        return 0.0
    sh, sw = bw.shape
    center = (sw / 2, sh / 2)

    def score(a: float) -> float:
        M = cv2.getRotationMatrix2D(center, a, 1.0)
        rot = cv2.warpAffine(bw, M, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        prof = rot.sum(axis=1, dtype=np.float64)
        return float(np.sum(np.diff(prof) ** 2))

    best, best_s = 0.0, score(0.0)
    a = -max_angle
    while a <= max_angle + 1e-9:
        s = score(a)
        if s > best_s:
            best, best_s = a, s
        a += 0.25
    lo, hi = best - 0.25, best + 0.25
    a = lo
    while a <= hi + 1e-9:
        s = score(a)
        if s > best_s:
            best, best_s = a, s
        a += 0.05
    return round(best, 2)


def detect_table_cells(gray: "np.ndarray") -> list[tuple[int, int, int, int]]:
    """괘선으로 둘러싸인 표 셀(px) 목록. 2x2 이상 격자만 표로 인정."""
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(255 - gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(25, w // 45), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(25, h // 70)))
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk)
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    grid = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((3, 3), np.uint8), iterations=2)
    joints = cv2.bitwise_and(cv2.dilate(horiz, np.ones((5, 5), np.uint8)),
                             cv2.dilate(vert, np.ones((5, 5), np.uint8)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(grid, connectivity=8)
    cells: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, cw, ch, _area = stats[i]
        if cw < w * 0.08 or ch < 30:
            continue
        region_joints = joints[y:y + ch, x:x + cw]
        nj, _, _, _ = cv2.connectedComponentsWithStats(region_joints, connectivity=8)
        if nj - 1 < 6:  # 교차점이 너무 적으면 표가 아님(단순 박스/밑줄)
            continue
        sub = (labels[y:y + ch, x:x + cw] == i).astype(np.uint8) * 255
        inv = cv2.bitwise_not(sub)
        m, _lab, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
        found = []
        for j in range(1, m):
            cx, cy, cw2, ch2, a2 = st[j]
            if cx <= 0 or cy <= 0 or cx + cw2 >= cw or cy + ch2 >= ch:
                continue  # 표 바깥 영역
            if cw2 < 12 or ch2 < 10 or a2 > 0.9 * cw * ch:
                continue
            found.append((x + cx, y + cy, x + cx + cw2, y + cy + ch2))
        if len(found) >= 4:
            cells += found
    return cells


def sample_colors(rgb: "np.ndarray", box: tuple[int, int, int, int]) -> tuple[tuple, tuple, float]:
    """박스 안 글자색(fg), 배경색(bg), 획 두께/높이 비율 추정. 색은 0~1 float."""
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), 0.0
    crop = rgb[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    if gray.std() < 6:  # 단색 → 글자 없음
        c = tuple(float(v) / 255 for v in np.median(crop.reshape(-1, 3), axis=0))
        return (0.0, 0.0, 0.0), c, 0.0
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)  # 어두운 쪽 = 255
    dark = mask > 0
    if dark.mean() > 0.5:  # 어두운 배경에 밝은 글자
        dark = ~dark
    fg_px = crop[dark]
    bg_px = crop[~dark]
    fg = tuple(float(v) / 255 for v in np.median(fg_px, axis=0)) if len(fg_px) else (0.0, 0.0, 0.0)
    bg = tuple(float(v) / 255 for v in np.median(bg_px, axis=0)) if len(bg_px) else (1.0, 1.0, 1.0)
    # 거의 흰색/검정 배경은 순수 흰색/검정으로 스냅 (스캔 잡음 제거)
    if min(bg) > 0.985:
        bg = (1.0, 1.0, 1.0)
    if max(fg) < 0.25:
        fg = (0.0, 0.0, 0.0)
    m8 = dark.astype(np.uint8)
    dt = cv2.distanceTransform(m8, cv2.DIST_L2, 3)
    stroke = 4.0 * float(dt[dark].mean()) if dark.any() else 0.0
    return fg, bg, stroke / max(1.0, (y1 - y0))


def strip_rule_bars(w: Word) -> Word:
    """표의 세로 괘선을 '|' 로 읽은 것은 글자가 아니다. 남겨 두면 옆 칸 글자와 한 줄로 이어지고
    (예: '500 | 1000 | 2000'), 그 줄을 지우고 다시 쓸 때 괘선까지 지워진다.
    '|' 만으로 된 낱말은 text='' (버림), 앞뒤에 붙은 '|' 는 뗀다 ('|125' → '125', 상자는 글자 수 비율만큼 줄임)."""
    t = w.text
    a = len(t) - len(t.lstrip("|¦‖"))
    if a and t[a:a + 1].isupper():
        a = 0  # '|Q3' '|P68': 대문자 I 를 '|' 로 읽은 것 (pdf_engine.fix_ocr_tokens 가 I 로 고친다)
    b = len(t) - len(t.rstrip("|¦‖"))
    if not (a or b) or a + b >= len(t):
        return w if not (a or b) else Word(w.x0, w.y0, w.x1, w.y1, "", w.conf, w.grp)
    cw = (w.x1 - w.x0) / len(t)
    return Word(w.x0 + a * cw, w.y0, w.x1 - b * cw, w.y1, t[a:len(t) - b], w.conf, w.grp)


def dedupe_words(words: list[Word]) -> list[Word]:
    """Tesseract 가 한 줄을 두 번(서로 다른 줄 번호로) 인식해 같은 자리에 단어가 겹쳐 나오는 경우가 있다
    (예: 'tailored to hen the needs of f our our clients'). 다른 줄 번호의 단어끼리
    상자가 많이 겹치거나(작은 쪽 폭의 절반 이상), 맞닿아 있으면서 한쪽 글자가 다른 쪽에 들어 있으면('f'⊂'of')
    신뢰도가 높은 쪽 하나만 남긴다. 같은 줄 안의 단어, 맞닿기만 한 다른 낱말('Pp' 'inion')은 건드리지 않는다."""
    def core(t: str) -> str:
        return re.sub(r"[^0-9a-z]", "", t.lower())
    ws = sorted(words, key=lambda w: (-w.conf, -(w.x1 - w.x0)))
    kept: list[Word] = []
    for w in ws:
        dup = False
        for k in kept:
            if k.grp == w.grp and w.grp != -1:
                continue
            hmin = max(1.0, min(k.h, w.h))
            if min(k.y1, w.y1) - max(k.y0, w.y0) < 0.5 * hmin:
                continue
            ov = min(k.x1, w.x1) - max(k.x0, w.x0)
            if ov >= 0.5 * max(1.0, min(k.x1 - k.x0, w.x1 - w.x0)):
                dup = True
                break
            a, b = core(k.text), core(w.text)
            if ov > -0.15 * hmin and a and b and (a in b or b in a):
                dup = True
                break
        if not dup:
            kept.append(w)
    return kept


def em_factor(text: str, dots: bool = True) -> float:
    """글자 상자 높이 ÷ 글자 크기(em). 낱말에 든 글자 모양에 따라 다르다:
    'Optional:'(위·아래로 다 뻗음) ≈ 0.93, 'Supply'/'ISO'(위로만) ≈ 0.72, 'mm'(소문자 몸통만) ≈ 0.50.
    평균값 하나로 나누면 낱말마다 글자 크기가 들쭉날쭉해진다."""
    # i/j 의 점은 대문자 높이까지 올라간다 ('size' 를 'mm' 처럼 보면 글자 크기를 1.4배로 잰다).
    # dots=False: 점을 뺀 글자 띠 높이로 잴 때
    asc = bool(re.search(r"[A-Zbdfhklt0-9(){}\[\]/|!?%&$#@" + ("ij" if dots else "") + "]", text))
    desc = bool(re.search(r"[gjpqy(){}\[\],;|]", text))
    if asc and desc:
        return 0.93
    if asc or desc:
        return 0.72
    return 0.50


def bg_busyness(gray: "np.ndarray", box: tuple, pad: int) -> float:
    """글자 주변 배경의 얼룩 정도(밝기 표준편차). 종이/단색 배경은 작고, 사진 위는 크다."""
    H, W = gray.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0
    crop = gray[y0:y1, x0:x1]
    _, mask = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dark = mask > 0
    if dark.mean() > 0.5:
        dark = ~dark
    bg = crop[~dark]
    return float(bg.std()) if bg.size else 0.0


def snap_sizes(blocks: list) -> None:
    """한 페이지의 글자 크기를 몇 가지 대표값으로 맞춘다 (인쇄물은 본문/제목 등 몇 가지 크기만 쓴다).
    OCR 로 잰 크기는 줄마다 ±10% 흔들리므로, 가까운 크기끼리 묶어 글자 수 기준 중앙값을 대표값으로 정한 뒤
    각 문구를 가장 가까운 대표값(18% 이내)으로 맞춘다. 묶음 경계에 걸린 값이 옆 묶음 대표값으로 끌려가지 않게 한다."""
    items = sorted(((b.size, max(1, len(b.text)), b) for b in blocks), key=lambda t: t[0])
    reps: list[float] = []
    group: list = []

    def flush():
        if len(group) < 2:
            return
        tot = sum(n for _, n, _ in group)
        acc = 0
        for s_, n, _ in group:
            acc += n
            if acc * 2 >= tot:
                reps.append(float(s_))
                break
    for it in items:
        if group and it[0] > group[0][0] * 1.18:
            flush()
            group = []
        group.append(it)
    flush()
    if not reps:
        return
    for s_, _, b in items:
        r = min(reps, key=lambda r: abs(math.log(r / max(s_, 0.1))))
        if max(r, s_) <= min(r, s_) * 1.18:
            b.size = round(r, 2)


def find_underline(gray: "np.ndarray", box: tuple, h: float) -> Optional[tuple]:
    """줄 상자 바로 아래(또는 상자 아래쪽 디센더 영역)의 밑줄: 글자 폭의 85% 이상을 덮는 가는 가로선.
    양옆으로 글자보다 훨씬 길게 이어지면 표 괘선/구분선이므로 밑줄이 아니다. 반환: (x0, y0, x1, y1) px"""
    H, W = gray.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    if x1 - x0 < 8 or h < 4:
        return None
    pad = int(0.6 * h)
    ya, yb = max(0, int(y1 - 0.3 * h)), min(H, int(y1 + 0.45 * h) + 1)
    xa, xb = max(0, x0 - pad), min(W, x1 + pad)
    if yb - ya < 2:
        return None
    crop = gray[ya:yb, xa:xb]
    thr = min(140, float(np.percentile(gray[max(0, y0):min(H, y1 + 1), max(0, x0):min(W, x1)], 5)) + 60) \
        if y1 > y0 and x1 > x0 else 140
    dark = crop < thr
    width = x1 - x0
    rows = []
    for i in range(dark.shape[0]):
        r = dark[i]
        if r[pad:pad + width].mean() < 0.85:
            continue
        rows.append(i)
    if not rows:
        return None
    # 연속된 행 묶음 중 가장 아래쪽(글자 몸통과 떨어진) 묶음
    groups: list[list[int]] = []
    for i in rows:
        if groups and i - groups[-1][-1] <= 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    g = groups[-1]
    if len(g) > max(2, 0.15 * h):
        return None  # 두꺼운 띠 = 글자 몸통/배경
    band = dark[g[0]:g[-1] + 1].any(axis=0)
    # 글자 폭 밖으로도 이어지면 표 괘선
    if band[:max(1, pad // 2)].mean() > 0.6 or band[-max(1, pad // 2):].mean() > 0.6:
        return None
    on = np.where(band)[0]
    return (xa + int(on[0]), ya + g[0], xa + int(on[-1]) + 1, ya + g[-1] + 1)


def ink_band_height(gray: "np.ndarray", box: tuple) -> tuple[float, int]:
    """줄 상자 안에서 글자가 실제로 차지하는 가로 띠의 높이(중앙값)와 띠 개수.
    Tesseract 가 위아래 두 행을 한 줄로 묶으면 상자 높이가 실제 글자의 2~3배가 된다 → 글자 크기 과대 추정 방지."""
    H, W = gray.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 - x0 < 4 or y1 - y0 < 6:
        return 0.0, 0
    crop = gray[y0:y1, x0:x1]
    if crop.std() < 6:
        return 0.0, 0
    _, mask = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dark = mask > 0
    if dark.mean() > 0.5:
        dark = ~dark
    rows = dark.mean(axis=1) > 0.02
    bands: list[list[int]] = []
    for i, on in enumerate(rows):
        if on:
            if bands and i - bands[-1][1] <= 1:
                bands[-1][1] = i
            else:
                bands.append([i, i])
    if not bands:
        return 0.0, 0
    tall = max(b[1] - b[0] + 1 for b in bands)
    merged: list[list[int]] = []
    for b in bands:  # i 의 점, 악센트처럼 가까운 띠는 합친다
        if merged and b[0] - merged[-1][1] < 0.2 * tall:
            merged[-1][1] = b[1]
        else:
            merged.append(list(b))
    # i 점·악센트·밑줄처럼 낮은 띠(가장 높은 띠의 35% 미만)는 글자 행이 아니다 → 행 수/높이 계산에서 뺀다
    tall = max(b[1] - b[0] + 1 for b in merged)
    hs = [b[1] - b[0] + 1 for b in merged if b[1] - b[0] + 1 >= max(4, 0.35 * tall)]
    if not hs:
        return 0.0, 0
    return float(median(hs)), len(hs)


# ----------------------------------------------------------------------------
# 단어 → 줄 → 블록 묶기
# ----------------------------------------------------------------------------
LABEL_RE = re.compile(r"^\(?\d{1,2}[.)]$|^\(?[A-Za-z][.)]$")  # '1.' '(2)' 'a)' - 번호/기호 목록은 글에 포함


def is_junk_marker(t: str, conf: float = 100.0, nxt: str = "") -> bool:
    """줄 맨 앞의 글머리표 조각 또는 로고/아이콘을 글자로 잘못 읽은 것 (')' '}' '»' ':Q)' 'Q)' …).
    번호 목록('1.', 'a)')은 글의 일부이므로 해당 없음. 단 신뢰도가 낮은(70 미만) 짧은 기호 섞인 조각은 로고로 본다.
    nxt: 바로 뒤 낱말. 꺾쇠/동그라미 글머리표를 'y' 'e' 'o' 같은 소문자 한 글자로 읽는 경우가 많다 →
    대문자로 시작하는 낱말 앞의 외톨이 소문자 한 글자(관사 a 제외)는 글머리표로 본다."""
    if len(t) > 4:
        return False
    if len(t) == 1 and t.islower() and t != "a" and nxt[:1].isupper():
        return True
    alnum = sum(c.isalnum() for c in t)
    if alnum < len(t) and len(t) <= 3 and conf < 70 and not re.fullmatch(r"\(?\d{1,2}[.)]", t):
        return True  # 낮은 신뢰도 + 기호 포함: 로고/아이콘
    if LABEL_RE.match(t):
        return False
    return alnum == 0 or (alnum <= 1 and len(t) - alnum >= 2)


def words_to_lines(words: list[Word], gap_factor: float) -> list[Line]:
    """단어를 줄로 묶는다. 왼쪽에서 오른쪽으로, 바로 왼쪽에 있는(세로로 겹치고 간격이 좁은) 단어 뒤에 이어 붙인다.
    페이지 전체 폭으로 행을 만들지 않는다 → 2단 편집에서 옆 단의 글자(기준선이 조금 다름) 때문에
    한 줄이 여러 조각으로 갈라지지 않는다. 간격이 넓으면(표의 열, 다른 단) 새 줄."""
    chains: list[dict] = []
    for w in sorted(words, key=lambda w: (w.x0, w.y0)):
        best, best_gap = None, 0.0
        for c in chains:
            gap = w.x0 - c["x1"]
            if gap > c["max_gap"]:
                continue
            ov = min(c["y1"], w.y1) - max(c["y0"], w.y0)
            # 앞 단어 상자가 너무 넓게 잡혀 이 단어를 덮은 경우(gap < 0)도 같은 줄로 잇되, 세로 겹침을 더 엄격히 본다
            need = 0.5 if gap >= -0.5 * max(c["h"], w.h) else 0.7
            if ov < need * min(c["y1"] - c["y0"], w.h):
                continue
            g = max(gap, 0.0)
            if best is None or g < best_gap:
                best, best_gap = c, g
        if best is None:
            chains.append({"words": [w], "x1": w.x1, "y0": w.y0, "y1": w.y1, "h": w.h,
                           "max_gap": max(gap_factor * w.h, 6)})
            continue
        ws = best["words"]
        ws.append(w)
        tail = ws[-5:]  # 줄이 조금 기울어도 따라가도록 최근 단어 기준
        best["x1"] = max(best["x1"], w.x1)
        best["y0"] = median([x.y0 for x in tail])
        best["y1"] = median([x.y1 for x in tail])
        best["h"] = median([x.h for x in ws])
        best["max_gap"] = max(gap_factor * best["h"], 6)
    out: list[Line] = []
    for c in chains:
        ws = sorted(c["words"], key=lambda w: w.x0)
        marker = False
        # 맨 앞 조각이 글머리표/아이콘이면 글에서 떼어 낸다 → 원본 그림에 그대로 남고(덮지 않음) 번역문에 잡음이 끼지 않는다
        for _ in range(2):  # 로고가 두 조각('Q)' 'aN')으로 읽히기도 한다
            if len(ws) < 2 or ws[1].x0 - ws[0].x1 > 3.5 * max(ws[1].h, 1.0):
                break
            w0 = ws[0]
            if is_junk_marker(w0.text, w0.conf, ws[1].text) or (marker and len(w0.text) <= 2 and w0.conf <= 60):
                # 로고 상자 안에 들어 있는 작은 조각들(로고의 일부를 글자로 읽은 것)도 함께 뗀다
                ws = [w for w in ws[1:] if not (w.x0 >= w0.x0 - 1 and w.x1 <= w0.x1 + 1 and w.h < 0.8 * w0.h)]
                marker = True
            else:
                break
        out.append(Line(ws, marker))
    return out


CONT_WORD_RE = re.compile(r"(?:of|and|or|for|to|in|on|at|by|with|from|when|where|which|that|the|as|into|"
                          r"than|while|during|under|over|between|via|per|without|within)\b")
CONT_END_RE = re.compile(r"(?:\b(?:of|and|or|for|to|in|on|at|by|with|from|the|a|an)|[,&/-])$", re.I)


def _line_break_intended(last: Line, ln: Line, right: float) -> bool:
    """윗줄 끝에 다음 줄 첫 낱말이 들어갈 자리가 있었는데도 줄을 바꿨는가 (= 목록/표의 다른 항목).
    문장이 끝나지 않았고 다음 줄이 소문자로 시작하면(이어지는 문장) 자리가 조금 남아도 이어진 것으로 본다."""
    ws = sorted(ln.words, key=lambda w: w.x0)
    if not ws:
        return False
    fw = ws[0].x1 - ws[0].x0
    avail = right - last.x1
    if fw * 1.15 + 0.3 * last.h > avail:
        return False
    t, n = last.text.rstrip(), ln.text.lstrip()
    words_t = re.findall(r"[A-Za-z]{2,}", t)
    # 'Actuator' / 'size' 처럼 한 낱말 머리글이 두 줄로 나뉜 경우도 이어진 글로 본다 (둘째 줄이 짧고 소문자로 시작)
    soft = (not re.search(r"[.:;!?]['\")\]]?$", t) and bool(re.match(r"[\"'(\[]*[a-z]", n))
            and (len(words_t) >= 2 or (len(words_t) == 1 and len(words_t[0]) >= 3 and len(ln.words) <= 2)))
    filled = (last.x1 - last.x0) >= 0.6 * max(right - last.x0, 1.0)
    if soft and len(words_t) == 1:
        return False  # 한 낱말 머리글 + 소문자로 시작하는 짧은 줄 ('Actuator' / 'size') = 두 줄로 나눈 머리글
    if soft and (CONT_WORD_RE.match(n) or CONT_END_RE.search(t)):
        return False  # 'Low cost / of ownership' 처럼 일부러 줄을 나눈 제목: 새 항목이 of/and… 로 시작하지 않는다
    if CONT_END_RE.search(t) and re.match(r"[\"'(\[]*[a-z]", n):
        return False
    return not (soft and (filled or avail < 2.0 * fw))


def _subheader_row(ln: Line, last: Line, lines: list[Line]) -> bool:
    """ln 이 표 머리글 아래의 '소제목 행'(예: 'IQ (3-Phase)' 아래의 'Min   Max')의 한 칸인가.
    같은 높이에 짧은 낱말이 크게 떨어져 나란히 있고, 그 옆 칸도 윗줄 아래에 들어 있으면 윗줄의 이어진 글이 아니다."""
    if len(ln.words) > 2:
        return False
    for m in lines:
        if m is ln or m is last or len(m.words) > 2:
            continue
        ov = min(m.y1, ln.y1) - max(m.y0, ln.y0)
        if ov < 0.5 * min(m.y1 - m.y0, ln.y1 - ln.y0):
            continue
        gap = m.x0 - ln.x1 if m.x0 >= ln.x1 else ln.x0 - m.x1
        if gap >= 2.0 * ln.h and m.x0 < last.x1 and m.x1 > last.x0:
            return True
    return False


def lines_to_blocks(lines: list[Line], bullet_re, merge_all: bool = False,
                    right: Optional[float] = None) -> list[list[Line]]:
    """줄을 문단(블록)으로 묶는다. merge_all=True(표 셀)면 글머리표 외에는 모두 하나로.
    right: 줄이 쓸 수 있는 오른쪽 한계(표 셀의 오른쪽 변). 없으면 같은 시작점의 줄 중 가장 긴 줄의 끝."""
    lines = sorted(lines, key=lambda l: (l.y0, l.x0))
    blocks: list[list[Line]] = []
    for ln in lines:
        is_bullet = bool(bullet_re.match(ln.text)) or ln.marker
        target = None
        if not is_bullet:
            for b in reversed(blocks[-6:]):
                last = b[-1]
                if merge_all:
                    target = b
                    break
                gap = ln.y0 - last.y1
                hr = ln.h / max(last.h, 1)
                xov = min(ln.x1, last.x1) - max(ln.x0, last.x0)
                aligned = (abs(ln.x0 - last.x0) <= 1.5 * last.h
                           or abs((ln.x0 + ln.x1) / 2 - (last.x0 + last.x1) / 2) <= 1.5 * last.h
                           or (xov > 0.5 * min(ln.x1 - ln.x0, last.x1 - last.x0)
                               and abs(ln.x0 - last.x0) <= 3.5 * last.h))
                if -0.3 * last.h <= gap <= 0.9 * last.h and 0.7 <= hr <= 1.45 and xov > 0 and aligned \
                        and not _subheader_row(ln, last, lines):
                    # 오른쪽 한계: 같은 시작점의 줄 중 가장 긴 줄. 더 작은 글자 줄(제목 아래 본문)은 제목의 폭 기준이 아님
                    lim = right if right is not None else max(
                        [l.x1 for l in lines if abs(l.x0 - last.x0) <= 1.5 * last.h
                         and l.h >= 0.8 * min(last.h, ln.h)] + [last.x1, ln.x1])
                    if _line_break_intended(last, ln, lim):
                        continue  # 목록의 다음 항목: 앞 문단에 붙이지 않는다
                    target = b
                    break
        if target is not None:
            target.append(ln)
        else:
            blocks.append([ln])
    return blocks


def absorb_row_fragments(blocks: list[list[Line]]) -> list[list[Line]]:
    """양쪽 정렬 문단에서 낱말 사이가 넓게 벌어져 따로 떨어진 줄 조각을 원래 줄에 다시 붙인다.
    조건: 여러 줄 문단의 어느 줄과 같은 높이에 있고, 그 줄 바로 오른쪽이며, 문단의 오른쪽 끝을 넘지 않는 한 줄짜리 조각.
    (표의 옆 칸은 문단의 폭 밖에 있으므로 붙지 않는다)"""
    changed = True
    while changed:
        changed = False
        for b in blocks:
            if len(b) < 2:
                continue
            right = max(l.x1 for l in b)
            for f in blocks:
                if f is b or len(f) != 1:
                    continue
                fl = f[0]
                if fl.x1 > right + fl.h:
                    continue
                for k, l in enumerate(b):
                    ov = min(l.y1, fl.y1) - max(l.y0, fl.y0)
                    if ov >= 0.5 * min(l.y1 - l.y0, fl.y1 - fl.y0) and 0 <= fl.x0 - l.x1 <= 4.0 * l.h:
                        b[k] = Line(l.words + fl.words)
                        blocks.remove(f)
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
    return blocks


def join_lines(lines: list[Line]) -> str:
    out = ""
    for ln in lines:
        t = ln.text.strip()
        if not out:
            out = t
        elif out.endswith("-") and len(out) > 2 and out[-2].isalpha() and t[:1].islower():
            out = out[:-1] + t  # 줄끝 하이픈 연결: main-\ntenance → maintenance
        else:
            out += " " + t
    return re.sub(r"\s+", " ", out).strip()


def detect_align(lines: list[Line]) -> str:
    if len(lines) < 2:
        return "left"
    L = [l.x0 for l in lines]
    R = [l.x1 for l in lines]
    C = [(l.x0 + l.x1) / 2 for l in lines]
    tol = 0.6 * median([l.h for l in lines])
    if max(L) - min(L) < tol:
        return "left"
    if max(C) - min(C) < tol:
        return "center"
    if max(R) - min(R) < tol:
        return "right"
    return "left"


# ----------------------------------------------------------------------------
# OCR 엔진
# ----------------------------------------------------------------------------
class OcrEngine:
    def __init__(self, lang: str = "eng", dpi: int = 400, min_conf: float = 50.0,
                 fig_min_conf: float = 70.0, tesseract_cmd: Optional[str] = None,
                 deskew: bool = True, tables: bool = True, retry_low_conf: bool = True,
                 bullet_re=None):
        self.lang = lang
        self.dpi = dpi
        self.min_conf = min_conf
        self.fig_min_conf = fig_min_conf
        self.deskew = deskew
        self.tables = tables
        self.retry_low_conf = retry_low_conf
        self.bullet_re = bullet_re or DEFAULT_BULLET_RE
        self.available = False
        self.reason = ""
        self.cmd = None
        if np is None or cv2 is None:
            self.reason = "numpy/opencv 미설치 (pip install opencv-python-headless numpy)"
            return
        if pytesseract is None:
            self.reason = "pytesseract 미설치 (pip install pytesseract)"
            return
        cmd = self.find_tesseract(tesseract_cmd)
        if not cmd:
            self.reason = ("Tesseract 실행파일을 찾지 못했습니다. "
                           "Windows: https://github.com/UB-Mannheim/tesseract/wiki 에서 설치 후 "
                           "--tesseract \"C:\\Program Files\\Tesseract-OCR\\tesseract.exe\" 지정")
            return
        pytesseract.pytesseract.tesseract_cmd = cmd
        self.cmd = cmd
        try:
            langs = set(pytesseract.get_languages(config=""))
        except Exception as e:  # noqa: BLE001
            self.reason = f"Tesseract 실행 실패: {e}"
            return
        missing = [l for l in lang.split("+") if l not in langs]
        if missing:
            self.reason = f"Tesseract 언어 데이터 없음: {missing} (설치된 언어: {sorted(langs)})"
            return
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # 병렬 실행 시 과도한 스레드 방지
        self.available = True

    @staticmethod
    def find_tesseract(explicit: Optional[str]) -> Optional[str]:
        if explicit:
            return explicit if os.path.exists(explicit) else None
        w = shutil.which("tesseract")
        if w:
            return w
        for p in TESSERACT_CANDIDATES:
            if p and os.path.exists(p):
                return p
        return None

    def signature(self) -> str:
        return (f"v{OCR_VERSION}|{self.lang}|{self.dpi}|{self.min_conf}|{self.fig_min_conf}|"
                f"{int(self.deskew)}|{int(self.tables)}|{int(self.retry_low_conf)}")

    # -------------------------------------------------------------- 렌더링(메인 스레드 전용)
    def render(self, page, clip=None, dpi: Optional[int] = None) -> "np.ndarray":
        pix = page.get_pixmap(dpi=dpi or self.dpi, clip=clip, alpha=False)
        return pixmap_to_rgb(pix)

    # -------------------------------------------------------------- Tesseract 호출
    def _tess(self, img, psm: int) -> list[Word]:
        cfg = f"--oem 1 --psm {psm} -c preserve_interword_spaces=1"
        d = pytesseract.image_to_data(img, lang=self.lang, config=cfg,
                                      output_type=pytesseract.Output.DICT)
        out = []
        for i, t in enumerate(d["text"]):
            t = (t or "").strip()
            try:
                c = float(d["conf"][i])
            except (TypeError, ValueError):
                c = -1
            if not t or c < 0:
                continue
            x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            grp = (d["block_num"][i] * 1000 + d["par_num"][i]) * 1000 + d["line_num"][i] if "line_num" in d else -1
            wd = strip_rule_bars(Word(x, y, x + w, y + h, t, c, grp))  # 표 괘선 '|' 는 글자가 아니다
            if wd.text:
                out.append(wd)
        return out

    def _ocr_crop(self, crop, psm: int) -> list[Word]:
        """잘라낸 영역 OCR: 작은 글자 확대, 어두운 배경 반전, 흰 여백 추가. 좌표는 crop 기준."""
        f = 1.0
        if crop.shape[0] < 60:
            f = min(3.0, 60.0 / max(crop.shape[0], 1))
            crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        if crop.mean() < 110:
            crop = 255 - crop
        crop = cv2.copyMakeBorder(crop, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
        try:
            ws = self._tess(crop, psm)
        except Exception:  # noqa: BLE001
            return []
        return [Word((w.x0 - 12) / f, (w.y0 - 12) / f, (w.x1 - 12) / f, (w.y1 - 12) / f, w.text, w.conf)
                for w in ws]

    def _retry_line(self, gray, ln: Line) -> Line:
        """신뢰도 낮은 줄만 잘라서 이진화/반전/확대 후 psm 7로 재인식."""
        H, W = gray.shape
        pad = int(max(4, 0.35 * ln.h))
        x0, y0 = max(0, int(ln.x0) - pad), max(0, int(ln.y0) - pad)
        x1, y1 = min(W, int(ln.x1) + pad), min(H, int(ln.y1) + pad)
        crop = gray[y0:y1, x0:x1]
        if crop.size == 0:
            return ln
        f = 1.0
        if ln.h < 28:
            f = min(3.0, 32.0 / max(ln.h, 1))
            crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        _, bw = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if (bw < 128).mean() > 0.5:
            bw = 255 - bw  # 흰 글자/어두운 배경 반전
        bw = cv2.copyMakeBorder(bw, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
        try:
            ws = self._tess(bw, 7)
        except Exception:  # noqa: BLE001
            return ln
        if not ws:
            return ln
        new = Line([Word(x0 + (w.x0 - 10) / f, y0 + (w.y0 - 10) / f, x0 + (w.x1 - 10) / f,
                         y0 + (w.y1 - 10) / f, w.text, w.conf) for w in ws])
        # 잘라낸 범위에 이웃 줄(아래 그림 라벨 등)이 걸려 그 글자를 읽은 경우는 버린다
        # (예: 그림 조각 'AL' 을 다시 읽어 아래 'Work station' 까지 합친 'a statiol' → 그림 전체를 덮는 상자)
        tol = 0.1 * ln.h
        if any(not (ln.y0 - tol <= w.cy <= ln.y1 + tol) for w in new.words):
            return ln  # 낱말 중심이 원래 줄 높이 밖(이웃 줄)에 있다
        return new if new.conf > ln.conf + 3 else ln

    def _ocr_words(self, gray, origin, scale) -> list[OcrBlock]:
        """글자깨짐 페이지용: 단어 단위 인식 결과만 돌려준다 (위치/크기는 PDF 원본 정보를 사용).
        1차 psm 3 + 인식된 단어를 지운 이미지로 psm 11 복구 패스."""
        Hh = gray.shape[0]
        words = self._tess(gray, 3)
        masked = gray.copy()
        for w in words:
            pad = int(0.3 * w.h) + 2
            masked[max(0, int(w.y0) - pad):int(w.y1) + pad, max(0, int(w.x0) - pad):int(w.x1) + pad] = 255
        try:
            words += [w for w in self._tess(masked, 11) if w.conf >= 60]
        except Exception:  # noqa: BLE001
            pass
        out = []
        for w in words:
            if w.h < 4 or w.h > Hh * 0.2 or w.conf < 20:
                continue
            if NOISE_RE.match(w.text) and w.conf < 85 and not set(w.text) <= set(".·…"):
                continue
            out.append(OcrBlock(
                rect=(float(origin[0] + w.x0 / scale), float(origin[1] + w.y0 / scale),
                      float(origin[0] + w.x1 / scale), float(origin[1] + w.y1 / scale)),
                text=w.text, conf=float(w.conf), size=float(w.h / scale / 0.78),
                origin_y=float(origin[1] + w.y1 / scale), source="ocr-words"))
        return out

    # -------------------------------------------------------------- 공개 API
    def ocr_image(self, rgb: "np.ndarray", origin: tuple[float, float], scale: float,
                  mode: str = "page", exclude_pt: Optional[list] = None) -> list[OcrBlock]:
        """rgb: 렌더링된 이미지, origin: 이미지 좌상단의 PDF 좌표(pt), scale: px/pt.
        mode='page'(스캔 페이지 전체) | 'figure'(그림 속 라벨) | 'words'(글자깨짐 페이지 단어). 스레드 안전."""
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        Hh, Ww = gray.shape
        angle = 0.0
        Minv = None
        if mode == "page" and self.deskew:
            angle = estimate_skew(gray)
            if abs(angle) >= 0.15:
                M = cv2.getRotationMatrix2D((Ww / 2, Hh / 2), angle, 1.0)
                gray = cv2.warpAffine(gray, M, (Ww, Hh), flags=cv2.INTER_CUBIC, borderValue=255)
                rgb = cv2.warpAffine(rgb, M, (Ww, Hh), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
                Minv = cv2.invertAffineTransform(M)
            else:
                angle = 0.0

        if mode == "words":
            return self._ocr_words(gray, origin, scale)

        cells = detect_table_cells(gray) if (mode == "page" and self.tables) else []
        words = self._tess(gray, 3 if mode == "page" else 11)
        min_conf = self.min_conf if mode == "page" else self.fig_min_conf

        # 표 괘선/그림 잡음 제거
        words = [w for w in words if not (NOISE_RE.match(w.text) and w.conf < 85)]
        words = [w for w in words if w.h >= 5 and w.h < Hh * 0.2]
        words = dedupe_words(words)

        # 복구 패스: Tesseract 레이아웃 분석이 '그림'으로 보고 건너뛴 글자(색 배경 표 헤더 등)를
        # 찾기 위해, 이미 인식한 단어를 지운 이미지를 희소 텍스트 모드(psm 11)로 한 번 더 OCR
        if mode == "page":
            masked = gray.copy()
            for w in words:
                pad = int(0.3 * w.h) + 2
                masked[max(0, int(w.y0) - pad):int(w.y1) + pad, max(0, int(w.x0) - pad):int(w.x1) + pad] = 255
            try:
                extra = self._tess(masked, 11)
            except Exception:  # noqa: BLE001
                extra = []
            words += [w for w in extra
                      if w.conf >= max(min_conf, 60) and not NOISE_RE.match(w.text) and 5 <= w.h < Hh * 0.2]

        # 이미 원본 텍스트가 있는 영역(그림 위 벡터 글자)은 제외
        if exclude_pt:
            ex = [((r[0] - origin[0]) * scale - 2, (r[1] - origin[1]) * scale - 2,
                   (r[2] - origin[0]) * scale + 2, (r[3] - origin[1]) * scale + 2) for r in exclude_pt]
            words = [w for w in words if not any(a <= w.cx <= c and b <= w.cy <= d for a, b, c, d in ex)]

        # 셀 배정
        cell_words: dict[int, list[Word]] = {}
        free: list[Word] = []
        for w in words:
            idx = None
            best = None
            for i, (a, b, c, d) in enumerate(cells):
                if a <= w.cx <= c and b <= w.cy <= d:
                    area = (c - a) * (d - b)
                    if best is None or area < best:
                        idx, best = i, area
            if idx is None:
                free.append(w)
            else:
                cell_words.setdefault(idx, []).append(w)

        # 아직 비어 있는 셀은 셀만 잘라서 개별 OCR (잉크가 있는 셀만)
        for i, (a, b, c, d) in enumerate(cells):
            if i in cell_words:
                continue
            crop = gray[b + 3:d - 2, a + 3:c - 2]
            if crop.size == 0 or (crop < 128).mean() < 0.004:
                continue
            ws = self._ocr_crop(crop, psm=6)
            ws = [Word(a + 3 + w.x0, b + 3 + w.y0, a + 3 + w.x1, b + 3 + w.y1, w.text, w.conf)
                  for w in ws if w.conf >= min_conf and not NOISE_RE.match(w.text)]
            if ws:
                cell_words[i] = ws

        gap = 1.8 if mode == "page" else 1.0  # 낱말 사이 줄표(—)가 빠져도 한 줄이 끊기지 않을 만큼
        groups: list[tuple[list[Line], Optional[tuple]]] = []
        for i, ws in cell_words.items():
            lns = words_to_lines(ws, gap_factor=3.0)  # 셀 안에서는 넓은 간격도 같은 줄
            for b in lines_to_blocks(lns, self.bullet_re, merge_all=False, right=cells[i][2] - 3):
                groups.append((b, cells[i]))
        for b in absorb_row_fragments(lines_to_blocks(words_to_lines(free, gap_factor=gap), self.bullet_re)):
            groups.append((b, None))

        # 낮은 신뢰도 줄 재인식
        if self.retry_low_conf:
            for gi, (lns, cell) in enumerate(groups):
                new = []
                for ln in lns:
                    if ln.conf < 80 and len(ln.text) >= 2:
                        ln = self._retry_line(gray, ln)
                    new.append(ln)
                groups[gi] = (new, cell)

        # 글자 높이 중앙값(굵기 판단 기준)
        blocks: list[OcrBlock] = []
        strokes = []
        tmp = []
        for lns, cell in groups:
            text = join_lines(lns)
            if not text:
                continue
            conf = sum(l.conf * len(l.text) for l in lns) / max(1, sum(len(l.text) for l in lns))
            if conf < min_conf:
                continue
            if mode == "page" and conf < 75 and len(re.sub(r"[^A-Za-z0-9]", "", text)) <= 3:
                continue  # 그림/아이콘을 글자로 잘못 읽은 짧은 조각('AL', 'x, a'): 덮어 쓰면 그림이 망가진다
            if mode == "figure" and not re.search(r"[A-Za-z]{2,}", text):
                continue
            x0 = min(l.x0 for l in lns)
            y0 = min(l.y0 for l in lns)
            x1 = max(l.x1 for l in lns)
            y1 = max(l.y1 for l in lns)
            fg, bg, stroke = sample_colors(rgb, (x0 - 1, y0 - 1, x1 + 1, y1 + 1))
            # 줄 단위 획 두께 (블록 높이 대신 줄 높이 기준)
            hs = []
            for l in lns:
                h = l.h
                bh, nb = ink_band_height(gray, (l.x0, l.y0, l.x1, l.y1))
                if nb >= 1 and bh < 0.75 * h:
                    h = bh  # 상자가 실제 글자보다 큼(두 행이 묶였거나 괘선이 걸림): 글자 띠 높이를 쓴다
                hs.append(h)
            hmed = median(hs)
            # 글자 크기(em, px): 낱말마다 글자 모양에 맞는 비율로 환산한 값의 중앙값
            ems = []
            for l, h in zip(lns, hs):
                if h < 0.99 * l.h:  # 띠 높이로 바꾼 줄
                    ems.append(h / em_factor(l.text, dots=False))
                else:
                    ems += [w.h / em_factor(w.text) for w in l.words if len(w.text) >= 2] or [l.h / 0.78]
            em_px = median(ems)
            # 사진 위의 짧은 글자(간판, 명판, 로고): 덧씌우면 사진이 망가지므로 건드리지 않는다
            n_words = sum(len(l.words) for l in lns)
            busy = bool(mode == "page" and n_words <= 3 and bg_busyness(gray, (x0, y0, x1, y1), int(hmed)) > 28)
            stroke_ratio = stroke * max(1.0, (y1 - y0)) / max(1.0, hmed)
            strokes.append(stroke_ratio)
            # 줄마다 밑줄 (표 셀 안은 괘선과 헷갈리므로 보지 않음)
            uls_px = []
            if mode == "page" and cell is None and not busy:
                for l, h in zip(lns, hs):
                    # 짧은 글자('2' 'e' '44')는 글자 자체의 아래 획을 밑줄로 오인하기 쉽다 → 3글자 이상, 폭 2.5×높이 이상만
                    if len(re.findall(r"[A-Za-z0-9]", l.text)) < 3 or (l.x1 - l.x0) < 2.5 * h:
                        continue
                    ul = find_underline(gray, (l.x0, l.y0, l.x1, l.y1), h)
                    if ul:
                        uls_px.append(ul)
                if uls_px and len(uls_px) < len(lns):
                    uls_px = []  # 일부 줄만 밑줄: 위치를 옮겨 그릴 수 없으므로 원본 그대로 두는 쪽이 안전 → 생략
            tmp.append((lns, cell, text, conf, (x0, y0, x1, y1), fg, bg, stroke_ratio, em_px, busy, uls_px))
        ref = median(strokes) if strokes else 0.0

        def to_pt(x, y):
            if Minv is not None:
                x, y = (Minv[0, 0] * x + Minv[0, 1] * y + Minv[0, 2],
                        Minv[1, 0] * x + Minv[1, 1] * y + Minv[1, 2])
            return float(origin[0] + x / scale), float(origin[1] + y / scale)

        def box_pt(x0, y0, x1, y1):
            pts = [to_pt(x0, y0), to_pt(x1, y0), to_pt(x0, y1), to_pt(x1, y1)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))

        def inner_box_pt(x0, y0, x1, y1):
            """기울기 보정된 셀의 '안쪽' 사각형 (기울어진 괘선을 패치가 덮지 않도록)."""
            pts = [to_pt(x0, y0), to_pt(x1, y0), to_pt(x0, y1), to_pt(x1, y1)]
            xs = sorted(p[0] for p in pts)
            ys = sorted(p[1] for p in pts)
            return (xs[1], ys[1], xs[2], ys[2])

        for lns, cell, text, conf, box, fg, bg, stroke_ratio, em_px, busy, uls_px in tmp:
            rect = box_pt(*box)
            size = em_px / scale
            if len(lns) > 1:
                # 윗변/아랫변 차이 중 큰 값: 한쪽 상자가 위·아래로 늘어나도(어센더/디센더, 이웃 줄 조각) 줄 간격을 작게 재지 않는다
                pitches = [max(lns[i + 1].y0 - lns[i].y0, lns[i + 1].y1 - lns[i].y1) for i in range(len(lns) - 1)]
                pitch = median(pitches) / scale
                if pitch > 0:
                    size = min(size, pitch / 1.15)
            size = max(4.0, min(size, 40.0))
            first = lns[0]
            origin_y = to_pt(first.x0, first.y1)[1] - 0.05 * size
            # 굵기: 짧은 낱말('Max')은 획 두께 측정이 흔들려 잘못 굵게 잡히기 쉽다 → 4글자 이상에서만 판단
            bold = bool(ref > 0 and stroke_ratio > ref * 1.3 and len(re.sub(r"\W", "", text)) >= 4)
            blocks.append(OcrBlock(
                rect=rect, text=text, conf=round(float(conf), 1), size=round(float(size), 2),
                origin_y=float(origin_y), align=detect_align(lns), bold=bold, fg=fg, bg=bg,
                cell=inner_box_pt(*cell) if cell else None,
                source="ocr-page" if mode == "page" else "ocr-figure",
                n_lines=len(lns),
                lines=[box_pt(l.x0, l.y0, l.x1, l.y1) + (l.text,) for l in lns],
                busy=busy,
                uls=[box_pt(*ul) for ul in uls_px],
            ))
        if mode == "page":
            snap_sizes(blocks)
        blocks.sort(key=lambda b: (round(b.rect[1]), b.rect[0]))
        return blocks


# ----------------------------------------------------------------------------
# 단독 실행: OCR 결과 확인/디버그 이미지
# ----------------------------------------------------------------------------
def _main():
    import argparse
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover
        import fitz  # type: ignore
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="OM PDF OCR 확인용")
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, default=1, help="1부터 시작하는 페이지 번호")
    ap.add_argument("--mode", choices=["page", "figure"], default="page")
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--lang", default="eng")
    ap.add_argument("--tesseract")
    ap.add_argument("--debug", action="store_true", help="인식 박스를 그린 PNG 저장")
    a = ap.parse_args()
    eng = OcrEngine(lang=a.lang, dpi=a.dpi, tesseract_cmd=a.tesseract)
    if not eng.available:
        sys.exit(eng.reason)
    doc = fitz.open(a.pdf)
    page = doc[a.page - 1]
    rgb = eng.render(page)
    blocks = eng.ocr_image(rgb, (page.rect.x0, page.rect.y0), a.dpi / 72.0, mode=a.mode)
    for b in blocks:
        tag = "CELL" if b.cell else "    "
        print(f"[{tag}] conf={b.conf:5.1f} size={b.size:4.1f} bold={int(b.bold)} "
              f"{tuple(round(v, 1) for v in b.rect)}  {b.text}")
    print(f"\n블록 {len(blocks)}개")
    if a.debug:
        d2 = fitz.open()
        p2 = d2.new_page(width=page.rect.width, height=page.rect.height)
        p2.show_pdf_page(p2.rect, doc, page.number)
        for b in blocks:
            if b.cell:
                p2.draw_rect(fitz.Rect(b.cell), color=(0, 0.6, 0), width=0.6)
            p2.draw_rect(fitz.Rect(b.rect), color=(1, 0, 0), width=0.5)
        fn = f"ocr_debug_p{a.page}.png"
        p2.get_pixmap(dpi=150).save(fn)
        print(f"디버그 이미지: {fn}")


if __name__ == "__main__":
    _main()
