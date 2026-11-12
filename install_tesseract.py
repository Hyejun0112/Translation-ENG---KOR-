#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install_tesseract.py - Tesseract OCR 자동 설치 (Windows)
=========================================================
실행: py install_tesseract.py        (GUI의 [Tesseract 설치] 버튼도 이 파일을 실행)

설치 순서
 1) 이미 설치되어 있으면 종료
 2) GitHub(tesseract-ocr/tesseract) 최신 Windows 설치 파일 다운로드
 3) 관리자 권한 없이 사용자 폴더에 조용히 설치  (%LOCALAPPDATA%\\Programs\\Tesseract-OCR)
 4) 설치 파일이 관리자 권한을 요구하면 → Windows 권한 확인(UAC) 창을 띄워 C:\\Program Files 에 설치
 5) 다운로드가 막혀 있으면 → winget(UB-Mannheim.TesseractOCR) 으로 설치
 6) 설치 확인 (영어 언어 데이터 eng 포함 여부)

표준 라이브러리만 사용합니다. 회사 프록시/보안 인증서 환경을 위해 truststore(있으면)를 사용합니다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

RELEASE_API = "https://api.github.com/repos/tesseract-ocr/tesseract/releases?per_page=10"
FALLBACK_URL = ("https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/"
                "tesseract-ocr-w64-setup-5.5.3.20260724.exe")
WINGET_ID = "UB-Mannheim.TesseractOCR"
USER_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "Tesseract-OCR"
ERROR_ELEVATION_REQUIRED = 740


def say(msg: str = ""):
    print(msg, flush=True)


def find_tesseract() -> str | None:
    try:
        from om_ocr import OcrEngine
        return OcrEngine.find_tesseract(None)
    except Exception:  # noqa: BLE001
        w = shutil.which("tesseract")
        if w:
            return w
        for p in (USER_DIR / "tesseract.exe", Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")):
            if p.exists():
                return str(p)
    return None


def check_installed(cmd: str) -> bool:
    try:
        out = subprocess.run([cmd, "--list-langs"], capture_output=True, text=True, timeout=30,
                             creationflags=0x08000000 if os.name == "nt" else 0)
        langs = (out.stdout + out.stderr).split()
        ver = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=30,
                             creationflags=0x08000000 if os.name == "nt" else 0)
        say(f"  설치 위치: {cmd}")
        say(f"  버전: {(ver.stdout or ver.stderr).splitlines()[0] if (ver.stdout or ver.stderr) else '?'}")
        if "eng" not in langs:
            say("! 영어 언어 데이터(eng)가 없습니다. 설치 프로그램을 다시 실행해 English 를 선택하세요.")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        say(f"! 실행 확인 실패: {e}")
        return False


def _ssl_setup():
    """회사망(SSL 검사 프록시)에서도 Windows 인증서 저장소를 쓰도록."""
    try:
        import truststore  # type: ignore
        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001
        pass


def latest_installer_url() -> str:
    try:
        req = urllib.request.Request(RELEASE_API, headers={"Accept": "application/vnd.github+json",
                                                           "User-Agent": "om-translate"})
        with urllib.request.urlopen(req, timeout=30) as r:
            releases = json.loads(r.read().decode("utf-8"))
        for rel in releases:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            for a in rel.get("assets", []):
                n = a.get("name", "")
                if n.startswith("tesseract-ocr-w64-setup") and n.endswith(".exe"):
                    return a["browser_download_url"]
    except Exception as e:  # noqa: BLE001
        say(f"  (최신 버전 조회 실패: {e} → 기본 버전 사용)")
    return FALLBACK_URL


def download(url: str, dst: Path) -> bool:
    say(f"다운로드: {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "om-translate"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 0.5:
                    last = time.time()
                    pct = f"{done * 100 // total}%" if total else ""
                    print(f"  {done / 1e6:.1f} MB {pct}", end="\r", flush=True)
        say(f"  완료 {done / 1e6:.1f} MB" + " " * 20)
        return dst.stat().st_size > 5_000_000
    except Exception as e:  # noqa: BLE001
        say(f"! 다운로드 실패: {e}")
        return False


def run_installer_user(exe: Path) -> str:
    """관리자 권한 없이 사용자 폴더에 무인 설치. 반환: ok | elevate | fail"""
    USER_DIR.parent.mkdir(parents=True, exist_ok=True)
    say(f"설치 시도 (관리자 권한 없이): {USER_DIR}")
    try:
        # NSIS 설치 파일: /S = 무인 설치, /D=경로 (반드시 마지막, 따옴표 없이)
        rc = subprocess.run([str(exe), "/S", f"/D={USER_DIR}"], timeout=900).returncode
    except OSError as e:
        if getattr(e, "winerror", None) == ERROR_ELEVATION_REQUIRED:
            say("  설치 파일이 관리자 권한을 요구합니다.")
            return "elevate"
        say(f"  실행 실패: {e}")
        return "fail"
    except subprocess.TimeoutExpired:
        say("  시간 초과")
        return "fail"
    time.sleep(2)
    return "ok" if rc == 0 and (USER_DIR / "tesseract.exe").exists() else "elevate"


def run_installer_admin(exe: Path) -> bool:
    """Windows 권한 확인(UAC) 창을 띄워 관리자 권한으로 무인 설치 (C:\\Program Files\\Tesseract-OCR)."""
    say("관리자 권한으로 설치합니다. 화면에 'Windows 권한 확인' 창이 뜨면 [예]를 누르세요.")
    ps = (f"$p = Start-Process -FilePath '{exe}' -ArgumentList '/S' -Verb RunAs -Wait -PassThru; "
          f"exit $p.ExitCode")
    try:
        rc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                            timeout=1200).returncode
    except Exception as e:  # noqa: BLE001
        say(f"  실행 실패: {e}")
        return False
    if rc != 0:
        say(f"  설치가 취소되었거나 실패했습니다 (코드 {rc}).")
    time.sleep(2)
    return find_tesseract() is not None


def run_winget() -> bool:
    if not shutil.which("winget"):
        say("  winget 이 없습니다.")
        return False
    say(f"winget 으로 설치: {WINGET_ID}  (권한 확인 창이 뜨면 [예])")
    cmd = ["winget", "install", "-e", "--id", WINGET_ID, "--silent",
           "--accept-package-agreements", "--accept-source-agreements"]
    try:
        rc = subprocess.run(cmd, timeout=1800).returncode
    except Exception as e:  # noqa: BLE001
        say(f"  실행 실패: {e}")
        return False
    say(f"  winget 종료 코드: {rc}")
    time.sleep(2)
    return find_tesseract() is not None


def run_installer_interactive(exe: Path) -> bool:
    say("설치 마법사를 엽니다. 안내에 따라 [Next]로 진행하세요 (설치 폴더는 기본값 그대로).")
    try:
        if os.name == "nt":
            os.startfile(str(exe))  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        say(f"  실행 실패: {e}")
        return False
    say("설치를 마친 뒤 GUI의 [환경 점검]을 눌러 확인하세요.")
    return False


def main() -> int:
    say("=== Tesseract OCR 설치 ===")
    cmd = find_tesseract()
    if cmd:
        say("이미 설치되어 있습니다.")
        return 0 if check_installed(cmd) else 1
    if os.name != "nt":
        say("Windows가 아닙니다. Linux: sudo apt install tesseract-ocr / macOS: brew install tesseract")
        return 1

    _ssl_setup()
    exe = Path(tempfile.gettempdir()) / "tesseract-ocr-w64-setup.exe"
    ok_dl = download(latest_installer_url(), exe)
    if not ok_dl:
        say("GitHub 다운로드가 막혀 있는 것 같습니다 → winget 으로 시도합니다.")
        if run_winget():
            cmd = find_tesseract()
            say("\n✔ 설치 완료")
            return 0 if cmd and check_installed(cmd) else 1
        say("\n✘ 자동 설치 실패. 아래 주소에서 직접 받아 설치하거나 IT팀에 'Tesseract OCR 설치'를 요청하세요.")
        say("   https://github.com/UB-Mannheim/tesseract/wiki")
        return 1

    res = run_installer_user(exe)
    if res != "ok":
        if not run_installer_admin(exe):
            say("관리자 설치가 되지 않았습니다 → winget 으로 시도합니다.")
            if not run_winget():
                run_installer_interactive(exe)
                say("\n✘ 자동 설치를 끝내지 못했습니다. 관리자 권한이 없으면 IT팀에 'Tesseract OCR 설치'를 요청하세요.")
                return 1
    cmd = find_tesseract()
    if cmd and check_installed(cmd):
        say("\n✔ Tesseract 설치 완료 — 이제 스캔/글자깨짐 페이지(목차 등)도 번역됩니다.")
        try:
            exe.unlink()
        except Exception:  # noqa: BLE001
            pass
        return 0
    say("\n✘ 설치 후에도 tesseract.exe 를 찾지 못했습니다. GUI의 [환경 점검] 결과를 보내 주세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
