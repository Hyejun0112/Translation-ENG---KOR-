#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
om_ocr.py - OM 번역용 OCR 엔진 (Tesseract + OpenCV)
=====================================================
om_translate.py 에서 사용합니다. 단독 실행 시 OCR 결과를 미리 확인할 수 있습니다.

    py om_ocr.py <PDF> --page 3            # 3페이지 OCR 결과를 콘솔에 출력
    py om_ocr.py <PDF> --page 3 --debug    # 인식 박스/표 셀을 그린 PNG 저장 (ocr_debug_p3.png)

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

OCR_VERSION = "5"  # OCR 로직이 바뀌면 올려서 캐시를 무효화

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


# ----------------------------------------------------------------------------
# 단어 → 줄 → 블록 묶기
# ----------------------------------------------------------------------------
def words_to_lines(words: list[Word], gap_factor: float) -> list[Line]:
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.cy, w.x0)):
        placed = False
        for row in reversed(rows[-6:]):
            ry0 = median([x.y0 for x in row])
            ry1 = median([x.y1 for x in row])
            ov = min(ry1, w.y1) - max(ry0, w.y0)
            if ov >= 0.5 * min(ry1 - ry0, w.h):
                row.append(w)
                placed = True
                break
        if not placed:
            rows.append([w])
    lines: list[Line] = []
    for row in rows:
        row.sort(key=lambda w: w.x0)
        hm = median([w.h for w in row])
        cur = [row[0]]
        for w in row[1:]:
            if w.x0 - cur[-1].x1 > max(gap_factor * hm, 6):
                lines.append(Line(cur))
                cur = []
            cur.append(w)
        lines.append(Line(cur))
    return lines


def lines_to_blocks(lines: list[Line], bullet_re, merge_all: bool = False) -> list[list[Line]]:
    """줄을 문단(블록)으로 묶는다. merge_all=True(표 셀)면 글머리표 외에는 모두 하나로."""
    lines = sorted(lines, key=lambda l: (l.y0, l.x0))
    blocks: list[list[Line]] = []
    for ln in lines:
        is_bullet = bool(bullet_re.match(ln.text))
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
                if -0.3 * last.h <= gap <= 0.9 * last.h and 0.7 <= hr <= 1.45 and xov > 0 and aligned:
                    target = b
                    break
        if target is not None:
            target.append(ln)
        else:
            blocks.append([ln])
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
            out.append(Word(x, y, x + w, y + h, t, c))
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
        return new if new.conf > ln.conf + 3 else ln

    # -------------------------------------------------------------- 공개 API
    def ocr_image(self, rgb: "np.ndarray", origin: tuple[float, float], scale: float,
                  mode: str = "page", exclude_pt: Optional[list] = None) -> list[OcrBlock]:
        """rgb: 렌더링된 이미지, origin: 이미지 좌상단의 PDF 좌표(pt), scale: px/pt.
        mode='page'(스캔 페이지 전체) | 'figure'(그림 속 라벨). 스레드 안전."""
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

        cells = detect_table_cells(gray) if (mode == "page" and self.tables) else []
        words = self._tess(gray, 3 if mode == "page" else 11)
        min_conf = self.min_conf if mode == "page" else self.fig_min_conf

        # 표 괘선/그림 잡음 제거
        words = [w for w in words if not (NOISE_RE.match(w.text) and w.conf < 85)]
        words = [w for w in words if w.h >= 5 and w.h < Hh * 0.2]

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

        gap = 1.4 if mode == "page" else 1.0
        groups: list[tuple[list[Line], Optional[tuple]]] = []
        for i, ws in cell_words.items():
            lns = words_to_lines(ws, gap_factor=3.0)  # 셀 안에서는 넓은 간격도 같은 줄
            for b in lines_to_blocks(lns, self.bullet_re, merge_all=False):
                groups.append((b, cells[i]))
        for b in lines_to_blocks(words_to_lines(free, gap_factor=gap), self.bullet_re):
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
            if mode == "figure" and not re.search(r"[A-Za-z]{2,}", text):
                continue
            x0 = min(l.x0 for l in lns)
            y0 = min(l.y0 for l in lns)
            x1 = max(l.x1 for l in lns)
            y1 = max(l.y1 for l in lns)
            fg, bg, stroke = sample_colors(rgb, (x0 - 1, y0 - 1, x1 + 1, y1 + 1))
            # 줄 단위 획 두께 (블록 높이 대신 줄 높이 기준)
            hmed = median([l.h for l in lns])
            stroke_ratio = stroke * max(1.0, (y1 - y0)) / max(1.0, hmed)
            strokes.append(stroke_ratio)
            tmp.append((lns, cell, text, conf, (x0, y0, x1, y1), fg, bg, stroke_ratio, hmed))
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

        for lns, cell, text, conf, box, fg, bg, stroke_ratio, hmed in tmp:
            rect = box_pt(*box)
            h_pt = hmed / scale
            size = h_pt / 0.78
            if len(lns) > 1:
                pitches = [lns[i + 1].y0 - lns[i].y0 for i in range(len(lns) - 1)]
                pitch = median(pitches) / scale
                if pitch > 0:
                    size = min(size, pitch / 1.15)
            size = max(4.0, min(size, 40.0))
            first = lns[0]
            origin_y = to_pt(first.x0, first.y1)[1] - 0.05 * size
            bold = bool(ref > 0 and stroke_ratio > ref * 1.3)
            blocks.append(OcrBlock(
                rect=rect, text=text, conf=round(float(conf), 1), size=round(float(size), 2),
                origin_y=float(origin_y), align=detect_align(lns), bold=bold, fg=fg, bg=bg,
                cell=inner_box_pt(*cell) if cell else None,
                source="ocr-page" if mode == "page" else "ocr-figure",
                n_lines=len(lns),
                lines=[box_pt(l.x0, l.y0, l.x1, l.y1) + (l.text,) for l in lns],
            ))
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
