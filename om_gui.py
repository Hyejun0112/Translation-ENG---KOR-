#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
om_gui.py - OM 영→한 번역기 GUI
================================
더블클릭: "OM번역기 실행.bat"  (또는 VS Code에서 이 파일 ▶ 실행)

- PDF 첨부(여러 파일 / 폴더), 용어집·영어유지패턴·수정목록(overrides) 첨부
- 작업 선택: 분량 추정 / 용어 추출 / OCR 확인 / 레이아웃 테스트 / 샘플 번역 / 전체 번역
- 실행 로그 실시간 표시, 중지, 결과 폴더/결과 PDF/검수 CSV 바로 열기
- 마지막 설정은 om_gui_settings.json 에 저장 (API 키는 저장하지 않음)

실제 작업은 om_translate.py / om_ocr.py 를 별도 프로세스로 실행합니다.
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
    ("estimate", "1. 분량 추정 + 페이지 진단  (무료, API 사용 안 함)"),
    ("terms", "2. 용어 후보 추출 → terms_candidates.csv  (무료)"),
    ("ocr", "3. OCR 결과 확인  (PDF 1개, 한 페이지, 인식 박스 PNG)"),
    ("mock", "4. 레이아웃 테스트  (무료, 번역 대신 '[번역]' 표시)"),
    ("sample", "5. 샘플 번역  (지정 페이지만, 실제 API)"),
    ("full", "6. 전체 번역  (실제 API)"),
]
MODE_LABELS = [m[1] for m in MODES]
MODE_KEYS = {m[1]: m[0] for m in MODES}


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
        self.title("OM 영→한 번역기  (PDF 양식 유지 + OCR)")
        sh = self.winfo_screenheight()
        self.geometry(f"1000x{max(600, min(760, sh - 90))}+40+20")
        self.minsize(860, 600)
        self.proc: subprocess.Popen | None = None
        self.stop_flag = False
        self.q: queue.Queue = queue.Queue()
        self.files: list[str] = []
        self.started = 0.0
        self.last_outdir: Path | None = None
        self._logf = None

        self._style()
        self._build()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(200, self._bring_to_front)

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

    # ------------------------------------------------------------------ UI
    def _style(self):
        st = ttk.Style(self)
        if IS_WIN and "vista" in st.theme_names():
            st.theme_use("vista")
        base = ("Malgun Gothic", 10) if IS_WIN else ("TkDefaultFont", 10)
        self.option_add("*Font", base)
        st.configure("Title.TLabel", font=(base[0], 13, "bold"))
        st.configure("Run.TButton", font=(base[0], 11, "bold"), padding=(18, 6))
        st.configure("Hint.TLabel", foreground="#666666")

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self, padding=10)
        top.pack(fill="both", expand=True)
        top.columnconfigure(0, weight=1)

        ttk.Label(top, text="OM 영→한 번역기", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(top, text="원본 PDF 양식(그림·표·선) 그대로 글자만 한글로 교체합니다. 스캔 PDF는 OCR로 처리합니다.",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))

        self.nb = ttk.Notebook(top)
        self.nb.grid(row=2, column=0, sticky="nsew", pady=(0, 4))
        top.rowconfigure(2, weight=1)
        tab_set = ttk.Frame(self.nb, padding=4)
        tab_log = ttk.Frame(self.nb, padding=4)
        tab_set.columnconfigure(0, weight=1)
        tab_set.rowconfigure(0, weight=1)
        self.nb.add(tab_set, text="  설정  ")
        self.nb.add(tab_log, text="  실행 로그  ")
        self.tab_log = tab_log

        # ---------- 1. 첨부 파일
        f1 = ttk.LabelFrame(tab_set, text=" 1. 원본 PDF 첨부 ", padding=6)
        f1.grid(row=0, column=0, sticky="nsew", **pad)
        f1.rowconfigure(3, weight=1)
        f1.columnconfigure(0, weight=1)
        self.lst = tk.Listbox(f1, height=6, selectmode="extended", activestyle="none")
        self.lst.grid(row=0, column=0, rowspan=4, sticky="nsew")
        sb = ttk.Scrollbar(f1, orient="vertical", command=self.lst.yview)
        sb.grid(row=0, column=1, rowspan=4, sticky="ns")
        self.lst.configure(yscrollcommand=sb.set)
        bf = ttk.Frame(f1)
        bf.grid(row=0, column=2, rowspan=4, sticky="n", padx=(8, 0))
        ttk.Button(bf, text="PDF 파일 추가…", command=self.add_files).grid(row=0, column=0, sticky="ew")
        ttk.Button(bf, text="폴더 추가…", command=self.add_folder).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(bf, text="선택 삭제", command=self.remove_sel).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bf, text="모두 지우기", command=self.clear_files).grid(row=1, column=1, sticky="ew",
                                                                     padx=(4, 0), pady=(4, 0))
        ttk.Label(bf, text="여러 파일 선택 가능", style="Hint.TLabel",
                  justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._try_enable_dnd()

        # ---------- 2. 참고 파일
        f2 = ttk.LabelFrame(tab_set, text=" 2. 참고 파일 (선택) ", padding=6)
        f2.grid(row=1, column=0, sticky="ew", **pad)
        f2.columnconfigure(1, weight=1)
        self.v_gloss = tk.StringVar()
        self.v_keep = tk.StringVar()
        self.v_over = tk.StringVar()
        self.v_out = tk.StringVar(value=str(ROOT / "OM_KO"))
        rows = [
            ("용어집 (csv/xlsx)", self.v_gloss, [("용어집", "*.csv *.xlsx *.xlsm *.txt"), ("모든 파일", "*.*")]),
            ("영어 유지 패턴", self.v_keep, [("텍스트", "*.txt"), ("모든 파일", "*.*")]),
            ("수정 목록 (overrides.csv)", self.v_over, [("CSV", "*.csv"), ("모든 파일", "*.*")]),
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

        # ---------- 3. 작업 / 옵션
        f3 = ttk.LabelFrame(tab_set, text=" 3. 작업 선택 / 옵션 ", padding=6)
        f3.grid(row=2, column=0, sticky="ew", **pad)
        for c in (1, 3, 5):
            f3.columnconfigure(c, weight=1)
        self.v_mode = tk.StringVar(value=MODE_LABELS[3])
        ttk.Label(f3, text="작업").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(f3, textvariable=self.v_mode, values=MODE_LABELS, state="readonly", width=52)
        cb.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6, pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._mode_changed())
        self.v_pages = tk.StringVar(value="1-5")
        ttk.Label(f3, text="페이지").grid(row=0, column=4, sticky="e")
        self.ent_pages = ttk.Entry(f3, textvariable=self.v_pages, width=14)
        self.ent_pages.grid(row=0, column=5, sticky="w", padx=6)

        self.v_ocr = tk.StringVar(value="auto")
        self.v_fig = tk.BooleanVar(value=True)
        self.v_cmp = tk.BooleanVar(value=True)
        self.v_force = tk.BooleanVar(value=True)
        self.v_tone = tk.StringVar(value="formal")
        self.v_dpi = tk.StringVar(value="400")
        ttk.Label(f3, text="OCR").grid(row=1, column=0, sticky="w")
        ttk.Combobox(f3, textvariable=self.v_ocr, values=["auto", "force", "off"], state="readonly",
                     width=8).grid(row=1, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(f3, text="OCR 해상도(dpi)").grid(row=1, column=2, sticky="e")
        ttk.Combobox(f3, textvariable=self.v_dpi, values=["300", "400", "500", "600"],
                     width=6).grid(row=1, column=3, sticky="w", padx=6)
        ttk.Label(f3, text="문체").grid(row=1, column=4, sticky="e")
        ttk.Combobox(f3, textvariable=self.v_tone, values=["formal", "concise"], state="readonly",
                     width=10).grid(row=1, column=5, sticky="w", padx=6)
        opt = ttk.Frame(f3)
        opt.grid(row=2, column=0, columnspan=6, sticky="w", pady=(2, 0))
        ttk.Checkbutton(opt, text="그림 속 글자도 OCR", variable=self.v_fig).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(opt, text="원본|번역 비교본 PDF 생성", variable=self.v_cmp).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(opt, text="기존 결과 덮어쓰기", variable=self.v_force).pack(side="left")
        ttk.Label(f3, text="auto: 스캔/글자깨짐 페이지만 OCR · force: 모든 페이지 OCR · "
                           "문체 formal=~합니다, concise=개조식", style="Hint.TLabel").grid(
            row=3, column=0, columnspan=6, sticky="w")

        # ---------- API (샘플/전체 번역에만 필요)
        self.v_key = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.v_model = tk.StringVar(value=os.environ.get("OM_MODEL", "claude-sonnet-5-5"))
        self.v_show = tk.BooleanVar(value=False)
        api = ttk.Frame(f3)
        api.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(6, 0))
        api.columnconfigure(1, weight=1)
        ttk.Label(api, text="API 키").grid(row=0, column=0, sticky="w")
        self.ent_key = ttk.Entry(api, textvariable=self.v_key, show="•")
        self.ent_key.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Checkbutton(api, text="보기", variable=self.v_show,
                        command=lambda: self.ent_key.configure(show="" if self.v_show.get() else "•")).grid(row=0, column=2)
        ttk.Button(api, text="이 PC에 저장", command=self.save_key).grid(row=0, column=3, padx=(6, 12))
        ttk.Label(api, text="모델").grid(row=0, column=4, sticky="w")
        ttk.Entry(api, textvariable=self.v_model, width=22).grid(row=0, column=5, sticky="w", padx=6)
        ttk.Label(f3, text="API 키는 '5. 샘플 번역', '6. 전체 번역'에만 필요합니다.", style="Hint.TLabel").grid(
            row=5, column=0, columnspan=6, sticky="w")

        # ---------- 실행 버튼
        fb = ttk.Frame(top)
        fb.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 2))
        self.btn_run = ttk.Button(fb, text="▶  실행", style="Run.TButton", command=self.run)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(fb, text="■ 중지", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(fb, text="결과 폴더 열기", command=self.open_out).pack(side="right")
        ttk.Button(fb, text="검수 CSV 열기", command=self.open_review).pack(side="right", padx=6)
        ttk.Button(fb, text="결과 PDF 열기", command=self.open_result).pack(side="right")
        ttk.Button(fb, text="환경 점검", command=self.check_env).pack(side="right", padx=6)

        # ---------- 로그
        f5 = tab_log
        f5.columnconfigure(0, weight=1)
        f5.rowconfigure(0, weight=1)
        mono = ("Consolas", 9) if IS_WIN else ("TkFixedFont", 9)
        self.log = tk.Text(f5, height=10, wrap="word", font=mono, background="#1e1e1e",
                           foreground="#e6e6e6", insertbackground="#e6e6e6")
        self.log.grid(row=0, column=0, sticky="nsew")
        ls = ttk.Scrollbar(f5, orient="vertical", command=self.log.yview)
        ls.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=ls.set, state="disabled")
        self.log.tag_configure("err", foreground="#ff8080")
        self.log.tag_configure("ok", foreground="#7ee787")
        self.log.tag_configure("cmd", foreground="#79c0ff")
        self.v_status = tk.StringVar(value="대기 중")
        ttk.Label(top, textvariable=self.v_status, style="Hint.TLabel").grid(row=4, column=0, sticky="w", padx=8)
        self.pbar = ttk.Progressbar(top, mode="indeterminate")
        self.pbar.grid(row=5, column=0, sticky="ew", padx=8, pady=(2, 0))
        self._mode_changed()

    def _try_enable_dnd(self):
        """tkinterdnd2가 설치되어 있으면 목록에 파일 끌어놓기 지원 (선택 기능)."""
        if DND_FILES is None:
            return
        try:
            self.lst.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            self.lst.dnd_bind("<<Drop>>", lambda e: self._add_paths(self.tk.splitlist(e.data)))  # type: ignore
            self.lst.insert("end", "  ← 여기로 PDF 파일/폴더를 끌어다 놓거나 오른쪽 버튼으로 추가하세요")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 파일 선택
    def _add_paths(self, paths):
        for p in paths:
            p = str(Path(p))
            if p not in self.files and (Path(p).is_dir() or p.lower().endswith(".pdf")):
                self.files.append(p)
        self._refresh_list()

    def _refresh_list(self):
        self.lst.delete(0, "end")
        for p in self.files:
            tag = "[폴더] " if Path(p).is_dir() else ""
            self.lst.insert("end", tag + p)

    def add_files(self):
        init = self._init_dir()
        ps = filedialog.askopenfilenames(title="원본 PDF 선택 (여러 개 선택 가능)", initialdir=init,
                                         filetypes=[("PDF", "*.pdf"), ("모든 파일", "*.*")])
        self._add_paths(ps)

    def add_folder(self):
        p = filedialog.askdirectory(title="원본 PDF 폴더 선택 (하위 폴더 포함)", initialdir=self._init_dir())
        if p:
            self._add_paths([p])

    def remove_sel(self):
        for i in reversed(self.lst.curselection()):
            del self.files[i]
        self._refresh_list()

    def clear_files(self):
        self.files.clear()
        self._refresh_list()

    def _init_dir(self):
        if self.files:
            p = Path(self.files[-1])
            return str(p if p.is_dir() else p.parent)
        return str(ROOT / "OM_EN") if (ROOT / "OM_EN").exists() else str(ROOT)

    def pick_file(self, var: tk.StringVar, ft):
        p = filedialog.askopenfilename(title="파일 선택", initialdir=str(ROOT), filetypes=ft)
        if p:
            var.set(p)

    def pick_out(self):
        p = filedialog.askdirectory(title="결과 저장 폴더", initialdir=self.v_out.get() or str(ROOT))
        if p:
            self.v_out.set(p)

    def _mode_changed(self):
        k = MODE_KEYS[self.v_mode.get()]
        if k == "ocr":
            if not self.v_pages.get().isdigit():
                self.v_pages.set("1")
        elif k in ("mock", "sample") and not self.v_pages.get():
            self.v_pages.set("1-5")
        self.ent_pages.configure(state="normal" if k in ("ocr", "mock", "sample", "estimate") else "disabled")

    # ------------------------------------------------------------------ 설정 저장
    def _load_settings(self):
        try:
            d = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        for k, var in self._vars().items():
            if k in d:
                var.set(d[k])
        self.files = [p for p in d.get("files", []) if Path(p).exists()]
        self._refresh_list()
        if not self.v_gloss.get():
            for g in ("glossary.csv", "glossary.xlsx", "glossary_sample.csv"):
                if (ROOT / g).exists():
                    self.v_gloss.set(str(ROOT / g))
                    break
        if not self.v_keep.get() and (ROOT / "keep_patterns.txt").exists():
            self.v_keep.set(str(ROOT / "keep_patterns.txt"))
        if self.v_mode.get() not in MODE_LABELS:
            self.v_mode.set(MODE_LABELS[3])
        self._mode_changed()

    def _vars(self):
        return {"gloss": self.v_gloss, "keep": self.v_keep, "over": self.v_over, "out": self.v_out,
                "mode": self.v_mode, "pages": self.v_pages, "ocr": self.v_ocr, "fig": self.v_fig,
                "cmp": self.v_cmp, "force": self.v_force, "tone": self.v_tone, "dpi": self.v_dpi,
                "model": self.v_model}

    def _save_settings(self):
        d = {k: v.get() for k, v in self._vars().items()}
        d["files"] = self.files
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
                messagebox.showinfo("API 키", "이 PC 사용자 환경변수에 저장했습니다.\n다음 실행부터 자동으로 입력됩니다.")
            else:
                messagebox.showerror("API 키", r.stderr or r.stdout)
        else:
            messagebox.showinfo("API 키", "이번 실행에만 적용됩니다. (Windows에서는 setx로 저장)")

    # ------------------------------------------------------------------ 명령 구성
    def _common(self) -> list[str]:
        a: list[str] = []
        if self.v_gloss.get().strip():
            a += ["--glossary", self.v_gloss.get().strip()]
        if self.v_keep.get().strip():
            a += ["--keep-patterns", self.v_keep.get().strip()]
        if self.v_over.get().strip():
            a += ["--overrides", self.v_over.get().strip()]
        a += ["--ocr", self.v_ocr.get(), "--ocr-dpi", self.v_dpi.get() or "400"]
        if not self.v_fig.get():
            a += ["--no-ocr-figures"]
        return a

    def build_commands(self) -> list[tuple[list[str], str]]:
        """[(명령, 작업폴더)]"""
        mode = MODE_KEYS[self.v_mode.get()]
        out = Path(self.v_out.get().strip() or (ROOT / "OM_KO"))
        py = sys.executable
        script = str(ROOT / "om_translate.py")
        cache = ["--cache", str(out / "translation_cache.sqlite")]
        pages = self.v_pages.get().strip()
        cmds: list[tuple[list[str], str]] = []
        if mode == "ocr":
            pdfs = [p for p in self.files if p.lower().endswith(".pdf")]
            if not pdfs:
                raise ValueError("OCR 확인은 PDF 파일 1개를 첨부해야 합니다. (폴더 불가)")
            page = pages if pages.isdigit() else "1"
            dbg = out / "OCR_확인"
            dbg.mkdir(parents=True, exist_ok=True)
            cmds.append(([py, "-u", str(ROOT / "om_ocr.py"), pdfs[0], "--page", page, "--debug",
                          "--dpi", self.v_dpi.get() or "400"], str(dbg)))
            self.last_outdir = dbg
            return cmds
        for src in self.files:
            if mode == "estimate":
                a = [py, "-u", script, "estimate", src, *self._common(), *cache]
                if pages:
                    a += ["--pages", pages]
                self.last_outdir = out
            elif mode == "terms":
                a = [py, "-u", script, "terms", src, "-o", str(out / "terms_candidates.csv"),
                     *self._common(), *cache]
                self.last_outdir = out
            else:
                o = out / "SAMPLE" if mode in ("mock", "sample") else out
                a = [py, "-u", script, "translate", src, "-o", str(o), *self._common(), *cache,
                     "--tone", self.v_tone.get(), "--model", self.v_model.get().strip() or "claude-sonnet-5-5"]
                if mode in ("mock", "sample") and pages:
                    a += ["--pages", pages]
                if mode == "mock":
                    a += ["--backend", "mock"]
                if self.v_cmp.get():
                    a += ["--compare"]
                if self.v_force.get():
                    a += ["--force"]
                self.last_outdir = o
            cmds.append((a, str(ROOT)))
        if mode == "terms" and len(self.files) > 1:
            # 여러 파일이면 결과 CSV가 덮어써지므로 파일별 이름으로 저장
            cmds = []
            for src in self.files:
                name = Path(src).stem
                a = [py, "-u", script, "terms", src, "-o", str(out / f"terms_{name}.csv"), *self._common(), *cache]
                cmds.append((a, str(ROOT)))
        return cmds

    # ------------------------------------------------------------------ 실행
    def run(self):
        if self.proc is not None:
            return
        if not self.files:
            messagebox.showwarning("첨부 파일 없음", "원본 PDF 파일(또는 폴더)을 먼저 추가하세요.")
            return
        mode = MODE_KEYS[self.v_mode.get()]
        if mode in ("sample", "full") and not self.v_key.get().strip():
            messagebox.showwarning("API 키 필요", "샘플/전체 번역에는 Anthropic API 키가 필요합니다.\n"
                                               "API 없이 확인하려면 '4. 레이아웃 테스트'를 선택하세요.")
            return
        if mode == "full" and not messagebox.askyesno(
                "전체 번역", "첨부한 모든 PDF를 전체 번역합니다. API 비용이 발생합니다.\n"
                            "먼저 '1. 분량 추정'으로 분량을 확인하셨나요?\n\n계속할까요?"):
            return
        try:
            Path(self.v_out.get().strip() or ROOT / "OM_KO").mkdir(parents=True, exist_ok=True)
            cmds = self.build_commands()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("설정 오류", str(e))
            return
        self._save_settings()
        self._clear_log()
        self.nb.select(self.tab_log)
        self.stop_flag = False
        self.started = time.time()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.pbar.start(12)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", OM_BOOTSTRAPPED="1",
                   ANTHROPIC_API_KEY=self.v_key.get().strip(), PYTHONUNBUFFERED="1")
        try:  # 실행 로그를 파일로도 저장 (문제 생기면 이 파일을 보내 주세요)
            logp = Path(self.v_out.get().strip() or ROOT / "OM_KO") / "last_run.log"
            self._logf = open(logp, "w", encoding="utf-8")
            self._logf.write(time.strftime("%Y-%m-%d %H:%M:%S") + f"  python={sys.executable}\n")
        except Exception:  # noqa: BLE001
            self._logf = None
        threading.Thread(target=self._worker, args=(cmds, env), daemon=True).start()

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
                tag = "err" if (low.startswith(("!", "Traceback", "Error", "  File")) or "Error:" in line) else "out"
                self.q.put((tag, line + "\n"))
                self._logfile_write(line + "\n")

            while True:
                ch = self.proc.stdout.read(1)
                if not ch:
                    break
                if pending_cr:
                    pending_cr = False
                    if ch == b"\n":  # \r\n = 일반 줄바꿈
                        emit_line(buf)
                        buf = b""
                        continue
                    # 단독 \r = 진행률 줄 (같은 줄 덮어쓰기)
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

    def _logfile_write(self, text: str):
        try:
            if self._logf is not None:
                self._logf.write(text)
                self._logf.flush()
        except Exception:  # noqa: BLE001
            pass

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
        if self.proc is not None or self.btn_stop.instate(["!disabled"]):
            el = int(time.time() - self.started)
            cur = self.v_status.get()
            if not cur.startswith("["):
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
        if self.stop_flag:
            self.v_status.set("중지됨")
        elif rc == 0:
            self._append("ok", f"\n✔ 완료 ({el // 60}분 {el % 60}초)\n")
            self.v_status.set(f"완료 — 결과 폴더: {self.last_outdir}")
            if MODE_KEYS[self.v_mode.get()] == "ocr" and self.last_outdir:
                pngs = sorted(self.last_outdir.glob("ocr_debug_p*.png"), key=lambda p: p.stat().st_mtime)
                if pngs:
                    open_path(pngs[-1])
        else:
            self._append("err", f"\n✘ 오류로 종료 (코드 {rc}). 위 로그의 빨간 줄을 확인하세요.\n"
                                f"   전체 로그: {Path(self.v_out.get().strip() or ROOT / 'OM_KO') / 'last_run.log'}\n")
            self.v_status.set("오류")

    # ------------------------------------------------------------------ 로그/열기
    def _append(self, tag, text):
        self.log.configure(state="normal")
        self.log.insert("end", text, (tag,))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _outdir(self) -> Path:
        return self.last_outdir or Path(self.v_out.get().strip() or ROOT / "OM_KO")

    def _newest(self, pattern: str):
        d = self._outdir()
        if not d.exists():
            return None
        fs = sorted(d.rglob(pattern), key=lambda p: p.stat().st_mtime)
        return fs[-1] if fs else None

    def open_out(self):
        d = self._outdir()
        d.mkdir(parents=True, exist_ok=True)
        open_path(d)

    def open_result(self):
        f = (self._newest("*_KO_compare.pdf") if self.v_cmp.get() else None) or self._newest("*_KO.pdf")
        if f:
            open_path(f)
        else:
            messagebox.showinfo("결과 없음", "아직 생성된 번역 PDF가 없습니다.")

    def open_review(self):
        f = self._newest("*.review.csv") or self._newest("terms*.csv")
        if f:
            open_path(f)
        else:
            messagebox.showinfo("결과 없음", "아직 생성된 검수 CSV가 없습니다.")

    def check_env(self):
        if self.proc is not None:
            return
        self._clear_log()
        self.nb.select(self.tab_log)
        self.started = time.time()
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
