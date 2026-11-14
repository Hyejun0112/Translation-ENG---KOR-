# -*- coding: utf-8 -*-
"""
pdf_engine.py - PDF 레이아웃 유지 텍스트 교체
=============================================
- classify_page  : 페이지 진단 (네이티브 / 스캔 / 글자깨짐 / 숨은 OCR층 / 빈 페이지)
- extract_units  : 텍스트 블록·표 셀 단위로 번역 단위(Unit) 추출
- prepare_units  : 진단 → 추출 → (필요 시) OCR → 번역 범위 판단 → 보호 토큰화
- render_page    : 원문 글자만 지우고(선·그림·배경 보존) 같은 자리에 한글 삽입.
                   OCR 영역은 원본 배경색 패치 후 삽입. 폭/높이 확장 → 글자 축소 순으로 맞춤.
                   텍스트 겹침/페이지 밖 검사
- 폰트           : 맑은 고딕(Windows) 자동 사용, 사용 글자만 서브셋 삽입. 한글 글꼴에 없는 기호는 대체 글꼴(MixFont)
- 주석 보존      : detach_annotations / restore_annotations (Markup·스탬프·링크는 번역 과정에서 건드리지 않음)
- 밑줄           : 번역할 문구의 밑줄은 지우고 번역문 폭에 맞춰 다시 그림
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from config import Config
from engines.glossary_engine import BULLET_RE, Protector, clean_text, needs_translation
from engines.scope_engine import A4_AREA, load_scope_keywords, page_scope
from engines.table_engine import (
    cell_merge, column_merge, continues, find_table_cells, is_tabular, line_merge, mark_keep_columns,
    merge_unruled_cells, x_ok,
)
from engines.translation_engine import Cache

TEXT_FLAGS = (
    getattr(fitz, "TEXTFLAGS_TEXT", 0)
    | getattr(fitz, "TEXT_PRESERVE_WHITESPACE", 0)
)


TEXT_FLAGS &= ~getattr(fitz, "TEXT_PRESERVE_LIGATURES", 0)


@dataclass
class Unit:
    page: int
    idx: int
    rect: "fitz.Rect"
    text: str
    size: float
    color: int
    bold: bool
    origin_y: float
    align: str = "left"
    rotated: bool = False
    kind: str = "text"  # text | toc
    ocr: bool = False
    source: str = "native"  # native | ocr-page | ocr-figure | toc
    conf: float = 100.0
    cell: Optional["fitz.Rect"] = None  # 표 셀 경계(번역문이 넘지 않을 한계)
    tkey: Optional[tuple] = None  # 표 셀 위치 (table_id, row, col)
    scope_keep: str = ""  # 번역 범위 밖 사유 (데이터시트/도면/표 Tag·Value 열 등)
    fg: Optional[tuple] = None  # OCR: 글자색 (0~1 RGB)
    bg: Optional[tuple] = None  # OCR: 배경색 (0~1 RGB) → 원문을 덮는 패치 색
    ptxt: str = ""
    tokens: list = field(default_factory=list)
    status: str = ""
    ko: Optional[str] = None
    notes: list = field(default_factory=list)
    used_size: float = 0.0
    last_x1: float = 0.0     # 마지막 줄의 오른쪽 끝 (문단 이어짐 판단용)
    leader: bool = False     # 목차 줄: 제목 뒤 점선(......)을 번역 후 다시 채움
    leader_num: str = ""     # 점선 뒤 쪽번호가 같은 글자 상자에 있으면 보관
    bullet: bool = False     # 글머리표/번호로 시작하는 항목 (앞 문구와 합치지 않음)
    lines: list = field(default_factory=list)  # 원문 줄별 (x0, y0, x1, y1, 기준선 y, 크기) → 줄 단위로만 지운다
    uls: list = field(default_factory=list)    # 밑줄: {"rect", "x0", "x1", "oy", "size", "color"} (번역 폭에 맞춰 다시 그림)
    segs: list = field(default_factory=list, repr=False)  # 원문 줄 조각 (문단 이어짐 판단용)
    tab: bool = False        # 괘선 없는 표/다단 블록에서 나온 문구 (다른 블록과 합치지 않음)
    hang: float = 0.0        # 내어쓰기: 둘째 줄부터 왼쪽 시작이 첫 줄보다 안쪽인 정도 ("1. 글…" 의 다음 줄)
    symbol: bool = False     # 첨자가 붙은 기호 (P wbox, V PA total, T bed ave …) → 번역하지 않고 원본 그대로


PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


MAX_PAGE_IMAGES = 2000


PAGE_CLASS_KO = {
    "native": "네이티브", "scanned": "스캔(이미지)", "garbled": "글자깨짐",
    "invisible": "스캔+숨은OCR층", "empty": "빈 페이지", "forced": "강제OCR",
}


def classify_page(page) -> str:
    """native | scanned | garbled | invisible | empty"""
    raw = page.get_text("text")
    raw = re.sub(r"(?:[.·…_-]\s*){3,}", " ", raw)  # 목차 점선/밑줄은 '깨진 글자' 판단에서 제외
    s = "".join(raw.split())
    try:
        imgs = page.get_image_info()
    except Exception:  # noqa: BLE001
        imgs = []
    area = max(1.0, abs(page.rect))
    cov = 0.0
    for im in imgs:
        cov = max(cov, abs(fitz.Rect(im["bbox"]) & page.rect) / area)
    if len(s) < 5:
        return "scanned" if imgs else "empty"
    bad = sum(1 for c in s if c == "\ufffd" or 0xE000 <= ord(c) <= 0xF8FF or ord(c) < 32)
    punct = sum(1 for c in s if c in PUNCT)
    letters = sum(1 for c in s if c.isalpha())
    if bad / len(s) > 0.10 or (len(s) > 40 and punct / len(s) > 0.40 and letters / len(s) < 0.4):
        return "garbled"
    if cov > 0.6:
        try:
            tr = page.get_texttrace()
            tot = sum(len(t["chars"]) for t in tr)
            inv = sum(len(t["chars"]) for t in tr if t.get("type") == 3 or t.get("opacity", 1) == 0)
            if tot and inv / tot > 0.8:
                return "invisible"
        except Exception:  # noqa: BLE001
            pass
    return "native"


# 글머리표/번호 기호만 있는 조각: "–", "•", "1.", "a)", "1.2.3"
MARKER_RE = re.compile(r"^(?:[•●▪■◦○‣·*\-–—►▶➢✓□❑o]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\d{1,2}(?:\.\d{1,2}){0,3}\.?)$")


def is_math_span(s: dict) -> bool:
    """수식 글꼴(Cambria Math 등) 또는 수학용 기울임 문자(𝑃, 𝑚𝑒𝑎𝑠𝑢𝑟𝑒𝑑 …)로 된 글자 조각"""
    f = (s.get("font") or "").lower()
    if "math" in f or "stix" in f or f.startswith("cmmi") or f.startswith("cmsy"):
        return True
    t = s.get("text") or ""
    return any(0x1D400 <= ord(c) <= 0x1D7FF for c in t)


_SUP = str.maketrans("0123456789+-\u2212n()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻ⁿ⁽⁾")
_SUB = str.maketrans("0123456789+-\u2212", "₀₁₂₃₄₅₆₇₈₉₊₋₋")
# 한글 글꼴에 없을 때 되돌릴 글자 (모양은 조금 달라도 뜻은 같음)
DENORM = {**{k: v for k, v in zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ⁽⁾", "0123456789+-n()")},
          **{k: v for k, v in zip("₀₁₂₃₄₅₆₇₈₉₊₋", "0123456789+-")},
          "\u2212": "-", "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2015": "-", "\u00a0": " ",
          "\u2009": " ", "\u202f": " ", "\u2264": "<=", "\u2265": ">=", "\u2260": "!=", "\u2248": "~",
          "\u00d7": "x", "\u2022": "-", "\u2026": "...", "\u2032": "'", "\u2033": '"'}


def seg_text(sg: list[dict]) -> str:
    """줄 조각의 글자. 작게 올려/내려 쓴 숫자(cm², m³, H₂O, CO₂)는 위/아래 첨자 글자로 바꿔
    번역 후에도 'cm2', 'H2O' 처럼 풀어지지 않게 한다."""
    sizes = [s["size"] for s in sg if re.search(r"[A-Za-z0-9]", s["text"])]
    main = max(sizes) if sizes else 0.0
    out: list[str] = []
    base_y = None
    for s in sg:
        t = s["text"]
        core = t.strip()
        small = bool(main and core and s["size"] <= 0.8 * main and len(core) <= 6)
        if small and base_y is not None and out and re.search(r"[A-Za-z0-9)\]]$", out[-1]):
            dy = s["origin"][1] - base_y
            if (s["flags"] & 1 or dy < -0.2 * main) and all(c in "0123456789+-\u2212n()" for c in core):
                t = t.translate(_SUP)
            elif dy > -0.1 * main and all(c in "0123456789" for c in core) and re.search(r"[A-Za-z)]$", out[-1]):
                t = t.translate(_SUB)
        elif main and s["size"] >= 0.95 * main:
            base_y = s["origin"][1]
        out.append(t)
    return "".join(out).strip()


# Symbol / Wingdings 글꼴의 사용자 정의 영역 글자 → 같은 모양의 표준 글자 (그대로 두면 한글 글꼴에서 □ 로 깨짐)
SYMBOL_PUA = {0xF0B1: "±", 0xF0B0: "°", 0xF0B4: "×", 0xF0B7: "•", 0xF0A3: "≤", 0xF0B3: "≥", 0xF06D: "µ",
              0xF0D7: "·", 0xF0A2: "′", 0xF0B2: "″", 0xF0BB: "≈", 0xF0B9: "≠", 0xF0AE: "→", 0xF0AC: "←",
              0xF057: "Ω", 0xF044: "Δ", 0xF061: "α", 0xF062: "β", 0xF064: "δ", 0xF070: "π", 0xF071: "θ",
              0xF06C: "λ", 0xF068: "η", 0xF072: "ρ", 0xF073: "σ", 0xF0C6: "∅", 0xF0A5: "∞", 0xF0D6: "√",
              0xF02D: "−", 0xF0BC: "…", 0xF0A0: " ", 0xF020: " "}
WINGDINGS_PUA = {0xF0A7: "▪", 0xF06E: "■", 0xF0FC: "✓", 0xF0D8: "➢", 0xF09F: "•", 0xF0B7: "•", 0xF06C: "●",
                 0xF071: "❑", 0xF0A8: "◻", 0xF0E0: "→", 0xF076: "❖"}


def _fix_span(s: dict) -> dict:
    """글자 조각 보정: (1) Symbol/Wingdings 사용자 정의 글자를 표준 글자로,
    (2) 앞쪽 공백(스페이스로 들여쓴 줄, '•' 뒤의 공백)만큼 시작 위치를 오른쪽으로 옮긴다
        → 번역문이 들여쓰기 없이 왼쪽으로 당겨지지 않게"""
    t = s["text"]
    if any(0xF000 <= ord(c) <= 0xF0FF for c in t):
        table = WINGDINGS_PUA if "wingding" in (s.get("font") or "").lower() else SYMBOL_PUA
        t = "".join(table.get(ord(c), SYMBOL_PUA.get(ord(c), c)) for c in t)
    n = len(t) - len(t.lstrip(" \u00a0"))
    if t.strip() and n:
        x0, y0, x1, y1 = s["bbox"]
        shift = min(n * 0.278 * s["size"], max(0.0, (x1 - x0) - 0.4 * s["size"] * len(t.strip())))
        return dict(s, text=t[n:], bbox=(x0 + max(shift, 0.0), y0, x1, y1))
    return dict(s, text=t) if t is not s["text"] else s


BULLET_CH_RE = re.compile(r"^[•●▪■◦○‣·►▶➢✓□❑–—\-\uf0b7\uf0a7]$")


def split_line_segments(ln: dict, vedges: Optional[list] = None, cols: Optional[list] = None) -> list[dict]:
    """한 줄 안에서 열 간격이 크게 벌어진 텍스트(표의 셀 등)를 별도 세그먼트로 분리.
    - 세로 괘선을 사이에 둔 글자도 분리 (괘선 표인데 표 인식이 안 된 경우: 개정 이력 표 등)
    - 맨 앞이 글머리표/번호 기호만이면 기호는 원문 그대로 두고(번역 단위에서 제외),
      다음 조각에 '_marker' 표시 → 표로 오인하지 않고 항목 시작으로 처리
    - 공백만 있는 조각은 글자 사이의 공백으로 남긴다 ('•' + ' ' + 'Text' 가 '•Text' 로 붙지 않게)
    - cols: 같은 블록에서 여러 줄이 시작하는 x 위치. 그 위치에서 시작하는 조각은 간격이 좁아도 열로 나눈다
      (기호 | 설명 범례에서 '⌀d₁ Raised face' 처럼 간격이 좁은 행)"""
    y0, y1 = ln["bbox"][1], ln["bbox"][3]
    walls = [x for x, a, b in (vedges or []) if a <= y0 + 1 and b >= y1 - 1]
    spans_in = [_fix_span(s) for s in ln["spans"]]
    # 양쪽 정렬 문장: 낱말 사이가 고르게 넓게 벌어진 줄은 표의 열이 아니다 → 간격으로 나누지 않는다
    real = [s for s in spans_in if s["text"].strip()]
    gaps = [b["bbox"][0] - a["bbox"][2] for a, b in zip(real, real[1:])]
    wide = [g for g in gaps if g > max(6.0, 0.8 * max(real[0]["size"], 1.0))] if real else []
    justified = ln.get("_justified") or (len(wide) >= 3 and max(wide) <= 1.8 * min(wide) and max(wide) < 2.5 * max(real[0]["size"], 1.0)
                 and all(re.search(r"[A-Za-z]{2,}", s["text"]) for s in real))
    segs, cur, last = [], [], None
    for s in spans_in:
        if not s["text"].strip():
            if cur:
                cur.append(s)  # 글자 사이 공백
            continue
        if last is not None:
            px, nx = last["bbox"][2], s["bbox"][0]
            gap = nx - px
            size = max(s["size"], 1.0)
            wall = any(px - 0.5 <= x <= nx + 0.5 for x in walls)
            math_edge = is_math_span(last) != is_math_span(s)  # 수식 ↔ 일반 글자 경계
            n_real = sum(1 for x in cur if x["text"].strip())
            bullet = not segs and n_real == 1 and bool(BULLET_CH_RE.match(last["text"].strip()))
            col = gap > 0.3 * size and sum(1 for c in (cols or []) if abs(nx - c) < 0.6) >= 2
            hard = wall or bullet or col or (gap > max(6.0, 0.8 * size) and not justified)
            if hard or math_edge:
                segs.append(cur)
                cur = []
                if not hard:
                    s = dict(s, _soft=True)  # 수식 경계로만 나뉨 (문장 중간 수식이면 다시 붙임)
        cur.append(s)
        last = s
    if cur:
        segs.append(cur)
    for sg in segs:  # 끝에 붙은 공백 조각 제거
        while sg and not sg[-1]["text"].strip():
            sg.pop()
    out = []
    for sg in segs:
        r = fitz.Rect(sg[0]["bbox"])
        for s in sg[1:]:
            if s["text"].strip():
                r |= fitz.Rect(s["bbox"])
        text = seg_text(sg)
        if text:
            out.append({"bbox": tuple(r), "spans": sg, "dir": ln["dir"], "_text": text,
                        "_math": all(is_math_span(x) for x in sg)})
    # 수식 조각(줄 앞/뒤의 𝑃 기호, 첨자 등)은 번역하지 않고 원본 그대로 둔다
    # 예) "𝑃 measured/windbox = Current measured windbox pressure" → "= 현재 측정된 …" 만 번역
    lead = 0
    while lead < len(out) - 1 and out[lead]["_math"]:
        lead += 1
    trail = len(out)
    while trail > lead + 1 and out[trail - 1]["_math"]:
        trail -= 1
    trimmed = bool(lead or trail < len(out))
    if trimmed:
        out = out[lead:trail]
    # 문장 중간의 수식(예: "where 𝑥 is …")은 떼지 않고 다시 한 문장으로
    j = 1
    while j < len(out):
        if out[j]["spans"][0].get("_soft"):
            a, b = out[j - 1], out[j]
            out[j - 1] = dict(a, bbox=tuple(fitz.Rect(a["bbox"]) | fitz.Rect(b["bbox"])), spans=a["spans"] + b["spans"],
                              _text=a["_text"] + " " + b["_text"], _math=a["_math"] and b["_math"])
            del out[j]
        else:
            j += 1
    if trimmed:
        out[0]["_marker"] = True  # 수식 뒤 설명 = 항목 시작 (앞 줄과 합치지 않음)
    if out and all(o["_math"] for o in out):
        return []  # 수식만 있는 줄
    if len(out) >= 2 and MARKER_RE.match(out[0]["_text"]):
        out = out[1:]
        out[0]["_marker"] = True
    return out


def _group_to_unit(page, g: list[dict], cell=None) -> Optional[Unit]:
    rect = fitz.Rect(g[0]["bbox"])
    for ln in g[1:]:
        rect |= fitz.Rect(ln["bbox"])
    text = clean_text(" ".join(ln["_text"] for ln in g))
    if not text:
        return None
    w = Counter()
    for ln in g:
        for s in ln["spans"]:
            if s["text"].strip():
                bold = bool(s["flags"] & 16) or "bold" in s["font"].lower()
                w[(round(s["size"], 1), s["color"], bold)] += len(s["text"].strip())
    (size, color, bold), _ = w.most_common(1)[0]
    first = g[0]["spans"][0]
    rotated = any(abs(ln["dir"][0]) < 0.99 for ln in g)
    align = "left"
    if len(g) > 1:
        L = [ln["bbox"][0] for ln in g]
        R = [ln["bbox"][2] for ln in g]
        C = [(ln["bbox"][0] + ln["bbox"][2]) / 2 for ln in g]
        if max(L) - min(L) < 1.5:
            align = "left"
        elif max(C) - min(C) < 1.5:
            align = "center"
        elif max(R) - min(R) < 1.5:
            align = "right"
    elif cell is not None:  # 한 줄짜리 셀: 셀 안에서 가운데 정렬인지 확인
        lm, rm = rect.x0 - cell.x0, cell.x1 - rect.x1
        if lm > 4 and rm > 4 and abs(lm - rm) < max(2.0, 0.15 * (lm + rm)):
            align = "center"
        elif lm > 3 * max(rm, 1) and rm < 6 and not re.search(r"[A-Za-z]{3,}", text):
            align = "right"  # 오른쪽 정렬은 숫자 위주 칸만 (글자 칸은 왼쪽 정렬로 둔다)
    lines = [(*ln["bbox"], ln.get("_oy", ln["spans"][0]["origin"][1]), ln.get("_size", ln["spans"][0]["size"]))
             for ln in g]
    # 기호 + 글자 첨자(P wbox, T bed ave): 번역하면 첨자 모양이 사라지므로 원본 그대로 둔다
    symbol = False
    if len(g) == 1 and len(text) <= 24 and len(text.split()) <= 3:
        sp = [s for s in g[0]["spans"] if re.search(r"[A-Za-z0-9]", s["text"])]
        big = max((s["size"] for s in sp), default=0.0)
        symbol = (any(s["size"] >= 0.95 * big and len(s["text"].strip()) <= 3 for s in sp)
                  and any(s["size"] <= 0.8 * big and re.search(r"[A-Za-z]", s["text"]) for s in sp))
    uls = [ul for ln in g for ul in ln.get("_uls", [])]
    return Unit(page=page.number, idx=0, rect=rect, text=text, size=size, color=color,
                bold=bold, origin_y=first["origin"][1], align=align, rotated=rotated,
                cell=fitz.Rect(cell) if cell is not None else None, last_x1=g[-1]["bbox"][2],
                bullet=bool(g[0].get("_marker") or BULLET_RE.match(text)),
                lines=lines, uls=uls, segs=list(g), symbol=symbol,
                hang=(g[1]["bbox"][0] - g[0]["bbox"][0]
                      if len(g) > 1 and cell is None and 2.0 < g[1]["bbox"][0] - g[0]["bbox"][0] < 60.0
                      and BULLET_RE.match(text) else 0.0))


# 목차 점선: "Purpose of the manual ........" / "Title . . . . ." / "Title ……"
LEADER_RE = re.compile(r"^(.*?[^.\s·…])\s*(?:[.·…]\s*){4,}(\d{1,4})?\s*$")
NUMBERING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[A-Z]\.|\(?[a-z]\))\s+\S")


def _rule_between(a, b, hedges) -> bool:
    """a(위)와 b(아래) 사이에 가로 괘선(표의 행 경계 등)이 있는지"""
    lo, hi = min(a.y1, b.y0) - 1.0, max(a.y1, b.y0) + 1.0
    x0, x1 = max(a.x0, b.x0), min(a.x1, b.x1)
    if x1 <= x0:
        x0, x1 = min(a.x0, b.x0), max(a.x1, b.x1)
    for y, xa, xb in hedges or []:
        if lo <= y <= hi and xa < x1 and xb > x0:
            return True
    return False


def merge_paragraphs(units: list[Unit], hedges: Optional[list] = None) -> list[Unit]:
    """블록이 나뉘어 따로 저장된 문단을 하나의 번역 단위로 합친다.
    블록 안에서 줄을 잇는 것과 같은 규칙(continues)을 쓴다. 표/다단 문구, 글머리표/번호/목차 줄은 합치지 않음."""
    def ok(u: Unit) -> bool:
        return (u.kind == "text" and u.cell is None and not u.rotated and not u.tab and bool(u.segs)
                and u.source in ("native", "native-ocr") and bool(re.search(r"[A-Za-z]{2,}", u.text)))

    out: list[Unit] = []
    for b in sorted(units, key=lambda u: (round(u.rect.y0, 1), u.rect.x0)):
        target = None
        if (ok(b) and not b.bullet and not BULLET_RE.match(b.text) and not NUMBERING_RE.match(b.text)
                and not LEADER_RE.match(b.text)):
            for a in reversed(out[-8:]):
                if not ok(a) or LEADER_RE.match(a.text):
                    continue
                if abs(a.size - b.size) > 0.6 or a.bold != b.bold or a.color != b.color:
                    continue
                if not x_ok(a.segs, b.segs[0]) or not continues(a.segs[-1], b.segs[0], hedges):
                    continue
                # 같은 높이에 다른 글이 나란히 있으면(표의 행) 문단이 아니다
                if any(o is not a and o is not b and o.rect.y1 > b.rect.y0 + 1 and o.rect.y0 < b.rect.y1 - 1
                       and (o.rect.x1 < b.rect.x0 or o.rect.x0 > b.rect.x1) and re.search(r"\w{2,}", o.text)
                       for o in units):
                    continue
                target = a
                break
        if target is None:
            out.append(b)
            continue
        t = target.text
        if t.endswith("-") and len(t) > 2 and t[-2].isalpha() and b.text[:1].islower():
            target.text = t[:-1] + b.text  # 줄끝 하이픈 연결
        else:
            target.text = t + " " + b.text
        target.rect |= b.rect
        target.last_x1 = b.last_x1
        target.align = "left"
        target.lines += b.lines
        target.uls += b.uls
        target.segs += b.segs
    return out


def merge_justified_rows(lines: list) -> list:
    """양쪽 정렬로 낱말 사이가 넓게 벌어져 낱말마다 따로 저장된 줄을 한 줄로 되돌린다.
    (같은 높이에 낱말 4개 이상이 고른 간격으로 놓인 경우만 - 표의 열은 간격이 고르지 않다)"""
    out: list = []
    i = 0
    while i < len(lines):
        row = [lines[i]]
        j = i + 1
        while j < len(lines) and abs(lines[j]["bbox"][1] - lines[i]["bbox"][1]) < 1.0 \
                and lines[j]["bbox"][0] >= row[-1]["bbox"][2] - 0.5 and abs(lines[j]["dir"][0]) > 0.99:
            row.append(lines[j])
            j += 1
        if len(row) >= 4:
            size = max((sp["size"] for sp in row[0]["spans"]), default=10.0)
            gaps = [b["bbox"][0] - a["bbox"][2] for a, b in zip(row, row[1:])]
            words_ok = all(re.search(r"[A-Za-z]{2,}", "".join(sp["text"] for sp in ln["spans"])) for ln in row)
            if words_ok and min(gaps) > 0 and max(gaps) <= 1.8 * min(gaps) and max(gaps) < 2.5 * size:
                spans = []
                for k, ln in enumerate(row):
                    if k:
                        ref = ln["spans"][0]
                        spans.append(dict(ref, text=" ", bbox=(row[k - 1]["bbox"][2], ref["bbox"][1],
                                                               ln["bbox"][0], ref["bbox"][3])))
                    spans += ln["spans"]
                bb = (row[0]["bbox"][0], min(ln["bbox"][1] for ln in row), row[-1]["bbox"][2],
                      max(ln["bbox"][3] for ln in row))
                out.append(dict(row[0], spans=spans, bbox=bb, _justified=True))
                i = j
                continue
        out.append(lines[i])
        i += 1
    return out


def _seg_style(sg: dict) -> tuple:
    w = Counter()
    for sp in sg["spans"]:
        n = len(sp["text"].strip())
        if n:
            w[(round(sp["size"], 1), sp["origin"][1])] += n
    (size, oy), _ = w.most_common(1)[0]
    return size, oy


def annotate_segments(blocks: list, cells: list, vedges: list, thin: list) -> set:
    """줄 조각마다 이어 붙이기 판단에 쓸 값을 계산한다.
    _size/_oy(대표 글자 크기·기준선), _fw(첫 단어 폭), _pitch(그 문단의 줄 간격),
    _left/_limit(그 줄이 쓸 수 있는 폭), _ul/_uls(밑줄), _cell(표 셀 번호).
    반환: 밑줄로 쓰인 가로선 (y, x0, x1) 모음 → 표의 행 경계선 판단에서 뺀다."""
    allsegs = [sl for bl in blocks for sl in bl[0]]
    for sg in allsegs:
        sg["_size"], sg["_oy"] = _seg_style(sg)
        raw = "".join(sp["text"] for sp in sg["spans"]).strip()
        first = raw.split(" ", 1)[0] if raw else ""
        width = sg["bbox"][2] - sg["bbox"][0]
        sg["_fw"] = width * len(first) / max(len(raw), 1)
        bb = sg["bbox"]
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        hit, best = None, None
        for i, (c, *_meta) in enumerate(cells):
            if c.x0 <= cx <= c.x1 and c.y0 <= cy <= c.y1:
                area = abs(c)
                if best is None or area < best:
                    hit, best = i, area
        sg["_cell"] = hit
    # 줄 간격: 블록 안 최소값과 페이지에서 가장 흔한 값 중 작은 쪽
    page_pitch: dict[int, Counter] = {}
    block_pitch: list[Optional[float]] = []
    for seg_lines, _tab in blocks:
        vals = []
        for a, b in zip(seg_lines, seg_lines[1:]):
            pitch = b["_oy"] - a["_oy"]
            if abs(a["bbox"][0] - b["bbox"][0]) < 1.0 and abs(a["_size"] - b["_size"]) < 0.6 \
                    and 0.9 * a["_size"] <= pitch <= 2.0 * a["_size"]:
                vals.append(pitch)
                page_pitch.setdefault(round(a["_size"]), Counter())[round(pitch * 2) / 2] += 1
        block_pitch.append(min(vals) if len(vals) >= 2 else None)
    mode = {k: min(p for p, n in c.items() if n == max(c.values())) for k, c in page_pitch.items()}
    used_rules = set()
    free = [sg for sg in allsegs if sg["_cell"] is None]
    cell_segs: dict[int, list] = {}
    for sg in allsegs:
        if sg["_cell"] is not None:
            cell_segs.setdefault(sg["_cell"], []).append(sg)
    for (seg_lines, _tab), bp in zip(blocks, block_pitch):
        bmax = max((sg["bbox"][2] for sg in seg_lines), default=0.0)
        for sg in seg_lines:
            size = max(sg["_size"], 1.0)
            cands = [v for v in (bp, mode.get(round(sg["_size"]))) if v]
            sg["_pitch"] = min(cands) if cands else 1.35 * size
            bb = sg["bbox"]
            if sg["_cell"] is not None:
                c = cells[sg["_cell"]][0]
                sg["_left"], sg["_limit"] = c.x0 + 2.0, c.x1 - 2.0
                # 셀 안에 세로선 없이 열이 더 있으면(여러 줄이 같은 위치에서 시작하는 오른쪽 글) 그 열 앞까지만
                gap = max(6.0, 0.8 * size)
                mates = cell_segs.get(sg["_cell"], [])
                starts = [o["bbox"][0] for o in mates if o is not sg and o["bbox"][0] > bb[2] + gap]
                nxt = [x for x in starts if sum(1 for y in starts if abs(x - y) < 2.0) >= 2
                       or any(o["bbox"][0] == x and abs(o["_oy"] - sg["_oy"]) < 0.35 * size for o in mates)]
                if nxt:
                    sg["_left"], sg["_limit"] = bb[0], max(min(nxt) - 3.0, bb[2])
                    sg["_subcol"] = sg["_rowmate"] = True
                elif any(o is not sg and o["bbox"][2] < bb[0] - gap for o in mates):
                    sg["_left"] = bb[0]
                    sg["_subcol"] = sg["_rowmate"] = True
            else:
                measure = bmax
                if len(seg_lines) < 3:  # 줄 수가 적은 블록: 같은 시작점의 다른 문단 폭을 참고
                    measure = max([measure] + [o["bbox"][2] for o in free
                                               if abs(o["bbox"][0] - bb[0]) <= size])
                cy = (bb[1] + bb[3]) / 2
                for o in free:  # 같은 줄 오른쪽의 다른 글 / 세로 괘선까지만
                    ob = o["bbox"]
                    if o is sg or not (ob[1] < cy < ob[3]):
                        continue
                    if ob[0] >= bb[2] - 1.0:
                        measure = min(measure, ob[0] - 3.0)
                    if ob[0] >= bb[2] + 6.0 or ob[2] <= bb[0] - 6.0:
                        sg["_rowmate"] = True  # 같은 줄에 떨어져 있는 다른 글 = 표/다단의 한 행
                for x, y0, y1 in vedges:
                    if x >= bb[2] - 0.5 and y0 < cy < y1:
                        measure = min(measure, x - 2.0)
                sg["_left"], sg["_limit"] = bb[0], max(measure, bb[2])
            # 밑줄: 기준선 바로 아래, 글자 폭 안에 있는 가는 가로선 (표 괘선은 글자보다 길어서 제외)
            uls = []
            for r, color in thin:
                yc = (r.y0 + r.y1) / 2
                if not (sg["_oy"] + 0.03 * size <= yc <= sg["_oy"] + 0.32 * size):
                    continue
                if r.x0 < bb[0] - 1.5 or r.x1 > bb[2] + 1.5 or r.width < 4.0:
                    continue
                if any((abs(x - r.x0) < 1.5 or abs(x - r.x1) < 1.5) and y0 - 1 <= yc <= y1 + 1 and y1 - y0 > 3
                       for x, y0, y1 in vedges):
                    continue  # 세로선과 만나는 선 = 표/박스 테두리
                uls.append({"rect": fitz.Rect(r), "x0": bb[0], "x1": bb[2], "oy": sg["_oy"], "size": size,
                            "color": color})
            if uls:
                sg["_uls"] = uls
                sg["_ul"] = sum(u["rect"].width for u in uls) / max(bb[2] - bb[0], 1.0)
                for u in uls:
                    used_rules.add((round(u["rect"].y0, 1), round(u["rect"].x0, 1), round(u["rect"].x1, 1)))
                    used_rules.add((round(u["rect"].y1, 1), round(u["rect"].x0, 1), round(u["rect"].x1, 1)))
            else:
                sg["_ul"] = 0.0
    return used_rules


def extract_units(page, table_cells: Optional[list] = None) -> list[Unit]:
    """네이티브(텍스트 레이어가 있는) 페이지에서 번역 단위를 추출."""
    d = page.get_text("dict", flags=TEXT_FLAGS)
    vedges, hedges, thin = page_geometry(page)
    cells = merge_unruled_cells(find_table_cells(page) if table_cells is None else table_cells, hedges)
    units: list[Unit] = []
    seen = set()
    cell_lines: dict[int, list[dict]] = {}

    def add(u: Optional[Unit], tab: bool = False):
        if u is None:
            return
        key = (round(u.rect.x0), round(u.rect.y0), round(u.rect.x1), round(u.rect.y1), u.text)
        if key in seen:  # 겹쳐 그린 중복 텍스트(가짜 볼드 등) 제거
            return
        seen.add(key)
        u.idx = len(units)
        u.tab = tab
        units.append(u)

    # 1차: 블록별 줄 조각
    blocks = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        seg_lines: list[dict] = []
        tabular = False
        b_lines = merge_justified_rows(b.get("lines", []))
        cols = [sp["bbox"][0] for ln in b_lines for sp in ln["spans"][:1] if sp["text"].strip()]
        for ln in b_lines:
            segs = split_line_segments(ln, vedges, cols)
            if len(segs) > 1:
                tabular = True
            seg_lines += segs
        blocks.append([seg_lines, tabular])
    # 2차: 글머리표 기호가 따로 떨어진 줄("–" 혼자)이면 같은 높이 오른쪽 글자에 '항목 시작' 표시 후 기호는 원문 유지
    allsegs = [sl for bl in blocks for sl in bl[0]]
    drop = set()
    for m in allsegs:
        if not MARKER_RE.match(m["_text"]):
            continue
        mb = m["bbox"]
        mh = max(mb[3] - mb[1], 1.0)
        best = None
        for t in allsegs:
            if t is m or MARKER_RE.match(t["_text"]):
                continue
            tb = t["bbox"]
            ov = min(mb[3], tb[3]) - max(mb[1], tb[1])
            if ov < 0.5 * min(mh, tb[3] - tb[1]) or tb[0] < mb[2] - 0.5 or tb[0] - mb[2] > 45:
                continue
            if best is None or tb[0] < best["bbox"][0]:
                best = t
        if best is not None:
            best["_marker"] = True
            drop.add(id(m))
    for bl in blocks:
        bl[0] = [sl for sl in bl[0] if id(sl) not in drop]
    blocks = [bl for bl in blocks if bl[0]]
    # 3차: 줄 조각별 폭/줄 간격/밑줄 계산 → 밑줄은 '행 경계선'에서 제외
    used = annotate_segments(blocks, cells, vedges, thin)
    if used:
        hedges = [h for h in hedges if (round(h[0], 1), round(h[1], 1), round(h[2], 1)) not in used]
    # 4차: 괘선 표 셀 안의 줄은 셀 단위로, 나머지는 블록 단위로 모은다
    rests: list[list] = []
    for seg_lines, tabular in blocks:
        rest = []
        for sl in seg_lines:
            if sl["_cell"] is None:
                rest.append(sl)
            else:
                cell_lines.setdefault(sl["_cell"], []).append(sl)
        if rest:
            rests.append([rest, tabular or is_tabular(rest)])
    # 선 없는 표는 열마다 다른 블록으로 저장되기도 한다 → 두 줄 이상 나란히 놓인 좁은 블록끼리는 한 표로 묶는다
    parent = list(range(len(rests)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    narrow = 0.25 * page.rect.width
    widths = [max(s["bbox"][2] - s["bbox"][0] for s in r[0]) for r in rests]
    for i in range(len(rests)):
        for j in range(i + 1, len(rests)):
            if min(widths[i], widths[j]) > narrow:
                continue
            shared = set()
            for a in rests[i][0]:
                for b in rests[j][0]:
                    if abs(a["_oy"] - b["_oy"]) < 0.35 * max(a["_size"], b["_size"], 1.0) and (
                            a["bbox"][0] >= b["bbox"][2] + 6.0 or b["bbox"][0] >= a["bbox"][2] + 6.0):
                        shared.add(round(a["_oy"]))
            if len(shared) >= 2:
                parent[find(j)] = find(i)
    sets: dict[int, list] = {}
    for i, (rest, tabular) in enumerate(rests):
        st = sets.setdefault(find(i), [[], False, 0])
        st[0] += rest
        st[1] = st[1] or tabular
        st[2] += 1
    for rest, tabular, n in sets.values():
        tabular = tabular or n > 1
        groups = column_merge(rest, hedges) if tabular else line_merge(rest, hedges)
        for g in groups:
            add(_group_to_unit(page, g), tabular)

    # 셀: 가로선 없이 여러 행이 들어 있는 칸(band)은 같은 표의 다른 열과 함께 '행' 단위로 판단한다
    bands: dict[int, list] = {}
    for ci, lines in cell_lines.items():
        if cells[ci][4] >= 2:
            bands.setdefault(cells[ci][1], []).extend(lines)
        else:
            bands[-1 - ci] = lines
    for key, lines in bands.items():
        groups = column_merge(lines, hedges) if key >= 0 else cell_merge(lines, hedges)
        for g in groups:
            rect, ti, ri, col = cells[g[0]["_cell"]][:4]
            u = _group_to_unit(page, g, rect)
            if u is not None:
                u.tkey = (ti, ri, col)
                if key >= 0 or any(sg.get("_subcol") for sg in g):  # 숨은 열/행: 셀 가운데 정렬로 보지 않는다
                    u.tab = True
                    if len(g) == 1:
                        u.align = "left"
            add(u, bool(u is not None and u.tab))
    mark_keep_columns(units)
    return units


def figure_regions(page, min_w: float = 40, min_h: float = 20) -> list["fitz.Rect"]:
    """페이지에 배치된 래스터 이미지(그림/사진/스캔 도면) 영역. 겹치면 합친다."""
    try:
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001
        return []
    rects = []
    for im in infos:
        r = fitz.Rect(im["bbox"]) & page.rect
        if r.is_empty or r.width < min_w or r.height < min_h:
            continue
        rects.append(r)
    changed = True
    while changed:
        changed = False
        out: list = []
        for r in rects:
            for o in out:
                if r.intersects(o):
                    o |= r
                    changed = True
                    break
            else:
                out.append(fitz.Rect(r))
        rects = out
    return rects


def build_ocr_engine(cfg: Config):
    """OCR 엔진 준비. OCR 끔이면 None, Tesseract가 없으면 available=False 인 엔진."""
    if cfg.ocr_mode == "off":
        return None
    try:
        from engines.ocr_engine import OcrEngine
    except Exception as e:  # noqa: BLE001
        print(f"! OCR 모듈 로드 실패: {e}", file=sys.stderr)
        return None
    return OcrEngine(lang=cfg.ocr_lang, dpi=cfg.ocr_dpi, min_conf=cfg.ocr_min_conf,
                     fig_min_conf=cfg.ocr_fig_min_conf, tesseract_cmd=cfg.tesseract_cmd or None,
                     deskew=cfg.deskew, bullet_re=BULLET_RE)


def run_ocr_jobs(doc, jobs: list, ocr, cache: Cache, fhash: str, cfg: Config, label: str = "") -> dict:
    """jobs: [(pno, mode, clip Rect, exclude rects)] → {job index: [OcrBlock]}"""
    from engines.ocr_engine import OcrBlock
    results: dict[int, list] = {}
    pending = []
    for i, (pno, mode, clip, excl) in enumerate(jobs):
        sig = json.dumps([fhash, pno, mode, [round(v, 1) for v in clip],
                          sorted([round(v, 1) for v in r] for r in excl), ocr.signature()])
        key = "ocr|" + hashlib.sha256(sig.encode()).hexdigest()
        hit = cache.get(key)
        if hit is not None:
            results[i] = [OcrBlock.from_json(x) for x in json.loads(hit)]
        else:
            pending.append((i, key))
    if not pending:
        return results
    t0 = time.time()
    n_done = 0
    with ThreadPoolExecutor(max_workers=max(1, cfg.ocr_workers)) as ex:
        live = {}

        def collect(fs):
            nonlocal n_done
            for f in fs:
                i, key = live.pop(f)
                try:
                    blocks = f.result()
                except Exception as e:  # noqa: BLE001
                    print(f"\n  ! OCR 실패 p.{jobs[i][0] + 1}: {e}", file=sys.stderr)
                    blocks = []
                results[i] = blocks
                cache.put(key, json.dumps([b.to_json() for b in blocks], ensure_ascii=False))
                n_done += 1
                print(f"  [{label}] OCR {n_done}/{len(pending)} ({time.time() - t0:.0f}s)",
                      end="\r", flush=True)

        for i, key in pending:
            pno, mode, clip, excl = jobs[i]
            page = doc[pno]
            dpi = ocr.dpi if mode == "page" else min(600, int(ocr.dpi * 1.25))
            rgb = ocr.render(page, clip=clip, dpi=dpi)  # 렌더링은 메인 스레드에서만
            f = ex.submit(ocr.ocr_image, rgb, (clip.x0, clip.y0), dpi / 72.0, mode,
                          [tuple(r) for r in excl])
            live[f] = (i, key)
            del rgb
            while len(live) >= max(2, cfg.ocr_workers * 2):
                done, _ = wait(list(live), return_when=FIRST_COMPLETED)
                collect(done)
        collect(list(as_completed(list(live))))
    print(" " * 70, end="\r")
    return results


def assign_ocr_words(units: list[Unit], words: list) -> None:
    """글자깨짐 페이지: OCR 단어를 원본 텍스트 상자(Unit)에 넣어 글자 내용을 교체한다."""
    rects = [(fitz.Rect(w.rect), w) for w in words]
    for u in units:
        box = fitz.Rect(u.rect) + (-1.5, -1.5, 1.5, 1.5)
        inside = [w for r, w in rects if box.contains(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))]
        if not inside:
            u.text = ""  # 인식된 글자 없음 → 번역하지 않고 원본 그대로
            continue
        inside.sort(key=lambda w: ((w.rect[1] + w.rect[3]) / 2, w.rect[0]))
        lines: list[list] = []
        for w in inside:
            cy = (w.rect[1] + w.rect[3]) / 2
            h = max(1.0, w.rect[3] - w.rect[1])
            if lines and abs(cy - (lines[-1][-1].rect[1] + lines[-1][-1].rect[3]) / 2) < 0.5 * h:
                lines[-1].append(w)
            else:
                lines.append([w])
        text = ""
        for ln in lines:
            t = " ".join(w.text for w in sorted(ln, key=lambda w: w.rect[0]))
            if text.endswith("-") and len(text) > 2 and text[-2].isalpha() and t[:1].islower():
                text = text[:-1] + t
            else:
                text = (text + " " + t).strip()
        u.text = clean_text(text)
        u.conf = sum(w.conf for w in inside) / len(inside)
        u.source = "native-ocr"
        if u.conf < 80:
            u.notes.append(f"OCR_LOW_CONF({u.conf:.0f}) 원문 확인")


def block_to_unit(pno: int, b) -> Unit:
    u = Unit(page=pno, idx=0, rect=fitz.Rect(b.rect), text=clean_text(b.text), size=b.size,
             color=0, bold=b.bold, origin_y=b.origin_y, align=b.align, ocr=True,
             source=b.source, conf=b.conf, cell=fitz.Rect(b.cell) if b.cell else None,
             fg=tuple(b.fg), bg=tuple(b.bg))
    if b.conf < 80:
        u.notes.append(f"OCR_LOW_CONF({b.conf:.0f}) 원문 확인")
    return u


FONT_CANDIDATES = {
    "regular": [
        r"C:\Windows\Fonts\malgun.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/Library/Fonts/NanumGothic.ttf",
        os.path.expanduser("~/Library/Fonts/NanumGothic.ttf"),
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    ],
    "bold": [
        r"C:\Windows\Fonts\malgunbd.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/Library/Fonts/NanumGothicBold.ttf",
    ],
}


# 한글 글꼴에 없는 기호(−, ≤, ₂, µ, Ω …)를 그릴 때 차례로 찾아보는 글꼴
FALLBACK_FONTS = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\seguisym.ttf",
    r"C:\Windows\Fonts\cambria.ttc",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


class MixFont:
    """한글 글꼴 + 대체 글꼴 묶음. 한글 글꼴에 없는 글자만 대체 글꼴로 그려 □(깨진 글자)가 생기지 않게 한다.
    fitz.Font 처럼 text_length / has_glyph 를 제공한다."""

    def __init__(self, primary, fallbacks: Optional[list] = None):
        self.primary = primary
        self.fallbacks = fallbacks or []   # [{"name", "font", "buf", "path"}]
        self._where: dict[str, Optional[int]] = {}

    def where(self, ch: str) -> Optional[int]:
        """-1: 한글 글꼴, 0..: 대체 글꼴 번호, None: 어느 글꼴에도 없음"""
        if ch not in self._where:
            idx: Optional[int] = None
            if ch.isspace() or self.primary.has_glyph(ord(ch)):
                idx = -1
            else:
                for k, fb in enumerate(self.fallbacks):
                    if fb["font"].has_glyph(ord(ch)):
                        idx = k
                        break
            self._where[ch] = idx
        return self._where[ch]

    def has_glyph(self, cp: int) -> bool:
        return self.where(chr(cp)) is not None

    def runs(self, text: str) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for ch in text:
            k = self.where(ch)
            k = -1 if k is None else k
            if out and out[-1][0] == k:
                out[-1] = (k, out[-1][1] + ch)
            else:
                out.append((k, ch))
        return out

    def font_of(self, k: int):
        return self.primary if k < 0 else self.fallbacks[k]["font"]

    def text_length(self, text: str, fontsize: float = 11) -> float:
        if all(self.where(ch) in (-1, None) for ch in text):
            return self.primary.text_length(text, fontsize=fontsize)
        return sum(self.font_of(k).text_length(part, fontsize=fontsize) for k, part in self.runs(text))


def find_font(explicit: str, kind: str) -> str:
    if explicit:
        if not Path(explicit).exists():
            sys.exit(f"폰트 파일을 찾을 수 없습니다: {explicit}")
        return explicit
    for p in FONT_CANDIDATES[kind]:
        if Path(p).exists():
            return p
    return ""


EXTRA_CHARS = "°²³±×µΩ℃℉–—•·≤≥≈~∅Ø′″“”‘’…→←↑↓"


def _korean_face_index(path: str) -> int:
    """TTC/OTC 안에서 한국어 얼굴(KR)을 찾는다. 없으면 0."""
    try:
        from fontTools.ttLib import TTCollection
        coll = TTCollection(path)
        for i, f in enumerate(coll.fonts):
            nm = " ".join(str(f["name"].getDebugName(k) or "") for k in (1, 4, 16))
            if re.search(r"\bKR\b|Korean|Malgun|Nanum|Gothic Neo", nm, re.I):
                return i
    except Exception:  # noqa: BLE001
        pass
    return 0


def _renders_hangul(data: bytes) -> bool:
    """서브셋 폰트가 실제로 한글을 그리는지 검증."""
    try:
        d = fitz.open()
        pg = d.new_page(width=200, height=50)
        pg.insert_font(fontname="T", fontbuffer=data)
        pg.insert_text((8, 30), "한글가나", fontname="T", fontsize=16)
        s = pg.get_pixmap(dpi=72).samples
        return sum(1 for i in range(0, len(s), 3) if s[i] < 128) > 40
    except Exception:  # noqa: BLE001
        return False


def subset_font_bytes(path: str, chars: set[str], hangul: bool = True) -> Optional[bytes]:
    """사용된 글자만 남긴 폰트 바이트를 만든다(출력 PDF 용량 절감). 실패 시 None."""
    try:
        from fontTools import subset
        import io
    except ImportError:
        print("  ! fonttools 미설치: 폰트 전체가 PDF에 삽입되어 용량이 커집니다 (pip install fonttools)",
              file=sys.stderr)
        return None
    try:
        opts = subset.Options()
        opts.retain_gids = True  # 필수: 없으면 MuPDF에서 글리프가 비어 보임
        opts.hinting = False
        opts.notdef_outline = True
        if path.lower().endswith((".ttc", ".otc")):
            opts.font_number = _korean_face_index(path)
        font = subset.load_font(path, opts, dontLoadGlyphNames=True)
        ss = subset.Subsetter(opts)
        ss.populate(text="".join(sorted(chars)))
        ss.subset(font)
        buf = io.BytesIO()
        subset.save_font(font, buf, opts)
        data = buf.getvalue()
        if not hangul or _renders_hangul(data):
            return data
        print("  ! 폰트 서브셋 검증 실패 → 전체 폰트 삽입", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  ! 폰트 서브셋 실패({e}) → 전체 폰트 삽입", file=sys.stderr)
    return None


def build_fonts(cfg: Config, units: list[Unit]) -> dict:
    reg = find_font(cfg.font, "regular")
    if not reg:
        sys.exit("한글 폰트를 찾지 못했습니다. --font 로 TTF/TTC 경로를 지정하세요. "
                 "(예: C:\\Windows\\Fonts\\malgun.ttf)")
    bold = find_font(cfg.font_bold, "bold")
    used = set()
    for u in units:
        if u.ko:
            used |= set(u.ko)
    chars = set(EXTRA_CHARS) | {chr(c) for c in range(0x20, 0x7F)} | used
    out = {}
    for key, path in (("regular", reg), ("bold", bold)):
        if not path:
            out[key] = {"font": None, "buf": None, "path": None}
            continue
        data = subset_font_bytes(path, chars)
        raw = fitz.Font(fontbuffer=data) if data else fitz.Font(fontfile=path)
        out[key] = {"font": raw, "buf": data, "path": path}
    # 한글 글꼴에 없는 글자 → 대체 글꼴(필요한 글자만 서브셋)에서 찾는다
    base = out["regular"]["font"]
    missing = {c for c in used if not c.isspace() and not base.has_glyph(ord(c))}
    fallbacks: list[dict] = []
    for path in FALLBACK_FONTS:
        if not missing:
            break
        if not Path(path).exists():
            continue
        try:
            full = fitz.Font(fontfile=path)
        except Exception:  # noqa: BLE001
            continue
        got = {c for c in missing if full.has_glyph(ord(c))}
        if not got:
            continue
        data = subset_font_bytes(path, got | {" "}, hangul=False)
        font = full
        if data:
            try:
                sub = fitz.Font(fontbuffer=data)
                if all(sub.has_glyph(ord(c)) for c in got):
                    font = sub
                else:
                    data = None
            except Exception:  # noqa: BLE001
                data = None
        fallbacks.append({"name": f"OMFB{len(fallbacks)}", "font": font, "buf": data, "path": path})
        missing -= got
    for key in ("regular", "bold"):
        if out[key]["font"] is not None:
            out[key]["font"] = MixFont(out[key]["font"], fallbacks)
    out["fallbacks"] = fallbacks
    return out


def insert_font(page, name: str, spec: dict):
    if spec["buf"]:
        page.insert_font(fontname=name, fontbuffer=spec["buf"])
    else:
        page.insert_font(fontname=name, fontfile=spec["path"])


LINE_H = 1.22


def wrap_text(text: str, font, size: float, width: float, hang: float = 0.0) -> list[str]:
    """hang: 둘째 줄부터 줄 폭을 그만큼 줄인다 (내어쓰기 문단)"""
    lines: list[str] = []
    full = width
    for para in text.split("\n"):
        cur = ""
        for w in para.split(" "):
            width = full - hang if lines else full
            cand = w if not cur else cur + " " + w
            if font.text_length(cand, fontsize=size) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
                width = full - hang
            if font.text_length(w, fontsize=size) <= width:
                cur = w
            else:  # 한 단어가 너무 길면 글자 단위로 줄바꿈
                piece = ""
                for ch in w:
                    if font.text_length(piece + ch, fontsize=size) <= width:
                        piece += ch
                    else:
                        if piece:
                            lines.append(piece)
                        piece = ch
                cur = piece
        lines.append(cur)
    return lines


def _shrink_fit(text, font, width, height, start, lo, tol=0.35, hang=0.0):
    size = start
    words = [w for w in text.split() if w]
    while True:
        lines = wrap_text(text, font, size, width, hang)
        ok = len(lines) * size * LINE_H <= height + size * tol
        if ok and words:  # 단어가 글자 단위로 쪼개지면(예: '개/정') 더 줄여 본다
            ok = max(font.text_length(w, fontsize=size) for w in words) <= width - hang + 0.5
        if ok or size <= lo + 1e-6:
            return size, lines, ok
        size = max(lo, size - 0.25)


def fit_text(text, font, w0, h0, box_w, box_h, orig_size, cfg: Config, hang: float = 0.0):
    """원래 박스 → 폭 확장 → 폭+높이 확장(셀 바닥/다음 글자 직전까지) 순서로 92%, 85%까지 축소해 시도,
    그래도 안 되면 확장 박스에서 하한(min_scale/min_size)까지 축소.
    반환: (크기, 줄 목록, 맞음여부, 사용폭)"""
    lo = min(orig_size, max(cfg.min_size, orig_size * cfg.min_scale))
    W, H = max(box_w, w0), max(box_h, h0)
    for tier in (0.92, 0.85):  # 약간 축소 → 폭/높이 확장 → 조금 더 축소 순
        mid = max(lo, orig_size * tier)
        for w, h in ((w0, h0), (W, h0), (W, H)):
            s, ls, ok = _shrink_fit(text, font, w, h, orig_size, mid, hang=hang)
            if ok:
                return s, ls, ok, w
    s, ls, ok = _shrink_fit(text, font, W, H, orig_size, lo, hang=hang)
    if not ok:  # 겹쳐 그리는 것보다 작은 글자가 낫다: 원래 크기의 45%(최소 4pt)까지 더 축소
        s, ls, ok = _shrink_fit(text, font, W, H, s, max(4.0, orig_size * 0.45), hang=hang)
    return s, ls, ok, W


def page_geometry(page) -> tuple[list, list, list]:
    """표 테두리/박스 경계선과 가는 가로선.
    반환: 세로선 [(x, y0, y1)], 가로선 [(y, x0, x1)], 가는 가로선(밑줄 후보) [(Rect, 색)]"""
    vedges: list[tuple[float, float, float]] = []
    hedges: list[tuple[float, float, float]] = []
    thin: list = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return vedges, hedges, thin
    if len(drawings) > 8000:  # 매우 복잡한 도면: 속도를 위해 생략
        return vedges, hedges, thin
    for d in drawings:
        items = d["items"]
        for it in items:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) < 0.8 and abs(p1.y - p2.y) >= 5:
                    vedges.append((p1.x, min(p1.y, p2.y), max(p1.y, p2.y)))
                elif abs(p1.y - p2.y) < 0.8 and abs(p1.x - p2.x) >= 5:
                    hedges.append((p1.y, min(p1.x, p2.x), max(p1.x, p2.x)))
                    w = d.get("width") or 0.5
                    if len(items) == 1 and w <= 1.6 and d.get("color") is not None:
                        y = (p1.y + p2.y) / 2
                        thin.append((fitz.Rect(min(p1.x, p2.x), y - w / 2, max(p1.x, p2.x), y + w / 2),
                                     tuple(d["color"])))
            elif it[0] == "re":
                r = it[1]
                if r.height >= 5:
                    vedges.append((r.x0, r.y0, r.y1))
                    vedges.append((r.x1, r.y0, r.y1))
                if r.width >= 5:
                    hedges.append((r.y0, r.x0, r.x1))
                    hedges.append((r.y1, r.x0, r.x1))
                if len(items) == 1 and r.height <= 1.6 and r.width >= 4 and d.get("fill") is not None:
                    thin.append((fitz.Rect(r), tuple(d["fill"])))
    return vedges, hedges, thin


def page_edges(page) -> tuple[list, list]:
    """표 테두리/박스 경계선. 세로선 (x, y0, y1), 가로선 (y, x0, x1)."""
    v, h, _thin = page_geometry(page)
    return v, h


def free_span(u: Unit, units: list[Unit], vedges, page_rect) -> tuple[float, float]:
    """유닛이 가로로 사용할 수 있는 한계 (left, right)."""
    cy = (u.rect.y0 + u.rect.y1) / 2
    left, right = page_rect.x0 + 12, page_rect.x1 - 12
    for x, y0, y1 in vedges:
        if y0 + 0.5 < cy < y1 - 0.5:
            if x >= u.rect.x1 - 0.5:
                right = min(right, x - 2)
            elif x <= u.rect.x0 + 0.5:
                left = max(left, x + 2)
    for o in units:
        if o is u:
            continue
        ov = min(o.rect.y1, u.rect.y1) - max(o.rect.y0, u.rect.y0)
        if ov > 0.3 * min(o.rect.height, u.rect.height):
            if o.rect.x0 >= u.rect.x1 - 0.5:
                right = min(right, o.rect.x0 - 3)
            elif o.rect.x1 <= u.rect.x0 + 0.5:
                left = max(left, o.rect.x1 + 3)
    if u.cell is not None:
        left = max(left, u.cell.x0 + 1.5)
        right = min(right, u.cell.x1 - 1.5)
    return min(left, u.rect.x0), max(right, u.rect.x1)


def free_bottom(u: Unit, units: list[Unit], hedges, page_rect, left: float, right: float) -> float:
    """유닛이 아래로 늘어날 수 있는 한계 y. 셀 안이면 셀 바닥까지, 아니면 최대 1줄."""
    bottom = page_rect.y1 - 10
    if u.cell is not None:
        bottom = min(bottom, u.cell.y1 - 1.5)
    for o in units:
        if o is u:
            continue
        # 줄 간격이 좁으면 위아래 글자 상자가 1~2pt 겹친다 → 윗변이 내 높이의 절반 아래면 '아래 줄'
        if o.rect.y0 > u.rect.y0 + 0.5 * u.rect.height and o.rect.x1 > left and o.rect.x0 < right:
            bottom = min(bottom, max(o.rect.y0 - 0.5, u.rect.y1))
    for y, xa, xb in hedges:
        if y >= u.rect.y1 - 0.3 and xb > left and xa < right:
            bottom = min(bottom, y - 1.5)
    if u.cell is None:
        bottom = min(bottom, u.rect.y1 + u.size * LINE_H * 1.05)
    return max(bottom, u.rect.y1)


def apply_redactions(page, whiteout: bool):
    page.apply_redactions(**_redact_kw(page, whiteout))


def detach_annotations(doc) -> dict:
    """모든 페이지의 주석(Bluebeam Markup, 스탬프, 서명, 링크, 양식)을 잠시 떼어 둔다.
    - 리댁션(원문 글자 지우기)은 겹치는 주석을 함께 지워 버린다 → 떼어 두면 지워지지 않는다
    - 주석 안의 글자(스탬프 문구, 글상자)를 본문으로 잘못 읽어 번역하지 않는다
    restore_annotations 로 저장 직전에 그대로 다시 붙인다. 반환: {페이지 번호: 원래 /Annots 값}"""
    stash: dict[int, str] = {}
    for pno in range(len(doc)):
        try:
            xref = doc.page_xref(pno)
            kind, val = doc.xref_get_key(xref, "Annots")
            if kind != "null" and val and val != "null":
                stash[pno] = val
                doc.xref_set_key(xref, "Annots", "null")
        except Exception:  # noqa: BLE001
            continue
    return stash


def restore_annotations(doc, stash: dict) -> None:
    for pno, val in stash.items():
        try:
            doc.xref_set_key(doc.page_xref(pno), "Annots", val)
        except Exception as e:  # noqa: BLE001
            print(f"  ! p.{pno + 1} 주석 복원 실패: {e}", file=sys.stderr)


def _place_lines(u: Unit, font, size: float, lines: list[str]):
    """줄별 위치 계산. 반환: (placed[(x, y, 줄)], 차지 영역, 줄 목록)"""
    asc = (u.origin_y - u.rect.y0) / u.size if u.size else 0.85
    asc = min(max(asc, 0.7), 1.0)
    if u.size and not u.rotated and u.origin_y > u.rect.y0:
        y = _ink(u).y0 + 0.78 * size  # 원래 글자 윗선에 맞춤 (원래 크기면 원래 기준선과 같음)
    else:
        y = u.rect.y0 + asc * size
    placed = []
    area = None
    if u.leader and lines and u.align == "left":
        # 목차: 한글 제목 뒤를 원래 점선 끝(쪽번호 앞)까지 점으로 채운다
        num = (" " + u.leader_num) if u.leader_num else ""
        x_end = u.rect.x0 + font.text_length(lines[-1], fontsize=size)
        dot = font.text_length(".", fontsize=size) or 1.0
        n = int((u.rect.x1 - x_end - font.text_length(num, fontsize=size) - dot) / dot)
        if n >= 3:
            lines = lines[:-1] + [lines[-1] + " " + "." * (n - 1) + num]
        elif num:
            lines = lines[:-1] + [lines[-1] + num]
    for k, ln in enumerate(lines):
        tw = font.text_length(ln, fontsize=size)
        if u.align == "center":
            x = u.rect.x0 + (u.rect.width - tw) / 2
        elif u.align == "right":
            x = u.rect.x1 - tw
        else:
            x = u.rect.x0 + (u.hang if k and not u.leader else 0.0)
        if ln:
            placed.append((x, y, ln))
            r = fitz.Rect(x, y - asc * size, x + tw, y + 0.28 * size)
            area = r if area is None else (area | r)
        y += size * LINE_H
    return placed, (area if area is not None else fitz.Rect(u.rect)), lines


def _ink(o: Unit) -> "fitz.Rect":
    """글자가 실제로 찍히는 영역 (글꼴 상자는 위아래 여백이 커서 줄 간격이 좁으면 서로 겹친다)"""
    r = o.rect
    if not o.size or o.rotated:
        return fitz.Rect(r)
    top = min(max(r.y0, o.origin_y - 0.78 * o.size), r.y1)
    pad = max(0.0, top - r.y0)
    bot = max(top, r.y1 - 0.5 * pad)
    return fitz.Rect(r.x0, top, r.x1, bot)


def _line_ink(ln: tuple) -> "fitz.Rect":
    """원문 한 줄에서 글자가 실제로 찍힌 영역 (x0, y0, x1, y1, 기준선, 크기)"""
    x0, y0, x1, y1, oy, size = ln
    top = min(max(y0, oy - 0.78 * size), y1)
    pad = max(0.0, top - y0)
    return fitz.Rect(x0, top, x1, max(top, y1 - 0.5 * pad))


def _snap(pos: float, stops: list[float]) -> float:
    return min(stops, key=lambda v: abs(v - pos)) if stops else pos


def _underline_spans(u: Unit, placed: list, size: float, font) -> list:
    """번역문에 다시 그릴 밑줄 [(x0, x1, y, 두께, 색)].
    원문 줄 전체에 밑줄 → 번역문 전체에 밑줄. 일부만 밑줄 → 같은 비율의 위치를 낱말 경계에 맞춰 밑줄."""
    if not u.uls or not placed:
        return []
    ref = u.uls[0]
    k = size / max(ref["size"], 1.0)
    off = (ref["rect"].y0 - ref["oy"]) * k
    th = max(0.35, ref["rect"].height * k)
    full = all(sum(x["rect"].width for x in u.uls if abs(x["oy"] - ln[4]) < 0.5) >= 0.85 * (ln[2] - ln[0])
               for ln in u.lines) if u.lines else False
    out = []
    if full:
        for x, y, ln in placed:
            tw = font.text_length(ln.rstrip(), fontsize=size)
            if tw > 1:
                out.append((x, x + tw, y + off, th, ref["color"]))
        return out
    x, y, ln = placed[0]
    ln = ln.rstrip()
    tw = font.text_length(ln, fontsize=size)
    starts, ends = [0.0], []
    for m in re.finditer(r"\s+", ln):
        ends.append(font.text_length(ln[:m.start()], fontsize=size))
        starts.append(font.text_length(ln[:m.end()], fontsize=size))
    ends.append(tw)
    w0 = max(ref["x1"] - ref["x0"], 1.0)
    dropped = False
    for ul in u.uls:
        if abs(ul["oy"] - u.lines[0][4]) > 0.5:
            dropped = True  # 둘째 줄 이후의 부분 밑줄: 번역문에서 위치를 알 수 없어 생략
            continue
        a = _snap((ul["rect"].x0 - ul["x0"]) / w0 * tw, starts)
        b = _snap((ul["rect"].x1 - ul["x0"]) / w0 * tw, ends)
        if b - a > 1:
            out.append((x + a, x + b, y + off, th, ul["color"]))
        else:
            dropped = True
    if dropped:
        u.notes.append("UNDERLINE_PART(문장 중간의 밑줄 일부 생략 - 필요 시 수동 표시)")
    return out


def _line_boxes(placed: list, size: float, font) -> list:
    return [fitz.Rect(x, y - 0.8 * size, x + font.text_length(ln, fontsize=size), y + 0.18 * size)
            for x, y, ln in placed]


def _conflicts(u: Unit, placed: list, size: float, font, others: list[Unit]) -> bool:
    """번역문이 실제로 찍힐 영역이 다른 문구의 글자 영역을 침범하는지 (원래부터 겹치던 만큼은 허용)."""
    boxes = _line_boxes(placed, size, font)
    if not boxes:
        return False
    mine = _ink(u)
    for o in others:
        oi = _ink(o)
        allow = (mine & oi).height if not (mine & oi).is_empty else 0.0
        allow = min(allow, 0.35 * max(min(u.size, o.size), 1.0))
        for bx in boxes:
            it = bx & oi
            if not it.is_empty and it.width > 2.0 and it.height > allow + 0.3:
                return True
    return False


def _same_as_source(u: Unit) -> bool:
    norm = lambda t: re.sub(r"\s+", "", t or "")  # noqa: E731
    return norm(u.ko) == norm(u.text)


def _redact_kw(page, whiteout: bool = False) -> dict:
    try:
        params = inspect.signature(page.apply_redactions).parameters
    except (TypeError, ValueError):
        params = {}
    kw = {}
    if "images" in params:
        kw["images"] = fitz.PDF_REDACT_IMAGE_PIXELS if whiteout else fitz.PDF_REDACT_IMAGE_NONE
    if "graphics" in params:
        kw["graphics"] = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
    return kw


def remove_underlines(page, units: list[Unit]) -> set:
    """번역할 문구의 원래 밑줄(가는 선)만 지운다. 글자·다른 선·그림은 건드리지 않는다.
    반환: 실제로 밑줄이 지워진 Unit id (이 문구들만 번역 폭에 맞춰 밑줄을 다시 그린다)."""
    cand = [u for u in units if u.uls]
    if not cand:
        return set()
    kw = _redact_kw(page)
    try:
        params = inspect.signature(page.apply_redactions).parameters
    except (TypeError, ValueError):
        params = {}
    if "graphics" not in kw or "text" not in params or not hasattr(fitz, "PDF_REDACT_TEXT_NONE"):
        return set()  # 구버전 PyMuPDF: 글자를 지키면서 선만 지울 수 없음 → 원래 밑줄 그대로 둠
    kw["graphics"] = getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1)
    kw["text"] = fitz.PDF_REDACT_TEXT_NONE
    for u in cand:
        for ul in u.uls:
            page.add_redact_annot(ul["rect"] + (-0.4, -0.4, 0.4, 0.4), fill=False)
    page.apply_redactions(**kw)
    left = page_geometry(page)[2]
    done = set()
    for u in cand:
        gone = all(not any(abs(r.y0 - ul["rect"].y0) < 0.3 and r.x0 < ul["rect"].x1 - 1 and r.x1 > ul["rect"].x0 + 1
                           for r, _c in left) for ul in u.uls)
        if gone:
            done.add(id(u))
    return done


def render_page(page, units: list[Unit], fonts, cfg: Config) -> dict:
    """반환: {"ul_removed": 지운 밑줄 수, "drawn": 새로 그린 도형 수(밑줄 + OCR 배경 패치)} → 최종 검사(선/도형 보존)에 사용"""
    stats = {"ul_removed": 0, "drawn": 0}
    # 번역해도 원문과 같은 문구(사람 이름·날짜·코드 등)는 지우고 다시 쓰지 않고 원본 글꼴 그대로 둔다
    todo = [u for u in units if u.ko and not _same_as_source(u)]
    for u in list(todo):
        font = fonts["bold" if (u.bold and fonts["bold"]["font"]) else "regular"]["font"]
        miss = {c for c in u.ko if not c.isspace() and not font.has_glyph(ord(c))}
        if miss:  # 어느 글꼴에도 없는 글자: 같은 뜻의 기본 글자로 바꿔 본다 (− → -, ₂ → 2 …)
            u.ko = "".join(DENORM.get(c, c) if c in miss else c for c in u.ko)
            miss = sorted({c for c in u.ko if not c.isspace() and not font.has_glyph(ord(c))})
        if miss:  # 그래도 없으면 그리면 □□□ 로 깨짐 → 원문 유지
            todo.remove(u)
            u.notes.append("GLYPH_MISSING(글꼴에 없는 글자 " + "".join(miss[:8]) + " → 원문 유지)")
    if not todo:
        return stats
    vedges, hedges = page_edges(page)  # 리댁션 전에 계산(원본 도형 기준)
    has_text_layer = bool(page.get_text("text").strip())

    # 1) 배치 계산 (원본 위치/폭/높이 기준)
    plans = []
    for u in todo:
        use_bold = bool(u.bold and fonts["bold"]["font"])
        font = fonts["bold"]["font"] if use_bold else fonts["regular"]["font"]
        fname = "OMKOB" if use_bold else "OMKO"
        left, right = free_span(u, units, vedges, page.rect)
        if u.align == "center":
            cx = (u.rect.x0 + u.rect.x1) / 2
            box_w = 2 * min(cx - left, right - cx)
        elif u.align == "right":
            box_w = u.rect.x1 - left
        else:
            box_w = right - u.rect.x0
        bottom = free_bottom(u, units, hedges, page.rect, left, right)
        if u.source == "ocr-figure":
            # 그림 속 라벨: 래스터 그림의 선/기호를 덮지 않도록 원래 자리에서 거의 넓히지 않고 축소로 맞춘다
            box_w = min(box_w, u.rect.width * 1.12 + 2)
            bottom = u.rect.y1 + 0.15 * u.size
        # 번역 글자 크기: 원문 크기 × 설정 비율(기본 92%)에서 시작해 필요하면 더 줄인다
        base = max(3.5, u.size * getattr(cfg, "text_scale", 1.0))
        hang = u.hang if u.align == "left" and not u.leader else 0.0
        size, lines, ok, _w = fit_text(u.ko, font, u.rect.width, u.rect.height, box_w,
                                       bottom - u.rect.y0, base, cfg, hang)
        u.used_size = size
        if not ok:
            u.notes.append("OVERFLOW(박스 초과, 글자 축소 한계)")
        placed, area, lines = _place_lines(u, font, size, lines)
        # 겹침 방지: 번역문이 다른 문구 자리(아래 줄 등)를 침범하면 원래 칸 안으로 다시 맞춘다
        others = [o for o in units if o is not u and o.text]
        if _conflicts(u, placed, size, font, others):
            # 크기를 0.25pt씩 줄이며(최소 4pt) 다른 문구를 침범하지 않는 첫 크기를 찾는다
            fixed = False
            sz = size
            while sz > 4.0 + 1e-6:
                sz = max(4.0, sz - 0.25)
                ls = wrap_text(u.ko, font, sz, box_w, hang)
                pl, ar, ls = _place_lines(u, font, sz, ls)
                if not _conflicts(u, pl, sz, font, others):
                    size, lines, placed, area, fixed = sz, ls, pl, ar, True
                    break
            u.used_size = size
            if fixed:
                u.notes.append(f"FIT_SMALLER(겹침 방지로 {size:.1f}pt 축소)")
            else:
                u.notes.append("TEXT_OVERLAP(공간 부족 - 확인 필요)")
        if u.ocr:
            color = tuple(u.fg) if u.fg else (0, 0, 0)
        else:
            color = fitz.sRGB_to_pdf(u.color) if hasattr(fitz, "sRGB_to_pdf") else (0, 0, 0)
        plans.append((u, fname, size, placed, area, color))

    # 1-1) 겹침/페이지 밖 검사 (최종 검사용)
    fonts_obj = {"OMKO": fonts["regular"]["font"], "OMKOB": fonts["bold"]["font"]}
    for i in range(len(plans)):
        ui, ai = plans[i][0], plans[i][4]
        if not page.rect.contains(ai):
            ui.notes.append("PAGE_OVER(페이지 밖으로 나감)")
        bi = _line_boxes(plans[i][3], plans[i][2], fonts_obj[plans[i][1]])
        for j in range(i + 1, len(plans)):
            uj, aj = plans[j][0], plans[j][4]
            if (ai & aj).is_empty:
                continue
            bj = _line_boxes(plans[j][3], plans[j][2], fonts_obj[plans[j][1]])
            if any((a & b).width > 1.5 and (a & b).height > 0.5 for a in bi for b in bj
                   if not (a & b).is_empty):
                ui.notes.append(f"TEXT_OVERLAP(다른 문구와 겹침: {uj.text[:20]})")

    # 2) 원문 제거. 주석(스탬프/Markup/링크)은 main 에서 미리 떼어 두었다가 저장 직전에 그대로 다시 붙인다
    #    (리댁션은 겹치는 주석을 지워 버리므로)
    #  2-1) 번역할 문구의 밑줄만 먼저 제거 (번역문 폭에 맞춰 4단계에서 다시 그림)
    try:
        ul_done = remove_underlines(page, [u for u in todo if not u.ocr and not u.leader])
    except Exception:  # noqa: BLE001
        ul_done = set()
    stats["ul_removed"] = sum(len(u.uls) for u in todo if id(u) in ul_done)
    #  2-2) 글자 제거: 네이티브 텍스트는 리댁션(그림/선은 보존), OCR 영역의 숨은/깨진 텍스트층도 제거
    n_redact = 0
    any_ocr = any(u.ocr for u in todo)
    for u in todo:
        if not u.ocr:
            # 문구 전체 상자가 아니라 '원문 줄마다, 글자가 실제로 찍힌 영역'만 지운다
            # → 상자 안에 걸친 다른 글자(번호 '2.', 수식 기호, 이웃 줄)를 지우지 않음
            src_lines = u.lines or [(u.rect.x0, u.rect.y0, u.rect.x1, u.rect.y1, u.origin_y, u.size)]
            for ln in src_lines:
                ink = _line_ink(ln) if (u.size and not u.rotated) else fitz.Rect(ln[:4])
                r = fitz.Rect(ink.x0 + 0.3, ink.y0 + 0.2, ink.x1 - 0.3, max(ink.y1 - 0.2, ink.y0 + 0.6))
                if r.is_empty:
                    continue
                page.add_redact_annot(r, fill=(1, 1, 1) if cfg.whiteout else False)
                n_redact += 1
        elif has_text_layer and u.source == "ocr-page":
            page.add_redact_annot(fitz.Rect(u.rect) + (-1, -1, 1, 1), fill=False)
            n_redact += 1
    if n_redact:
        apply_redactions(page, cfg.whiteout and not any_ocr)

    # 3) OCR 영역: 원본 배경색으로 영어를 덮는 패치 (표 셀이면 괘선을 침범하지 않게 셀 안쪽으로 제한)
    for u, fname, size, placed, area, color in plans:
        if not u.ocr:
            continue
        patch = (fitz.Rect(u.rect) + (-1.2, -1.2, 1.2, 1.2)) | (area + (-0.6, -0.6, 0.6, 0.6))
        if u.cell is not None:
            patch &= (u.cell + (1.5, 1.5, -1.5, -1.5))
        if patch.is_empty:
            continue
        page.draw_rect(patch, color=None, fill=tuple(u.bg) if u.bg else (1, 1, 1), width=0, overlay=True)
        stats["drawn"] += 1

    # 4) 한글 쓰기 (한글 글꼴에 없는 기호는 대체 글꼴로) + 밑줄 다시 그리기
    insert_font(page, "OMKO", fonts["regular"])
    if fonts["bold"]["font"]:
        insert_font(page, "OMKOB", fonts["bold"])
    fb_used: set = set()
    for u, fname, size, placed, area, color in plans:
        mix = fonts_obj[fname]
        for x, y, ln in placed:
            for k, part in mix.runs(ln):
                fn = fname
                if k >= 0:
                    fb = mix.fallbacks[k]
                    fn = fb["name"]
                    if fn not in fb_used:
                        insert_font(page, fn, fb)
                        fb_used.add(fn)
                page.insert_text(fitz.Point(x, y), part, fontname=fn, fontsize=size, color=color)
                x += mix.font_of(k).text_length(part, fontsize=size)
        if id(u) in ul_done:
            for x0, x1, y, th, col in _underline_spans(u, placed, size, mix):
                page.draw_rect(fitz.Rect(x0, y, x1, y + th), color=None, fill=col, width=0, overlay=True)
                stats["drawn"] += 1
    return stats


def draw_layout_debug(src: str, units: list[Unit], pages: list[int], dst: str) -> int:
    """레이아웃 점검용 PDF: 인식한 문구 상자를 원본 위에 번호와 함께 표시 (API 사용 안 함).
    빨강=번역 대상, 회색=원문 유지, 초록=표 칸. 상자끼리 겹치거나 한 문장이 여러 상자로 쪼개졌는지 확인."""
    doc = fitz.open(src)
    by_page: dict[int, list[Unit]] = {}
    for u in units:
        by_page.setdefault(u.page, []).append(u)
    for pno in pages:
        page = doc[pno]
        if page.rotation and hasattr(page, "remove_rotation"):
            page.remove_rotation()
        for k, u in enumerate(by_page.get(pno, []), 1):
            col = (0.85, 0.1, 0.1) if u.status == "PENDING" else (0.55, 0.55, 0.55)
            if u.cell is not None:
                page.draw_rect(u.cell, color=(0.1, 0.6, 0.2), width=0.4, overlay=True)
            page.draw_rect(u.rect, color=col, width=0.6, overlay=True)
            page.insert_text(fitz.Point(u.rect.x0, max(u.rect.y0 - 0.8, 5)), f"{k}",
                             fontsize=5, color=(0, 0, 0.9), overlay=True)
        y = 8
        page.insert_text(fitz.Point(4, y), f"p.{pno + 1}  units={len(by_page.get(pno, []))}  "
                         "red=translate grey=keep green=table-cell", fontsize=6, color=(0, 0, 0.9))
    select_pages_safe(doc, pages)
    save_pdf_safe(doc, Path(dst))
    doc.close()
    return len(pages)


def parse_pages(spec: str, n: int) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else n
            out += list(range(a - 1, min(b, n)))
        else:
            out.append(int(part) - 1)
    return sorted({p for p in out if 0 <= p < n})


def prepare_units(doc, cfg: Config, protector: Protector, overrides: dict[str, str],
                  ocr=None, cache: Optional[Cache] = None, fhash: str = "", label: str = ""):
    """페이지 진단 → 네이티브 추출 + (필요 시) OCR → 번역 단위 목록.
    반환: (units, {pno: page_class}, n_figure_regions, {pno: (번역여부, 사유)})"""
    pages = cfg.pages if cfg.pages is not None else list(range(len(doc)))
    load_scope_keywords()
    scopes: dict[int, tuple[bool, str]] = {}
    drawing_counts: dict[int, int] = {}
    ocr_ok = bool(ocr is not None and ocr.available and cfg.ocr_mode != "off" and cache is not None)
    per_page: dict[int, list[Unit]] = {}
    classes: dict[int, str] = {}
    jobs = []
    t_start = time.time()
    for k, pno in enumerate(pages, 1):
        page = doc[pno]
        if k == 1 or k == len(pages) or k % 5 == 0:  # 진행 표시 (멈춘 것처럼 보이지 않도록)
            print(f"  [{label}] 페이지 분석 {k}/{len(pages)} ({int(time.time() - t_start)}초)      ",
                  end="\r", flush=True)
        cls = "forced" if (cfg.ocr_mode == "force" and ocr_ok) else classify_page(page)
        classes[pno] = cls
        if cls == "empty":
            scopes[pno] = (False, "빈 페이지")
            continue
        if cfg.scope == "om" and abs(page.rect) > A4_AREA * 1.6:
            scopes[pno] = (False, "대형 페이지(A3 이상) → 도면으로 판단")
            continue
        try:  # 그림 조각이 수천 개인 페이지(곡선·도면을 잘게 나눠 저장): 글자 지우기가 매우 느리고 위험 → 원본 그대로
            n_img = len(page.get_image_info())
        except Exception:  # noqa: BLE001
            n_img = 0
        if n_img > MAX_PAGE_IMAGES:
            scopes[pno] = (False, f"그림 조각 {n_img}개로 이루어진 페이지(곡선/도면) → 원문 유지")
            continue
        if cls == "garbled":
            # 글자깨짐 페이지: 글자 위치/크기/굵기는 PDF 원본 그대로, 글자 내용만 OCR로 읽는다
            try:
                units = extract_units(page)
            except Exception as e:  # noqa: BLE001
                print(f"  ! p.{pno + 1} 추출 실패: {e}", file=sys.stderr)
                units = []
            per_page[pno] = units
            if ocr_ok:
                jobs.append((pno, "words", fitz.Rect(page.rect), []))
            else:
                for u in units:
                    u.scope_keep = "글자깨짐 페이지 - OCR(Tesseract) 필요"
            continue
        if cls in ("scanned", "invisible", "forced"):
            if ocr_ok:
                jobs.append((pno, "page", fitz.Rect(page.rect), []))
            continue
        try:
            units = extract_units(page)
        except Exception as e:  # noqa: BLE001
            print(f"  ! p.{pno + 1} 추출 실패: {e}", file=sys.stderr)
            units = []
        per_page[pno] = units
        try:
            drawing_counts[pno] = len(page.get_cdrawings()) if hasattr(page, "get_cdrawings") else 0
        except Exception:  # noqa: BLE001
            drawing_counts[pno] = 0
        if ocr_ok and cfg.ocr_figures:
            for r in figure_regions(page):
                excl = [tuple(u.rect) for u in units if u.rect.intersects(r)]
                jobs.append((pno, "figure", r, excl))
    n_fig = sum(1 for j in jobs if j[1] == "figure")
    if jobs:
        res = run_ocr_jobs(doc, jobs, ocr, cache, fhash, cfg, label=label)
        for i, (pno, mode, clip, excl) in enumerate(jobs):
            if mode == "words":
                assign_ocr_words(per_page.get(pno, []), res.get(i, []))
                continue
            for b in res.get(i, []):
                per_page.setdefault(pno, []).append(block_to_unit(pno, b))
    all_units: list[Unit] = []
    for pno in pages:
        us = [u for u in per_page.get(pno, []) if u.text]
        us = merge_paragraphs(us, page_edges(doc[pno])[1])
        per_page[pno] = us
        if pno not in scopes:
            if cfg.scope == "om":
                scopes[pno] = page_scope(doc[pno], us, drawing_counts.get(pno, 0))
            else:
                scopes[pno] = (True, "")
        ok, why = scopes[pno]
        for i, u in enumerate(us):
            u.idx = i
            if not ok:
                u.scope_keep = why
            elif cfg.scope != "om":
                u.scope_keep = ""
            all_units.append(u)
    for u in all_units:
        classify_unit(u, protector, overrides)
    return all_units, classes, n_fig, scopes


def classify_unit(u: Unit, protector: Protector, overrides: dict[str, str]):
    m = LEADER_RE.match(u.text)
    if m and u.kind == "text":
        # 점선(과 쪽번호)은 번역에서 빼고 렌더링 때 다시 채움
        u.text, u.leader, u.leader_num = m.group(1).strip(), True, (m.group(2) or "")
        u.align = "left"
    if u.text in overrides:
        u.ko, u.status = overrides[u.text], "OVERRIDE"
        return
    ptxt, tokens = protector.protect(u.text, tokenize_numbers=len(u.text) <= 80)
    u.ptxt, u.tokens = ptxt, tokens
    if u.scope_keep:
        u.status = "SCOPE_KEEP"
    elif u.symbol or not needs_translation(ptxt):
        u.status = "KEEP"
    elif u.rotated:
        u.status = "ROTATED_SKIPPED"
        u.notes.append("회전 텍스트: 번역하지 않음(수동 처리 필요)")
    else:
        u.status = "PENDING"


def describe_classes(classes: dict[int, str]) -> str:
    c = Counter(classes.values())
    return ", ".join(f"{PAGE_CLASS_KO.get(k, k)} {v}p" for k, v in c.most_common())


def select_pages_safe(doc, pages: list[int]):
    """지정 페이지만 남긴다. 구조(태그/StructTree)가 비정상인 PDF에서 select()가 실패하면
    새 문서에 페이지를 복사하는 방식으로 대체한다."""
    try:
        doc.select(pages)
        return doc
    except Exception as e:  # noqa: BLE001
        print(f"  (페이지 선택 대체 방식 사용: {type(e).__name__})", file=sys.stderr)
    new = fitz.open()
    for p in pages:
        new.insert_pdf(doc, from_page=p, to_page=p, links=True, annots=True)
    return new


def save_pdf_safe(doc, dst: Path):
    """저장 최적화(garbage/deflate)가 비정상 PDF에서 실패하면 단계적으로 옵션을 낮춰 저장."""
    attempts = [dict(garbage=3, deflate=True), dict(garbage=1, deflate=True), dict()]
    last = None
    for kw in attempts:
        try:
            doc.save(str(dst), **kw)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  (저장 옵션 {kw or '기본'} 실패 → 재시도: {type(e).__name__})", file=sys.stderr)
    # 마지막 수단: 새 문서로 페이지 복사 후 저장
    new = fitz.open()
    new.insert_pdf(doc)
    try:
        new.save(str(dst), garbage=3, deflate=True)
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"PDF 저장 실패: {last}")
    finally:
        new.close()
