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
    r"kg/cm[²2](?:\s?\(?[ga]\)?)?", r"[Kk][Gg][Ff]/[Cc][Mm][²2]?(?:\s?\(?[ga]\)?|[ga])?", r"mmH[2₂]O", r"mmHg",
    r"(?:bar|kPa|MPa|Pa|psi|mbar)(?:\s?\(?[ga]\)?|[ga])?",
    r"N?m[³3]/(?:h|hr|min|s)", r"kg/(?:h|hr|s|m[³3])", r"[tT]/h(?:r)?",
    r"mg/N?(?:L|m[³3])", r"[kMG]?Wh?", r"k?VA?", r"m?A", r"Hz", r"rpm", r"%",
    r"ppm", r"ppb", r"mm", r"cm", r"[kK]m", r"m[²³23]", r"m", r"kg", r"g", r"L",
    r"l/min", r"mmH2O", r"mmHg", r"NPS", r"DN", r"inch", r"\"", r"Ω",
    r"dB(?:\(A\))?", r"kcal/(?:kg|Nm[³3]|h)", r"[kM]?J(?:/kg)?",
    r"ft(?:[²³23]|/s|/min)?", r"AWG", r"V\s?(?:DC|AC|dc|ac)", r"lbs?", r"lbf(?:\.ft|·ft)?", r"[kM]?N(?:·?m)?",
    r"gpm", r"GPM", r"[sS][cC][fF][mM]", r"[mM]Pa[ga]?", r"inH[2₂]O",
]


_UNIT_RX = (
    r"(?<![\w.])[-+±]?\d+(?:[.,]\d+)*"
    r"(?:\s?[-–~±]\s?[-+]?\d+(?:[.,]\d+)*)?\s?(?:" + "|".join(_UNITS) + r")(?![A-Za-z])"
)


DEFAULT_KEEP_PATTERNS = [
    r"⟦I\d+⟧",  # 문장 속 아이콘 자리 (번역문과 함께 옮겨 다시 그림)
    r"https?://\S+",
    # 파일 이름(mda4201.pdf, BK212_manual.docx)과 밑줄로 이은 식별자(bk212_steamtrap_manual): 번역하면 찾을 수 없게 된다
    r"(?<![\w.])[\w\-.]+\.(?:pdf|PDF|docx?|xlsx?|dwg|dxf|zip|txt|csv|xml|stp|step|igs|eds|EDS|gsd|GSD|gsdml|csp|cfg|ini|hex|bin|exe|msi|jpg|png|tif)(?![\w])",
    r"(?<![\w-])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:_[A-Za-z0-9]+)+(?:-[A-Za-z0-9_]+)*(?![\w])",
    r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b",
    # 규격/표준 번호: ASME B31.1, API 610, ISO 9001, EN 10204 ...
    r"\b(?:ASME|ASTM|ANSI|API|ISO|IEC|IEEE|NFPA|AWS|NEMA|JIS|DIN|KEPIC|KS|EN|BS|UL|IP)"
    r"\s?(?:[A-Z]{1,3}[\s\-]?)?\d[\w.\-/]*(?::\d{4})?",
    r"\b(?:ASME|ASTM|ANSI|API|ISO|IEC|IEEE|NFPA|AWS|NEMA|JIS|DIN|KEPIC)\b",
    # 도면/문서 Revision
    r"\bRev(?:ision)?\.?\s?[A-Z0-9]{1,3}\b",
    # Tag No. / 모델명 / 부품번호 / 도면번호 (영문 대문자+숫자 혼합)
    r"\b(?=[A-Z0-9\-_/.]*\d)(?=[A-Z0-9\-_/.]*[A-Z])[A-Z0-9][A-Z0-9\-_/.]*[A-Z0-9]\b",
    # 모델명 끝에 소문자 형식 기호가 붙은 것 (DVC6200f, DVC6200p, DVC6200SIS 의 변형): 통째로 보호
    r"\b[A-Z]{1,6}\d{2,}[a-z]{1,2}\b",
    # 대소문자가 섞인 재질/규격/암호 코드 (CK3MCuN, AlSi10Mg, IK545-Split, Basic128Rsa15): 숫자 포함 + 대문자 2개 이상.
    # 'Weight1', 'Class150' 처럼 낱말 끝에 각주 번호가 붙은 것은 대문자가 하나라서 제외 (낱말은 번역)
    r"(?<![\w-])(?=[A-Za-z0-9]*\d)(?=(?:[a-z0-9]*[A-Z]){2})[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*(?:-[A-Za-z0-9]+)*(?![\w-])",
    # 수치 + 단위
    _UNIT_RX,
    # 숫자에 바로 붙은 단위/기호 (14.0Kgf/cm, 100L/hr, 1.27kW, 80Nm, 60lb.ft): 소수점이 갈라지지 않게 한 덩어리로 보호
    # (서수 1st/2nd/7th 는 제외 - 번역문에서 '첫째/일곱째' 등으로 쓰일 수 있음)
    r"(?<![\w.])\d+(?:[.,]\d+)*(?!(?:st|nd|rd|th)\b)[A-Za-zµμΩ°][A-Za-z0-9µμΩ°²³]*(?:[./][A-Za-z²³][A-Za-z0-9²³]*)*",
    # 숫자 없이 홀로 쓴 복합 단위 (표 머리글 'kgf/cm2', '(MPa)', 'm3/h', 'N/mm2'): 영어 낱말이 아니므로 그대로 둔다
    r"(?<![\w/])(?:[Kk][Gg][Ff]?/[Cc][Mm][²2]?(?:[ga]|\s?\([ga]\))?|N/mm[²2]?|N?m[³3]/(?:h|hr|min|s)|kg/(?:h|hr|s|m[³3])"
    r"|[lL]/(?:min|hr|h|s)|(?:mm|in|m)H[2₂]O|mmHg|ft[²³23](?:/(?:h|s|min))?|(?:in|mm|cm|dm|m)[²³23]"
    r"|kcal/(?:kg|Nm[³3]|h)|kPa|MPa|kVA|kW|rpm|psi[ga]?|bar[ga]|kN|MN|kNm|Nm|lbf(?:\.ft|·ft)?|lb\.?ft|ft\.?lbf?"
    r"|gpm|GPM|gph|GPH|[sSaA]?[cC][fF][mM]|gal/(?:rev|min|h)|liters?/(?:rev|min|h)|l/rev|cc/rev)(?![\w/])",
    # 괄호/대괄호 안에 홀로 쓴 단위 (표 머리글 '[mm]', '[in]', '[dm³/min]', '(°C)', '[%]'): 그대로 둔다
    r"[\[(](?:[kMmcµμdn]?(?:m|g|l|L|s|N|W|V|A|Pa|J|Hz|bar)|in|ft|lb|lbf|psi[ga]?|°\s?[CF]|[℃℉%]|min|h|rpm|AWG)"
    r"(?:[²³23])?(?:/(?:min|s|h|hr|m[²³23]?|kg))?[\])]",
    # 점 세 개/말줄임표 범위 (4...20mA, 0…10 V)
    r"(?<![\w.])\d+(?:[.,]\d+)?\s?(?:\.{2,3}|…)\s?\d+(?:[.,]\d+)?(?:\s?[A-Za-z%°µ][A-Za-z0-9/%°µ]*)?",
    # 하이픈/슬래시로 이어진 번호 (부품번호 397802-0042, 그림 13-5, 분수 1/4, 범위 10-40, 날짜 09/2022) → 한 덩어리로 보호
    r"(?<![\w.])\d+(?:[.,]\d+)?(?:[-/]\d+(?:[.,]\d+)?)+(?![\w/])",
    # 보팅 로직 / 계기 로직: 2oo3, 1oo2D
    r"\b\d\s?oo\s?\d[A-Z]?\b",
    # 화학식/단위 첨자: H2O, CO2, NOx, SO2, O2 (단독 사용)
    r"\b(?:H[2₂]O|CO[2₂]|CO|NOx|NO[2₂]|SO[2₂]|SO[3₃]|SOx|O[2₂]|N[2₂]|NH[3₃]|CaCO[3₃]|HCl)(?![A-Za-z0-9])",
    # 날짜
    r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b",
    r"\b\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-, ]+\d{2,4}\b",
]


NUMBERED_WORD_RE = re.compile(r"^(\d{1,2}[.)])[A-Z][A-Z]{2,}$")


NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w])")


TOKEN_RE = re.compile(r"\[\[K(\d+)\]\]")


BULLET_RE = re.compile(
    r"^\s*(?:[•●▪■◦○‣·*\-–—]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\d+(?:\.\d+)+\.?)\s+"
)


LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
# 줄 중간의 U+00AD: PDF 에 실제로 찍힌 글자이므로 글꼴이 그린 '보이는 하이픈'이다
# (WinAnsi 0xAD = 하이픈 모양. 예: 모델 코드 'MTV-A5-AAA-A11-DN100-D20-B19' 가 전부 U+00AD 로 저장됨).
# 지우면 'MTVA5AAAA11DN100D20B19' 처럼 코드가 바뀐다. 줄 끝의 U+00AD(낱말 끊김)는 join_hyphenated 에서 붙인다.
SOFT_HYPHEN_MID_RE = re.compile("\u00ad(?=[^\\s\u00ad])")


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
        # '1.FUSES:' '2.REMOTE' 처럼 번호에 낱말이 바로 붙은 제목은 Tag 가 아니다 → 번호만 보호하고 낱말은 번역
        fixed = []
        for s, e in spans:
            m = NUMBERED_WORD_RE.match(text[s:e])
            fixed.append((s, s + len(m.group(1))) if m else (s, e))
        spans = fixed
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


# ----------------------------------------------------------------------------
# 한국어 조사 보정: Tag/숫자/영문 약어(보호 토큰) 뒤의 '은(는)', '을(를)' 같은 병기 형태를 올바른 조사로 바꾼다
# (번역 모델은 보호 토큰의 실제 글자를 볼 수 없어서 받침을 알 수 없다)
# ----------------------------------------------------------------------------
# (받침 있음, 받침이 ㄹ)  - 한국어로 읽을 때의 마지막 소리 기준
_DIGIT_JONG = {"0": (True, False), "1": (True, True), "2": (False, False), "3": (True, False), "4": (False, False),
               "5": (False, False), "6": (True, False), "7": (True, True), "8": (True, True), "9": (False, False)}
# 영문 대문자 읽기: 엘, 엠, 엔, 알 만 받침이 있다 (에이, 비, 씨, 디, 이, 에프, 지, 에이치, 아이, 제이, 케이, 오, 피, 큐,
# 에스, 티, 유, 브이, 더블유, 엑스, 와이, 제트 는 받침 없음)
_LETTER_JONG = {"L": (True, True), "R": (True, True), "M": (True, False), "N": (True, False)}

PARTICLES = [  # (병기 형태들, 받침 있을 때, 받침 없을 때, ㄹ받침일 때(다르면))
    (("은(는)", "는(은)"), "은", "는", None),
    (("을(를)", "를(을)"), "을", "를", None),
    (("이(가)", "가(이)"), "이", "가", None),
    (("과(와)", "와(과)"), "과", "와", None),
    (("이나(나)", "(이)나"), "이나", "나", None),
    (("이란(란)", "(이)란"), "이란", "란", None),
    (("이며(며)", "(이)며"), "이며", "며", None),
    (("이라(라)", "(이)라"), "이라", "라", None),
    (("으로(로)", "로(으로)", "(으)로"), "으로", "로", "로"),
]
_PARTICLE_RE = re.compile("(" + "|".join(re.escape(f) for ps, *_ in PARTICLES for f in ps) + ")")
_SKIP_BACK = set(")]}\"'”’»>*,.;:")


# 단위는 한국어로 읽는 소리 기준 (킬로그램, 밀리미터, 파스칼, 바 …)
_UNIT_JONG = {"kg": (True, False), "mm": (False, False), "cm": (False, False), "km": (False, False),
              "m": (False, False), "bar": (False, False), "psi": (False, False), "Pa": (True, True),
              "kPa": (True, True), "MPa": (True, True), "Hz": (False, False), "rpm": (True, False),
              "ppm": (True, False), "g": (True, False), "kW": (False, False), "kV": (False, False),
              "kA": (False, False), "mA": (False, False), "Vac": (False, False), "Vdc": (False, False),
              "%": (False, False), "℃": (False, False), "°C": (False, False), "mmH2O": (False, False),
              "Nm": (False, False), "lb.ft": (False, False), "lbf": (False, False), "ft": (False, False),
              "lb": (False, False), "lbs": (False, False), "gpm": (True, False), "scfm": (True, False),
              "l/min": (True, False), "L/min": (True, False), "kg/h": (True, False), "m/s": (False, False),
              "in": (False, False), "L/hr": (False, False), "l/hr": (False, False), "L/h": (False, False),
              "m3/h": (False, False), "°F": (False, False), "rpm.": (True, False)}
# 기호: 도(°), 제곱/세제곱 미터(²³ → '…미터'), 아래첨자 숫자(O₂ '오투', H₂O)
_SYMBOL_JONG = {"°": (False, False), "²": (False, False), "³": (False, False), "℉": (False, False),
                "Ω": (False, False), "µ": (False, False), "μ": (False, False)}
_SUB_DIGIT = str.maketrans("₀₁₂₃₄₅₆₇₈₉⁰¹⁴⁵⁶⁷⁸⁹", "0123456789" "0145678" "9")
_UNITS_BY_LEN = sorted(_UNIT_JONG, key=len, reverse=True)


# 인터넷 주소 끝 (읽는 소리): 닷컴, 닷넷, 닷오알지, 닷케이알 …
_DOMAIN_JONG = {".com": (True, False), ".net": (True, False), ".org": (False, False), ".kr": (True, True),
                ".de": (False, False), ".fi": (False, False), ".jp": (False, False), ".uk": (False, False),
                ".gov": (False, False), ".edu": (False, False), ".info": (False, False), ".pdf": (False, False)}


def _jong_of(prev: str):
    """prev: 조사 바로 앞 글자들. 반환 (받침 있음, ㄹ받침) 또는 알 수 없으면 None"""
    i = len(prev) - 1
    while i >= 0 and prev[i] in _SKIP_BACK:  # 닫는 괄호/따옴표/문장부호는 건너뜀
        i -= 1
    if i < 0:
        return None
    head = prev[:i + 1]
    if head.endswith("⟧"):  # 문장 속 아이콘(키 모양) 뒤: '키'처럼 받침 없이 읽는다 → '를/는/가'
        return (False, False)
    low = head.lower()
    for dom, j in _DOMAIN_JONG.items():  # 이메일/URL 뒤
        if low.endswith(dom):
            return j
    for u in _UNITS_BY_LEN:  # 숫자·공백·여는 괄호 바로 뒤(또는 맨 앞)의 단위: '10 Nm', '(Nm)', 'kN'
        if head.endswith(u) and (len(head) == len(u) or not head[-len(u) - 1].isalpha()):
            if len(head) == len(u) or head[-len(u) - 1].isdigit() or head[-len(u) - 1] in " /([{<":
                return _UNIT_JONG[u]
    c = prev[i]
    if c in _SYMBOL_JONG:
        return _SYMBOL_JONG[c]
    c = c.translate(_SUB_DIGIT)
    if "가" <= c <= "힣":
        j = (ord(c) - 0xAC00) % 28
        return (j != 0, j == 8)
    if c in _DIGIT_JONG:
        return _DIGIT_JONG[c]
    if c.isascii() and c.isalpha():
        if c.isupper():
            return _LETTER_JONG.get(c, (False, False))
        # 소문자: 단위(mm, kg, bar …)는 읽는 법이 갈려 판단하지 않는다. 단, 앞이 숫자+Vac/Vdc 같은 끝이 c/d/e/a 등이면 받침 없음
        # 외래어 표기 기준: 끝소리 n·m·ng 은 받침(에머슨을), l 은 ㄹ받침(…컬을/…로), 나머지는 '으' 로 끝나 받침 없음(로토크를)
        if c in "mn":
            return (True, False)
        if c == "l":
            return (True, True)
        return (False, False)
    return None


def fix_particles(ko: str) -> str:
    """'PUB002-041을(를)' → 'PUB002-041을', '5A을(를)' → '5A를', '-PS1은(는)' → '-PS1은' (한글/숫자/영문 대문자 기준)"""
    if not ko or "(" not in ko and "로" not in ko:
        return ko
    table = {}
    for forms, yes, no, rieul in PARTICLES:
        for f in forms:
            table[f] = (yes, no, rieul)

    def rep(m: "re.Match") -> str:
        form = m.group(1)
        j = _jong_of(ko[:m.start()])
        if j is None:
            return form
        yes, no, rieul = table[form]
        has, is_r = j
        if not has:
            return no
        return rieul if (rieul and is_r) else yes

    return _PARTICLE_RE.sub(rep, ko)


def tokens_ok(src_ptxt: str, ko: str) -> bool:
    return Counter(TOKEN_RE.findall(src_ptxt)) == Counter(TOKEN_RE.findall(ko))


def needs_translation(ptxt: str) -> bool:
    rem = TOKEN_RE.sub(" ", ptxt)
    if re.search(r"[가-힣]", rem) and not re.search(r"[A-Za-z]{3,}", rem):
        return False
    return bool(re.search(r"[A-Za-z]{2,}", rem))


CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def clean_text(s: str) -> str:
    s = CONTROL_RE.sub(" ", s)  # PDF 글꼴에 섞인 제어문자 (엑셀 저장 오류·번역 요청 오류의 원인)
    s = SOFT_HYPHEN_MID_RE.sub("-", s).replace("\u00ad", "")
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
