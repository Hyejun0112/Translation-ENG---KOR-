# -*- coding: utf-8 -*-
"""
table_engine.py - 표 인식 / 셀 단위 번역 단위 구성
=================================================
- find_table_cells : 괘선 표(2x2 이상)의 셀 경계 (PyMuPDF find_tables)
- cell_merge       : 셀 안의 여러 줄을 한 번역 단위로 묶기
- column_merge     : 괘선 없는 표에서 같은 열의 이어지는 줄 묶기
- mark_keep_columns: 머리글이 Tag/Model/Parameter/Code/Value/Unit 등인 열은 원문 유지
"""
from __future__ import annotations

import re

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from engines.glossary_engine import BULLET_RE

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 타입 표시 전용 (순환 import 방지)
    from engines.pdf_engine import Unit

def is_tabular(lines: list[dict]) -> bool:
    """같은 행(세로 겹침)에 가로로 떨어진 줄이 있으면 표/다단 블록으로 판단."""
    for i in range(len(lines)):
        a = lines[i]["bbox"]
        ha = a[3] - a[1]
        for j in range(i + 1, len(lines)):
            b = lines[j]["bbox"]
            ov = min(a[3], b[3]) - max(a[1], b[1])
            if ov < 0.5 * min(ha, b[3] - b[1]):
                continue
            gap = max(a[0], b[0]) - min(a[2], b[2])
            size = lines[i]["spans"][0]["size"]
            if gap > max(6.0, 0.8 * max(size, 1.0)):
                return True
    return False


def column_merge(segs: list[dict]) -> list[list[dict]]:
    """괘선 없는 표 블록: 같은 열에서 바로 아래 이어지는 줄만 하나의 셀(문단)로 합친다."""
    segs = sorted(segs, key=lambda s: (s["bbox"][1], s["bbox"][0]))
    groups: list[list[dict]] = []
    for s in segs:
        placed = False
        for g in reversed(groups):
            last = g[-1]
            lb, sb = last["bbox"], s["bbox"]
            same_col = (abs(lb[0] - sb[0]) < 2.0 or abs(lb[2] - sb[2]) < 2.0
                        or abs((lb[0] + lb[2]) / 2 - (sb[0] + sb[2]) / 2) < 1.5)
            gap = sb[1] - lb[3]
            size = last["spans"][0]["size"]
            if same_col and -1.0 <= gap < 0.5 * size and not BULLET_RE.match(s["_text"]):
                g.append(s)
                placed = True
                break
        if not placed:
            groups.append([s])
    return groups


def cell_merge(segs: list[dict]) -> list[list[dict]]:
    """표 셀 안의 줄: 글머리표가 아니면 위에서 아래로 이어 붙인다(같은 행은 같은 그룹)."""
    segs = sorted(segs, key=lambda s: (round(s["bbox"][1], 1), s["bbox"][0]))
    groups: list[list[dict]] = []
    for s in segs:
        if groups and not BULLET_RE.match(s["_text"]):
            last = groups[-1][-1]
            size = last["spans"][0]["size"]
            gap = s["bbox"][1] - last["bbox"][3]
            if gap < 1.2 * size:
                groups[-1].append(s)
                continue
        groups.append([s])
    return groups


def find_table_cells(page) -> list[tuple]:
    """괘선 표(2x2 이상)의 셀 목록 [(Rect, table_id, row, col)] (PyMuPDF find_tables)."""
    if not hasattr(page, "find_tables"):
        return []
    try:
        tf = page.find_tables()
    except Exception:  # noqa: BLE001
        return []
    cells = []
    for ti, t in enumerate(getattr(tf, "tables", [])):
        if t.row_count < 2 or t.col_count < 2:
            continue
        try:
            rows = t.rows
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            for ri, row in enumerate(rows):
                for ci, c in enumerate(row.cells):
                    if c:
                        r = fitz.Rect(c)
                        if r.width > 3 and r.height > 3:
                            cells.append((r, ti, ri, ci))
        else:
            for c in t.cells:
                if c:
                    r = fitz.Rect(c)
                    if r.width > 3 and r.height > 3:
                        cells.append((r, ti, -1, -1))
    return cells


# 표에서 원문 유지할 열 (머리글 기준): Tag / Model / Parameter / Code / Value / Unit 등
KEEP_COL_RE = re.compile(
    r"^(?:tag(?:\s*no\.?|\s*number)?|item\s*tag|model(?:\s*no\.?)?|parameter(?:\s*name)?|"
    r"(?:alarm\s*|error\s*|fault\s*)?code|value|values|unit|units|signal(?:\s*name)?|"
    r"part\s*(?:no\.?|number)|drawing\s*no\.?|doc(?:ument)?\s*no\.?|serial\s*no\.?|loop\s*no\.?)$",
    re.I)


def mark_keep_columns(units: list[Unit]):
    """표 머리글(첫 행)이 Tag/Value/Unit 등이면 그 열의 본문 셀은 원문 유지."""
    headers: dict[tuple, str] = {}
    for u in units:
        if u.tkey and u.tkey[1] == 0:
            headers[(u.tkey[0], u.tkey[2])] = re.sub(r"[:\s]+$", "", u.text.strip())
    keep_cols = {k for k, t in headers.items() if KEEP_COL_RE.match(t)}
    for u in units:
        if u.tkey and u.tkey[1] > 0 and (u.tkey[0], u.tkey[2]) in keep_cols:
            u.scope_keep = f"표 '{headers[(u.tkey[0], u.tkey[2])]}' 열 원문유지"
