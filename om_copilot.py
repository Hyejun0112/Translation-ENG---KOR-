#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
om_copilot.py - API 없이 Copilot(또는 다른 챗봇)으로 번역하는 흐름
====================================================================
1) export : PDF에서 번역할 문장만 뽑아(중복 제거, 이미 번역한 문장 제외) Copilot에 붙여넣을
            배치 프롬프트 파일을 만든다.  →  <결과폴더>/Copilot_작업/batch_001_프롬프트.txt ...
2) Copilot에 프롬프트를 붙여넣고, 답변("[0001] 번역문" 형식)을 복사한다.
3) import : 답변을 붙여넣으면 형식을 해석하고 Tag/숫자/단위가 바뀌지 않았는지 검사한 뒤
            번역 메모리(translation_cache.sqlite)에 저장한다.
4) om_translate.py translate --backend memory : 번역 메모리로 한국어 PDF 생성.

GUI(om_gui.py)의 'Copilot 번역' 탭이 이 과정을 버튼으로 진행합니다.
프롬프트 문구는 copilot_prompt.txt 에서 수정할 수 있습니다 ({glossary}, {count}, {items} 자리표시자 유지).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
WORK_DIRNAME = "Copilot_작업"
MANIFEST = "manifest.json"

DEFAULT_TEMPLATE = """아래 [번역 대상]의 각 줄을 발전소 O&M 매뉴얼용 공식 한국어로 번역하십시오.
입력과 같은 번호·순서로 "[번호] 번역문" 형식으로만 한 줄씩 출력하십시오. 설명 없이 번역만 출력하십시오.
숫자·단위·Tag No.·모델명·제작사명·문서번호는 원문 그대로 두십시오.

[용어집]
{glossary}

[번역 대상] (총 {count}줄)
{items}
"""


def load_template() -> str:
    p = ROOT / "copilot_prompt.txt"
    if p.exists():
        t = p.read_text(encoding="utf-8-sig")
        if "{items}" in t:
            return t
    return DEFAULT_TEMPLATE


def work_dir(outdir: Path) -> Path:
    return Path(outdir) / WORK_DIRNAME


# ----------------------------------------------------------------------------
# 프롬프트 만들기
# ----------------------------------------------------------------------------
def build_prompt(items: list[tuple[str, str]], gloss, template: Optional[str] = None) -> str:
    """items: [(id, 원문)]"""
    template = template or load_template()
    joined = "\n".join(t for _, t in items)
    entries = gloss.find(joined) if gloss is not None else []
    lines = [f"- {en} = {ko}" for en, ko, _ in entries]
    if gloss is not None:
        for k in gloss.keep:
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])", joined):
                lines.append(f"- {k} = {k} (영어 유지)")
    glossary = "\n".join(lines) if lines else "(이번 배치에 해당 용어 없음)"
    body = "\n".join(f"[{i}] {t}" for i, t in items)
    return (template.replace("{glossary}", glossary).replace("{count}", str(len(items)))
            .replace("{items}", body))


def make_batches(ids: list[str], texts: dict[str, str], max_chars: int, max_items: int) -> list[list[str]]:
    batches, cur, n = [], [], 0
    for i in ids:
        L = len(texts[i]) + 8
        if cur and (n + L > max_chars or len(cur) >= max_items):
            batches.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += L
    if cur:
        batches.append(cur)
    return batches


# ----------------------------------------------------------------------------
# 1) export
# ----------------------------------------------------------------------------
def export(pdfs: list[Path], outdir: Path, cfg, gloss, protector, overrides, ocr, cache,
           max_chars: int = 6000, max_items: int = 60, log=print) -> dict:
    import om_translate as T
    fitz = T.fitz
    texts: dict[str, dict] = {}
    for src in pdfs:
        doc = fitz.open(src)
        n = len(doc)
        pages = cfg.pages if cfg.pages is not None else list(range(n))
        for pno in pages:
            pg = doc[pno]
            if pg.rotation and hasattr(pg, "remove_rotation"):
                pg.remove_rotation()  # translate 와 같은 좌표계 → OCR 캐시 재사용
        units, classes, _n_fig, scopes = T.prepare_units(doc, cfg, protector, overrides, ocr, cache,
                                                           T.file_sha(src), label=src.stem[:20])
        if cfg.pages is None:
            for _lvl, title, pgno in doc.get_toc(simple=True):
                tu = T.Unit(page=pgno - 1, idx=-1, rect=fitz.Rect(), text=T.clean_text(title), size=0,
                            color=0, bold=False, origin_y=0, kind="toc", source="toc")
                T.classify_unit(tu, protector, overrides)
                units.append(tu)
        doc.close()
        n_new = 0
        for u in units:
            if u.status != "PENDING":
                continue
            if T.tm_get(cache, u.text) is not None:
                continue
            e = texts.setdefault(u.text, {"files": [], "pages": []})
            if src.name not in e["files"]:
                e["files"].append(src.name)
            if len(e["pages"]) < 5 and u.page + 1 not in e["pages"]:
                e["pages"].append(u.page + 1)
            n_new += 1
        n_scope = sum(1 for ok, _ in scopes.values() if not ok)
        log(f"{src.name}: {n}p [{T.describe_classes(classes)}], 원문유지 페이지 {n_scope}, "
            f"번역 필요 문구 {n_new}개")

    ids = [f"{i:04d}" for i in range(1, len(texts) + 1)]
    id_text = dict(zip(ids, texts.keys()))
    batches = make_batches(ids, id_text, max_chars, max_items)
    wd = work_dir(outdir)
    wd.mkdir(parents=True, exist_ok=True)
    for old in list(wd.glob("batch_*_프롬프트.txt")) + list(wd.glob("batch_*_답변.txt")):
        old.unlink()
    template = load_template()
    man = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": [str(p) for p in pdfs],
           "items": {i: {"text": id_text[i], **texts[id_text[i]]} for i in ids},
           "batches": []}
    for bi, b in enumerate(batches, 1):
        prompt = build_prompt([(i, id_text[i]) for i in b], gloss, template)
        fn = wd / f"batch_{bi:03d}_프롬프트.txt"
        fn.write_text(prompt, encoding="utf-8")
        man["batches"].append({"no": bi, "ids": b, "prompt_file": fn.name, "chars": len(prompt)})
    (wd / MANIFEST).write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    total_chars = sum(len(t) for t in id_text.values())
    log(f"\n번역할 문구 {len(ids)}개 ({total_chars:,}자) → Copilot 배치 {len(batches)}개")
    log(f"작업 폴더: {wd}")
    if not ids:
        log("모든 문구가 이미 번역되어 있습니다. 바로 '한국어 PDF 만들기'를 실행하세요.")
    return man


def load_manifest(outdir: Path) -> Optional[dict]:
    p = work_dir(outdir) / MANIFEST
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def batch_prompt_text(outdir: Path, man: dict, no: int) -> str:
    b = man["batches"][no - 1]
    p = work_dir(outdir) / b["prompt_file"]
    return p.read_text(encoding="utf-8") if p.exists() else ""


def batch_status(man: dict, cache) -> list[dict]:
    """배치별 번역 완료 수 (번역 메모리 기준)."""
    import om_translate as T
    out = []
    for b in man["batches"]:
        done = sum(1 for i in b["ids"] if T.tm_get(cache, man["items"][i]["text"]) is not None)
        out.append({"no": b["no"], "total": len(b["ids"]), "done": done})
    return out


# ----------------------------------------------------------------------------
# 2) 답변 해석
# ----------------------------------------------------------------------------
ID_BRACKET = re.compile(r"^\[\s*(\d{1,5})\s*\](?:\*\*)?\s*:?\s?(.*)$")       # [0001] 번역 / **[0001]** 번역
ID_PLAIN = re.compile(r"^(\d{3,5})(?:\*\*)?\s*(?:[:.)|\t]|-(?=\s))\s*(.*)$")    # 0001: 번역 / 0001. 번역


def parse_answer(text: str) -> dict[int, str]:
    """Copilot 답변 → {번호: 번역문}. '[0001] 번역', '0001: 번역', 표(| 0001 | ... | 번역 |) 형식 지원.
    번호 없는 줄은 앞 번호의 이어지는 내용으로 붙인다."""
    out: dict[int, str] = {}
    last: Optional[int] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "--") for c in cells):
                continue
            m = re.fullmatch(r"\[?(\d{1,5})\]?", cells[0]) if cells else None
            if m and len(cells) >= 2:
                last = int(m.group(1))
                out[last] = cells[-1]
            continue
        l2 = re.sub(r"^(?:[-*•]\s+)?(?:\*\*)?", "", line)  # 목록 기호/굵게 표시 제거 (번역문 속 기호는 유지)
        m = ID_BRACKET.match(l2) or ID_PLAIN.match(l2)
        if m:
            last = int(m.group(1))
            out[last] = m.group(2).strip().rstrip("*").strip()
        elif last is not None:
            out[last] = (out[last] + " " + line).strip()
    return out


def import_answer(outdir: Path, no: int, answer: str, cache, protector) -> dict:
    """배치 no 의 답변을 검사해서 번역 메모리에 저장. 결과 요약을 돌려준다."""
    import om_translate as T
    man = load_manifest(outdir)
    if man is None:
        raise RuntimeError("먼저 '번역할 문장 추출'을 실행하세요.")
    b = man["batches"][no - 1]
    parsed = parse_answer(answer)
    ok, missing, rejected = 0, [], []
    for i in b["ids"]:
        src = man["items"][i]["text"]
        ko = parsed.get(int(i))
        if ko is None or not ko.strip():
            missing.append(i)
            continue
        ko = T.clean_text(ko.strip().strip('"').strip("“”"))
        miss = T.missing_protected(protector, src, ko)
        if miss:
            rejected.append((i, "원문의 " + ", ".join(miss[:3]) + " 누락/변경"))
            continue
        T.tm_put(cache, src, ko)
        ok += 1
    extra = sorted(set(parsed) - {int(i) for i in b["ids"]})
    (work_dir(outdir) / f"batch_{no:03d}_답변.txt").write_text(answer, encoding="utf-8")
    return {"batch": no, "total": len(b["ids"]), "saved": ok, "missing": missing,
            "rejected": rejected, "extra": extra}


def format_result(r: dict) -> str:
    s = f"배치 {r['batch']}: {r['saved']}/{r['total']}개 저장"
    if r["missing"]:
        s += f"\n  · 답변에 없는 번호 {len(r['missing'])}개: {', '.join(r['missing'][:15])}"
    if r["rejected"]:
        s += f"\n  · 거부 {len(r['rejected'])}개 (Tag/숫자/단위가 바뀜):"
        for i, why in r["rejected"][:10]:
            s += f"\n      [{i}] {why}"
    if r["extra"]:
        s += f"\n  · 이 배치에 없는 번호는 무시: {', '.join(str(x) for x in r['extra'][:10])}"
    if r["missing"] or r["rejected"]:
        s += "\n  → 빠진 문구는 '번역할 문장 추출'을 다시 누르면 남은 것만 새 배치로 만들어집니다."
    return s
