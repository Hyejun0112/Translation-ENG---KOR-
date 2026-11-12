# -*- coding: utf-8 -*-
"""
OM_Translator.pyw - Plant O&M Translator v2.0 실행 파일 (더블클릭)
=================================================================
- 처음 실행: '처음 실행 준비' 창에서 프로그램 전용 Python 환경(.venv)을 만들고 필요한 부품을 설치 (1~3분)
- 이후 실행: 바로 번역 프로그램 창(gui.py)이 열림
- requirements.txt 가 바뀌면(업데이트 후) 자동으로 다시 설치
표준 라이브러리(tkinter)만 사용합니다. .pyw 파일은 검은 콘솔 창 없이 실행됩니다.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV = BASE / ".venv"
IS_WIN = os.name == "nt"
VPY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
VPYW = VENV / ("Scripts/pythonw.exe" if IS_WIN else "bin/python")
REQ = BASE / "requirements.txt"
STAMP = VENV / "requirements.sha256"
NO_WINDOW = 0x08000000 if IS_WIN else 0
CHECK = "import pymupdf, anthropic, pytesseract, cv2, numpy, PIL, fontTools, openpyxl"


def req_hash() -> str:
    return hashlib.sha256(REQ.read_bytes()).hexdigest() if REQ.exists() else ""


def in_project_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def venv_ready() -> bool:
    if not VPY.exists():
        return False
    if not STAMP.exists() or STAMP.read_text(encoding="utf-8").strip() != req_hash():
        return False
    r = subprocess.run([str(VPY), "-c", CHECK], capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def launch_gui_in_venv():
    exe = VPYW if VPYW.exists() else VPY
    subprocess.Popen([str(exe), str(BASE / "OM_Translator.pyw"), *sys.argv[1:]], cwd=str(BASE),
                     creationflags=0 if exe == VPYW else NO_WINDOW)


def run_gui_here():
    sys.path.insert(0, str(BASE))
    import gui
    gui.main()


def setup_window():
    import tkinter as tk
    from tkinter import ttk

    win = tk.Tk()
    win.title("Plant O&M Translator v2.0 - 처음 실행 준비")
    win.geometry("720x420")
    font = ("Malgun Gothic", 10) if IS_WIN else ("TkDefaultFont", 10)
    ttk.Label(win, text="처음 실행 준비 중입니다 (1~3분, 한 번만)", font=(font[0], 13, "bold")).pack(anchor="w", padx=14, pady=(14, 2))
    v = tk.StringVar(value="프로그램 전용 Python 환경을 만드는 중...")
    ttk.Label(win, textvariable=v, font=font).pack(anchor="w", padx=14)
    pb = ttk.Progressbar(win, mode="indeterminate")
    pb.pack(fill="x", padx=14, pady=8)
    pb.start(12)
    txt = tk.Text(win, height=14, font=("Consolas", 9) if IS_WIN else ("TkFixedFont", 9),
                  background="#1e1e1e", foreground="#e6e6e6")
    txt.pack(fill="both", expand=True, padx=14, pady=(0, 14))

    def log(s: str):
        win.after(0, lambda: (txt.insert("end", s), txt.see("end")))

    def fail(msg: str):
        def f():
            pb.stop()
            v.set(msg)
            ttk.Button(win, text="닫기", command=win.destroy).pack(pady=(0, 12))
        win.after(0, f)

    def run(cmd: list[str]) -> int:
        log("> " + " ".join(cmd) + "\n")
        p = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
        assert p.stdout is not None
        for line in p.stdout:
            log(line)
        return p.wait()

    def work():
        try:
            if not VPY.exists():
                base_py = sys.executable
                if IS_WIN and base_py.lower().endswith("pythonw.exe"):
                    cand = Path(base_py).with_name("python.exe")
                    base_py = str(cand) if cand.exists() else base_py
                if run([base_py, "-m", "venv", str(VENV)]) != 0 or not VPY.exists():
                    fail("✘ Python 환경을 만들지 못했습니다. Python 3.12 를 다시 설치해 보세요.")
                    return
            win.after(0, lambda: v.set("필요한 부품을 설치하는 중... (인터넷 필요)"))
            run([str(VPY), "-m", "pip", "install", "--upgrade", "pip", "-q"])
            if run([str(VPY), "-m", "pip", "install", "-r", str(REQ)]) != 0:
                fail("✘ 부품 설치 실패 - 인터넷 연결 / 회사 프록시를 확인하세요. (위 기록 참고)")
                return
            STAMP.write_text(req_hash(), encoding="utf-8")
            win.after(0, lambda: v.set("✔ 준비 완료 - 번역 프로그램을 엽니다."))
            launch_gui_in_venv()
            win.after(1200, win.destroy)
        except Exception as e:  # noqa: BLE001
            fail(f"✘ 준비 실패: {e}")

    threading.Thread(target=work, daemon=True).start()
    win.mainloop()


def main():
    if in_project_venv():
        run_gui_here()
        return
    if venv_ready():
        launch_gui_in_venv()
        return
    setup_window()


if __name__ == "__main__":
    main()
