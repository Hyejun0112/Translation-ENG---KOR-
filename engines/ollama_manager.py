# -*- coding: utf-8 -*-
"""
ollama_manager.py - Ollama 자동 준비
====================================
run.bat 실행 시 자동으로:
  1) Ollama 서버가 이미 켜져 있는지 확인 (http://127.0.0.1:11434/api/version)
  2) 꺼져 있으면 ollama.exe 를 찾아 'ollama serve' 실행 (창 없이)
  3) Ollama 가 설치되어 있지 않으면 무인 설치
       - https://ollama.com/download/OllamaSetup.exe 다운로드 → /VERYSILENT (관리자 권한 불필요, 사용자 폴더 설치)
       - 실패 시 winget install Ollama.Ollama
  4) 번역 모델(qwen2.5:14b)이 없으면 다운로드 (진행률/속도/남은 시간 표시, 중단 후 재실행하면 이어받음)
  5) 모델을 메모리에 미리 올려(warm-up) 첫 번역 지연 방지
  6) PC 메모리(RAM)가 부족하면 더 작은 모델(qwen2.5:7b) 사용을 제안
표준 라이브러리만 사용합니다.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"
WINGET_ID = "Ollama.Ollama"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED = 0x00000008 if os.name == "nt" else 0


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def _get(url: str, timeout: float = 5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def server_version(base: str) -> Optional[str]:
    d = _get(base.rstrip("/") + "/api/version", timeout=3)
    return d.get("version") if d else None


def list_models(base: str) -> list[str]:
    d = _get(base.rstrip("/") + "/api/tags", timeout=10)
    return [m.get("name", "") for m in (d or {}).get("models", [])]


def has_model(base: str, model: str) -> bool:
    names = list_models(base)
    want = model if ":" in model else model + ":latest"
    return want in names or model in names


# ----------------------------------------------------------------------------
# PC 사양
# ----------------------------------------------------------------------------
def system_ram_gb() -> float:
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
            return st.ullTotalPhys / 1024 ** 3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except Exception:  # noqa: BLE001
        return 0.0


def gpu_info() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        return r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    except Exception:  # noqa: BLE001
        return ""


def free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / 1024 ** 3
    except Exception:  # noqa: BLE001
        return 0.0


def models_dir() -> Path:
    env = os.environ.get("OLLAMA_MODELS")
    return Path(env) if env else Path.home() / ".ollama" / "models"


# ----------------------------------------------------------------------------
# 설치 / 실행
# ----------------------------------------------------------------------------
def find_exe() -> Optional[str]:
    w = shutil.which("ollama")
    if w:
        return w
    cands = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(r"C:\Program Files\Ollama\ollama.exe"),
        Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"),
    ]
    for c in cands:
        if str(c) and c.exists():
            return str(c)
    return None


def _ssl_setup():
    try:
        import truststore  # type: ignore
        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001
        pass


def _download(url: str, dst: Path) -> bool:
    print(f"  다운로드: {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "plant-om-translator"})
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
                    pct = f" ({done * 100 // total}%)" if total else ""
                    print(f"    {done / 1e6:.0f} MB{pct}      ", end="\r", flush=True)
        print(f"    완료 {done / 1e6:.0f} MB            ")
        return dst.stat().st_size > 10_000_000
    except Exception as e:  # noqa: BLE001
        print(f"  ! 다운로드 실패: {e}")
        return False


def install_ollama() -> bool:
    """Windows: 공식 설치 파일 무인 설치 → 실패 시 winget. 성공하면 True."""
    if os.name != "nt":
        print("  Windows 가 아닙니다. https://ollama.com/download 에서 설치하세요.")
        return False
    _ssl_setup()
    exe = Path(tempfile.gettempdir()) / "OllamaSetup.exe"
    if _download(OLLAMA_SETUP_URL, exe):
        print("  Ollama 설치 중... (1~2분, 관리자 권한 불필요)")
        try:
            subprocess.run([str(exe), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], timeout=900)
        except Exception as e:  # noqa: BLE001
            print(f"  ! 설치 실행 실패: {e}")
        for _ in range(30):
            if find_exe():
                break
            time.sleep(2)
        try:
            exe.unlink()
        except Exception:  # noqa: BLE001
            pass
        if find_exe():
            return True
    if shutil.which("winget"):
        print("  winget 으로 설치를 시도합니다... (권한 확인 창이 뜨면 [예])")
        subprocess.run(["winget", "install", "-e", "--id", WINGET_ID, "--silent",
                        "--accept-package-agreements", "--accept-source-agreements"], timeout=1800)
        for _ in range(15):
            if find_exe():
                return True
            time.sleep(2)
    return False


def start_server(base: str, exe: str, wait_sec: int = 60) -> bool:
    """ollama serve 를 창 없이 백그라운드로 실행하고 응답을 기다린다."""
    if server_version(base):
        return True
    env = dict(os.environ)
    host = base.replace("http://", "").replace("https://", "").rstrip("/")
    env.setdefault("OLLAMA_HOST", host)
    try:
        subprocess.Popen([exe, "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW | DETACHED,
                         close_fds=True)
    except Exception as e:  # noqa: BLE001
        print(f"  ! Ollama 실행 실패: {e}")
        return False
    for _ in range(wait_sec):
        if server_version(base):
            return True
        time.sleep(1)
    return False


def pull_model(base: str, model: str) -> bool:
    """모델 다운로드 (/api/pull 스트림). 진행률/속도/남은 시간 표시. 중단 후 다시 실행하면 이어받음."""
    print(f"  모델 다운로드: {model}  (처음 한 번, 약 9GB - 네트워크에 따라 10분~1시간)")
    req = urllib.request.Request(base.rstrip("/") + "/api/pull",
                                 data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    last_print = 0.0
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    print(f"\n  ! 다운로드 오류: {ev['error']}")
                    return False
                st = ev.get("status", "")
                total, done = ev.get("total") or 0, ev.get("completed") or 0
                if total and time.time() - last_print > 0.5:
                    last_print = time.time()
                    el = time.time() - t0
                    speed = done / el if el > 0 else 0
                    remain = (total - done) / speed if speed > 0 else 0
                    print(f"    {done / 1e9:.2f} / {total / 1e9:.2f} GB ({done * 100 // total}%) "
                          f"{speed / 1e6:.1f} MB/s, 남은 시간 약 {int(remain // 60)}분      ",
                          end="\r", flush=True)
                elif not total and st:
                    print(f"    {st[:70]:<70}", end="\r", flush=True)
                if st == "success":
                    print(f"\n  ✔ 모델 다운로드 완료 ({int((time.time() - t0) // 60)}분)")
                    return True
    except Exception as e:  # noqa: BLE001
        print(f"\n  ! 모델 다운로드 실패: {e}")
        print("    회사망에서 registry.ollama.ai 가 막혀 있을 수 있습니다. 다른 네트워크에서 run.bat 을 실행해 "
              "모델만 먼저 받아 두면, 이후에는 인터넷 없이 사용할 수 있습니다.")
        return False
    return has_model(base, model)


def warm_up(base: str, model: str) -> float:
    """모델을 메모리에 올린다. 걸린 시간(초) 반환."""
    t0 = time.time()
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/generate",
                                     data=json.dumps({"model": model, "prompt": "OK", "stream": False,
                                                      "keep_alive": "30m",
                                                      "options": {"num_predict": 1}}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        print(f"  ! 모델 로드 실패: {e}")
        return -1
    return time.time() - t0


# ----------------------------------------------------------------------------
# 전체 준비
# ----------------------------------------------------------------------------
def ensure_ollama(base: str, model: str, fallback_model: str, interactive: bool,
                  min_ram_gb: float = 15.0) -> Optional[str]:
    """Ollama 서버 + 모델 준비. 사용할 모델 이름을 반환, 실패하면 None."""
    ram = system_ram_gb()
    gpu = gpu_info()
    print(f"  PC 사양: RAM {ram:.0f} GB | GPU {gpu or '없음(CPU로 번역 - 느림)'}")

    # 1) 서버
    ver = server_version(base)
    if not ver:
        exe = find_exe()
        if not exe:
            print("  Ollama 가 설치되어 있지 않습니다.")
            if interactive:
                ans = input("  지금 자동 설치할까요? (Y/n): ").strip().lower()
                if ans not in ("", "y", "yes", "예", "ㅛ"):
                    return None
            if not install_ollama():
                print("  ✘ Ollama 자동 설치 실패. https://ollama.com/download 에서 직접 설치한 뒤 run.bat 을 다시 실행하세요.")
                return None
            exe = find_exe()
        print("  Ollama 서버 시작 중...")
        if not exe or not start_server(base, exe):
            print("  ✘ Ollama 서버를 시작하지 못했습니다. 작업표시줄 트레이의 Ollama 아이콘을 실행하거나 PC를 재시작해 보세요.")
            return None
        ver = server_version(base)
    print(f"  Ollama {ver} 실행 중 ({base})")

    # 2) 사양에 맞는 모델
    if "14b" in model and 0 < ram < min_ram_gb and not gpu:
        print(f"  ! RAM {ram:.0f}GB 로는 {model} 이(가) 매우 느리거나 실행되지 않을 수 있습니다 (16GB 이상 권장).")
        if interactive:
            ans = input(f"    더 작은 모델 {fallback_model} 을(를) 사용할까요? (Y/n): ").strip().lower()
            if ans in ("", "y", "yes", "예", "ㅛ"):
                model = fallback_model

    # 3) 모델 다운로드
    if not has_model(base, model):
        need = 10.0 if "14b" in model else 6.0
        target = models_dir() if models_dir().exists() else Path.home()
        free = free_disk_gb(target)
        if 0 < free < need + 2:
            print(f"  ! 디스크 여유 공간 {free:.0f}GB - 모델 저장에 약 {need:.0f}GB 필요합니다.")
        if not pull_model(base, model):
            return None
    # 4) 메모리에 로드
    print(f"  모델 불러오는 중: {model} ...", end="", flush=True)
    sec = warm_up(base, model)
    if sec < 0:
        return None
    print(f" 완료 ({sec:.0f}초)")
    return model


def status_lines(base: str, model: str) -> list[tuple[bool, str, str]]:
    """설치 점검용 (항목, 결과, 상세)."""
    out = []
    exe = find_exe()
    ver = server_version(base)
    out.append((bool(exe or ver), "Ollama 설치", exe or ("서버 응답" if ver else "미설치 → run.bat 이 자동 설치")))
    out.append((bool(ver), "Ollama 서버", f"{ver} ({base})" if ver else "꺼져 있음 → run.bat 이 자동 실행"))
    if ver:
        ok = has_model(base, model)
        out.append((ok, "번역 모델", model if ok else f"{model} 없음 → run.bat 이 자동 다운로드(약 9GB)"))
    ram = system_ram_gb()
    out.append((ram >= 15 or ram == 0, "RAM", f"{ram:.0f} GB (14B 모델은 16GB 이상 권장)"))
    gpu = gpu_info()
    out.append((True, "GPU", gpu or "없음 (CPU로 번역 - 느림)"))
    return out


if __name__ == "__main__":  # 단독 실행: 상태 출력
    b = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    for ok, name, msg in status_lines(b, "qwen2.5:14b"):
        print(f"[{'OK' if ok else '!!'}] {name:<12} {msg}")
    sys.exit(0)
