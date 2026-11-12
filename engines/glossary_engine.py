# -*- coding: utf-8 -*-
"""
glossary_engine.py - 용어집 적용 / Tag·숫자·단위 보호
=====================================================
- Glossary : 용어집(csv/xlsx: en, ko, note) 로드, 문장에서 용어 찾기. ko 가 KEEP 이면 영어 유지
- Protector: 영어로 남겨야 할 문자열(규격, Tag No., 모델/부품번호, 도면/문서번호, Rev., 수치+단위,
             2oo3, 화학식, 날짜, URL/메일)을 [[K1]] 토큰으로 바꿔 번역기가 건드리지 못하게 함
- missing_protected: 번역문에 원문의 Tag/숫자/단위가 그대로 있는지 검사
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

_UNITS = [
    r"°\s?[CF]", r"[℃℉]",
    r"kg/cm[²2](?:\s?\(?[ga]\)?)?",
    r"(?:bar|kPa|MPa|Pa|psi|mbar)(?:\s?\(?[ga]\)?|[ga])?",
    r"N?m[³3]/(?:h|hr|min|s)", r"kg/(?:h|hr|s|m[³3])", r"[tT]/h(?:r)?",
    r"mg/N?(?:L|m[³3])", r"[kMG]?Wh?", r"k?VA?", r"m?A", r"Hz", r"rpm", r"%",
    r"ppm", r"ppb", r"mm", r"cm", r"[kK]m", r"m[²³23]", r"m", r"kg", r"g", r"L",
    r"l/min", r"mmH2O", r"mmHg", r"NPS", r"DN", r"inch", r"\"", r"Ω",
    r"dB(?:\(A\))?", r"kcal/(?:kg|Nm[³3]|h)", r"[kM]?J(?:/kg)?",
]


_UNIT_RX = (
    r"(?<![\w.])[-+±]?\d+(?:[.,]\d+)*"
    r"(?:\s?[-–~±]\s?[-+]?\d+(?:[.,]\d+)*)?\s?(?:" + "|".join(_UNITS) + r")(?![A-Za-z])"
)


DEFAULT_KEEP_PATTERNS = [
    r"https?://\S+",
    r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b",
    # 규격/표준 번호: ASME B31.1, API 610, ISO 9001, EN 10204 ...
    r"\b(?:ASME|ASTM|ANSI|API|ISO|IEC|IEEE|NFPA|AWS|NEMA|JIS|DIN|KEPIC|KS|EN|BS|UL|IP)"
    r"\s?(?:[A-Z]{1,3}[\s\-]?)?\d[\w.\-/]*(?::\d{4})?",
    r"\b(?:ASME|ASTM|ANSI|API|ISO|IEC|IEEE|NFPA|AWS|NEMA|JIS|DIN|KEPIC)\b",
    # 도면/문서 Revision
    r"\bRev(?:ision)?\.?\s?[A-Z0-9]{1,3}\b",
    # Tag No. / 모델명 / 부품번호 / 도면번호 (영문 대문자+숫자 혼합)
    r"\b(?=[A-Z0-9\-_/.]*\d)(?=[A-Z0-9\-_/.]*[A-Z])[A-Z0-9][A-Z0-9\-_/.]*[A-Z0-9]\b",
    # 수치 + 단위
    _UNIT_RX,
    # 보팅 로직 / 계기 로직: 2oo3, 1oo2D
    r"\b\d\s?oo\s?\d[A-Z]?\b",
    # 화학식/단위 첨자: H2O, CO2, NOx, SO2, O2 (단독 사용)
    r"\b(?:H2O|CO2|CO|NOx|NO2|SO2|SOx|O2|N2|NH3|CaCO3|HCl)\b",
    # 날짜
    r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b",
    r"\b\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-, ]+\d{2,4}\b",
]


NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w])")


TOKEN_RE = re.compile(r"\[\[K(\d+)\]\]")


BULLET_RE = re.compile(
    r"^\s*(?:[•●▪■◦○‣·*\-–—]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\d+(?:\.\d+)+\.?)\s+"
)


LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "\u00ad": ""}


def optional_path(path: Optional[str], what: str) -> Optional[str]:
    """선택 입력 파일이 없으면 경고만 하고 None (없이 진행)."""
    if path and not Path(path).exists():
        print(f"! {what} 파일이 없어 건너뜁니다: {path}", file=sys.stderr)
        return None
    return path


def load_keep_patterns(path: Optional[str]) -> list[re.Pattern]:
    pats = list(DEFAULT_KEEP_PATTERNS)
    path = optional_path(path, "영어유지 패턴")
    if path:
        for line in read_text_auto(Path(path)).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    return [re.compile(p) for p in pats]


def read_text_auto(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


KEEP_MARKERS = {"KEEP", "keep", "Keep", "=", "-", "유지", "영어유지"}  # ko 칸이 비어 있으면 '무시'(영어 유지 아님)


class Glossary:
    def __init__(self, entries: list[tuple[str, str, str]]):
        self.trans: dict[str, tuple[str, str, str]] = {}
        self.keep: list[str] = []
        for en, ko, note in entries:
            en = re.sub(r"\s+", " ", en.strip())
            ko = ko.strip()
            if not en or not ko:
                continue  # ko가 비어 있는 행(예: terms 후보 중 아직 안 채운 것)은 무시
            if ko in KEEP_MARKERS:
                self.keep.append(en)
            else:
                self.trans[en.lower()] = (en, ko, note.strip())
        self.rx = self._compile_trans()
        self.keep_rx = self._compile_keep()
        h = hashlib.sha256(
            json.dumps(sorted(self.trans.values()) + sorted(self.keep), ensure_ascii=False).encode()
        )
        self.hash = h.hexdigest()[:12]

    @staticmethod
    def _esc(term: str) -> str:
        return re.escape(term).replace(r"\ ", r"\s+")

    def _compile_trans(self):
        if not self.trans:
            return None
        terms = sorted(self.trans, key=len, reverse=True)
        return re.compile(
            r"(?<![A-Za-z0-9])(?:" + "|".join(self._esc(t) for t in terms) + r")(?:s|es)?(?![A-Za-z0-9])",
            re.I,
        )

    def _compile_keep(self):
        if not self.keep:
            return None
        terms = sorted(set(self.keep), key=len, reverse=True)
        return re.compile(
            r"(?<![A-Za-z0-9])(?:" + "|".join(self._esc(t) for t in terms) + r")(?![A-Za-z0-9])"
        )

    @classmethod
    def load(cls, path: Optional[str]) -> "Glossary":
        path = optional_path(path, "용어집")
        if not path:
            return cls([])
        p = Path(path)
        rows: list[list[str]] = []
        if p.suffix.lower() in (".xlsx", ".xlsm"):
            try:
                import openpyxl
            except ImportError:
                sys.exit("xlsx 용어집을 읽으려면 'pip install openpyxl' 이 필요합니다. (또는 CSV로 저장하세요)")
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            for ws in wb.worksheets:
                for r in ws.iter_rows(values_only=True):
                    rows.append([("" if c is None else str(c)) for c in r])
        else:
            text = read_text_auto(p)
            first = text.splitlines()[0] if text.strip() else ""
            dialect = csv.excel_tab if "\t" in first else csv.excel
            rows = list(csv.reader(text.splitlines(), dialect))
        entries = []
        for i, r in enumerate(rows):
            if len(r) < 1:
                continue
            en = r[0]
            ko = r[1] if len(r) > 1 else ""
            note = r[2] if len(r) > 2 else ""
            if en.strip().lower() in ("en", "english", "term", "영어", "영문", "source", "원문"):
                continue  # 머리글 행 (xlsx는 시트마다 있음)
            if en.strip().startswith("#"):
                continue
            entries.append((en, ko, note))
        return cls(entries)

    def _lookup(self, matched: str):
        key = re.sub(r"\s+", " ", matched.lower())
        if key in self.trans:
            return self.trans[key]
        for suf in ("es", "s"):
            if key.endswith(suf) and key[: -len(suf)] in self.trans:
                return self.trans[key[: -len(suf)]]
        return None

    def find(self, text: str) -> list[tuple[str, str, str]]:
        if not self.rx:
            return []
        seen, out = set(), []
        for m in self.rx.finditer(text):
            e = self._lookup(m.group(0))
            if e and e[0] not in seen:
                seen.add(e[0])
                out.append(e)
        return out

    def keep_spans(self, text: str) -> list[tuple[int, int]]:
        if not self.keep_rx:
            return []
        return [(m.start(), m.end()) for m in self.keep_rx.finditer(text)]


class Protector:
    def __init__(self, patterns: list[re.Pattern], glossary: Glossary):
        self.patterns = patterns
        self.glossary = glossary

    def protect(self, text: str, tokenize_numbers: bool) -> tuple[str, list[str]]:
        spans: list[tuple[int, int]] = []
        for rx in self.patterns:
            spans += [(m.start(), m.end()) for m in rx.finditer(text) if m.end() > m.start()]
        spans += self.glossary.keep_spans(text)
        if tokenize_numbers:
            spans += [(m.start(), m.end()) for m in NUM_RE.finditer(text)]
        spans.sort()
        merged: list[list[int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out, tokens, pos = [], [], 0
        for s, e in merged:
            out.append(text[pos:s])
            tokens.append(text[s:e])
            out.append(f"[[K{len(tokens)}]]")
            pos = e
        out.append(text[pos:])
        return "".join(out), tokens


def restore_tokens(ko: str, tokens: list[str]) -> str:
    def rep(m):
        i = int(m.group(1)) - 1
        return tokens[i] if 0 <= i < len(tokens) else m.group(0)

    return TOKEN_RE.sub(rep, ko)


def tokens_ok(src_ptxt: str, ko: str) -> bool:
    return Counter(TOKEN_RE.findall(src_ptxt)) == Counter(TOKEN_RE.findall(ko))


def needs_translation(ptxt: str) -> bool:
    rem = TOKEN_RE.sub(" ", ptxt)
    if re.search(r"[가-힣]", rem) and not re.search(r"[A-Za-z]{3,}", rem):
        return False
    return bool(re.search(r"[A-Za-z]{2,}", rem))


def clean_text(s: str) -> str:
    for a, b in LIGATURES.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def _nospace(t: str) -> str:
    return re.sub(r"\s+", "", t)


def missing_protected(protector: "Protector", text: str, ko: str) -> list[str]:
    """원문에서 보호해야 하는 문자열(Tag/숫자/단위/규격 등)이 번역문에 그대로 있는지 검사.
    빠진 항목 목록을 돌려준다(공백 차이는 허용)."""
    _, toks = protector.protect(text, tokenize_numbers=True)  # 번역 검사는 문장 길이와 관계없이 모든 숫자 확인
    need = Counter(_nospace(t) for t in toks if _nospace(t))
    ko_ns = _nospace(ko)
    miss = []
    for t, n in need.items():
        if ko_ns.count(t) < n:
            miss.append(t)
    return miss


STOP = set("the a an of and or to in on for with by from at as is are be this that these those it its "
           "if not no all any each per than then when where which shall will can may must should "
           "after before during under over into out up down".split())
