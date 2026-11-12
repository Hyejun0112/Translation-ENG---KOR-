#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py - Plant O&M Translator v2.0 화면(GUI)
============================================
run.bat 더블클릭 시 이 창이 열립니다.

  1) [PDF 추가] 또는 [폴더 추가]로 번역할 PDF 선택 (input\\pdf 폴더의 PDF는 자동으로 목록에 표시)
  2) 옵션 확인 (모델, 페이지 범위, 다시 만들기)
  3) [▶ 번역 시작] → 진행률 / 남은 시간 표시
  4) 끝나면 [한국어 PDF 열기] / [번역 로그 열기] / [결과 폴더]

실제 번역은 main.py(검증된 번역 파이프라인)를 백그라운드로 실행합니다.
설정은 cache\\gui_settings.json 에 저장됩니다.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

import config  # noqa: E402

IS_WIN = os.name == "nt"
SETTINGS = config.CACHE_DIR / "gui_settings.json"
MODELS = ["qwen2.5:14b", "qwen2.5:7b", "qwen2.5:32b"]
NO_WINDOW = 0x08000000 if IS_WIN else 0

RE_FILE = re.compile(r"^\[(\d+)/(\d+)\]\s+(.+?)\s*$")
RE_PAGE = re.compile(r"페이지 분석 (\d+)/(\d+)")
RE_OCR = re.compile(r"OCR (\d+)/(\d+)")
RE_TR = re.compile(r"번역 (\d+)/(\d+) 묶음 \((\d+)%\).*?남은 시간 약 (.+?)\s*$")


def open_path(p: Path):
    try:
        if IS_WIN:
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:  # noqa: BLE001
        messagebox.showerror("열기 실패", f"{p}\n\n{e}")


class App(tk.Tk):
    def __init__(self, initial_files: list[str] | None = None):
        super().__init__()
        self.title(f"{config.APP_TITLE}  -  O&M Manual 영→한 번역 (Ollama 로컬)")
        sh = self.winfo_screenheight()
        self.geometry(f"960x{max(600, min(760, sh - 90))}+60+30")
        self.minsize(820, 560)
        config.ensure_dirs()
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self.stopping = False
        self.t0 = 0.0
        self.files: list[str] = []
        self.done_files: list[str] = []
        self._style()
        self._build()
        self._load_settings()
        for f in initial_files or []:
            self._add(f)
        self.refresh_input_folder()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll)
        self.after(300, self.check_ollama_async)
        self.after(200, self._front)

    # ------------------------------------------------------------------ 화면
    def _style(self):
        st = ttk.Style(self)
        if IS_WIN and "vista" in st.theme_names():
            st.theme_use("vista")
        f = "Malgun Gothic" if IS_WIN else "TkDefaultFont"
        self.option_add("*Font", (f, 10))
        st.configure("Title.TLabel", font=(f, 14, "bold"))
        st.configure("Run.TButton", font=(f, 12, "bold"), padding=(18, 8))
        st.configure("Hint.TLabel", foreground="#666666")
        st.configure("Ok.TLabel", foreground="#1a7f37")
        st.configure("Bad.TLabel", foreground="#c62828")

    def _front(self):
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(700, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        head = ttk.Frame(root)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="발전플랜트 O&M Manual 영→한 번역", style="Title.TLabel").pack(side="left")
        self.v_ollama = tk.StringVar(value="Ollama 확인 중...")
        self.lbl_ollama = ttk.Label(head, textvariable=self.v_ollama, style="Hint.TLabel")
        self.lbl_ollama.pack(side="right")
        ttk.Label(root, text="원본 레이아웃(그림·표·도면) 그대로, O&M 문구만 한국어로 바꾼 PDF를 만듭니다. "
                             "번역은 이 PC에서만 실행됩니다(외부 전송 없음).",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 8))

        # 1. 파일
        f1 = ttk.LabelFrame(root, text=" 1. 번역할 PDF ", padding=8)
        f1.grid(row=2, column=0, sticky="nsew")
        root.rowconfigure(2, weight=1)
        f1.columnconfigure(0, weight=1)
        f1.rowconfigure(0, weight=1)
        self.lst = tk.Listbox(f1, height=6, selectmode="extended", activestyle="none")
        self.lst.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f1, orient="vertical", command=self.lst.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lst.configure(yscrollcommand=sb.set)
        bf = ttk.Frame(f1)
        bf.grid(row=0, column=2, sticky="n", padx=(8, 0))
        for i, (t, cmd) in enumerate([("PDF 추가…", self.add_files), ("폴더 추가…", self.add_folder),
                                      ("선택 삭제", self.remove_sel), ("모두 지우기", self.clear_files),
                                      ("input\\pdf 열기", lambda: open_path(config.INPUT_DIR))]):
            ttk.Button(bf, text=t, command=cmd, width=15).grid(row=i, column=0, pady=2, sticky="ew")

        # 2. 옵션
        f2 = ttk.LabelFrame(root, text=" 2. 옵션 ", padding=8)
        f2.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.v_model = tk.StringVar(value=config.DEFAULT_MODEL)
        self.v_pages = tk.StringVar(value="")
        self.v_force = tk.BooleanVar(value=False)
        self.v_scope = tk.BooleanVar(value=True)
        self.v_ocr = tk.BooleanVar(value=True)
        ttk.Label(f2, text="번역 모델").grid(row=0, column=0, sticky="w")
        ttk.Combobox(f2, textvariable=self.v_model, values=MODELS, width=16).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(f2, text="14b: 품질 우선 / 7b: 속도 우선", style="Hint.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Label(f2, text="페이지").grid(row=0, column=3, sticky="e", padx=(20, 0))
        ttk.Entry(f2, textvariable=self.v_pages, width=12).grid(row=0, column=4, sticky="w", padx=6)
        ttk.Label(f2, text="비우면 전체 (예: 1-5)", style="Hint.TLabel").grid(row=0, column=5, sticky="w")
        o = ttk.Frame(f2)
        o.grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Checkbutton(o, text="O&M 대상만 번역 (도면·데이터시트·부품목록·표 Tag/Value 열은 원문 유지)",
                        variable=self.v_scope).pack(side="left")
        ttk.Checkbutton(o, text="스캔 페이지 OCR", variable=self.v_ocr).pack(side="left", padx=14)
        ttk.Checkbutton(o, text="이미 번역된 파일도 다시 만들기", variable=self.v_force).pack(side="left")

        # 3. 실행
        f3 = ttk.Frame(root)
        f3.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.btn_run = ttk.Button(f3, text="▶  번역 시작", style="Run.TButton", command=self.start)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(f3, text="■ 중지", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(f3, text="결과 폴더", command=lambda: open_path(config.OUTPUT_DIR)).pack(side="right")
        ttk.Button(f3, text="번역 로그 열기", command=self.open_log).pack(side="right", padx=6)
        ttk.Button(f3, text="한국어 PDF 열기", command=self.open_pdf).pack(side="right")

        # 진행률
        f4 = ttk.Frame(root)
        f4.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        f4.columnconfigure(1, weight=1)
        ttk.Label(f4, text="전체").grid(row=0, column=0, sticky="w")
        self.pb_all = ttk.Progressbar(f4, maximum=100)
        self.pb_all.grid(row=0, column=1, sticky="ew", padx=6)
        self.v_all = tk.StringVar(value="")
        ttk.Label(f4, textvariable=self.v_all, width=26).grid(row=0, column=2, sticky="w")
        ttk.Label(f4, text="현재 파일").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.pb_cur = ttk.Progressbar(f4, maximum=100)
        self.pb_cur.grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        self.v_cur = tk.StringVar(value="")
        ttk.Label(f4, textvariable=self.v_cur, width=26).grid(row=1, column=2, sticky="w", pady=(4, 0))
        self.v_status = tk.StringVar(value="대기 중 - PDF를 추가하고 [번역 시작]을 누르세요.")
        ttk.Label(root, textvariable=self.v_status).grid(row=6, column=0, sticky="w", pady=(6, 0))

        # 로그
        f5 = ttk.LabelFrame(root, text=" 진행 기록 ", padding=4)
        f5.grid(row=7, column=0, sticky="nsew", pady=(6, 0))
        root.rowconfigure(7, weight=2)
        f5.columnconfigure(0, weight=1)
        f5.rowconfigure(0, weight=1)
        mono = ("Consolas", 9) if IS_WIN else ("TkFixedFont", 9)
        self.log = tk.Text(f5, height=8, wrap="word", font=mono, background="#1e1e1e", foreground="#e6e6e6")
        self.log.grid(row=0, column=0, sticky="nsew")
        ls = ttk.Scrollbar(f5, orient="vertical", command=self.log.yview)
        ls.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=ls.set, state="disabled")
        self.log.tag_configure("err", foreground="#ff8080")
        self.log.tag_configure("ok", foreground="#7ee787")

        tools = ttk.Frame(root)
        tools.grid(row=8, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(tools, text="설치 점검", command=self.check_env).pack(side="left")
        ttk.Button(tools, text="Tesseract(OCR) 설치", command=self.install_tesseract).pack(side="left", padx=6)
        ttk.Button(tools, text="로그 폴더", command=lambda: open_path(config.LOG_DIR)).pack(side="left")

    # ------------------------------------------------------------------ 파일 목록
    def _add(self, p: str):
        p = str(Path(p))
        if Path(p).is_dir():
            for f in sorted(Path(p).rglob("*.pdf")):
                self._add(str(f))
            return
        if p.lower().endswith(".pdf") and p not in self.files and not Path(p).stem.endswith(config.OUTPUT_SUFFIX):
            self.files.append(p)
            self.lst.insert("end", p)

    def refresh_input_folder(self):
        for f in sorted(config.INPUT_DIR.rglob("*.pdf")):
            if config.PROCESSED_DIRNAME not in f.parts:
                self._add(str(f))

    def add_files(self):
        init = str(Path(self.files[-1]).parent) if self.files else str(Path.home() / "Desktop")
        for p in filedialog.askopenfilenames(title="번역할 PDF 선택 (여러 개 가능)", initialdir=init,
                                             filetypes=[("PDF", "*.pdf")]):
            self._add(p)

    def add_folder(self):
        p = filedialog.askdirectory(title="PDF가 들어 있는 폴더 선택 (하위 폴더 포함)")
        if p:
            self._add(p)

    def remove_sel(self):
        for i in reversed(self.lst.curselection()):
            self.lst.delete(i)
            del self.files[i]

    def clear_files(self):
        self.lst.delete(0, "end")
        self.files.clear()

    # ------------------------------------------------------------------ 설정
    def _load_settings(self):
        try:
            d = json.loads(SETTINGS.read_text(encoding="utf-8"))
            self.v_model.set(d.get("model", config.DEFAULT_MODEL))
            self.v_scope.set(d.get("scope", True))
            self.v_ocr.set(d.get("ocr", True))
        except Exception:  # noqa: BLE001
            pass

    def _save_settings(self):
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps({"model": self.v_model.get(), "scope": self.v_scope.get(),
                                            "ocr": self.v_ocr.get()}, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ Ollama 상태
    def check_ollama_async(self):
        def work():
            try:
                from engines.ollama_manager import has_model, server_version, system_ram_gb
                ver = server_version(config.OLLAMA_URL)
                if not ver:
                    msg, ok = "Ollama 꺼짐 (번역 시작 시 자동 실행/설치)", False
                elif has_model(config.OLLAMA_URL, self.v_model.get()):
                    msg, ok = f"Ollama {ver} 준비됨 · {self.v_model.get()} · RAM {system_ram_gb():.0f}GB", True
                else:
                    msg, ok = f"Ollama {ver} · {self.v_model.get()} 모델 없음 (시작 시 자동 다운로드)", False
            except Exception as e:  # noqa: BLE001
                msg, ok = f"Ollama 확인 실패: {e}", False
            self.q.put(("ollama", (msg, ok)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ 실행
    def start(self):
        if self.running:
            return
        if not self.files:
            messagebox.showwarning("PDF 없음", "[PDF 추가]로 번역할 PDF를 선택하세요.")
            return
        self._save_settings()
        args = [sys.executable, "-u", str(BASE_DIR / "main.py"), "--once", "--no-open",
                "--model", self.v_model.get().strip() or config.DEFAULT_MODEL]
        if self.v_pages.get().strip():
            args += ["--pages", self.v_pages.get().strip()]
        if self.v_force.get():
            args += ["--force"]
        if not self.v_scope.get():
            args += ["--scope", "all"]
        if not self.v_ocr.get():
            args += ["--no-ocr"]
        args += self.files
        self._run(args, total_files=len(self.files))

    def _run(self, args: list[str], total_files: int = 0):
        self.running, self.stopping = True, False
        self.t0 = time.time()
        self.n_total = max(1, total_files)
        self.cur_idx = 0
        self.done_files = []
        self.pb_all["value"] = 0
        self.pb_cur["value"] = 0
        self.v_all.set("")
        self.v_cur.set("")
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._log_clear()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
                   OM_PACKAGES_INSTALLED="1")
        threading.Thread(target=self._worker, args=(args, env), daemon=True).start()

    def _worker(self, args, env):
        try:
            self.proc = subprocess.Popen(args, cwd=str(BASE_DIR), env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         creationflags=NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            self.q.put(("err", f"실행 실패: {e}"))
            self.q.put(("done", 1))
            return
        buf, cr = b"", False
        assert self.proc.stdout is not None
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:
                break
            if cr:
                cr = False
                if ch == b"\n":
                    self.q.put(("line", buf.decode("utf-8", "replace")))
                    buf = b""
                    continue
                if buf.strip():
                    self.q.put(("progress", buf.decode("utf-8", "replace").strip()))
                buf = b""
            if ch == b"\r":
                cr = True
            elif ch == b"\n":
                self.q.put(("line", buf.decode("utf-8", "replace")))
                buf = b""
            else:
                buf += ch
        if buf.strip():
            self.q.put(("line", buf.decode("utf-8", "replace")))
        rc = self.proc.wait()
        self.proc = None
        self.q.put(("done", rc))

    def stop(self):
        if self.proc is not None:
            self.stopping = True
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._log("중지했습니다. 번역된 문구는 저장되어 있어 다시 시작하면 이어서 진행합니다.", "err")

    # ------------------------------------------------------------------ 진행 표시
    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "line":
                    self._on_line(data)
                elif kind == "progress":
                    self._on_progress(data)
                elif kind == "ollama":
                    msg, ok = data
                    self.v_ollama.set(msg)
                    self.lbl_ollama.configure(style="Ok.TLabel" if ok else "Bad.TLabel")
                elif kind == "err":
                    self._log(data, "err")
                elif kind == "done":
                    self._on_done(data)
        except queue.Empty:
            pass
        if self.running:
            el = int(time.time() - self.t0)
            self.v_all.set(f"{self.cur_idx}/{self.n_total} 파일 · 경과 {el // 3600}:{el % 3600 // 60:02d}:{el % 60:02d}")
        self.after(150, self._poll)

    def _on_line(self, line: str):
        s = line.strip()
        if not s or set(s) <= set("=-"):
            return
        m = RE_FILE.match(s)
        if m:
            self.cur_idx, self.n_total = int(m.group(1)), int(m.group(2))
            self.pb_all["value"] = (self.cur_idx - 1) * 100 / self.n_total
            self.pb_cur["value"] = 0
            self.v_status.set(f"번역 중: {m.group(3)}")
        if s.startswith("✔ 완료"):
            self.pb_cur["value"] = 100
            self.pb_all["value"] = self.cur_idx * 100 / self.n_total
        if s.startswith("→") and s.endswith(".pdf"):
            self.done_files.append(s[1:].strip())
        tag = "err" if (s.startswith(("!", "✘", "Traceback")) or "Error" in s) else ("ok" if s.startswith("✔") else "")
        self._log(line.rstrip(), tag)

    def _on_progress(self, s: str):
        self.v_status.set(s)
        m = RE_TR.search(s)
        if m:
            self.pb_cur["value"] = 20 + int(m.group(3)) * 0.8
            self.v_cur.set(f"번역 {m.group(3)}% · 남은 {m.group(4)}")
            return
        m = RE_PAGE.search(s) or RE_OCR.search(s)
        if m:
            k, n = int(m.group(1)), int(m.group(2))
            self.pb_cur["value"] = k * 20 / max(1, n)
            self.v_cur.set(("페이지 분석 " if "페이지" in s else "OCR ") + f"{k}/{n}")

    def _on_done(self, rc: int):
        self.running = False
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        el = int(time.time() - self.t0)
        self.check_ollama_async()
        if self.stopping:
            self.v_status.set("중지됨")
            return
        if rc == 0:
            self.pb_all["value"] = 100
            self.pb_cur["value"] = 100
            self.v_status.set(f"완료 ({el // 60}분 {el % 60}초) - [한국어 PDF 열기]로 결과를 확인하세요.")
            self._log("✔ 모든 작업 완료", "ok")
            if self.done_files:
                if messagebox.askyesno("번역 완료", f"{len(self.done_files)}개 파일 번역이 끝났습니다.\n결과 폴더를 열까요?"):
                    open_path(config.OUTPUT_DIR)
        else:
            self.v_status.set("오류로 종료 - 진행 기록의 빨간 줄을 확인하세요.")
            messagebox.showerror("오류", "번역 중 오류가 발생했습니다.\n진행 기록(빨간 줄) 또는 [로그 폴더]의 로그를 확인하세요.")

    def _log(self, text: str, tag: str = ""):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ 결과 열기
    def _newest(self, pattern: str):
        fs = sorted(config.OUTPUT_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
        return fs[-1] if fs else None

    def open_pdf(self):
        f = self._newest(f"*{config.OUTPUT_SUFFIX}*.pdf")
        open_path(f) if f else messagebox.showinfo("결과 없음", "아직 만들어진 한국어 PDF가 없습니다.")

    def open_log(self):
        f = self._newest(f"*{config.LOG_SUFFIX}.xlsx")
        open_path(f) if f else messagebox.showinfo("결과 없음", "아직 만들어진 번역 로그가 없습니다.")

    # ------------------------------------------------------------------ 도구
    def check_env(self):
        if not self.running:
            self._run([sys.executable, "-u", str(BASE_DIR / "main.py"), "--check"])

    def install_tesseract(self):
        if not self.running:
            self._run([sys.executable, "-u", str(config.TOOLS_DIR / "install_tesseract.py")])

    def on_close(self):
        if self.running and not messagebox.askyesno("종료", "번역이 진행 중입니다. 중지하고 닫을까요?\n"
                                                         "(번역된 문구는 저장되어 다음에 이어서 진행)"):
            return
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.destroy()


def main():
    if IS_WIN:
        try:  # 고해상도 화면에서 글자 흐림 방지
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    App([a for a in sys.argv[1:] if not a.startswith("--")]).mainloop()


if __name__ == "__main__":
    main()
