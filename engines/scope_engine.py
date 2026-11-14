# -*- coding: utf-8 -*-
"""
scope_engine.py - 번역 범위 판단 (Drawing / Datasheet 제외)
==========================================================
O&M 대상(운전/정비/점검/절차/고장진단/안전 등)만 번역하고 아래는 페이지 전체 원문 유지:
  Datasheet, Technical/Performance/Design Data, Equipment/Valve/Line/Instrument/Cable/I/O List,
  (Spare) Parts List, Bill of Material, Catalog, Brochure, Ordering Information,
  Material/Fuel Specification, Certificate, Inspection/Test Report, Packing/Shipping List,
  P&ID, PFD, Logic/Wiring/Single Line/Loop Diagram, GA/Arrangement/Dimensional Drawing,
  Hook-up, DCS/PLC/HMI Screen, A3 이상 대형 도면 페이지, 선이 많고 짧은 라벨 위주의 도면 페이지.
본문 속 'P&ID No. 0061' 같은 참조 표현은 판단에서 제외하고, 문장 위주 페이지는 번역한다.
추가 키워드: data/scope_keywords.txt (한 줄에 하나, 정규식 가능)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import config
from engines.glossary_engine import read_text_auto

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 타입 표시 전용 (순환 import 방지)
    from engines.pdf_engine import Unit

EXCLUDE_TITLE_KW = [
    r"data\s*sheets?", r"instrument\s+specification", r"specification\s+sheet", r"technical\s+data", r"performance\s+data", r"design\s+data",
    r"equipment\s+list", r"valve\s+list", r"line\s+list", r"instrument\s+list", r"cable\s+list",
    r"i\s*/\s*o\s+list", r"(?:spare\s+)?parts?\s+list", r"bill\s+of\s+materials?", r"\bBOM\b",
    r"catalogu?e", r"brochure", r"ordering\s+information", r"material\s+specification",
    r"fuel\s+specification", r"certificates?", r"inspection\s+report", r"test\s+report",
    r"packing\s+list", r"shipping\s+list", r"P\s*&\s*I\s*D", r"piping\s+(?:and|&)\s+instrument(?:ation)?\s+diagram",
    r"process\s+flow\s+diagram", r"\bPFD\b", r"logic\s+diagram", r"wiring\s+diagram",
    r"single\s+line\s+diagram", r"loop\s+diagram", r"general\s+arrangement", r"\bGA\s+drawing",
    r"arrangement\s+drawing", r"dimension(?:al)?\s+drawing", r"hook[\s-]*up", r"DCS\s+screen",
    r"PLC\s+screen", r"HMI\s+screen",
]


EXCLUDE_TITLE_RE = re.compile("|".join(f"(?:{k})" for k in EXCLUDE_TITLE_KW), re.I)


A4_AREA = 595 * 842


def load_scope_keywords(path: Optional[str] = None):
    """scope_keywords.txt(한 줄에 하나, 정규식 가능)가 있으면 제외 키워드에 추가."""
    global EXCLUDE_TITLE_RE
    p = Path(path) if path else config.SCOPE_KEYWORDS_FILE
    if not p.exists():
        return
    extra = [ln.strip() for ln in read_text_auto(p).splitlines() if ln.strip() and not ln.startswith("#")]
    if extra:
        EXCLUDE_TITLE_RE = re.compile("|".join(f"(?:{k})" for k in EXCLUDE_TITLE_KW + extra), re.I)


def page_title_text(page, units: list[Unit]) -> str:
    """페이지 제목 후보: 위쪽 35% 영역의 '제목처럼 보이는' 글자(본문보다 크거나 굵고 짧은 줄)만.
    제목이 없으면 빈 문자열 (본문 첫 줄을 제목으로 쓰지 않는다)."""
    if not units:
        return ""
    sizes = sorted(u.size for u in units if u.size)
    med = sizes[len(sizes) // 2] if sizes else 10
    top = page.rect.y0 + page.rect.height * 0.35
    cands = [u for u in units
             if u.rect.y0 < top and (u.size >= med * 1.12 or u.bold) and len(u.text) <= 90
             and not re.match(r"^\s*[•●▪■◦○‣·*\-–—]\s", u.text) and u.cell is None]
    return " | ".join(u.text for u in cands)


# "P&ID No. 0061", "Drawing No. xxx", "refer to Datasheet ..." 같은 참조 표현은 제목 판단에서 제외
REFERENCE_RE = re.compile(
    r"(?:P\s*&\s*I\s*D|PFD|drawing|document|doc\.?|data\s*sheet|datasheet|diagram)\s*(?:no\.?|number|nr\.?|#)\s*[:\w./-]*"
    r"|(?:refer(?:red)?\s+to|see|according\s+to|as\s+per|per)\s+(?:the\s+)?[^|]{0,60}",
    re.I)


def is_prose_page(units: list[Unit]) -> bool:
    """문장(8단어 이상) 위주의 페이지 = O&M 설명/절차 페이지."""
    body = [u for u in units if u.kind == "text" and u.cell is None]
    if len(body) < 3:
        return False
    prose = [u for u in body if len(u.text.split()) >= 8]
    return len(prose) >= 3 and len(prose) / len(body) >= 0.3


TOC_LINE_RE = re.compile(r"(?:[.·…]\s*){4,}\s*\d*\s*$")


def is_toc_page(units: list[Unit]) -> bool:
    """점선(......) 줄이 여러 개 = 목차 페이지 (목차 안의 'DATA', 'DRAWING' 같은 제목으로 제외하지 않도록)."""
    n = sum(1 for u in units if TOC_LINE_RE.search(u.text))
    return n >= 4 or (units and n >= 0.3 * len(units) and n >= 2)


def page_scope(page, units: list[Unit], drawings_count: int) -> tuple[bool, str]:
    """(번역 여부, 사유)"""
    area = abs(page.rect)
    if area > A4_AREA * 1.6:
        return False, "대형 페이지(A3 이상) → 도면으로 판단"
    if is_toc_page(units):
        return True, "목차 페이지"
    title = REFERENCE_RE.sub(" ", page_title_text(page, units))
    m = EXCLUDE_TITLE_RE.search(title)
    if m and not re.search(r"contents|목차", title, re.I):
        if is_prose_page(units):
            return True, f"제목에 '{m.group(0)}'가 있으나 문장 위주 페이지라 번역"
        return False, f"제외 문서 제목 '{m.group(0)}'"
    if units and not is_prose_page(units):
        # 양식형 데이터시트: 글이 거의 다 표 칸 안에 있고, 제목 칸(페이지 아래 표제란 포함)에 제외 키워드가 있음
        in_cells = sum(1 for u in units if u.cell is not None)
        if len(units) >= 30 and in_cells >= 0.7 * len(units):
            for u in units:
                if len(u.text) <= 60:
                    m = EXCLUDE_TITLE_RE.search(REFERENCE_RE.sub(" ", u.text))
                    if m:
                        return False, f"데이터시트/양식 페이지 (표제 '{m.group(0)}')"
        if drawings_count > 1500:
            avg = sum(len(u.text) for u in units) / len(units)
            short = sum(1 for u in units if len(u.text.split()) <= 4)
            # 영어 문장(8단어 이상)이 3개 이상이면 그림이 많은 설명 페이지 → 번역
            prose = sum(1 for u in units if len(re.findall(r"[A-Za-z]{2,}", u.text)) >= 8)
            if avg < 14 or (short >= 0.5 * len(units) and prose < 3):
                return False, "도면/다이어그램으로 판단(선 많고 짧은 라벨 위주)"
    return True, ""
