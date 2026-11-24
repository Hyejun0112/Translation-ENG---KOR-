#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_translation.py - 번역본의 '내용' 점검: 원본과 번역본을 쪽 단위로 대조 (API 사용 안 함)
=============================================================================
사용:  python tools\\check_translation.py 원본.pdf 번역본.pdf [--pages 1-25]

check_layout.py 가 모양(그림/선/겹침)을 본다면, 이 도구는 글 내용의 사소한 오류를 찾습니다.
  1) 숫자·Tag·모델/문서번호가 번역 중에 바뀌었거나 사라졌거나 새로 생겼는지 (쪽 단위로 개수 비교)
  2) 조사가 '을(를)', '은(는)' 처럼 병기된 채 남은 곳 (보정되지 않은 번역)
  3) OCR 이 잘못 읽은 것으로 보이는 식별자 (예: 1Q3, PUBOO2-137, IO0 …)
  4) 번역은 되었는데 영어 낱말이 많이 남은 쪽
  5) 번역된 쪽(한글이 있는 쪽)에 영어 문장 줄이 그대로 남은 곳 (번역 누락 후보: 쪽마다 최대 3줄 표시)
번역본이 일부 쪽만 번역한 것이면(--pages 로 번역) 같은 범위를 --pages 로 지정하세요.
원본이 스캔 PDF 라서 글자층이 없는 쪽은 1)·4) 를 건너뜁니다.
"""
from __future__ import annotations

import re
import sys
from collections import Counter

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

SUPER = str.maketrans("²³¹⁰⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉", "2310456789" "0123456789")
ID_RE = re.compile(r"[A-Za-z]*\d[A-Za-z0-9_.\-/%]*")  # 영문/숫자만 (한글 조사·단어가 붙지 않게)
PARTICLE_RE = re.compile(r"\S{0,14}(?:은\(는\)|는\(은\)|을\(를\)|를\(을\)|이\(가\)|가\(이\)|과\(와\)|와\(과\)|으로\(로\)|로\(으로\)|\(으\)로|\(이\)라)")
OCR_SUSPECT_RE = re.compile(r"(?<![A-Za-z0-9])(?:[01][A-Z]{1,3}\d[A-Za-z0-9\-]*|[A-Z]{2,5}O{1,3}\d{1,4}(?:-\d+)*)(?![A-Za-z0-9])")
STANDARD_RE = re.compile(r"^(?:ISO|IEC|EN|DIN|API|ASME|ANSI|ASTM|IEEE|NFPA|BS|UL|IP|JIS|KS|NEMA|AWS)")


def norm(s: str) -> str:
    s = re.sub("\u00ad(?=[^\\s\u00ad])", "-", s).replace("\u00ad", "")  # 줄 중간 U+00AD 는 보이는 하이픈
    s = re.sub(r"(?:\s?[.·…]){4,}\s?", " ", s)  # 목차 점선(제목 ....... 쪽번호)은 구분 기호일 뿐
    return s.translate(SUPER).replace("−", "-").replace("–", "-").replace("—", "-")


# 숫자 + 영어 낱말 꼬리('10-mm', '4-pole', '3-core', '7th', '1/6th', '10mm'): 한국어로는 '10mm', '4극', '3심',
# '7번째' 처럼 꼬리가 바뀌거나 띄어 쓰이는 것이 정상 → 숫자 부분만 비교 (Tag·모델명처럼 영문이 앞에 오는 것은 그대로)
NUM_TAIL_RE = re.compile(r"^(\d[\d.,/]*)-?[a-z]{1,12}$")


def ids(text: str) -> Counter:
    out = Counter()
    for t in ID_RE.findall(norm(text)):
        t = t.strip(".-/%").lower()
        m = NUM_TAIL_RE.match(t)
        if m:
            t = m.group(1).strip(".,/")
        if t:
            out[t] += 1
    return out


def parse_pages(spec: str, n: int) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a or 1) - 1, min(int(b or n), n)))
        elif part:
            out.append(int(part) - 1)
    return sorted({p for p in out if 0 <= p < n})


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    en, kr = fitz.open(argv[1]), fitz.open(argv[2])
    pages = parse_pages(argv[argv.index("--pages") + 1], len(en)) if "--pages" in argv else list(range(len(en)))
    if len(kr) != len(pages):
        print(f"! 번역본 {len(kr)}쪽 / 원본 대상 {len(pages)}쪽: 쪽 수가 다릅니다 (--pages 로 같은 범위를 지정하세요)")
    n_bad = 0
    for k, pno in enumerate(pages):
        if k >= len(kr):
            break
        te, tk = en[pno].get_text(), kr[k].get_text()
        msgs = []
        if len(te.strip()) > 20:
            ce, ck = ids(te), ids(tk)
            lost = [t for t, c in (ce - ck).items()]
            new = [t for t, c in (ck - ce).items()]
            # 글자층이 같은 쪽(원문 유지)은 건너뜀. 번역된 쪽만: 줄바꿈/단위 표기 차이로 생기는 잡음은 개수가 적다
            if lost:
                msgs.append("원문에만 있는 숫자/식별자 " + ", ".join(lost[:8]) + (" …" if len(lost) > 8 else ""))
            if new:
                msgs.append("번역본에만 있는 숫자/식별자 " + ", ".join(new[:8]) + (" …" if len(new) > 8 else ""))
            en_words = len(re.findall(r"[A-Za-z]{4,}", te))
            left = len(re.findall(r"[A-Za-z]{4,}", tk))
            if en_words > 40 and 0.5 * en_words < left < 0.9 * en_words and re.search(r"[가-힣]", tk):
                msgs.append(f"번역은 되었으나 영어 낱말이 많이 남음 ({left}/{en_words})")
        if re.search(r"[가-힣]", tk):  # 번역된 쪽: 영어 문장으로 남은 줄 (URL·메일·제품명/번호 줄 제외)
            left = []
            ann = [a.rect for a in (kr[k].annots() or [])]  # 메모/스탬프 주석 안의 글자는 번역 대상이 아님
            for bl in kr[k].get_text("dict")["blocks"]:
                for ln in bl.get("lines", []):
                    t = "".join(sp["text"] for sp in ln["spans"]).strip()
                    if any(fitz.Rect(ln["bbox"]).intersects(r) for r in ann):
                        continue
                    if re.search(r"[가-힣]", t) or re.search(r"https?://|www\.|@", t):
                        continue
                    if re.search(r"\s[x×*/+=−-]\s", t) and len(re.findall(r"\b[a-z]{3,}\b", t)) <= 4 \
                            and not re.search(r"\b(?:the|and|of|to|is|are|with|for)\b", t):
                        continue  # 수식('6 x rpm + gpm x psi'): 원본 그대로 두는 것이 정상
                    if len(re.findall(r"\b[a-z]{3,}\b", t)) >= 3:
                        left.append(t[:60])
            if left:
                msgs.append(f"영어 문장 줄 {len(left)}개 남음(번역 누락 확인): " + " / ".join(left[:3]))
        for m in PARTICLE_RE.finditer(tk):
            msgs.append("조사 병기 형태 남음: " + m.group(0))
            break
        known = set(OCR_SUSPECT_RE.findall(norm(te)))  # 원본에 이미 그렇게 적힌 것은 오독이 아님 (예: 0DS2)
        sus = sorted(t for t in set(OCR_SUSPECT_RE.findall(norm(tk))) if t not in known and not STANDARD_RE.match(t))
        if sus:
            msgs.append("OCR 오독 의심 식별자: " + ", ".join(sus[:6]))
        if msgs:
            n_bad += 1
            print(f"p.{pno + 1}: " + " | ".join(msgs))
    print(f"\n검사 {len(pages)}쪽 / 확인 필요 {n_bad}쪽")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
