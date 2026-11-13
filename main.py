#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Plant O&M Translator v2.0 번역 파이프라인
==================================================
일반 사용자는 OM_Translator.pyw(프로그램 창)를 더블클릭해서 사용합니다.
이 파일은 프로그램 창이 내부적으로 실행하며, 명령줄에서 직접 실행할 수도 있습니다.

결과물 (output 폴더)
  원본파일명_KR_O&M.pdf              한국어 O&M 문서 (원본 레이아웃 그대로)
  원본파일명_Translation_Log.xlsx    번역로그 / 페이지별 번역범위 / 최종검사
  Plant_O&M_Glossary.xlsx            적용된 용어집 + 미등록 용어 후보 (누적)

명령줄 옵션 (고급)
  python main.py                    input\\pdf 일괄 처리 후 폴더 감시 (새 PDF 자동 번역)
  python main.py a.pdf b.pdf        파일을 input\\pdf 로 복사 후 처리 (드래그 앤 드롭과 동일)
  python main.py --once             일괄 처리만 하고 종료
  python main.py --force            이미 번역된 파일도 다시 생성
  python main.py --pages 1-5        각 PDF의 일부 페이지만 (샘플 확인용, 결과는 해당 페이지만)
  python main.py --mock             API 없이 레이아웃만 테스트 (글자 앞에 [번역])
  python main.py --check            설치 상태 점검
  python main.py --set-key          Claude API 키 다시 입력
  python main.py --estimate         번역 분량/토큰 추정만
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


# ============================================================================
# 0. 필요한 패키지 자동 설치 (표준 라이브러리만 사용하는 단계)
# ============================================================================
REQUIRED_MODULES = {
    "pymupdf": "pymupdf", "anthropic": "anthropic", "pytesseract": "pytesseract",
    "cv2": "opencv-python-headless", "numpy": "numpy", "PIL": "Pillow",
    "fontTools": "fonttools", "openpyxl": "openpyxl",
}


def ensure_packages() -> None:
    missing = [pip for mod, pip in REQUIRED_MODULES.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    if os.environ.get("OM_PACKAGES_INSTALLED"):
        sys.exit("필요한 패키지를 설치하지 못했습니다: " + ", ".join(missing) +
                 f"\n직접 실행: \"{sys.executable}\" -m pip install -r requirements.txt")
    print(f"[준비] 필요한 패키지 설치 중 (처음 한 번, 1~3분): {', '.join(missing)}", flush=True)
    subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(BASE_DIR / "requirements.txt")])
    if rc != 0:
        sys.exit("패키지 설치 실패. 인터넷/회사 프록시 설정을 확인하세요.")
    env = dict(os.environ, OM_PACKAGES_INSTALLED="1")
    sys.exit(subprocess.call([sys.executable, *sys.argv], env=env))


if __name__ == "__main__":
    ensure_packages()

# ============================================================================
# 1. 엔진 import
# ============================================================================
import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import traceback  # noqa: E402
from collections import Counter  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Optional  # noqa: E402

import config  # noqa: E402
from config import Config  # noqa: E402
from engines.glossary_engine import (  # noqa: E402
    Glossary, Protector, load_keep_patterns, missing_protected, restore_tokens,
)
from engines.pdf_engine import (  # noqa: E402
    Unit, build_fonts, build_ocr_engine, classify_unit, clean_text, describe_classes, fitz,
    draw_layout_debug, parse_pages, prepare_units, render_page, select_pages_safe, save_pdf_safe,
)
from engines.qa_engine import load_overrides, verify_output, write_glossary_xlsx, write_log_xlsx  # noqa: E402
from engines.translation_engine import (  # noqa: E402
    AnthropicBackend, Cache, MockBackend, file_sha, tm_get, tm_put, translate_texts,
)

logging.getLogger("fontTools").setLevel(logging.ERROR)  # 폰트 서브셋 시 무해한 경고 숨김


# ============================================================================
# 2. 콘솔 + 로그 파일 동시 출력
# ============================================================================
class Tee:
    """print 출력을 콘솔과 logs\\run_날짜.log 에 함께 기록."""

    def __init__(self, stream, logfile):
        self.stream = stream
        self.logfile = logfile

    def write(self, data):
        try:
            self.stream.write(data)
        except Exception:  # noqa: BLE001
            pass
        try:
            if "\r" in data and "\n" not in data:
                return  # 진행률(같은 줄 덮어쓰기)은 로그 파일에 남기지 않음
            self.logfile.write(data.replace("\r", ""))
            self.logfile.flush()
        except Exception:  # noqa: BLE001
            pass

    def flush(self):
        try:
            self.stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def reconfigure(self, **kw):
        pass


def setup_logging() -> Path:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = config.LOG_DIR / time.strftime("run_%Y%m%d.log")
    f = open(path, "a", encoding="utf-8")
    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {config.APP_TITLE}  python={sys.executable}\n")
    sys.stdout = Tee(sys.stdout, f)  # type: ignore[assignment]
    sys.stderr = Tee(sys.stderr, f)  # type: ignore[assignment]
    return path


def banner():
    print("=" * 72)
    print(f"  {config.APP_TITLE}  -  발전플랜트 O&M Manual 영→한 자동 번역")
    print("=" * 72)
    print(f"  입력 폴더 : {config.INPUT_DIR}")
    print(f"  결과 폴더 : {config.OUTPUT_DIR}")
    print("-" * 72)


def is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


# ============================================================================
# 3. 첫 실행 설정: API 키 / Tesseract
# ============================================================================
def validate_api_key(key: str) -> tuple[bool, str]:
    """API 키 확인 (모델 목록 조회 - 번역 비용 없음)."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.models.list(limit=1)
        return True, ""
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "status_code", None)
        if code in (401, 403):
            return False, "API 키가 올바르지 않거나 권한이 없습니다."
        # 네트워크/프록시 문제 등은 키 자체의 문제로 단정하지 않음
        return True, f"(키 확인 생략: {type(e).__name__})"


def ensure_api_key(force_ask: bool = False) -> str:
    key = "" if force_ask else config.get_api_key()
    if key:
        return key
    if not is_interactive():
        sys.exit("Claude API 키가 없습니다. 프로그램 창의 [API 키] 칸에 키를 넣고 [저장]을 누르세요.")
    print("\n[첫 실행 설정] Claude API 키가 필요합니다.")
    print("  발급: https://console.anthropic.com  →  API Keys  →  Create Key  (sk-ant- 로 시작)")
    print("  키는 이 PC의 프로젝트 폴더 .env 파일에만 저장됩니다.\n")
    for _ in range(3):
        key = input("  API 키를 붙여넣고 Enter: ").strip().strip('"').strip("'")
        if not key:
            continue
        if not key.startswith("sk-"):
            print("  ! 'sk-' 로 시작하는 키를 넣어 주세요.")
            continue
        ok, msg = validate_api_key(key)
        if not ok:
            print(f"  ! {msg}")
            continue
        config.save_env("ANTHROPIC_API_KEY", key)
        print(f"  ✔ 저장했습니다. {msg}\n")
        return key
    sys.exit("API 키 입력을 취소했습니다.")


def ensure_tesseract(cfg: Config, ocr) -> object:
    """OCR 엔진 확인. 없으면 자동 설치를 제안(대화형일 때)."""
    if ocr is None or ocr.available:
        return ocr
    print(f"\n! Tesseract OCR 사용 불가: {ocr.reason}")
    print("  스캔 페이지 / 글자가 깨진 페이지(목차 등)는 번역되지 않고 영어로 남습니다.")
    if is_interactive():
        ans = input("  지금 자동 설치할까요? (Y/n): ").strip().lower()
        if ans in ("", "y", "yes", "예", "ㅛ"):
            subprocess.call([sys.executable, str(config.TOOLS_DIR / "install_tesseract.py")])
            ocr = build_ocr_engine(cfg)
            if ocr is not None and ocr.available:
                print("  ✔ Tesseract 사용 가능\n")
            else:
                print("  ! 아직 사용할 수 없습니다. OCR 없이 계속합니다.\n")
    return ocr


def check_env() -> int:
    ok_all = True

    def line(ok, name, msg=""):
        nonlocal ok_all
        ok_all &= bool(ok)
        print(f"[{'OK' if ok else '!!'}] {name:<20} {msg}")

    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 10):
        line(False, "Python 버전", "3.12 권장")
    for mod, pip in REQUIRED_MODULES.items():
        line(importlib.util.find_spec(mod) is not None, pip)
    ocr = build_ocr_engine(Config())
    if ocr is not None and ocr.available:
        import pytesseract
        line(True, "Tesseract", f"{pytesseract.get_tesseract_version()} ({ocr.cmd})")
    else:
        line(False, "Tesseract", (ocr.reason if ocr else "OCR 꺼짐") + "  → 프로그램 창의 [Tesseract(OCR) 설치]")
    from engines.pdf_engine import find_font
    reg = find_font("", "regular")
    line(bool(reg), "한글 폰트", reg or "없음 (C:\\Windows\\Fonts\\malgun.ttf 필요)")
    key = config.get_api_key()
    line(bool(key), "Claude API 키", "설정됨 (.env)" if key else "미설정 → 프로그램 창에서 입력 후 [저장]")
    gp = config.glossary_path()
    line(gp is not None, "용어집", str(gp) if gp else "data\\glossary.csv 없음")
    print("\n모두 정상입니다." if ok_all else "\n'!!' 항목을 확인하세요.")
    return 0 if ok_all else 1


# ============================================================================
# 4. 한 파일 처리
# ============================================================================
def output_paths(src: Path) -> tuple[Path, Path]:
    dst = config.OUTPUT_DIR / f"{src.stem}{config.OUTPUT_SUFFIX}.pdf"
    log = config.OUTPUT_DIR / f"{src.stem}{config.LOG_SUFFIX}.xlsx"
    return dst, log


def needs_processing(src: Path, force: bool) -> bool:
    dst, log = output_paths(src)
    if force or not dst.exists():
        return True
    if src.stat().st_mtime > dst.stat().st_mtime:
        return True  # 원본이 더 새로움
    if log.exists() and log.stat().st_mtime > dst.stat().st_mtime + 5:
        return True  # 번역 로그에 수정 번역을 입력함
    return False


def process_pdf(src: Path, cfg: Config, backend, gloss: Glossary, protector: Protector,
                cache: Cache, ocr) -> dict:
    t0 = time.time()
    dst, log_path = output_paths(src)
    # 번역 로그의 '수정 번역(입력)' 열 → 이번 실행에 반영
    overrides = load_overrides(str(log_path)) if log_path.exists() else {}
    if overrides:
        print(f"  수정 번역 {len(overrides)}건 반영 (번역 로그 엑셀)")

    doc = fitz.open(src)
    if doc.needs_pass:
        raise RuntimeError("암호가 걸린 PDF입니다. 암호를 해제한 뒤 다시 넣으세요.")
    n_pages = len(doc)
    pages = cfg.pages if cfg.pages is not None else list(range(n_pages))
    for pno in pages:  # 회전 페이지: 보이는 모습 그대로 회전값 정규화 (한글이 똑바로 들어가도록)
        pg = doc[pno]
        if pg.rotation and hasattr(pg, "remove_rotation"):
            pg.remove_rotation()

    units, classes, n_fig, scopes = prepare_units(doc, cfg, protector, overrides, ocr, cache,
                                                  file_sha(src), label=src.stem[:20])
    print(" " * 100, end="\r")  # 진행 표시 줄 지우기
    need_ocr = [p for p, c in classes.items() if c in ("scanned", "garbled", "invisible")]
    if need_ocr and not (ocr is not None and ocr.available):
        print(f"  ! OCR이 필요한 페이지 {len(need_ocr)}개는 영어로 남습니다 "
              f"(p.{', '.join(str(p + 1) for p in need_ocr[:10])}) → Tesseract 설치 필요")

    # 북마크(목차) 번역 - 전체 페이지 처리 시
    toc = doc.get_toc(simple=True) if cfg.pages is None else []
    toc_units = []
    for lvl, title, pg in toc:
        tu = Unit(page=pg - 1, idx=-1, rect=fitz.Rect(), text=clean_text(title), size=0, color=0,
                  bold=False, origin_y=0, kind="toc", source="toc")
        classify_unit(tu, protector, overrides)
        toc_units.append(tu)
    all_units = units + toc_units

    # 번역 메모리 먼저 (같은 문구는 다시 API 호출하지 않음)
    use_tm = backend.name != "mock"
    from_tm = 0
    if use_tm:
        for u in all_units:
            if u.status == "PENDING":
                hit = tm_get(cache, u.text)
                if hit is not None:
                    u.ko, u.status = hit, "FROM_TM"
                    from_tm += 1
    pending = list(dict.fromkeys(u.ptxt for u in all_units if u.status == "PENDING"))
    n_scope = sum(1 for ok, _ in scopes.values() if not ok)
    chars = sum(len(t) for t in pending)
    print(f"  페이지 {n_pages} ({describe_classes(classes)}) | 원문유지 페이지 {n_scope} | 텍스트블록 {len(units)}"
          f" | 번역메모리 재사용 {from_tm} | API 번역 {len(pending)}문구 ({chars:,}자)")
    res = translate_texts(pending, backend, gloss, cache, cfg, label=src.stem[:20]) if pending else {}

    for u in all_units:
        if u.status == "PENDING":
            ko = res.get(u.ptxt)
            if not ko:
                u.status = "FAILED"
                u.notes.append("번역 실패/보호 토큰 불일치 → 원문 영어 유지")
                continue
            u.ko = restore_tokens(ko, u.tokens)
            if use_tm:
                tm_put(cache, u.text, u.ko)
        elif u.status == "OVERRIDE":
            tm_put(cache, u.text, u.ko)
        if u.status in ("PENDING", "FROM_TM", "OVERRIDE") and u.ko is not None:
            if clean_text(u.ko) == u.text:
                u.ko, u.status = None, "UNCHANGED"
                continue
            if u.status != "OVERRIDE":
                u.status = "TRANSLATED"
            for en, kor, _ in gloss.find(u.text):
                if kor not in u.ko:
                    u.notes.append(f"GLOSSARY_MISS: {en}→{kor}")
            miss = missing_protected(protector, u.text, u.ko)
            if miss:
                u.notes.append("TAG_CHANGED(원문 Tag/숫자/단위 확인: " + ", ".join(miss[:5]) + ")")

    # 렌더링
    translated_pages: set[int] = set()
    if any(u.ko for u in units):
        fonts = build_fonts(cfg, units)
        by_page: dict[int, list[Unit]] = {}
        for u in units:
            by_page.setdefault(u.page, []).append(u)
        for pno, us in by_page.items():
            try:
                render_page(doc[pno], us, fonts, cfg)
                if any(u.ko for u in us):
                    translated_pages.add(pno)
            except Exception as e:  # noqa: BLE001
                print(f"  ! p.{pno + 1} 렌더링 실패(원본 유지): {e}", file=sys.stderr)
                for u in us:
                    if u.ko:
                        u.notes.append(f"RENDER_FAILED: {e}")
    if toc_units and any(t.ko for t in toc_units):
        try:
            doc.set_toc([[lvl, (tu.ko or title), pg] for (lvl, title, pg), tu in zip(toc, toc_units)])
        except Exception as e:  # noqa: BLE001
            print(f"  ! 북마크 번역 반영 실패: {e}", file=sys.stderr)

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if cfg.pages is not None:
        out_doc = select_pages_safe(doc, cfg.pages)
        translated_pages = {pages.index(p) for p in translated_pages}
    else:
        out_doc = doc
    try:
        save_pdf_safe(out_doc, dst)
    except PermissionError:
        alt = dst.with_name(dst.stem + time.strftime("_%H%M%S") + dst.suffix)
        print(f"  ! {dst.name} 이(가) 다른 프로그램(Bluebeam 등)에서 열려 있어 {alt.name} 로 저장합니다.")
        save_pdf_safe(out_doc, alt)
        dst = alt
    if out_doc is not doc:
        out_doc.close()
    doc.close()

    # 최종 검사 + 로그
    warns, checks = verify_output(dst, len(pages), translated_pages, src, pages)
    for w in warns:
        print(f"  ! 검사: {w}", file=sys.stderr)
    cnt = Counter(u.status for u in all_units)
    n_overlap = sum(1 for u in units if any(n.startswith(("TEXT_OVERLAP", "PAGE_OVER")) for n in u.notes))
    n_overflow = sum(1 for u in units if any(n.startswith("OVERFLOW") for n in u.notes))
    n_tag = sum(1 for u in units if any(n.startswith("TAG_CHANGED") for n in u.notes))
    checks.append(("텍스트 겹침", "OK" if not n_overlap else "확인필요", f"{n_overlap}건"))
    checks.append(("셀/박스 초과", "OK" if not n_overflow else "확인필요", f"{n_overflow}건 (글자 축소 한계)"))
    checks.append(("Tag/숫자/단위 유지", "OK" if not n_tag else "확인필요", f"변경 의심 {n_tag}건"))
    checks.append(("번역 실패", "OK" if not cnt["FAILED"] else "확인필요", f"{cnt['FAILED']}건 (원문 유지)"))
    write_log_xlsx(log_path, src.name, all_units, classes, scopes, checks, pages)
    save_doc_terms(src, all_units)

    flagged = sum(1 for u in units if u.notes and u.status != "SCOPE_KEEP")
    print(f"  ✔ 완료 {time.time() - t0:.0f}초 | 번역 {cnt['TRANSLATED'] + cnt['OVERRIDE']} | "
          f"원문유지 {cnt['KEEP'] + cnt['SCOPE_KEEP']} | 실패 {cnt['FAILED']} | 검토필요 {flagged}")
    print(f"    → {dst.name}")
    print(f"    → {log_path.name}")
    return {"file": src.name, "output": str(dst), "failed": cnt["FAILED"], "flagged": flagged}


def save_doc_terms(src: Path, units: list[Unit]):
    """누적 용어집(Plant_O&M_Glossary.xlsx) 생성을 위해 문서별 원문 목록을 저장."""
    d = config.CACHE_DIR / "doc_texts"
    d.mkdir(parents=True, exist_ok=True)
    data = [[u.text, u.status] for u in units if u.status not in ("KEEP",)]
    (d / f"{src.stem}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def update_glossary_xlsx(gloss: Glossary):
    d = config.CACHE_DIR / "doc_texts"
    items = []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            for text, status in json.loads(f.read_text(encoding="utf-8")):
                items.append(SimpleNamespace(text=text, status=status))
        except Exception:  # noqa: BLE001
            continue
    if items:
        path = config.OUTPUT_DIR / config.GLOSSARY_XLSX_NAME
        write_glossary_xlsx(path, gloss, items)
        print(f"  용어집 갱신: {path.name}")


# ============================================================================
# 5. 일괄 처리 / 폴더 감시 / 드래그 앤 드롭
# ============================================================================
def list_input_pdfs() -> list[Path]:
    out = []
    for p in sorted(config.INPUT_DIR.rglob("*")):
        if p.suffix.lower() != ".pdf" or not p.is_file():
            continue
        if config.PROCESSED_DIRNAME in p.parts:
            continue
        if p.stem.endswith(config.OUTPUT_SUFFIX):
            continue
        out.append(p)
    return out


def import_dropped(paths: list[str]) -> list[Path]:
    """명령줄로 받은 PDF/폴더를 input\\pdf 로 복사."""
    copied = []
    for a in paths:
        p = Path(a.strip('"'))
        if not p.exists():
            print(f"! 없는 경로: {p}")
            continue
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.suffix.lower() == ".pdf")
        for f in files:
            if f.suffix.lower() != ".pdf":
                print(f"! PDF가 아니라 건너뜀: {f.name}")
                continue
            dst = config.INPUT_DIR / f.name
            try:
                if f.resolve() != dst.resolve():
                    shutil.copy2(f, dst)
                copied.append(dst)
                print(f"  추가: {f.name}")
            except Exception as e:  # noqa: BLE001
                print(f"! 복사 실패 {f.name}: {e}")
    return copied


def file_is_ready(p: Path) -> bool:
    """복사 중인 파일은 건너뛰기: 크기가 일정 시간 변하지 않고 열 수 있어야 함."""
    try:
        s1 = p.stat().st_size
        if time.time() - p.stat().st_mtime < config.FILE_STABLE_SEC:
            time.sleep(config.FILE_STABLE_SEC)
        s2 = p.stat().st_size
        if s1 != s2 or s2 == 0:
            return False
        with open(p, "rb"):
            pass
        return True
    except OSError:
        return False


class Runner:
    def __init__(self, args):
        self.args = args
        self.cfg = Config()
        if args.mock:
            self.cfg.backend = "mock"
            self.cfg.model = "mock"
        if args.model:
            self.cfg.model = args.model
        if args.no_ocr:
            self.cfg.ocr_mode = "off"
        if args.scope:
            self.cfg.scope = args.scope
        if args.text_scale:
            self.cfg.text_scale = min(100, max(60, args.text_scale)) / 100.0
        self.cfg.force = args.force
        self.gloss = Glossary.load(str(config.glossary_path()) if config.glossary_path() else None)
        self.protector = Protector(load_keep_patterns(str(config.KEEP_PATTERNS_FILE)
                                                      if config.KEEP_PATTERNS_FILE.exists() else None), self.gloss)
        self.cache = Cache(config.CACHE_FILE)
        self.backend = None
        self.ocr = None
        self.failed: dict[str, float] = {}  # 실패한 파일(수정 시각) - 감시 모드에서 무한 재시도 방지

    def prepare(self):
        if self.cfg.backend == "mock":
            self.backend = MockBackend()
        else:
            os.environ["ANTHROPIC_API_KEY"] = ensure_api_key()
            self.backend = AnthropicBackend(self.cfg.model)
        self.ocr = ensure_tesseract(self.cfg, build_ocr_engine(self.cfg))
        ocr_desc = ("꺼짐" if self.ocr is None else
                    (f"사용 ({self.ocr.dpi}dpi)" if self.ocr.available else "사용 불가"))
        print(f"  번역: {'레이아웃 테스트(mock)' if self.cfg.backend == 'mock' else 'Claude API ' + self.cfg.model}"
              f" | 용어집 {len(self.gloss.trans)}개(영어유지 {len(self.gloss.keep)}) | OCR {ocr_desc}"
              f" | 범위 {'O&M 대상만' if self.cfg.scope == 'om' else '전체'}")
        print("-" * 72)

    def run_batch(self, files: list[Path]) -> int:
        todo = []
        for f in files:
            if not needs_processing(f, self.cfg.force):
                continue
            if self.failed.get(str(f)) == f.stat().st_mtime:
                continue
            todo.append(f)
        if not todo:
            return 0
        n_ok = 0
        for i, src in enumerate(todo, 1):
            if not file_is_ready(src):
                print(f"[{i}/{len(todo)}] {src.name}: 복사 중인 파일 → 다음 확인 때 처리")
                continue
            print(f"[{i}/{len(todo)}] {src.name}")
            try:
                doc = fitz.open(src)
                n = len(doc)
                doc.close()
                self.cfg.pages = parse_pages(self.args.pages, n) if self.args.pages else None
                process_pdf(src, self.cfg, self.backend, self.gloss, self.protector, self.cache, self.ocr)
                n_ok += 1
                self.failed.pop(str(src), None)
                if config.MOVE_DONE_INPUTS:
                    done = config.INPUT_DIR / config.PROCESSED_DIRNAME
                    done.mkdir(exist_ok=True)
                    shutil.move(str(src), str(done / src.name))
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                self.failed[str(src)] = src.stat().st_mtime if src.exists() else 0
                print(f"  ✘ 실패: {type(e).__name__}: {e}", file=sys.stderr)
                traceback.print_exc()
                print("    (원본 파일을 다시 저장하거나 교체하면 다시 시도합니다)")
        if n_ok:
            update_glossary_xlsx(self.gloss)
            if self.backend is not None and getattr(self.backend, "in_tokens", 0):
                print(f"  API 사용량 누적: 입력 {self.backend.in_tokens:,} / 출력 {self.backend.out_tokens:,} 토큰")
        return n_ok

    def debug_layout(self, files: Optional[list[Path]] = None):
        """번역 없이 문구 인식 상자만 그려서 확인 (글자 겹침 원인 찾기용)"""
        ocr = build_ocr_engine(self.cfg) if self.cfg.ocr_mode != "off" else None
        for src in files or list_input_pdfs():
            doc = fitz.open(src)
            for pg in doc:
                if pg.rotation and hasattr(pg, "remove_rotation"):
                    pg.remove_rotation()
            self.cfg.pages = parse_pages(self.args.pages, len(doc)) if self.args.pages else None
            units, classes, _, scopes = prepare_units(doc, self.cfg, self.protector, {}, ocr, self.cache,
                                                      file_sha(src), label=src.stem[:20])
            pages = self.cfg.pages if self.cfg.pages is not None else list(range(len(doc)))
            doc.close()
            print(" " * 100, end="\r")
            dst = config.LOG_DIR / f"{src.stem}_LAYOUT_CHECK.pdf"
            draw_layout_debug(str(src), units, pages, str(dst))
            print(f"{src.name}: 레이아웃 점검 PDF → {dst}")
            for pno in pages[:30]:
                pu = [u for u in units if u.page == pno]
                n_ov = sum(1 for i, a in enumerate(pu) for b in pu[i + 1:]
                           if not (a.rect & b.rect).is_empty and (a.rect & b.rect).height > 1.0)
                print(f"  p.{pno + 1}: {classes.get(pno, '?')}, 문구 {len(pu)}개, 상자 겹침 {n_ov}쌍, "
                      f"{'번역' if scopes.get(pno, (True, ''))[0] else '원문유지'}")
            if os.name == "nt":
                open_folder(dst)

    def estimate(self, files: Optional[list[Path]] = None):
        tot_files = tot_chars = tot_items = 0
        uniq: dict[str, None] = {}
        for src in files or list_input_pdfs():
            doc = fitz.open(src)
            self.cfg.pages = parse_pages(self.args.pages, len(doc)) if self.args.pages else None
            self.cfg.ocr_mode = "off"
            units, classes, _, scopes = prepare_units(doc, self.cfg, self.protector, {}, None, None, "")
            pend = [u for u in units if u.status == "PENDING" and tm_get(self.cache, u.text) is None]
            for u in pend:
                uniq[u.ptxt] = None
            n_scope = sum(1 for ok, _ in scopes.values() if not ok)
            print(f"{src.name}: {len(doc)}p [{describe_classes(classes)}], 원문유지 페이지 {n_scope}, "
                  f"번역 필요 {len(pend)}블록")
            tot_files += 1
            tot_items += len(pend)
            doc.close()
        tot_chars = sum(len(t) for t in uniq)
        in_tok = int(tot_chars / 3.5 + len(uniq) * 12 + 1100 * max(1, len(uniq) // 40)) if uniq else 0
        out_tok = int(tot_chars / 2.2)
        print(f"\n파일 {tot_files}개 | 번역 문구 {tot_items}개 (중복 제거 {len(uniq)}개, {tot_chars:,}자)")
        print(f"예상 API 사용량: 입력 약 {in_tok:,} 토큰 / 출력 약 {out_tok:,} 토큰 "
              f"(스캔 페이지 OCR 분량은 제외)")


def open_folder(p: Path):
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"{config.APP_TITLE} - O&M Manual 영→한 PDF 번역")
    ap.add_argument("paths", nargs="*", help="번역할 PDF 파일/폴더 (input\\pdf 로 복사 후 처리)")
    ap.add_argument("--once", action="store_true", help="일괄 처리 후 종료 (폴더 감시 안 함)")
    ap.add_argument("--watch", action="store_true", help="일괄 처리 후 input\\pdf 폴더 감시 (기본 동작)")
    ap.add_argument("--force", action="store_true", help="이미 번역된 파일도 다시 생성")
    ap.add_argument("--pages", help="일부 페이지만 처리 (예: 1-5,12)")
    ap.add_argument("--mock", action="store_true", help="API 없이 레이아웃 테스트")
    ap.add_argument("--model", help="Claude 모델 (기본: config.DEFAULT_MODEL)")
    ap.add_argument("--scope", choices=["om", "all"], help="om: O&M 대상만(기본) | all: 전부 번역")
    ap.add_argument("--no-ocr", action="store_true", help="OCR 사용 안 함")
    ap.add_argument("--text-scale", type=int, help="번역 글자 크기 %% (원문 대비, 60~100, 기본 92)")
    ap.add_argument("--check", action="store_true", help="설치 상태 점검")
    ap.add_argument("--set-key", action="store_true", help="Claude API 키 다시 입력")
    ap.add_argument("--estimate", action="store_true", help="번역 분량/토큰 추정만")
    ap.add_argument("--debug-layout", action="store_true",
                    help="레이아웃 점검 PDF만 만들기 (API 사용 안 함, logs 폴더에 *_LAYOUT_CHECK.pdf)")
    ap.add_argument("--no-open", action="store_true", help="완료 후 결과 폴더 열지 않기")
    args = ap.parse_args(argv)

    config.ensure_dirs()
    config.load_env()
    setup_logging()
    banner()

    if args.check:
        return check_env()
    if args.set_key:
        ensure_api_key(force_ask=True)
        return 0

    runner = Runner(args)
    dropped = import_dropped(args.paths) if args.paths else []
    if args.estimate:
        runner.estimate(dropped or None)
        return 0
    if args.debug_layout:
        runner.debug_layout(dropped or None)
        return 0

    runner.prepare()

    files = dropped or list_input_pdfs()
    if not files:
        print(f"input\\pdf 폴더에 PDF가 없습니다. → {config.INPUT_DIR}")
        if os.name == "nt" and not args.once:
            open_folder(config.INPUT_DIR)
    n = runner.run_batch(files) if files else 0
    if n and not args.no_open:
        open_folder(config.OUTPUT_DIR)
    skipped = [f for f in files if not needs_processing(f, False)] if files and not args.force else []
    if files and n == 0 and skipped:
        print(f"이미 번역된 파일 {len(skipped)}개는 건너뛰었습니다. (다시 만들려면 결과 PDF를 지우거나 --force)")

    if args.once or args.pages:
        return 0

    print("\n" + "=" * 72)
    print("  폴더 감시 중: input\\pdf 에 PDF를 넣으면 자동으로 번역합니다.")
    print("  종료하려면 이 창을 닫거나 Ctrl+C 를 누르세요.")
    print("=" * 72)
    runner.cfg.force = False
    try:
        while True:
            time.sleep(config.WATCH_INTERVAL_SEC)
            pending = [f for f in list_input_pdfs() if needs_processing(f, False)]
            if pending:
                print(f"\n[{time.strftime('%H:%M:%S')}] 새 PDF {len(pending)}개 감지")
                if runner.run_batch(pending) and not args.no_open:
                    open_folder(config.OUTPUT_DIR)
                print("\n  폴더 감시 중... (종료: 창 닫기 또는 Ctrl+C)")
    except KeyboardInterrupt:
        print("\n종료합니다.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n종료합니다.")
        sys.exit(0)
