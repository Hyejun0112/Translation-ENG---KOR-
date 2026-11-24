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
표지·문서 송부 양식·문서 목록 페이지는 제목에 위 키워드가 있어도 번역한다 (cover_page_reason).
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
    r"circuit\s+diagram", r"basic\s+diagram", r"connection\s+diagram", r"schematic\s+diagram",
    r"terminal\s+(?:connection\s+)?diagram", r"block\s+diagram",
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
    prose = [u for u in body if len(u.text.split()) >= 8 and not u.rotated]
    # 표 칸이 대부분인 페이지(데이터시트)는 표 밖의 몇 문장만으로 '문장 위주'라고 보지 않는다
    return len(prose) >= 3 and len(prose) / len(body) >= 0.3 and len(prose) >= 0.1 * len(units)


TOC_LINE_RE = re.compile(r"(?:[.·…]\s*){4,}\s*\d*\s*$")


def is_toc_page(units: list[Unit]) -> bool:
    """점선(......) 줄이 여러 개 = 목차 페이지 (목차 안의 'DATA', 'DRAWING' 같은 제목으로 제외하지 않도록)."""
    n = sum(1 for u in units if TOC_LINE_RE.search(u.text))
    return n >= 4 or (units and n >= 0.3 * len(units) and n >= 2)


# 표지 / 문서 송부 양식 / 문서 목록에 나오는 항목 이름 (데이터시트 양식에는 잘 나오지 않는 것들)
COVER_MARKERS = [re.compile(p, re.I) for p in (
    r"job\s*title", r"job\s*no", r"p\.?\s*o\.?\s*no", r"doc(?:ument)?\.?\s*no", r"item\s*no", r"\bvendor\b", r"\bowner\b",
    r"document\s+(?:code|type|status|title)", r"customer\s+code", r"\bsubject\s*:", r"\bproject\s*:", r"\bcustomer\s*:",
    r"revision\s+remarks", r"checked\s+by", r"approved\s+by", r"vendor'?s\s+document", r"transmittal",
    r"approved\s+as\s+noted|returned\s+for\s+correction", r"contractor\s+job\s+no",
)]
INDEX_PAGE_RE = re.compile(
    r"table\s+of\s+contents|document\s+list|list\s+of\s+documents|description\s+of\s+document|index\s+of\s+documents", re.I)


def cover_page_reason(page, units: list[Unit]) -> str:
    """표지·문서 송부 양식·문서 목록 페이지이면 사유를, 아니면 빈 문자열.
    문서 제목에 'Data sheets', 'Catalogue', 'P&ID' 같은 말이 들어 있어도 표지는 번역한다.
    - 문서 앞쪽 3쪽: 표지 항목(JOB TITLE, DOC NO., VENDOR, Subject:, Checked by …)이 3가지 이상
    - 그 뒤쪽(합본 안의 하위 문서 표지): 5가지 이상이고 글이 많지 않은 페이지 (칸이 빽빽한 데이터시트 양식과 구분)
    - 문서 목록: 'Table of Contents' / 'Document List' 가 있고 글이 많지 않은 페이지"""
    if not units:
        return ""
    text = " | ".join(u.text for u in units)
    n = sum(1 for rx in COVER_MARKERS if rx.search(text))
    front = getattr(page, "number", 99) < 3
    if (front and n >= 3 and len(units) <= 120) or (n >= 5 and len(units) <= 90):
        return "표지/문서 송부 양식 페이지"
    if len(units) <= 40 and any(len(u.text) <= 60 and INDEX_PAGE_RE.search(u.text) for u in units):
        return "문서 목록 페이지"
    return ""


# 도면 표제란의 항목 이름 ('Circuit Diagram Number 100B0010'): 회로도 쪽은 어디에 있든 이 글이 있다
DIAGRAM_NO_RE = re.compile(r"circuit\s+diagram\s+(?:number|no\b)", re.I)

KEPT_TITLE_REASONS = ("제외 문서 제목", "데이터시트/양식", "도면")


def smooth_scopes(pages: list[int], scopes: dict, units_by_page: dict) -> None:
    """같은 문서(데이터시트·회로도)의 쪽인데 제목이 없어서 혼자 번역 대상이 된 쪽을 바로잡는다.
    바로 앞 쪽과 바로 뒤 쪽이 모두 '제목 때문에' 원문 유지이고, 이 쪽은 문장 위주도 목차도 아니면 원문 유지.
    (예: 제작사 데이터시트 6쪽 중 가운데 한 쪽만 제목 머리글이 없어 번역되던 문제)"""
    before = dict(scopes)
    have = set(pages)
    for p in pages:
        if (p - 1) not in have or (p + 1) not in have:
            continue
        ok, why = before.get(p, (True, ""))
        if not ok or why:
            continue
        (oa, wa), (ob, wb) = before.get(p - 1, (True, "")), before.get(p + 1, (True, ""))
        if oa or ob or not (wa.startswith(KEPT_TITLE_REASONS) and wb.startswith(KEPT_TITLE_REASONS)):
            continue
        us = [u for u in units_by_page.get(p, []) if not (u.scope_keep or "").startswith("글머리 기호")]
        if not us or is_prose_page(us) or is_toc_page(us):
            continue
        scopes[p] = (False, "앞뒤 쪽이 모두 원문 유지(데이터시트/도면) → 같은 문서의 쪽으로 보고 원문 유지")


def page_scope(page, units: list[Unit], drawings_count: int) -> tuple[bool, str]:
    """(번역 여부, 사유)"""
    # 떼어 낸 글머리 기호/번호(원문 유지 문구로만 남긴 것)는 쪽 판단에 넣지 않는다 (짧은 라벨로 세면 도면으로 오판)
    units = [u for u in units if not (u.scope_keep or "").startswith("글머리 기호")]
    area = abs(page.rect)
    if area > A4_AREA * 1.6:
        return False, "대형 페이지(A3 이상) → 도면으로 판단"
    if is_toc_page(units):
        return True, "목차 페이지"
    why = cover_page_reason(page, units)
    if why:
        return True, why
    title = REFERENCE_RE.sub(" ", page_title_text(page, units))
    m = EXCLUDE_TITLE_RE.search(title)
    if m and not re.search(r"contents|목차", title, re.I):
        if is_prose_page(units):
            return True, f"제목에 '{m.group(0)}'가 있으나 문장 위주 페이지라 번역"
        return False, f"제외 문서 제목 '{m.group(0)}'"
    if units and not is_prose_page(units):
        # 양식형 데이터시트: 글이 거의 다 표 칸 안에 있고, 제목 칸(페이지 아래 표제란 포함)에 제외 키워드가 있음
        in_cells = sum(1 for u in units if u.cell is not None)
        # 표 밖에 설명 문장(8낱말 이상)이 있으면 매뉴얼 쪽의 작은 사양표다 ('Technical data' 표 + Note 문단) → 번역
        prose_out = any(u.cell is None and len(re.findall(r"[A-Za-z]{2,}", u.text)) >= 8 for u in units)
        if len(units) >= 30 and in_cells >= 0.7 * len(units):
            sizes = sorted(u.size for u in units if u.size)
            med = sizes[len(sizes) // 2] if sizes else 10
            for u in units:
                if len(u.text) <= 60:
                    m = EXCLUDE_TITLE_RE.search(REFERENCE_RE.sub(" ", u.text))
                    # 표제처럼 보이는 글만 (대문자/굵게/큰 글자). 표의 한 행 이름('Wiring Diagram | 도면번호')은 제외
                    caps = bool(m) and m.group(0).isupper()  # 'INSTRUMENT SPECIFICATION' (뒤에 부제가 붙어도 인정)
                    if m and (caps or u.bold or u.size >= med * 1.12) and len(m.group(0)) >= 0.35 * len(u.text.strip()):
                        # 매뉴얼 쪽 안의 작은 사양표('Technical data' 표 + 설명 Note 문단)는 번역한다.
                        # 인증서·부품 목록·데이터시트 양식은 설명문이 있어도 원문 유지
                        if prose_out and re.fullmatch(r"(?i)technical\s+data|specifications?|technical\s+specifications?",
                                                      m.group(0).strip()):
                            continue
                        return False, f"데이터시트/양식 페이지 (표제 '{m.group(0)}')"
        # 표제가 글자로 남아 있지 않은 양식(제목이 그림/윤곽선): 표 칸 안의 항목 이름이 거의 다 대문자 낱말이면
        # 데이터시트 양식으로 본다 (예: 전동기 DATA SHEET 의 'ENCLOSURE', 'COOLING METHOD' …)
        if len(units) >= 30 and in_cells >= 0.7 * len(units):
            labels = [u.text for u in units if re.search(r"[A-Za-z]{3,}", u.text) and not re.search(r"\d", u.text)]
            if len(labels) >= 25 and sum(1 for t in labels if t.isupper()) >= 0.85 * len(labels):
                return False, "데이터시트/양식 페이지 (표 칸의 대문자 항목 위주)"
        for u in units:
            if len(u.text) <= 40 and DIAGRAM_NO_RE.search(u.text):
                return False, "회로도/도면 페이지 (표제란 'Circuit Diagram Number')"
        # 도면/데이터시트의 표제란: 페이지 어디에 있든, 짧은 글이 거의 제외 키워드로만 이루어져 있으면 제목으로 본다
        # (예: 오른쪽 아래 표제란의 'DIMENSIONAL DRAWING FOR …'). 그림 설명문('Figure 3: Wiring diagram for …')은 제외
        for u in units:
            if len(u.text) <= 60 and not re.match(r"\s*(?:fig(?:ure)?\.?|table|see|refer)\b", u.text, re.I):
                t = REFERENCE_RE.sub(" ", u.text)
                m = EXCLUDE_TITLE_RE.search(t)
                # 표제란 글은 보통 대문자. 표의 한 행 이름('Wiring Diagram | 도면번호')은 제목이 아니다
                if m and m.group(0).isupper() and len(m.group(0)) >= 0.35 * len(u.text.strip()) and re.search(
                        r"drawing|diagram|arrangement|P\s*&\s*I\s*D|PFD|hook", m.group(0), re.I):
                    return False, f"도면 페이지 (표제 '{m.group(0)}')"
        if drawings_count > 1500:
            avg = sum(len(u.text) for u in units) / len(units)
            short = sum(1 for u in units if len(u.text.split()) <= 4)
            # 영어 문장(8단어 이상)이 3개 이상이면 그림이 많은 설명 페이지 → 번역
            prose = sum(1 for u in units if len(re.findall(r"[A-Za-z]{2,}", u.text)) >= 8)
            if avg < 14 or (short >= 0.5 * len(units) and prose < 3):
                return False, "도면/다이어그램으로 판단(선 많고 짧은 라벨 위주)"
    return True, ""
