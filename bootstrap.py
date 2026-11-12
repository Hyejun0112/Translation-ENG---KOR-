# -*- coding: utf-8 -*-
"""
bootstrap.py - 실행 환경 자동 준비
==================================
필요한 패키지가 없는 파이썬으로 실행하면(예: VS Code ▶ 버튼이 다른 파이썬을 잡은 경우)
1) 프로젝트 폴더에 .venv 가상환경을 만들고
2) requirements.txt 패키지를 설치한 뒤
3) 같은 명령을 .venv 파이썬으로 다시 실행합니다.

이 파일은 표준 라이브러리만 사용합니다. (om_translate.py / om_ocr.py / check_env.py 맨 앞에서 호출)
자동 설치를 끄려면 환경변수 OM_NO_BOOTSTRAP=1
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQ_FILE = ROOT / "requirements.txt"

# import 이름 → pip 패키지 이름
REQUIRED = {
    "pymupdf": "pymupdf",
    "anthropic": "anthropic",
    "pytesseract": "pytesseract",
    "cv2": "opencv-python-headless",
    "numpy": "numpy",
    "PIL": "Pillow",
    "fontTools": "fonttools",
    "tkinterdnd2": "tkinterdnd2",
}


def _say(msg: str):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)


def missing_packages() -> list[str]:
    out = []
    for mod, pip_name in REQUIRED.items():
        try:
            if importlib.util.find_spec(mod) is None:
                out.append(pip_name)
        except (ImportError, ValueError):
            out.append(pip_name)
    return out


def _running_in_project_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def _run(cmd: list[str]) -> int:
    _say("  > " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return subprocess.call(cmd)


def _pip_install(python: str) -> bool:
    _run([python, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    return _run([python, "-m", "pip", "install", "-r", str(REQ_FILE)]) == 0


def _create_venv() -> bool:
    _say(f"[환경 준비] 가상환경 생성: {VENV}")
    if _run([sys.executable, "-m", "venv", str(VENV)]) == 0 and VENV_PY.exists():
        return True
    # uv로 설치된 파이썬 등 venv 모듈이 제한된 경우: uv 사용 시도
    if _run(["uv", "venv", str(VENV), "--python", sys.executable, "--seed"]) == 0 and VENV_PY.exists():
        return True
    return False


def ensure() -> None:
    """필요 패키지가 없으면 .venv를 준비하고 그 파이썬으로 현재 명령을 다시 실행한다."""
    miss = missing_packages()
    if not miss:
        return
    if os.environ.get("OM_NO_BOOTSTRAP") or os.environ.get("OM_BOOTSTRAPPED"):
        sys.exit(f"필요한 패키지가 없습니다: {', '.join(miss)}\n"
                 f"설치: \"{sys.executable}\" -m pip install -r requirements.txt")

    _say(f"[환경 준비] 현재 파이썬({sys.executable})에 패키지가 없습니다: {', '.join(miss)}")

    if _running_in_project_venv():
        python = sys.executable
    else:
        if not VENV_PY.exists() and not _create_venv():
            sys.exit("가상환경을 만들지 못했습니다. 터미널에서 직접 실행하세요:\n"
                     "  py -m venv .venv\n"
                     "  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        python = str(VENV_PY)

    # .venv에 이미 다 깔려 있으면 설치 생략
    check = "import " + ", ".join(REQUIRED)
    if subprocess.call([python, "-c", check], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        _say("[환경 준비] 패키지 설치 중... (처음 한 번만, 1~3분)")
        if not _pip_install(python):
            sys.exit("패키지 설치에 실패했습니다. 위 오류 메시지를 확인하세요. "
                     "(회사망이면 프록시/방화벽 설정이 필요할 수 있습니다)")

    if python == sys.executable:
        _say("[환경 준비] 완료. 다시 실행합니다.\n")
    else:
        _say("[환경 준비] 완료. .venv 파이썬으로 다시 실행합니다.\n"
             "  (VS Code 오른쪽 아래 인터프리터를 '.venv'로 바꾸면 다음부터 바로 실행됩니다)\n")
    env = dict(os.environ, OM_BOOTSTRAPPED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    script = os.path.abspath(sys.argv[0])
    rc = subprocess.call([python, script, *sys.argv[1:]], env=env)
    sys.exit(rc)
