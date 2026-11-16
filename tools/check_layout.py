#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_layout.py - 원본 PDF와 번역본 PDF를 페이지별로 자동 대조 (API 사용 안 함)
=============================================================================
사용:  python tools\\check_layout.py 원본.pdf 번역본.pdf [--png 폴더]

페이지마다 아래를 검사해 이상이 있는 페이지만 출력합니다.
  - 페이지 크기                     : 원본과 다르면 표시
  - 그림/선(표 괘선, 도면, 로고)    : 글자를 뺀 화면을 서로 비교 (밑줄을 다시 그린 쪽은 조금 달라질 수 있음)
  - 주석(Markup·스탬프)/링크 개수   : 원본과 다르면 표시
  - 깨진 글자                       : 번역본에만 있는 � / 사용자 정의 영역 글자
  - 글자 겹침                       : 원본보다 겹친 줄이 늘었으면 표시
  - 번역 누락                       : 영어 단어가 절반 넘게 그대로 남은 페이지
--png 폴더 를 주면 이상이 있는 페이지의 원본/번역본을 나란히 붙인 그림을 저장합니다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore


def _no_text_pixels(doc, pno: int, dpi: int = 60) -> bytes:
    """글자를 모두 뺀 페이지 화면 (그림·선·배경만)"""
    d = fitz.open()
    d.insert_pdf(doc, from_page=pno, to_page=pno, annots=False, links=False)
    p = d[0]
    p.add_redact_annot(p.rect, fill=False)
    p.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=0)
    pm = p.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return pm.samples, pm.width, pm.height


def _lines(page) -> list:
    out = []
    for bl in page.get_text("dict")["blocks"]:
        if bl["type"]:
            continue
        for ln in bl["lines"]:
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if not t or abs(ln["dir"][0]) < 0.99:
                continue
            sz = max(s["size"] for s in ln["spans"])
            oy = ln["spans"][0]["origin"][1]
            out.append((fitz.Rect(ln["bbox"][0], oy - 0.7 * sz, ln["bbox"][2], oy + 0.1 * sz), t))
    return out


def _overlaps(ls: list) -> int:
    n = 0
    for i in range(len(ls)):
        for j in range(i + 1, len(ls)):
            r = ls[i][0] & ls[j][0]
            if not r.is_empty and r.width > 2 and r.height > 1.2:
                n += 1
    return n


def _bad_chars(t: str) -> int:
    return sum(1 for c in t if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)


def compare(src: str, dst: str, png_dir: str = "") -> int:
    a, b = fitz.open(src), fitz.open(dst)
    if len(a) != len(b):
        print(f"! 페이지 수가 다릅니다: 원본 {len(a)} / 번역본 {len(b)}")
    flagged = 0
    for i in range(min(len(a), len(b))):
        pa, pb = a[i], b[i]
        msg = []
        if abs(pa.rect.width - pb.rect.width) > 1 or abs(pa.rect.height - pb.rect.height) > 1:
            msg.append("페이지 크기 다름")
        try:
            (xa, w, h), (xb, w2, h2) = _no_text_pixels(a, i), _no_text_pixels(b, i)
            if (w, h) == (w2, h2):
                diff = sum(1 for p, q in zip(xa, xb) if abs(p - q) > 60)
                if diff > 15:
                    msg.append(f"그림/선 차이 {diff}px (밑줄 제목이 있는 쪽은 밑줄을 다시 그려서 생기는 정상 차이)")
        except Exception as e:  # noqa: BLE001
            msg.append(f"그림 비교 실패({type(e).__name__})")
        ka = sorted(x.type[1] for x in (pa.annots() or []))
        kb = sorted(x.type[1] for x in (pb.annots() or []))
        if ka != kb:
            msg.append(f"주석 {len(ka)}→{len(kb)}개")
        if len(pa.get_links()) != len(pb.get_links()):
            msg.append("링크 개수 다름")
        ta, tb = pa.get_text(), pb.get_text()
        if _bad_chars(tb) > _bad_chars(ta):
            msg.append(f"깨진 글자 {_bad_chars(tb) - _bad_chars(ta)}개")
        oa, ob = _overlaps(_lines(pa)), _overlaps(_lines(pb))
        if ob > oa:
            msg.append(f"글자 겹침 {oa}→{ob}")
        en0, en1 = len(re.findall(r"[A-Za-z]{4,}", ta)), len(re.findall(r"[A-Za-z]{4,}", tb))
        if en0 > 30 and en1 > 0.5 * en0:
            msg.append(f"영어 남음 {en1}/{en0} 단어 (원문 유지 페이지면 정상)")
        if msg:
            flagged += 1
            print(f"p.{i + 1}: " + " | ".join(msg))
            if png_dir:
                Path(png_dir).mkdir(parents=True, exist_ok=True)
                m = fitz.Matrix(1.2, 1.2)
                p1, p2 = pa.get_pixmap(matrix=m), pb.get_pixmap(matrix=m)
                out = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, p1.width + p2.width, max(p1.height, p2.height)))
                out.clear_with(255)
                p1.set_origin(0, 0)
                p2.set_origin(p1.width, 0)
                out.copy(p1, p1.irect)
                out.copy(p2, p2.irect)
                out.save(str(Path(png_dir) / f"p{i + 1:04d}.png"))
    print(f"\n검사 {min(len(a), len(b))}쪽 / 확인 필요 {flagged}쪽")
    return flagged


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    png = sys.argv[sys.argv.index("--png") + 1] if "--png" in sys.argv else ""
    compare(sys.argv[1], sys.argv[2], png)
