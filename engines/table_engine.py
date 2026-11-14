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


def _hrule_between(lb, sb, hedges) -> bool:
    lo, hi = min(lb[3], sb[1]) - 1.0, max(lb[3], sb[1]) + 1.0
    x0, x1 = min(lb[0], sb[0]), max(lb[2], sb[2])
    return any(lo <= y <= hi and xa < x1 and xb > x0 for y, xa, xb in hedges or [])


# ----------------------------------------------------------------------------
# 줄 이어 붙이기 규칙 (본문 문단 / 괘선 없는 표 / 표 셀 공통)
# ----------------------------------------------------------------------------
TERMINAL_RE = re.compile(r"[.:;!?]['\")\]]?\s*$")
CONT_WORDS = {"of", "and", "or", "to", "in", "on", "for", "with", "by", "from", "at", "as", "the", "than",
              "that", "which", "nor", "but", "under", "over", "into", "per"}


def _starts_like_continuation(text: str) -> bool:
    """앞 줄에서 이어지는 문장처럼 시작하는가 (소문자 시작, 또는 대문자 전용 문장의 OR/AND/OF …)"""
    m = re.match(r"[\"'“‘(\[]*([A-Za-z][A-Za-z'\-]*)", text)
    if not m:
        return False
    w = m.group(1)
    return w[0].islower() or (len(w) > 1 and w.isupper() and w.lower() in CONT_WORDS)


def same_row(a: dict, b: dict) -> bool:
    return abs(a["_oy"] - b["_oy"]) < 0.35 * max(a["_size"], b["_size"], 1.0)


def continues(a: dict, b: dict, hedges=None) -> bool:
    """b(아래 줄)가 a(윗줄)에서 이어지는 같은 문단/같은 칸의 글인가.
    아래 중 하나라도 해당하면 '새 항목'으로 본다 (잘못 합쳐지면 번역이 섞이고 자리가 틀어진다):
      - b가 글머리표/번호로 시작
      - 글자 크기가 다름, 한쪽만 밑줄(밑줄 제목)
      - 줄 간격이 그 문단의 줄 간격보다 넓음 (문단 사이 간격)
      - 사이에 가로 괘선이 있음 (표의 행 경계)
      - b의 첫 단어가 a의 줄 끝 빈자리에 들어갈 수 있었음 (= a에서 줄을 일부러 바꾼 것: 목록/표의 행)
    단, a의 문장이 끝나지 않았고 b가 소문자로 시작하면(이어지는 문장) 줄 간격이 조금 넓거나
    빈자리가 조금 남았어도 이어진 것으로 본다."""
    size = max(a["_size"], 1.0)
    if b.get("_marker") or BULLET_RE.match(b["_text"]):
        return False
    if abs(a["_size"] - b["_size"]) > 0.6:
        return False
    if (a.get("_ul", 0.0) >= 0.85) != (b.get("_ul", 0.0) >= 0.85):
        return False
    pitch = b["_oy"] - a["_oy"]
    if pitch < 0.5 * size:
        return False
    left, limit = a.get("_left", a["bbox"][0]), a.get("_limit", a["bbox"][2])
    width = a["bbox"][2] - a["bbox"][0]
    avail = (limit - left) - width
    filled = width >= 0.6 * max(limit - left, 1.0)
    soft = strong_continuation(a, b)
    if pitch > a.get("_pitch", 1.35 * size) + max(1.2, 0.15 * size):
        # 줄 간격이 넓은 문단(1.5줄 간격)만 예외. 옆에 다른 열이 있는 줄(표의 행)은 예외 없음
        if not (soft and filled and pitch <= 2.0 * size and not a.get("_rowmate") and not b.get("_rowmate")):
            return False
    if _hrule_between(a["bbox"], b["bbox"], hedges):
        return False
    fw = b.get("_fw", 0.0)
    if fw * 1.15 + 0.1 * size <= avail:
        if not (soft and (filled or avail < 2.0 * fw)):
            return False
    return True


def strong_continuation(a: dict, b: dict) -> bool:
    """글 내용으로 볼 때 이어지는 문장인가 (앞 줄이 문장 끝이 아니고, 다음 줄이 소문자로 시작)"""
    return not TERMINAL_RE.search(a["_text"]) and _starts_like_continuation(b["_text"])


def _centered(a: dict, b: dict) -> bool:
    ab, bb = a["bbox"], b["bbox"]
    return abs((ab[0] + ab[2]) / 2 - (bb[0] + bb[2]) / 2) < 1.5 and abs(ab[0] - bb[0]) > 2.0


def x_ok(group: list[dict], b: dict) -> bool:
    """문단의 다음 줄로 볼 수 있는 왼쪽 시작 위치인가 (같은 시작점 / 내어쓰기 / 첫 줄 들여쓰기 / 가운데 정렬)"""
    a = group[-1]
    size = max(a["_size"], 1.0)
    dx = b["bbox"][0] - a["bbox"][0]
    if abs(dx) <= 0.4 * size or _centered(a, b):
        return True
    if len(group) >= 2:
        return False
    if dx > 0:
        return dx <= 4.0 * size  # 글머리표/번호/'NOTE:' 뒤 내어쓰기
    first_bullet = bool(group[0].get("_marker") or BULLET_RE.match(group[0]["_text"]))
    return (not first_bullet) and -dx <= 3.0 * size  # 첫 줄 들여쓰기 문단


def line_merge(segs: list[dict], hedges: list | None = None) -> list[list[dict]]:
    """본문 블록: 위에서 아래로 읽으면서 이어지는 줄만 한 문단으로 묶는다."""
    groups: list[list[dict]] = []
    for s in segs:
        if groups:
            last = groups[-1][-1]
            if same_row(last, s) and s["bbox"][0] >= last["bbox"][0] and not s.get("_marker"):
                groups[-1].append(s)  # 같은 줄의 이어지는 조각
                continue
            if x_ok(groups[-1], s) and continues(last, s, hedges):
                groups[-1].append(s)
                continue
        groups.append([s])
    return groups


def _same_col(lb, sb) -> bool:
    return (abs(lb[0] - sb[0]) < 2.0 or abs(lb[2] - sb[2]) < 2.0
            or abs((lb[0] + lb[2]) / 2 - (sb[0] + sb[2]) / 2) < 1.5)


def column_merge(segs: list[dict], hedges: list | None = None) -> list[list[dict]]:
    """괘선 없는 표(세로선 없이 열이 나뉜 칸 포함): '행' 단위로 판단한다.
    - 맨 왼쪽 열(표의 첫 열)이 새 항목이면 → 행 전체가 새 행 (기호 | 설명 목록이 한 문단으로 합쳐지지 않게)
    - 행의 모든 칸이 바로 윗행 같은 열의 글에서 이어지면 → 윗행의 줄바꿈
    - 그 밖에는 열마다 따로: 글 내용이 이어지는 문장(소문자 시작)일 때만 윗줄에 붙인다
      (애매하면 나눈다 - 잘못 합치면 아래 행의 글이 위 행으로 올라가 표가 틀어진다)"""
    segs = sorted(segs, key=lambda s: (s["_oy"], s["bbox"][0]))
    rows: list[list[dict]] = []
    for s in segs:
        if rows and same_row(rows[-1][0], s):
            rows[-1].append(s)
        else:
            rows.append([s])
    groups: list[list[dict]] = []
    prev: list[list[dict]] = []
    first_x = min((s["bbox"][0] for s in segs), default=0.0)
    for row in rows:
        row.sort(key=lambda s: s["bbox"][0])
        match = []
        for s in row:
            hit = None
            for g in prev:
                if any(g is m for m in match):
                    continue
                if (_same_col(g[-1]["bbox"], s["bbox"]) or (len(row) == 1 and x_ok(g, s))) \
                        and continues(g[-1], s, hedges):
                    hit = g
                    break
            match.append(hit)
        # 표의 첫 열이 새 항목으로 시작하면(이어지는 문장이 아니면) 그 행은 통째로 새 행
        lead_new = (len(row) > 1 and row[0]["bbox"][0] <= first_x + 3.0
                    and not (match[0] is not None and strong_continuation(match[0][-1], row[0])))
        if lead_new:
            new = [[s] for s in row]
            groups += new
            prev = new
            continue
        if all(m is not None for m in match):
            for s, g in zip(row, match):
                g.append(s)
            prev = match
            continue
        cur = []
        for s, g in zip(row, match):
            if g is not None and strong_continuation(g[-1], s):
                g.append(s)
                cur.append(g)
            else:
                groups.append([s])
                cur.append(groups[-1])
        prev = cur
    return groups


def cell_merge(segs: list[dict], hedges: list | None = None) -> list[list[dict]]:
    """표 셀 안의 줄: 같은 행의 조각은 한 그룹, 아래 줄은 이어지는 문장일 때만 붙인다
    (행 구분선이 없는 표에서 'Less than 30 mm / Less than 20 mm …' 가 한 문장으로 합쳐지지 않게).
    셀 안에 세로선 없이 여러 열이 있으면(같은 줄에 멀리 떨어진 글) 열별로 나눈다."""
    if is_tabular(segs):
        return column_merge(segs, hedges)
    segs = sorted(segs, key=lambda s: (round(s["_oy"], 1), s["bbox"][0]))
    groups: list[list[dict]] = []
    for s in segs:
        if groups:
            last = groups[-1][-1]
            if same_row(last, s) or continues(last, s, None):
                groups[-1].append(s)
                continue
        groups.append([s])
    return groups


def merge_unruled_cells(cells: list[tuple], hedges: list) -> list[tuple]:
    """표 인식이 만든 행 경계 중 실제로 그려진 가로선이 없는 것은 없앤다 (위아래 칸을 하나로).
    선 없이 여백으로만 행을 나눈 표에서 한 문장이 두 칸으로 갈라지는 것을 막는다."""
    out = [[fitz.Rect(c[0]), c[1], c[2], c[3], 1] for c in cells]
    out.sort(key=lambda c: (c[1], round(c[0].x0), c[0].y0))
    merged: list[list] = []
    for c in out:
        if merged:
            p = merged[-1]
            pr, r = p[0], c[0]
            if (p[1] == c[1] and abs(pr.x0 - r.x0) < 1.5 and abs(pr.x1 - r.x1) < 1.5 and abs(pr.y1 - r.y0) < 1.5):
                ruled = any(abs(y - r.y0) < 2.0 and min(xb, r.x1) - max(xa, r.x0) > 0.5 * r.width
                            for y, xa, xb in hedges or [])
                if not ruled:
                    p[0] = pr | r
                    p[4] += 1  # 합쳐진 칸 수 (2 이상 = 가로선 없이 여러 행이 들어 있는 칸)
                    continue
        merged.append(c)
    return [tuple(c) for c in merged]


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
