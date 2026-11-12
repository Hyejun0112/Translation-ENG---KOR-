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
- 폰트           : 맑은 고딕(Windows) 자동 사용, 사용 글자만 서브셋 삽입
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
from engines.table_engine import cell_merge, column_merge, find_table_cells, is_tabular, mark_keep_columns
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


PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


PAGE_CLASS_KO = {
    "native": "네이티브", "scanned": "스캔(이미지)", "garbled": "글자깨짐",
    "invisible": "스캔+숨은OCR층", "empty": "빈 페이지", "forced": "강제OCR",
}


def classify_page(page) -> str:
    """native | scanned | garbled | invisible | empty"""
    raw = page.get_text("text")
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


def split_line_segments(ln: dict) -> list[dict]:
    """한 줄 안에서 열 간격이 크게 벌어진 텍스트(표의 셀 등)를 별도 세그먼트로 분리."""
    spans = [s for s in ln["spans"] if s["text"].strip()]
    segs, cur = [], []
    for s in spans:
        if cur:
            gap = s["bbox"][0] - cur[-1]["bbox"][2]
            if gap > max(6.0, 0.8 * max(s["size"], 1.0)):
                segs.append(cur)
                cur = []
        cur.append(s)
    if cur:
        segs.append(cur)
    out = []
    for sg in segs:
        r = fitz.Rect(sg[0]["bbox"])
        for s in sg[1:]:
            r |= fitz.Rect(s["bbox"])
        text = "".join(s["text"] for s in sg).strip()
        if text:
            out.append({"bbox": tuple(r), "spans": sg, "dir": ln["dir"], "_text": text})
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
        elif lm > 3 * max(rm, 1) and rm < 6:
            align = "right"
    return Unit(page=page.number, idx=0, rect=rect, text=text, size=size, color=color,
                bold=bold, origin_y=first["origin"][1], align=align, rotated=rotated,
                cell=fitz.Rect(cell) if cell is not None else None)


def extract_units(page, table_cells: Optional[list] = None) -> list[Unit]:
    """네이티브(텍스트 레이어가 있는) 페이지에서 번역 단위를 추출."""
    d = page.get_text("dict", flags=TEXT_FLAGS)
    cells = find_table_cells(page) if table_cells is None else table_cells
    units: list[Unit] = []
    seen = set()
    cell_lines: dict[int, list[dict]] = {}

    def add(u: Optional[Unit]):
        if u is None:
            return
        key = (round(u.rect.x0), round(u.rect.y0), round(u.rect.x1), round(u.rect.y1), u.text)
        if key in seen:  # 겹쳐 그린 중복 텍스트(가짜 볼드 등) 제거
            return
        seen.add(key)
        u.idx = len(units)
        units.append(u)

    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        seg_lines: list[dict] = []
        tabular = False
        for ln in b.get("lines", []):
            segs = split_line_segments(ln)
            if len(segs) > 1:
                tabular = True
            seg_lines += segs
        # 괘선 표 셀 안에 있는 줄은 셀 단위로 따로 모은다
        rest = []
        for sl in seg_lines:
            bb = sl["bbox"]
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            hit, best = None, None
            for i, (c, *_meta) in enumerate(cells):
                if c.x0 <= cx <= c.x1 and c.y0 <= cy <= c.y1:
                    a = abs(c)
                    if best is None or a < best:
                        hit, best = i, a
            if hit is None:
                rest.append(sl)
            else:
                cell_lines.setdefault(hit, []).append(sl)
        if not rest:
            continue
        if not tabular:
            tabular = is_tabular(rest)
        if tabular:
            groups = column_merge(rest)
        else:
            groups, cur = [], []
            for ln in rest:
                if cur and BULLET_RE.match(ln["_text"]):
                    groups.append(cur)
                    cur = []
                cur.append(ln)
            if cur:
                groups.append(cur)
        for g in groups:
            add(_group_to_unit(page, g))

    for ci, lines in cell_lines.items():
        rect, ti, ri, col = cells[ci]
        for g in cell_merge(lines):
            u = _group_to_unit(page, g, rect)
            if u is not None:
                u.tkey = (ti, ri, col)
            add(u)
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


def subset_font_bytes(path: str, chars: set[str]) -> Optional[bytes]:
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
        if _renders_hangul(data):
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
    chars = set(EXTRA_CHARS) | {chr(c) for c in range(0x20, 0x7F)}
    for u in units:
        if u.ko:
            chars |= set(u.ko)
    out = {}
    for key, path in (("regular", reg), ("bold", bold)):
        if not path:
            out[key] = {"font": None, "buf": None, "path": None}
            continue
        data = subset_font_bytes(path, chars)
        font = fitz.Font(fontbuffer=data) if data else fitz.Font(fontfile=path)
        out[key] = {"font": font, "buf": data, "path": path}
    return out


def insert_font(page, name: str, spec: dict):
    if spec["buf"]:
        page.insert_font(fontname=name, fontbuffer=spec["buf"])
    else:
        page.insert_font(fontname=name, fontfile=spec["path"])


LINE_H = 1.22


def wrap_text(text: str, font, size: float, width: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split(" "):
            cand = w if not cur else cur + " " + w
            if font.text_length(cand, fontsize=size) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
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


def _shrink_fit(text, font, width, height, start, lo, tol=0.35):
    size = start
    while True:
        lines = wrap_text(text, font, size, width)
        ok = len(lines) * size * LINE_H <= height + size * tol
        if ok or size <= lo + 1e-6:
            return size, lines, ok
        size = max(lo, size - 0.25)


def fit_text(text, font, w0, h0, box_w, box_h, orig_size, cfg: Config):
    """원래 박스 → 폭 확장 → 폭+높이 확장(셀 바닥/다음 글자 직전까지) 순서로 92%, 85%까지 축소해 시도,
    그래도 안 되면 확장 박스에서 하한(min_scale/min_size)까지 축소.
    반환: (크기, 줄 목록, 맞음여부, 사용폭)"""
    lo = min(orig_size, max(cfg.min_size, orig_size * cfg.min_scale))
    W, H = max(box_w, w0), max(box_h, h0)
    for tier in (0.92, 0.85):  # 약간 축소 → 폭/높이 확장 → 조금 더 축소 순
        mid = max(lo, orig_size * tier)
        for w, h in ((w0, h0), (W, h0), (W, H)):
            s, ls, ok = _shrink_fit(text, font, w, h, orig_size, mid)
            if ok:
                return s, ls, ok, w
    s, ls, ok = _shrink_fit(text, font, W, H, orig_size, lo)
    return s, ls, ok, W


def page_edges(page) -> tuple[list, list]:
    """표 테두리/박스 경계선. 세로선 (x, y0, y1), 가로선 (y, x0, x1)."""
    vedges: list[tuple[float, float, float]] = []
    hedges: list[tuple[float, float, float]] = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return vedges, hedges
    if len(drawings) > 8000:  # 매우 복잡한 도면: 속도를 위해 생략
        return vedges, hedges
    for d in drawings:
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) < 0.8 and abs(p1.y - p2.y) >= 5:
                    vedges.append((p1.x, min(p1.y, p2.y), max(p1.y, p2.y)))
                elif abs(p1.y - p2.y) < 0.8 and abs(p1.x - p2.x) >= 5:
                    hedges.append((p1.y, min(p1.x, p2.x), max(p1.x, p2.x)))
            elif it[0] == "re":
                r = it[1]
                if r.height >= 5:
                    vedges.append((r.x0, r.y0, r.y1))
                    vedges.append((r.x1, r.y0, r.y1))
                if r.width >= 5:
                    hedges.append((r.y0, r.x0, r.x1))
                    hedges.append((r.y1, r.x0, r.x1))
    return vedges, hedges


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
        if o.rect.y0 >= u.rect.y1 - 0.5 and o.rect.x1 > left and o.rect.x0 < right:
            bottom = min(bottom, o.rect.y0 - 1)
    for y, xa, xb in hedges:
        if y >= u.rect.y1 - 0.3 and xb > left and xa < right:
            bottom = min(bottom, y - 1.5)
    if u.cell is None:
        bottom = min(bottom, u.rect.y1 + u.size * LINE_H * 1.05)
    return max(bottom, u.rect.y1)


def apply_redactions(page, whiteout: bool):
    kw = {}
    try:
        params = inspect.signature(page.apply_redactions).parameters
    except (TypeError, ValueError):
        params = {}
    if "images" in params:
        kw["images"] = fitz.PDF_REDACT_IMAGE_PIXELS if whiteout else fitz.PDF_REDACT_IMAGE_NONE
    if "graphics" in params:
        kw["graphics"] = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
    page.apply_redactions(**kw)


LINK_KEYS = ("kind", "from", "page", "to", "uri", "zoom", "file", "name")


def render_page(page, units: list[Unit], fonts, cfg: Config):
    todo = [u for u in units if u.ko]
    if not todo:
        return
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
        size, lines, ok, _w = fit_text(u.ko, font, u.rect.width, u.rect.height, box_w,
                                       bottom - u.rect.y0, u.size, cfg)
        u.used_size = size
        if not ok:
            u.notes.append("OVERFLOW(박스 초과, 글자 축소 한계)")
        asc = (u.origin_y - u.rect.y0) / u.size if u.size else 0.85
        asc = min(max(asc, 0.7), 1.0)
        y = u.rect.y0 + asc * size
        placed = []
        area = fitz.Rect(u.rect)
        for ln in lines:
            tw = font.text_length(ln, fontsize=size)
            if u.align == "center":
                x = u.rect.x0 + (u.rect.width - tw) / 2
            elif u.align == "right":
                x = u.rect.x1 - tw
            else:
                x = u.rect.x0
            if ln:
                placed.append((x, y, ln))
                area |= fitz.Rect(x, y - asc * size, x + tw, y + 0.28 * size)
            y += size * LINE_H
        if u.ocr:
            color = tuple(u.fg) if u.fg else (0, 0, 0)
        else:
            color = fitz.sRGB_to_pdf(u.color) if hasattr(fitz, "sRGB_to_pdf") else (0, 0, 0)
        plans.append((u, fname, size, placed, area, color))

    # 1-1) 겹침/페이지 밖 검사 (최종 검사용)
    for i in range(len(plans)):
        ui, ai = plans[i][0], plans[i][4]
        if not page.rect.contains(ai):
            ui.notes.append("PAGE_OVER(페이지 밖으로 나감)")
        for j in range(i + 1, len(plans)):
            uj, aj = plans[j][0], plans[j][4]
            inter = ai & aj
            if not inter.is_empty and inter.width > 1.5 and inter.height > 1.5:
                ui.notes.append(f"TEXT_OVERLAP(다른 문구와 겹침: {uj.text[:20]})")

    # 2) 원문 제거: 네이티브 텍스트는 리댁션(그림/선은 보존), OCR 영역의 숨은/깨진 텍스트층도 제거
    links = [{k: l[k] for k in LINK_KEYS if k in l} for l in page.get_links()]
    n_redact = 0
    any_ocr = any(u.ocr for u in todo)
    for u in todo:
        if not u.ocr:
            r = fitz.Rect(u.rect.x0 + 0.3, u.rect.y0 + 0.3, u.rect.x1 - 0.3, u.rect.y1 - 0.3)
            page.add_redact_annot(r, fill=(1, 1, 1) if cfg.whiteout else False)
            n_redact += 1
        elif has_text_layer and u.source == "ocr-page":
            page.add_redact_annot(fitz.Rect(u.rect) + (-1, -1, 1, 1), fill=False)
            n_redact += 1
    if n_redact:
        apply_redactions(page, cfg.whiteout and not any_ocr)
        for l in page.get_links():  # 링크 복원
            page.delete_link(l)
        for l in links:
            try:
                page.insert_link(l)
            except Exception:  # noqa: BLE001
                pass

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

    # 4) 한글 쓰기
    insert_font(page, "OMKO", fonts["regular"])
    if fonts["bold"]["font"]:
        insert_font(page, "OMKOB", fonts["bold"])
    for u, fname, size, placed, area, color in plans:
        for x, y, ln in placed:
            page.insert_text(fitz.Point(x, y), ln, fontname=fname, fontsize=size, color=color)


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
        if cls in ("scanned", "garbled", "invisible", "forced"):
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
            for b in res.get(i, []):
                per_page.setdefault(pno, []).append(block_to_unit(pno, b))
    all_units: list[Unit] = []
    for pno in pages:
        us = per_page.get(pno, [])
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
    if u.text in overrides:
        u.ko, u.status = overrides[u.text], "OVERRIDE"
        return
    ptxt, tokens = protector.protect(u.text, tokenize_numbers=len(u.text) <= 80)
    u.ptxt, u.tokens = ptxt, tokens
    if u.scope_keep:
        u.status = "SCOPE_KEEP"
    elif not needs_translation(ptxt):
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
