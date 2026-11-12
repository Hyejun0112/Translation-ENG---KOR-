#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
om_gui.py - OM 영→한 번역기 GUI (API 없이 Copilot으로 번역)
============================================================
더블클릭: "OM번역기 실행.bat"  (또는 VS Code에서 이 파일 ▶ 실행)

[설정] 탭       원본 PDF 첨부(여러 파일/폴더), 용어집·영어유지패턴·수정목록, 옵션
[Copilot 번역]  ① 번역할 문장 추출 → 프롬프트 복사 → Copilot에 붙여넣기 → 답변 붙여넣기/저장
                ② 모든 배치 완료 후 ③ 한국어 PDF 만들기
[실행 로그]     진행 상황, 오류 (결과 폴더의 last_run.log 에도 저장)

결과물: <원본파일명>_KR_O&M.pdf, <원본파일명>_Translation_Log.xlsx, Plant_O&M_Glossary.xlsx
마지막 설정은 om_gui_settings.json 에 저장 (API 키는 저장하지 않음)
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    import bootstrap  # 패키지가 없으면 .venv 생성/설치 후 다시 실행

    bootstrap.ensure()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:  # 파일 끌어다 놓기(드래그 앤 드롭) 지원 - 설치되어 있으면 사용
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    _BaseTk = TkinterDnD.Tk
except Exception:  # noqa: BLE001
    DND_FILES = None
    _BaseTk = tk.Tk

SETTINGS = ROOT / "om_gui_settings.json"
IS_WIN = os.name == "nt"

MODES = [
    ("build", "한국어 PDF 만들기  (Copilot 번역 결과 반영 · API 없음)"),
    ("estimate", "분량 추정 + 페이지 진단  (번역 범위/스캔 페이지 확인)"),
    ("ocr", "OCR 결과 확인  (PDF 1개, 한 페이지, 인식 박스 PNG)"),
    ("mock", "레이아웃 테스트  (번역 안 함 · 글자 앞에 '[번역]' 표시 · 납품용 아님)"),
    ("api", "API 자동 번역  (선택 · 유료 · Anthropic API 키 필요)"),
]
MODE_LABELS = [m[1] for m in MODES]
MODE_KEYS = {m[1]: m[0] for m in MODES}
BATCH_SIZES = ["3000", "4000", "6000", "8000", "12000"]


def open_path(p: Path):
    """Windows 탐색기/기본 프로그램(Bluebeam, Excel 등)으로 연다."""
    try:
        if IS_WIN:
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:  # noqa: BLE001
        messagebox.showerror("열기 실패", f"{p}\n\n{e}")


class App(_BaseTk):
    def __init__(self):
        super().__init__()
        self.title("OM 영→한 번역기  (PDF 양식 유지 · O&M 대상만 번역)")
        sh = self.winfo_screenheight()
        self.geometry(f"1040x{max(620, min(800, sh - 90))}+40+20")
        self.minsize(900, 600)
        self.proc: subprocess.Popen | None = None
        self.stop_flag = False
        self.q: queue.Queue = queue.Queue()
        self.files: list[str] = []
        self.started = 0.0
        self.last_outdir: Path | None = None
        self._logf = None
        self._on_done = None  # 작업 종료 후 호출할 함수
        self.cp_batches: list[dict] = []
        self.cp_man: dict | None = None

        self._style()
        self._build()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(200, self._bring_to_front)
        self.after(400, self.cp_reload)
        self.after(600, lambda: self._tesseract_ok() and self.btn_tess.configure(text="Tesseract 설치됨 ✔"))

    def _bring_to_front(self):
        """VS Code/콘솔에서 실행하면 창이 뒤에 숨는 경우가 있어 맨 앞으로 올린다."""
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.focus_force()
            self.after(800, lambda: self.attributes("-topmost", False))
        except Exception:  # noqa: BLE001
            pass

    # ================================================================== UI
    def _style(self):
        st = ttk.Style(self)
        if IS_WIN and "vista" in st.theme_names():
            st.theme_use("vista")
        self.base_font = "Malgun Gothic" if IS_WIN else "TkDefaultFont"
        self.option_add("*Font", (self.base_font, 10))
        st.configure("Title.TLabel", font=(self.base_font, 13, "bold"))
        st.configure("Run.TButton", font=(self.base_font, 11, "bold"), padding=(16, 6))
        st.configure("Big.TButton", font=(self.base_font, 10, "bold"), padding=(10, 4))
        st.configure("Hint.TLabel", foreground="#666666")
        st.configure("Ok.TLabel", foreground="#1a7f37")

    def _build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="both", expand=True)
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="OM 영→한 번역기", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(top, text="원본 PDF 양식(그림·표·도면·배경) 그대로, O&M 대상 문구만 한국어로 교체한 개별 PDF를 만듭니다.",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))

        self.nb = ttk.Notebook(top)
        self.nb.grid(row=2, column=0, sticky="nsew", pady=(0, 4))
        top.rowconfigure(2, weight=1)
        self.tab_set = ttk.Frame(self.nb, padding=4)
        self.tab_cp = ttk.Frame(self.nb, padding=6)
        self.tab_log = ttk.Frame(self.nb, padding=4)
        self.nb.add(self.tab_set, text="  1. 설정  ")
        self.nb.add(self.tab_cp, text="  2. Copilot 번역  ")
        self.nb.add(self.tab_log, text="  실행 로그  ")
        self._build_settings(self.tab_set)
        self._build_copilot(self.tab_cp)
        self._build_log(self.tab_log)

        fb = ttk.Frame(top)
        fb.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 2))
        self.btn_run = ttk.Button(fb, text="▶  실행", style="Run.TButton", command=self.run)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(fb, text="■ 중지", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(fb, text="결과 폴더", command=self.open_out).pack(side="right")
        ttk.Button(fb, text="번역 로그(Excel)", command=self.open_review).pack(side="right", padx=6)
        ttk.Button(fb, text="한국어 PDF 열기", command=self.open_result).pack(side="right")
        ttk.Button(fb, text="환경 점검", command=self.check_env).pack(side="right", padx=6)
        self.btn_tess = ttk.Button(fb, text="Tesseract 설치", command=self.install_tesseract)
        self.btn_tess.pack(side="right")
        self.v_status = tk.StringVar(value="대기 중")
        ttk.Label(top, textvariable=self.v_status, style="Hint.TLabel").grid(row=4, column=0, sticky="w", padx=8)
        self.pbar = ttk.Progressbar(top, mode="indeterminate")
        self.pbar.grid(row=5, column=0, sticky="ew", padx=8, pady=(2, 0))

    # ------------------------------------------------------------------ 설정 탭
    def _build_settings(self, tab):
        pad = {"padx": 8, "pady": 4}
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        f1 = ttk.LabelFrame(tab, text=" 원본 PDF 첨부 ", padding=6)
        f1.grid(row=0, column=0, sticky="nsew", **pad)
        f1.columnconfigure(0, weight=1)
        f1.rowconfigure(0, weight=1)
        self.lst = tk.Listbox(f1, height=5, selectmode="extended", activestyle="none")
        self.lst.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f1, orient="vertical", command=self.lst.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lst.configure(yscrollcommand=sb.set)
        bf = ttk.Frame(f1)
        bf.grid(row=0, column=2, sticky="n", padx=(8, 0))
        ttk.Button(bf, text="PDF 파일 추가…", command=self.add_files).grid(row=0, column=0, sticky="ew")
        ttk.Button(bf, text="폴더 추가…", command=self.add_folder).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(bf, text="선택 삭제", command=self.remove_sel).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bf, text="모두 지우기", command=self.clear_files).grid(row=1, column=1, sticky="ew",
                                                                     padx=(4, 0), pady=(4, 0))
        ttk.Label(bf, text="여러 파일 선택 가능", style="Hint.TLabel").grid(row=2, column=0, columnspan=2,
                                                                     sticky="w", pady=(6, 0))
        self._try_enable_dnd()

        f2 = ttk.LabelFrame(tab, text=" 참고 파일 ", padding=6)
        f2.grid(row=1, column=0, sticky="ew", **pad)
        f2.columnconfigure(1, weight=1)
        self.v_gloss = tk.StringVar()
        self.v_keep = tk.StringVar()
        self.v_over = tk.StringVar()
        self.v_out = tk.StringVar(value=str(ROOT / "OM_KO"))
        rows = [
            ("용어집 (csv/xlsx)", self.v_gloss, [("용어집", "*.csv *.xlsx *.xlsm *.txt"), ("모든 파일", "*.*")]),
            ("영어 유지 패턴", self.v_keep, [("텍스트", "*.txt"), ("모든 파일", "*.*")]),
            ("수정 목록 (Translation_Log.xlsx)", self.v_over, [("수정 목록", "*.xlsx *.csv"), ("모든 파일", "*.*")]),
        ]
        for i, (lab, var, ft) in enumerate(rows):
            ttk.Label(f2, text=lab).grid(row=i, column=0, sticky="w")
            ttk.Entry(f2, textvariable=var).grid(row=i, column=1, sticky="ew", padx=6, pady=2)
            ttk.Button(f2, text="찾기…", width=7,
                       command=lambda v=var, t=ft: self.pick_file(v, t)).grid(row=i, column=2)
            ttk.Button(f2, text="✕", width=3, command=lambda v=var: v.set("")).grid(row=i, column=3, padx=(2, 0))
        ttk.Label(f2, text="결과 저장 폴더").grid(row=3, column=0, sticky="w")
        ttk.Entry(f2, textvariable=self.v_out).grid(row=3, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(f2, text="찾기…", width=7, command=self.pick_out).grid(row=3, column=2)

        f3 = ttk.LabelFrame(tab, text=" 작업 / 옵션 ", padding=6)
        f3.grid(row=2, column=0, sticky="ew", **pad)
        for c in (1, 3, 5):
            f3.columnconfigure(c, weight=1)
        self.v_mode = tk.StringVar(value=MODE_LABELS[0])
        ttk.Label(f3, text="작업 (▶ 실행)").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(f3, textvariable=self.v_mode, values=MODE_LABELS, state="readonly", width=56)
        cb.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6, pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._mode_changed())
        self.v_pages = tk.StringVar(value="")
        ttk.Label(f3, text="페이지").grid(row=0, column=4, sticky="e")
        self.ent_pages = ttk.Entry(f3, textvariable=self.v_pages, width=12)
        self.ent_pages.grid(row=0, column=5, sticky="w", padx=6)

        self.v_ocr = tk.StringVar(value="auto")
        self.v_dpi = tk.StringVar(value="400")
        self.v_scope = tk.BooleanVar(value=True)
        self.v_fig = tk.BooleanVar(value=False)
        self.v_cmp = tk.BooleanVar(value=False)
        self.v_force = tk.BooleanVar(value=True)
        ttk.Label(f3, text="OCR").grid(row=1, column=0, sticky="w")
        ttk.Combobox(f3, textvariable=self.v_ocr, values=["auto", "force", "off"], state="readonly",
                     width=8).grid(row=1, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(f3, text="OCR 해상도(dpi)").grid(row=1, column=2, sticky="e")
        ttk.Combobox(f3, textvariable=self.v_dpi, values=["300", "400", "500", "600"],
                     width=6).grid(row=1, column=3, sticky="w", padx=6)
        opt = ttk.Frame(f3)
        opt.grid(row=2, column=0, columnspan=6, sticky="w", pady=(2, 0))
        ttk.Checkbutton(opt, text="O&M 대상만 번역 (데이터시트·부품목록·도면·표의 Tag/Value/Unit 열은 원문 유지)",
                        variable=self.v_scope).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(opt, text="그림(사진) 속 글자도 번역", variable=self.v_fig).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(opt, text="기존 결과 덮어쓰기", variable=self.v_force).grid(row=1, column=1, sticky="w", padx=14)
        ttk.Checkbutton(opt, text="검수용 비교본(원본|번역)도 만들기", variable=self.v_cmp).grid(row=1, column=2, sticky="w")
        ttk.Label(f3, text="페이지: 비우면 전체 (예: 1-5,12) · OCR auto: 스캔/글자깨짐 페이지만 OCR (Tesseract 필요)",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=6, sticky="w")

        # API (선택)
        self.v_key = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.v_model = tk.StringVar(value=os.environ.get("OM_MODEL", "claude-sonnet-5-5"))
        self.v_show = tk.BooleanVar(value=False)
        api = ttk.Frame(f3)
        api.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(6, 0))
        api.columnconfigure(1, weight=1)
        ttk.Label(api, text="API 키(선택)").grid(row=0, column=0, sticky="w")
        self.ent_key = ttk.Entry(api, textvariable=self.v_key, show="•")
        self.ent_key.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Checkbutton(api, text="보기", variable=self.v_show,
                        command=lambda: self.ent_key.configure(show="" if self.v_show.get() else "•")).grid(row=0, column=2)
        ttk.Button(api, text="이 PC에 저장", command=self.save_key).grid(row=0, column=3, padx=(6, 12))
        ttk.Label(api, text="모델").grid(row=0, column=4, sticky="w")
        ttk.Entry(api, textvariable=self.v_model, width=20).grid(row=0, column=5, sticky="w", padx=6)
        ttk.Label(f3, text="API 키는 'API 자동 번역'에만 필요합니다. Copilot 번역에는 필요 없습니다.",
                  style="Hint.TLabel").grid(row=5, column=0, columnspan=6, sticky="w")

    # ------------------------------------------------------------------ Copilot 탭
    def _build_copilot(self, tab):
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(2, weight=1)
        guide = ("① [번역할 문장 추출]  →  ② 배치를 고르고 [프롬프트 복사] → Copilot 채팅에 붙여넣기 → Copilot 답변 전체를 복사해 "
                 "아래 칸에 붙여넣고 [답변 저장]  (배치마다 반복)  →  ③ [한국어 PDF 만들기]")
        ttk.Label(tab, text=guide, style="Hint.TLabel", wraplength=960, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(bar, text="① 번역할 문장 추출", style="Big.TButton", command=self.cp_export).pack(side="left")
        ttk.Label(bar, text="  배치 크기(글자)").pack(side="left")
        self.v_bsize = tk.StringVar(value="6000")
        ttk.Combobox(bar, textvariable=self.v_bsize, values=BATCH_SIZES, width=7).pack(side="left", padx=4)
        ttk.Label(bar, text="(Copilot이 길다고 거부하면 줄이세요)", style="Hint.TLabel").pack(side="left")
        ttk.Button(bar, text="③ 한국어 PDF 만들기", style="Big.TButton", command=self.cp_build).pack(side="right")
        ttk.Button(bar, text="작업 폴더", command=self.cp_open_folder).pack(side="right", padx=6)

        left = ttk.Frame(tab)
        left.grid(row=2, column=0, sticky="nsw", padx=(0, 8))
        left.rowconfigure(1, weight=1)
        self.v_cp_total = tk.StringVar(value="배치 없음")
        ttk.Label(left, textvariable=self.v_cp_total, style="Ok.TLabel").grid(row=0, column=0, sticky="w")
        self.cp_list = tk.Listbox(left, width=24, activestyle="none", exportselection=False)
        self.cp_list.grid(row=1, column=0, sticky="nsew")
        self.cp_list.bind("<<ListboxSelect>>", lambda e: self.cp_show())

        right = ttk.Frame(tab)
        right.grid(row=2, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(3, weight=1)
        hp = ttk.Frame(right)
        hp.grid(row=0, column=0, sticky="ew")
        self.v_cp_head = tk.StringVar(value="Copilot에 보낼 프롬프트")
        ttk.Label(hp, textvariable=self.v_cp_head).pack(side="left")
        ttk.Button(hp, text="📋 프롬프트 복사", style="Big.TButton", command=self.cp_copy).pack(side="right")
        mono = ("Consolas", 9) if IS_WIN else ("TkFixedFont", 9)
        self.cp_prompt = tk.Text(right, height=8, wrap="word", font=mono, background="#f6f8fa")
        self.cp_prompt.grid(row=1, column=0, sticky="nsew")
        ha = ttk.Frame(right)
        ha.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(ha, text="Copilot 답변 붙여넣기 (Ctrl+V)").pack(side="left")
        ttk.Button(ha, text="✔ 답변 저장", style="Big.TButton", command=self.cp_save).pack(side="right")
        ttk.Button(ha, text="지우기", command=lambda: self.cp_answer.delete("1.0", "end")).pack(side="right", padx=6)
        ttk.Button(ha, text="클립보드에서 붙여넣기", command=self.cp_paste).pack(side="right")
        self.cp_answer = tk.Text(right, height=8, wrap="word", font=mono)
        self.cp_answer.grid(row=3, column=0, sticky="nsew")
        self.v_cp_result = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.v_cp_result, wraplength=700, justify="left").grid(
            row=4, column=0, sticky="w", pady=(4, 0))

    # ------------------------------------------------------------------ 로그 탭
    def _build_log(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        mono = ("Consolas", 9) if IS_WIN else ("TkFixedFont", 9)
        self.log = tk.Text(tab, height=10, wrap="word", font=mono, background="#1e1e1e",
                           foreground="#e6e6e6", insertbackground="#e6e6e6")
        self.log.grid(row=0, column=0, sticky="nsew")
        ls = ttk.Scrollbar(tab, orient="vertical", command=self.log.yview)
        ls.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=ls.set, state="disabled")
        self.log.tag_configure("err", foreground="#ff8080")
        self.log.tag_configure("ok", foreground="#7ee787")
        self.log.tag_configure("cmd", foreground="#79c0ff")

    def _try_enable_dnd(self):
        if DND_FILES is None:
            return
        try:
            self.lst.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            self.lst.dnd_bind("<<Drop>>", lambda e: self._add_paths(self.tk.splitlist(e.data)))  # type: ignore
        except Exception:  # noqa: BLE001
            pass

    # ================================================================== 파일 선택
    def _add_paths(self, paths):
        for p in paths:
            p = str(Path(p))
            if p not in self.files and (Path(p).is_dir() or p.lower().endswith(".pdf")):
                self.files.append(p)
        self._refresh_list()

    def _refresh_list(self):
        self.lst.delete(0, "end")
        for p in self.files:
            self.lst.insert("end", ("[폴더] " if Path(p).is_dir() else "") + p)
        if not self.files and DND_FILES is not None:
            self.lst.insert("end", "  여기로 PDF 파일/폴더를 끌어다 놓거나 오른쪽 버튼으로 추가하세요")

    def add_files(self):
        ps = filedialog.askopenfilenames(title="원본 PDF 선택 (여러 개 선택 가능)", initialdir=self._init_dir(),
                                         filetypes=[("PDF", "*.pdf"), ("모든 파일", "*.*")])
        self._add_paths(ps)

    def add_folder(self):
        p = filedialog.askdirectory(title="원본 PDF 폴더 선택 (하위 폴더 포함)", initialdir=self._init_dir())
        if p:
            self._add_paths([p])

    def remove_sel(self):
        for i in reversed(self.lst.curselection()):
            if i < len(self.files):
                del self.files[i]
        self._refresh_list()

    def clear_files(self):
        self.files.clear()
        self._refresh_list()

    def _init_dir(self):
        if self.files:
            p = Path(self.files[-1])
            return str(p if p.is_dir() else p.parent)
        return str(ROOT / "OM_EN") if (ROOT / "OM_EN").exists() else str(Path.home())

    def pick_file(self, var: tk.StringVar, ft):
        p = filedialog.askopenfilename(title="파일 선택", initialdir=str(ROOT), filetypes=ft)
        if p:
            var.set(p)

    def pick_out(self):
        p = filedialog.askdirectory(title="결과 저장 폴더", initialdir=self.v_out.get() or str(ROOT))
        if p:
            self.v_out.set(p)
            self.cp_reload()

    def _mode_changed(self):
        k = MODE_KEYS[self.v_mode.get()]
        if k == "ocr" and not self.v_pages.get().isdigit():
            self.v_pages.set("1")

    # ================================================================== 설정 저장
    def _vars(self):
        return {"gloss": self.v_gloss, "keep": self.v_keep, "over": self.v_over, "out": self.v_out,
                "mode": self.v_mode, "pages": self.v_pages, "ocr": self.v_ocr, "fig": self.v_fig,
                "cmp": self.v_cmp, "force": self.v_force, "dpi": self.v_dpi, "scope": self.v_scope,
                "model": self.v_model, "bsize": self.v_bsize}

    def _load_settings(self):
        try:
            d = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        if d.get("ver") != 2:  # 예전 설정(비교본 기본 켜짐 등)은 옵션을 새 기본값으로
            for k in ("mode", "cmp", "fig", "pages"):
                d.pop(k, None)
        for k, var in self._vars().items():
            if k in d:
                try:
                    var.set(d[k])
                except Exception:  # noqa: BLE001
                    pass
        self.files = [p for p in d.get("files", []) if Path(p).exists()]
        self._refresh_list()
        g = self.v_gloss.get()
        if not g or not Path(g).exists() or Path(g).name == "glossary_sample.csv":
            for name in ("glossary.csv", "glossary.xlsx", "glossary_sample.csv"):
                if (ROOT / name).exists():
                    self.v_gloss.set(str(ROOT / name))
                    break
        if not self.v_keep.get() and (ROOT / "keep_patterns.txt").exists():
            self.v_keep.set(str(ROOT / "keep_patterns.txt"))
        if self.v_mode.get() not in MODE_LABELS:
            self.v_mode.set(MODE_LABELS[0])

    def _save_settings(self):
        d = {k: v.get() for k, v in self._vars().items()}
        d["files"] = self.files
        d["ver"] = 2
        try:
            SETTINGS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def save_key(self):
        key = self.v_key.get().strip()
        if not key:
            messagebox.showwarning("API 키", "API 키를 입력하세요.")
            return
        os.environ["ANTHROPIC_API_KEY"] = key
        if IS_WIN:
            r = subprocess.run(["setx", "ANTHROPIC_API_KEY", key], capture_output=True, text=True)
            if r.returncode == 0:
                messagebox.showinfo("API 키", "이 PC 사용자 환경변수에 저장했습니다.")
            else:
                messagebox.showerror("API 키", r.stderr or r.stdout)
        else:
            messagebox.showinfo("API 키", "이번 실행에만 적용됩니다.")

    # ================================================================== 명령 구성
    def _outdir(self) -> Path:
        return Path(self.v_out.get().strip() or ROOT / "OM_KO")

    def _cache_args(self) -> list[str]:
        return ["--cache", str(self._outdir() / "translation_cache.sqlite")]

    def _common(self) -> list[str]:
        a: list[str] = []
        if self.v_gloss.get().strip():
            a += ["--glossary", self.v_gloss.get().strip()]
        if self.v_keep.get().strip():
            a += ["--keep-patterns", self.v_keep.get().strip()]
        if self.v_over.get().strip():
            a += ["--overrides", self.v_over.get().strip()]
        a += ["--ocr", self.v_ocr.get(), "--ocr-dpi", self.v_dpi.get() or "400",
              "--scope", "om" if self.v_scope.get() else "all"]
        if self.v_fig.get():
            a += ["--ocr-figures"]
        pages = self.v_pages.get().strip()
        if pages:
            a += ["--pages", pages]
        return a

    def build_commands(self, mode: str) -> list[tuple[list[str], str]]:
        out = self._outdir()
        py = sys.executable
        script = str(ROOT / "om_translate.py")
        if mode == "ocr":
            pdfs = [p for p in self.files if p.lower().endswith(".pdf")]
            if not pdfs:
                raise ValueError("OCR 확인은 PDF 파일 1개를 첨부해야 합니다. (폴더 불가)")
            page = self.v_pages.get().strip()
            page = page if page.isdigit() else "1"
            dbg = out / "OCR_확인"
            dbg.mkdir(parents=True, exist_ok=True)
            self.last_outdir = dbg
            return [([py, "-u", str(ROOT / "om_ocr.py"), pdfs[0], "--page", page, "--debug",
                      "--dpi", self.v_dpi.get() or "400"], str(dbg))]
        if mode == "estimate":
            self.last_outdir = out
            return [([py, "-u", script, "estimate", src, *self._common(), *self._cache_args()], str(ROOT))
                    for src in self.files]
        if mode == "export":
            self.last_outdir = out
            return [([py, "-u", script, "export", *self.files, "-o", str(out), *self._common(),
                      *self._cache_args(), "--batch-chars", self.v_bsize.get() or "6000"], str(ROOT))]
        # translate (build / mock / api)
        o = out / "레이아웃_테스트" if mode == "mock" else out
        a = [py, "-u", script, "translate", *self.files, "-o", str(o), *self._common(), *self._cache_args(),
             "--backend", {"build": "memory", "mock": "mock", "api": "anthropic"}[mode],
             "--model", self.v_model.get().strip() or "claude-sonnet-5-5"]
        if self.v_cmp.get():
            a += ["--compare"]
        if self.v_force.get():
            a += ["--force"]
        self.last_outdir = o
        return [(a, str(ROOT))]

    # ================================================================== 실행
    def run(self, mode: str | None = None, on_done=None):
        if self.proc is not None or self.btn_stop.instate(["!disabled"]):
            return
        mode = mode or MODE_KEYS[self.v_mode.get()]
        if not self.files:
            messagebox.showwarning("첨부 파일 없음", "'1. 설정' 탭에서 원본 PDF 파일(또는 폴더)을 먼저 추가하세요.")
            self.nb.select(self.tab_set)
            return
        if mode == "build" and not self._cp_ready_check():
            return
        if mode == "api" and not self.v_key.get().strip():
            messagebox.showwarning("API 키 필요", "API 자동 번역에는 Anthropic API 키가 필요합니다.\n"
                                               "API 없이 번역하려면 '2. Copilot 번역' 탭을 사용하세요.")
            return
        if mode in ("build", "export", "api", "estimate") and self.v_ocr.get() != "off" and not self._tesseract_ok():
            ans = messagebox.askyesnocancel(
                "Tesseract 없음",
                "Tesseract OCR이 설치되어 있지 않습니다.\n"
                "스캔 페이지 / 글자가 깨진 페이지(목차 등)는 번역되지 않고 영어로 남습니다.\n\n"
                "[예] 지금 자동 설치 (권장)\n[아니오] 설치하지 않고 계속\n[취소] 중단")
            if ans is None:
                return
            if ans:
                self.install_tesseract()
                return
        try:
            self._outdir().mkdir(parents=True, exist_ok=True)
            cmds = self.build_commands(mode)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("설정 오류", str(e))
            return
        self._save_settings()
        self._clear_log()
        self.nb.select(self.tab_log)
        self.stop_flag = False
        self.started = time.time()
        self._on_done = on_done
        self.cur_mode = mode
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.pbar.start(12)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", OM_BOOTSTRAPPED="1",
                   ANTHROPIC_API_KEY=self.v_key.get().strip(), PYTHONUNBUFFERED="1")
        try:
            logp = self._outdir() / "last_run.log"
            self._logf = open(logp, "w", encoding="utf-8")
            self._logf.write(time.strftime("%Y-%m-%d %H:%M:%S") + f"  python={sys.executable}\n")
        except Exception:  # noqa: BLE001
            self._logf = None
        threading.Thread(target=self._worker, args=(cmds, env), daemon=True).start()

    def _tesseract_ok(self) -> bool:
        try:
            from om_ocr import OcrEngine
            return bool(OcrEngine.find_tesseract(None))
        except Exception:  # noqa: BLE001
            return False

    def _logfile_write(self, text: str):
        try:
            if self._logf is not None:
                self._logf.write(text)
                self._logf.flush()
        except Exception:  # noqa: BLE001
            pass

    def _worker(self, cmds, env):
        rc_all = 0
        for i, (cmd, cwd) in enumerate(cmds, 1):
            if self.stop_flag:
                break
            shown = " ".join(f'"{c}"' if " " in c else c for c in cmd[2:])
            self.q.put(("cmd", f"[{i}/{len(cmds)}] python {shown}\n"))
            self._logfile_write(f"[{i}/{len(cmds)}] {' '.join(cmd)}\n")
            flags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
            try:
                self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             creationflags=flags)
            except Exception as e:  # noqa: BLE001
                self.q.put(("err", f"실행 실패: {e}\n"))
                rc_all = 1
                break
            buf = b""
            pending_cr = False  # Windows 줄바꿈(\r\n)과 진행률 표시(\r) 구분
            assert self.proc.stdout is not None

            def emit_line(raw: bytes):
                line = raw.decode("utf-8", "replace")
                low = line.lstrip()
                tag = "err" if (low.startswith(("!", "Traceback", "Error", "File ")) or "Error:" in line) else "out"
                self.q.put((tag, line + "\n"))
                self._logfile_write(line + "\n")

            while True:
                ch = self.proc.stdout.read(1)
                if not ch:
                    break
                if pending_cr:
                    pending_cr = False
                    if ch == b"\n":
                        emit_line(buf)
                        buf = b""
                        continue
                    if buf.strip():
                        self.q.put(("status", buf.decode("utf-8", "replace").strip()))
                    buf = b""
                if ch == b"\r":
                    pending_cr = True
                elif ch == b"\n":
                    emit_line(buf)
                    buf = b""
                else:
                    buf += ch
            if buf.strip():
                emit_line(buf)
            rc = self.proc.wait()
            rc_all = rc_all or rc
            self.proc = None
        self.q.put(("done", rc_all))

    def stop(self):
        self.stop_flag = True
        p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._append("err", "\n사용자가 중지했습니다. (다시 실행하면 캐시 덕분에 이어서 진행됩니다)\n")

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "status":
                    self.v_status.set(data)
                elif kind == "done":
                    self._finished(data)
                else:
                    self._append(kind, data)
        except queue.Empty:
            pass
        if self.btn_stop.instate(["!disabled"]):
            el = int(time.time() - self.started)
            if not self.v_status.get().startswith("["):
                self.v_status.set(f"실행 중… {el // 60}분 {el % 60}초")
        self.after(100, self._poll)

    def _finished(self, rc):
        if self._logf is not None:
            try:
                self._logf.write(f"\n종료 코드: {rc}\n")
                self._logf.close()
            except Exception:  # noqa: BLE001
                pass
            self._logf = None
        self.pbar.stop()
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        el = int(time.time() - self.started)
        mode = getattr(self, "cur_mode", "")
        if self.stop_flag:
            self.v_status.set("중지됨")
        elif rc == 0:
            self._append("ok", f"\n✔ 완료 ({el // 60}분 {el % 60}초)\n")
            self.v_status.set(f"완료 — 결과 폴더: {self.last_outdir}")
            if mode == "mock":
                self._append("cmd", "※ 레이아웃 테스트 결과라 글자 앞에 '[번역]'이 붙어 있습니다 (실제 번역 아님).\n")
            if mode == "ocr" and self.last_outdir:
                pngs = sorted(self.last_outdir.glob("ocr_debug_p*.png"), key=lambda p: p.stat().st_mtime)
                if pngs:
                    open_path(pngs[-1])
            if mode == "build":
                self._append("ok", "한국어 PDF가 만들어졌습니다. [한국어 PDF 열기]로 확인하세요.\n")
        else:
            if mode == "install":
                self._append("err", "\nTesseract 자동 설치를 끝내지 못했습니다. 위 메시지를 확인하세요.\n")
            self._append("err", f"\n✘ 오류로 종료 (코드 {rc}). 위 로그의 빨간 줄을 확인하세요.\n"
                                f"   전체 로그: {self._outdir() / 'last_run.log'}\n")
            self.v_status.set("오류")
        cb, self._on_done = self._on_done, None
        if cb is not None and rc == 0 and not self.stop_flag:
            cb()

    # ================================================================== Copilot 탭 동작
    def _cp_ctx(self):
        import om_translate as T
        cache = T.Cache(self._outdir() / "translation_cache.sqlite")
        gloss = T.Glossary.load(self.v_gloss.get().strip() or None)
        protector = T.Protector(T.load_keep_patterns(self.v_keep.get().strip() or None), gloss)
        return cache, protector

    def cp_export(self):
        self.run("export", on_done=lambda: (self.cp_reload(), self.nb.select(self.tab_cp)))

    def cp_reload(self):
        try:
            import om_copilot
            man = om_copilot.load_manifest(self._outdir())
        except Exception as e:  # noqa: BLE001
            self.v_cp_total.set(f"불러오기 실패: {e}")
            return
        self.cp_man = man
        self.cp_list.delete(0, "end")
        if not man or not man.get("batches"):
            self.cp_batches = []
            self.v_cp_total.set("배치 없음 → ① 번역할 문장 추출" if not man else "번역할 문구 없음 (모두 번역됨)")
            self.cp_prompt.delete("1.0", "end")
            return
        cache, _ = self._cp_ctx()
        st = om_copilot.batch_status(man, cache)
        self.cp_batches = st
        done_items = sum(b["done"] for b in st)
        tot_items = sum(b["total"] for b in st)
        n_done_b = sum(1 for b in st if b["done"] == b["total"])
        for b in st:
            mark = "✔" if b["done"] == b["total"] else ("△" if b["done"] else "  ")
            self.cp_list.insert("end", f"{mark} 배치 {b['no']:03d}   {b['done']}/{b['total']}")
            if b["done"] == b["total"]:
                self.cp_list.itemconfig("end", foreground="#1a7f37")
        self.v_cp_total.set(f"배치 {n_done_b}/{len(st)} 완료 · 문구 {done_items}/{tot_items}")
        nxt = next((i for i, b in enumerate(st) if b["done"] < b["total"]), None)
        sel = nxt if nxt is not None else len(st) - 1
        self.cp_list.selection_clear(0, "end")
        self.cp_list.selection_set(sel)
        self.cp_list.see(sel)
        self.cp_show()
        if nxt is None:
            self.v_cp_result.set("✔ 모든 배치 번역 완료 → [③ 한국어 PDF 만들기]를 누르세요.")

    def _cp_sel(self) -> int | None:
        s = self.cp_list.curselection()
        return (s[0] + 1) if s else None

    def cp_show(self):
        no = self._cp_sel()
        if not no or not self.cp_man:
            return
        import om_copilot
        txt = om_copilot.batch_prompt_text(self._outdir(), self.cp_man, no)
        self.cp_prompt.delete("1.0", "end")
        self.cp_prompt.insert("1.0", txt)
        b = self.cp_batches[no - 1]
        self.v_cp_head.set(f"배치 {no:03d} 프롬프트  ({b['total']}문구, {len(txt):,}자)")

    def cp_copy(self):
        txt = self.cp_prompt.get("1.0", "end").strip()
        if not txt:
            return
        self.clipboard_clear()
        self.clipboard_append(txt)
        self.update()
        self.v_cp_result.set("📋 복사됨 → Copilot 채팅창에 Ctrl+V 로 붙여넣고 보내세요. 답변 전체를 복사해 아래 칸에 붙여넣으세요.")

    def cp_paste(self):
        try:
            txt = self.clipboard_get()
        except Exception:  # noqa: BLE001
            txt = ""
        if txt:
            self.cp_answer.delete("1.0", "end")
            self.cp_answer.insert("1.0", txt)

    def cp_save(self):
        no = self._cp_sel()
        ans = self.cp_answer.get("1.0", "end").strip()
        if not no:
            messagebox.showwarning("배치 선택", "왼쪽 목록에서 배치를 선택하세요.")
            return
        if not ans:
            messagebox.showwarning("답변 없음", "Copilot 답변을 붙여넣으세요.")
            return
        try:
            import om_copilot
            cache, protector = self._cp_ctx()
            r = om_copilot.import_answer(self._outdir(), no, ans, cache, protector)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("저장 실패", str(e))
            return
        msg = om_copilot.format_result(r)
        self.cp_answer.delete("1.0", "end")
        self.cp_reload()
        self.v_cp_result.set(msg)
        if r["saved"] == 0:
            messagebox.showwarning("저장된 번역 없음",
                                   msg + "\n\n답변 형식이 '[0001] 번역문' 인지, 올바른 배치를 선택했는지 확인하세요.")

    def _cp_ready_check(self) -> bool:
        """한국어 PDF 만들기 전에 Copilot 번역이 되어 있는지 확인. 계속하면 True."""
        try:
            import om_copilot
            man = om_copilot.load_manifest(self._outdir())
        except Exception:  # noqa: BLE001
            man = None
        if man is None:
            go = messagebox.askyesno(
                "Copilot 번역 전",
                "아직 '2. Copilot 번역' 탭에서 번역을 하지 않았습니다.\n"
                "지금 PDF를 만들면 번역 없이 영어 원문 그대로 나옵니다.\n\n"
                "[예] Copilot 번역 탭으로 이동 (권장)\n[아니오] 그래도 만들기")
            if go:
                self.nb.select(self.tab_cp)
                return False
            return True
        self.cp_reload()
        if self.cp_batches and any(b["done"] < b["total"] for b in self.cp_batches):
            left = sum(b["total"] - b["done"] for b in self.cp_batches)
            done = sum(b["done"] for b in self.cp_batches)
            if not messagebox.askyesno(
                    "번역 미완료",
                    f"번역된 문구 {done}개 / 아직 Copilot 답변이 없는 문구 {left}개.\n"
                    "답변이 없는 문구는 영어 원문 그대로 남습니다. 계속할까요?"):
                self.nb.select(self.tab_cp)
                return False
        return True

    def cp_build(self):
        self.run("build")

    def cp_open_folder(self):
        import om_copilot
        d = om_copilot.work_dir(self._outdir())
        d.mkdir(parents=True, exist_ok=True)
        open_path(d)

    # ================================================================== 로그/열기
    def _append(self, tag, text):
        self.log.configure(state="normal")
        self.log.insert("end", text, (tag,))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _newest(self, pattern: str, base: Path | None = None):
        d = base or self._outdir()
        if not d.exists():
            return None
        fs = [f for f in d.glob(pattern)]
        fs.sort(key=lambda p: p.stat().st_mtime)
        return fs[-1] if fs else None

    def open_out(self):
        d = self._outdir()
        d.mkdir(parents=True, exist_ok=True)
        open_path(d)

    def open_result(self):
        f = self._newest("*_KR_O&M.pdf") or self._newest("*_KR_O&M.pdf", self._outdir() / "레이아웃_테스트")
        if f:
            open_path(f)
        else:
            messagebox.showinfo("결과 없음", "아직 만들어진 한국어 PDF가 없습니다.")

    def open_review(self):
        f = self._newest("*_Translation_Log.xlsx")
        if f:
            open_path(f)
        else:
            messagebox.showinfo("결과 없음", "아직 만들어진 번역 로그가 없습니다.")

    def install_tesseract(self):
        """install_tesseract.py 실행: 다운로드 → (권한 없이) 사용자 폴더 설치 → 필요 시 관리자 설치 → winget."""
        if self.proc is not None or self.btn_stop.instate(["!disabled"]):
            return
        if self._tesseract_ok() and not messagebox.askyesno("Tesseract", "이미 설치되어 있습니다. 설치 상태를 확인할까요?"):
            return
        self._clear_log()
        self.nb.select(self.tab_log)
        self._append("cmd", "Tesseract OCR 설치를 시작합니다. 'Windows 권한 확인' 창이 뜨면 [예]를 누르세요.\n")
        self.started = time.time()
        self.cur_mode = "install"
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.pbar.start(12)

        def done():
            if self._tesseract_ok():
                self.btn_tess.configure(text="Tesseract 설치됨 ✔")
                messagebox.showinfo("Tesseract", "Tesseract OCR 설치가 완료되었습니다.\n"
                                                  "이제 스캔/글자깨짐 페이지(목차 등)도 번역됩니다.")
        self._on_done = done
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        threading.Thread(target=self._worker,
                         args=([([sys.executable, "-u", str(ROOT / "install_tesseract.py")], str(ROOT))], env),
                         daemon=True).start()

    def check_env(self):
        if self.proc is not None or self.btn_stop.instate(["!disabled"]):
            return
        self._clear_log()
        self.nb.select(self.tab_log)
        self.started = time.time()
        self.cur_mode = "check"
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   ANTHROPIC_API_KEY=self.v_key.get().strip())
        threading.Thread(target=self._worker,
                         args=([([sys.executable, "-u", str(ROOT / "check_env.py")], str(ROOT))], env),
                         daemon=True).start()

    def _on_close(self):
        if self.proc is not None and not messagebox.askyesno("종료", "작업이 실행 중입니다. 중지하고 닫을까요?"):
            return
        self.stop_flag = True
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._save_settings()
        self.destroy()


def main():
    if IS_WIN:
        try:  # 고해상도 모니터에서 글자 흐림 방지
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    print("OM 번역기 창을 열었습니다. (창이 안 보이면 작업표시줄의 Python 아이콘을 클릭하세요)", flush=True)
    App().mainloop()


if __name__ == "__main__":
    main()
