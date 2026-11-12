#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""설치 상태 점검: 패키지 / Tesseract / 한글 폰트 / API 키.
실행: py check_env.py          (점검만)
     py check_env.py --setup  (없으면 .venv 생성 + 패키지 설치 후 점검)"""
import importlib
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) > 1 and sys.argv[1] == "--setup":
    import bootstrap  # .venv 생성 + 패키지 설치까지 자동 진행

    bootstrap.ensure()

ok_all = True


def line(ok: bool, name: str, msg: str = ""):
    global ok_all
    ok_all &= ok
    print(f"[{'OK' if ok else '!!'}] {name:<22} {msg}")


print(f"Python {sys.version.split()[0]}  ({sys.executable})")
for mod, pip_name in [("pymupdf", "pymupdf"), ("anthropic", "anthropic"), ("pytesseract", "pytesseract"),
                      ("cv2", "opencv-python-headless"), ("numpy", "numpy"), ("PIL", "Pillow"),
                      ("fontTools", "fonttools"), ("openpyxl", "openpyxl")]:
    try:
        m = importlib.import_module(mod)
        line(True, pip_name, getattr(m, "__version__", getattr(m, "VersionBind", "")))
    except ImportError:
        line(False, pip_name, f"미설치 → pip install {pip_name}")

try:
    from om_ocr import OcrEngine
    eng = OcrEngine()
    if eng.available:
        import pytesseract
        line(True, "Tesseract", f"{pytesseract.get_tesseract_version()}  ({eng.cmd})")
    else:
        line(False, "Tesseract", eng.reason + "\n" + " " * 28 + "→ GUI의 [Tesseract 설치] 버튼 또는: py install_tesseract.py")
except Exception as e:  # noqa: BLE001
    line(False, "Tesseract", str(e))

try:
    from om_translate import find_font
    reg, bold = find_font("", "regular"), find_font("", "bold")
    line(bool(reg), "한글 폰트(본문)", reg or "없음 → --font 로 지정")
    line(bool(bold), "한글 폰트(굵게)", bold or "없음 (본문 폰트로 대체됨)")
except SystemExit as e:
    line(False, "한글 폰트", str(e))

print(f"[--] {'ANTHROPIC_API_KEY':<22} " + ("설정됨 (API 자동 번역 사용 가능)" if os.environ.get("ANTHROPIC_API_KEY")
      else "미설정 (선택 사항 - Copilot 번역에는 필요 없음)"))

print("\n모두 정상입니다." if ok_all else "\n'!!' 항목을 확인하세요. (README 1장 참고)")
