# -*- coding: utf-8 -*-
"""
qa_engine.py - 최종 검사 / Translation Log / Excel 용어집
=========================================================
- verify_output       : 출력 PDF 열림, 페이지 수·순서·크기, 그림 유지, 한글 출력 검사
- write_log_xlsx      : 원본파일명_Translation_Log.xlsx (번역로그 / 페이지 / 최종검사)
                        '수정 번역(입력)' 열에 고쳐 쓰면 다음 실행 때 자동 반영
- load_overrides      : 위 수정 내용 읽기
- write_glossary_xlsx : Plant_O&M_Glossary.xlsx (적용된 용어 + 미등록 용어 후보)
"""
from __future__ import annotations

import csv
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from engines.glossary_engine import STOP, Glossary, clean_text, optional_path, read_text_auto
from engines.pdf_engine import PAGE_CLASS_KO, Unit

def verify_output(dst: Path, expect_pages: int, translated_pages: set[int],
                  src: Optional[Path] = None, pages: Optional[list[int]] = None) -> tuple[list[str], list]:
    """출력 PDF를 다시 열어 최종 검사. 반환: (경고 목록, [(항목, 결과, 상세)])"""
    warn: list[str] = []
    checks: list = []
    try:
        d = fitz.open(dst)
        checks.append(("PDF 열림", "OK", dst.name))
    except Exception as e:  # noqa: BLE001
        return [f"출력 PDF를 열 수 없음: {e}"], [("PDF 열림", "실패", str(e))]
    ok = len(d) == expect_pages
    checks.append(("페이지 누락", "OK" if ok else "확인필요", f"{len(d)} / {expect_pages}페이지"))
    if not ok:
        warn.append(f"페이지 수 불일치: {len(d)} != {expect_pages}")
    bad_pages = []
    for i in sorted(translated_pages):
        if i >= len(d):
            continue
        t = d[i].get_text("text")
        if not re.search(r"[가-힣]", t) or t.count("\ufffd") > 3:
            bad_pages.append(i + 1)
    checks.append(("한글 출력", "OK" if not bad_pages else "확인필요",
                   f"번역 페이지 {len(translated_pages)}개" + (f", 의심 {bad_pages[:20]}" if bad_pages else "")))
    if bad_pages:
        warn.append(f"한글 추출 불가/깨짐 의심 페이지: {bad_pages[:20]}")
    if src is not None:
        try:
            o = fitz.open(src)
            pl = pages if pages is not None else list(range(len(o)))
            size_bad, img_bad, order_bad = [], [], []
            for i, pno in enumerate(pl):
                if i >= len(d):
                    break
                a, b = o[pno], d[i]
                if abs(a.rect.width - b.rect.width) > 1 or abs(a.rect.height - b.rect.height) > 1:
                    size_bad.append(pno + 1)
                if len(a.get_images(full=True)) != len(b.get_images(full=True)):
                    img_bad.append(pno + 1)
            checks.append(("페이지 순서", "OK" if not order_bad else "확인필요", "원본과 같은 순서로 저장"))
            checks.append(("페이지 크기/방향", "OK" if not size_bad else "확인필요",
                           "원본과 동일 (회전 페이지는 보이는 모습 그대로 정규화)" if not size_bad else f"다름: {size_bad[:20]}"))
            checks.append(("그림/사진 유지", "OK" if not img_bad else "확인필요",
                           "페이지별 이미지 개수 동일" if not img_bad else f"이미지 개수 다름: {img_bad[:20]}"))
            o.close()
        except Exception as e:  # noqa: BLE001
            checks.append(("원본 비교", "확인필요", str(e)))
    d.close()
    return warn, checks


STATUS_KO = {
    "TRANSLATED": "번역", "OVERRIDE": "수정반영", "KEEP": "원문유지(번호/기호)",
    "SCOPE_KEEP": "원문유지(번역범위 밖)", "UNTRANSLATED": "미번역(답변없음)", "FAILED": "실패",
    "UNCHANGED": "원문과 동일", "ROTATED_SKIPPED": "회전문자(건너뜀)", "PENDING": "대기",
}


SOURCE_KO = {"native": "본문", "ocr-page": "OCR(스캔)", "ocr-figure": "OCR(그림)", "toc": "북마크"}


def _xlsx_sheet(wb, title, header, rows, widths, wrap_cols=()):
    from openpyxl.styles import Alignment, Font, PatternFill
    ws = wb.create_sheet(title)
    ws.append(header)
    for r in rows:
        ws.append(r)
    fill = PatternFill("solid", fgColor="1F4E78")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    for col in wrap_cols:
        for c in ws.iter_cols(min_col=col, max_col=col, min_row=2):
            for cell in c:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def write_log_xlsx(path: Path, src_name: str, units: list[Unit], classes: dict, scopes: dict,
                   checks: list, pages: list[int]):
    """Translation_Log.xlsx: 번역로그 / 페이지 / 최종검사. '수정 번역(입력)' 열에 고쳐 쓰고
    이 파일을 '수정 목록(overrides)'으로 지정해 다시 생성하면 반영된다."""
    try:
        import openpyxl
        from openpyxl.styles import PatternFill
    except ImportError:
        with open(path.with_suffix(".csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["페이지", "상태", "원문", "번역", "비고"])
            for u in units:
                w.writerow([u.page + 1, STATUS_KO.get(u.status, u.status), u.text, u.ko or "", "; ".join(u.notes)])
        return
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    rows = []
    for u in units:
        if u.status == "KEEP":
            continue
        kind = SOURCE_KO.get(u.source, u.source) + ("·표" if (u.cell is not None) else "")
        size = f"{u.size:.1f}→{u.used_size:.1f}" if u.used_size else (f"{u.size:.1f}" if u.size else "")
        rows.append([u.page + 1, u.idx + 1 if u.idx >= 0 else "", kind, STATUS_KO.get(u.status, u.status),
                     u.text, u.ko or "", "", size, "; ".join(u.notes) or u.scope_keep])
    ws = _xlsx_sheet(wb, "번역로그", ["페이지", "순번", "구분", "상태", "원문", "번역", "수정 번역(입력)",
                                     "글자크기(pt)", "비고"], rows, [7, 6, 11, 16, 60, 60, 40, 11, 40], (5, 6, 7, 9))
    warn_fill = PatternFill("solid", fgColor="FFF2CC")
    for r in ws.iter_rows(min_row=2):
        st, note = r[3].value or "", r[8].value or ""
        if st.startswith(("미번역", "실패")) or any(k in note for k in ("OVERFLOW", "OVERLAP", "TAG_CHANGED", "LOW_CONF")):
            for c in r:
                c.fill = warn_fill
    prow = []
    for pno in pages:
        ok, why = scopes.get(pno, (True, ""))
        n_tr = sum(1 for u in units if u.page == pno and u.status in ("TRANSLATED", "OVERRIDE") and u.kind == "text")
        prow.append([pno + 1, PAGE_CLASS_KO.get(classes.get(pno, ""), classes.get(pno, "")),
                     "번역" if ok else "원문유지", why, n_tr])
    _xlsx_sheet(wb, "페이지", ["페이지", "PDF 종류", "번역 여부", "사유", "번역 블록 수"], prow, [8, 16, 11, 50, 13])
    cnt = Counter(u.status for u in units)
    stat = [[STATUS_KO.get(k, k), v] for k, v in cnt.most_common()]
    _xlsx_sheet(wb, "최종검사", ["항목", "결과", "상세"], [list(c) for c in checks] + [["", "", ""]] +
                [["상태별 건수", s_, str(v)] for s_, v in stat], [22, 12, 70], (3,))
    try:
        wb.save(path)
    except PermissionError:
        alt = path.with_name(path.stem + time.strftime("_%H%M%S") + path.suffix)
        wb.save(alt)
        print(f"  ! {path.name} 이(가) 열려 있어 {alt.name} 로 저장했습니다.", file=sys.stderr)


def write_glossary_xlsx(path: Path, gloss: Glossary, units: list[Unit], top: int = 300):
    """Plant_O&M_Glossary.xlsx: 문서에 쓰인 용어집 용어 + 자주 나오는 미등록 용어 후보(ko 칸을 채워 재사용)."""
    try:
        import openpyxl
    except ImportError:
        return
    texts = [u.text for u in units if u.status in ("TRANSLATED", "OVERRIDE", "UNTRANSLATED", "FAILED")]
    used = Counter()
    for t in texts:
        for en, ko, note in gloss.find(t):
            used[(en, ko, note)] += 1
    for k in gloss.keep:
        n = sum(1 for t in texts if re.search(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])", t))
        if n:
            used[(k, "KEEP", "영어 유지")] += n
    rows1 = [[en, ko, note, n] for (en, ko, note), n in sorted(used.items(), key=lambda x: -x[1])]
    cnt: Counter = Counter()
    example: dict[str, str] = {}
    known = set(gloss.trans) | {k.lower() for k in gloss.keep}
    for t in texts:
        low = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-/&]+", t)]
        for n in (1, 2, 3):
            for i in range(len(low) - n + 1):
                g = low[i:i + n]
                if g[0] in STOP or g[-1] in STOP or any(len(x) < 3 for x in g):
                    continue
                k = " ".join(g)
                if k in known:
                    continue
                cnt[k] += 1
                example.setdefault(k, t[:90])
    rows2 = [[k, "", "", c, example[k]] for k, c in cnt.most_common(top) if c >= 2]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _xlsx_sheet(wb, "용어집(적용)", ["en", "ko", "note", "출현 수"], rows1, [34, 28, 24, 10])
    _xlsx_sheet(wb, "용어 후보(미등록)", ["en", "ko", "note", "출현 수", "예문"], rows2, [30, 24, 16, 10, 70], (5,))
    try:
        wb.save(path)
    except PermissionError:
        print(f"  ! {path.name} 이(가) 열려 있어 저장하지 못했습니다.", file=sys.stderr)


def load_overrides(path: Optional[str]) -> dict[str, str]:
    """수정 목록: CSV(원문,번역) 또는 Translation_Log.xlsx ('수정 번역(입력)' 열에 쓴 행만 반영)."""
    path = optional_path(path, "overrides")
    if not path:
        return {}
    d = {}
    if Path(path).suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            head = [str(c or "").strip() for c in rows[0]]
            if "원문" not in head:
                continue
            ci = head.index("원문")
            fix = head.index("수정 번역(입력)") if "수정 번역(입력)" in head else None
            if fix is None:
                continue
            for r in rows[1:]:
                if r and len(r) > max(ci, fix) and r[ci] and r[fix] and str(r[fix]).strip():
                    d[clean_text(str(r[ci]))] = str(r[fix]).strip()
        return d
    for row in csv.reader(read_text_auto(Path(path)).splitlines()):
        if len(row) >= 2 and not row[0].startswith("#"):
            d[clean_text(row[0])] = row[1].strip()
    return d
