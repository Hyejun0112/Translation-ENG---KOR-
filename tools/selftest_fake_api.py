#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_fake_api.py - API 비용 없이 '실제 번역 경로 전체'를 시험하는 개발용 도구
=====================================================================
Claude API 대신 가짜 서버(실제 SDK 예외/응답 모양을 흉내)를 끼워 넣고 main.py 의 일괄 번역을 그대로 실행합니다.
번역문은 길이가 비슷한 가짜 한글이라 레이아웃·안정성만 확인됩니다 (번역 품질은 실제 API 로 확인).

사용:  python tools/selftest_fake_api.py 결과폴더 원본.pdf [원본2.pdf …] [--mode normal|chaos|credit|oldsdk|nostruct|auth|ssl] [--ocr] [--pages 1-20]
  normal   : 정상 응답 (모든 토큰 뒤 조사 병기 '을(를)' 포함 → 조사 보정 확인)
  chaos    : 응답 잘림(max_tokens) / 과부하(529) / 거절(refusal) / 문구 누락 / 보호 토큰 누락 / ```json 감싸기 섞기
  credit   : 7번째 호출부터 잔액 부족 → 즉시 중지되는지
  oldsdk   : 구버전 SDK(TypeError) → 지원 안 되는 기능을 끄고 계속하는지
  nostruct : 구조화 출력 미지원(400) → 끄고 계속하는지
  auth/ssl : 키 오류 / 회사망 인증서 오류 → 알기 쉬운 문구로 즉시 중지되는지
결과는 결과폴더/output_dir 에 생기고, tools/check_layout.py · tools/check_translation.py 로 점검합니다.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import hashlib, json, random, re, threading
from types import SimpleNamespace as NS
import anthropic
try:
    import httpx2 as httpx
except ImportError:
    import httpx
REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
def err(cls, code, msg):
    return cls(msg, response=httpx.Response(code, request=REQ), body={"error": {"message": msg}})
SYL = "가나다라마바사아자차카타파하운전정비점검보일러압력온도밸브확인시험"
def ko_of(t):
    def rep(m):
        w = m.group(0); h = int(hashlib.md5(w.lower().encode()).hexdigest(), 16)
        return "".join(SYL[(h >> (5 * k)) % len(SYL)] for k in range(max(1, round(len(w) * 0.5))))
    parts = re.split(r"(\[\[K\d+\]\])", t)
    last = max((i for i, p in enumerate(parts) if p.startswith("[[K")), default=-1)
    return "".join(p + ("을(를)" if i == last else "") if p.startswith("[[K") else re.sub(r"[A-Za-z]{2,}", rep, p) for i, p in enumerate(parts))
class Stats:
    lock = threading.Lock(); calls = 0; items = 0; kinds = {}
def bump(k):
    with Stats.lock: Stats.kinds[k] = Stats.kinds.get(k, 0) + 1
class Msgs:
    def __init__(self, beta=False): self.beta = beta
    def create(self, **kw):
        mode = os.environ.get("MODE", "normal")
        if self.beta and mode == "oldsdk": raise TypeError("create() got an unexpected keyword argument 'fallbacks'")
        if "output_config" in kw and mode == "oldsdk" and "format" in kw["output_config"]:
            raise TypeError("create() got an unexpected keyword argument 'output_config'")
        if mode == "nostruct" and "format" in kw.get("output_config", {}):
            raise err(anthropic.BadRequestError, 400, "output_config.format: structured outputs are not supported")
        if mode == "auth": raise err(anthropic.AuthenticationError, 401, "invalid x-api-key")
        if mode == "ssl": raise anthropic.APIConnectionError(message="[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain", request=REQ)
        with Stats.lock:
            Stats.calls += 1; n = Stats.calls
        if mode == "credit" and n > 6: raise err(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API.")
        user = kw["messages"][0]["content"]
        items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
        with Stats.lock: Stats.items += len(items)
        rnd = random.Random(n)
        if mode == "chaos":
            if rnd.random() < 0.08: bump("529"); raise err(anthropic.InternalServerError, 529, "Overloaded")
            if len(items) > 12: bump("max_tokens"); return NS(content=[NS(type="text", text='{"translations":[{"id":"0","ko":"잘')], usage=NS(input_tokens=900, output_tokens=16000, cache_read_input_tokens=1100, cache_creation_input_tokens=0), stop_reason="max_tokens")
            if any("Warranty" in it["text"] for it in items): bump("refusal"); return NS(content=[], usage=NS(input_tokens=500, output_tokens=0), stop_reason="refusal")
        out = []
        for it in items:
            ko = ko_of(it["text"])
            if mode == "chaos" and rnd.random() < 0.05 and "[[K" in ko: ko = re.sub(r"\[\[K\d+\]\]", "", ko, 1); bump("token_drop")
            if mode == "chaos" and rnd.random() < 0.03: bump("missing_item"); continue
            out.append({"id": it["id"], "ko": ko})
        text = json.dumps({"translations": out}, ensure_ascii=False)
        if mode == "chaos" and rnd.random() < 0.05: text = "```json\n" + text + "\n```"; bump("fenced")
        return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
                  usage=NS(input_tokens=len(user) // 3, output_tokens=len(text) // 2, cache_read_input_tokens=1100 if n > 1 else 0, cache_creation_input_tokens=1100 if n == 1 else 0),
                  stop_reason="end_turn")
class FakeClient:
    def __init__(self, *a, **k):
        self.messages = Msgs(); self.beta = NS(messages=Msgs(beta=True))
        self.models = NS(list=lambda **kw: [])


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    mode = argv[argv.index("--mode") + 1] if "--mode" in argv else "normal"
    if "--mode" in argv:
        args.remove(mode)
    extra = []
    if "--pages" in argv:  # 쪽 범위만 시험 (예: --pages 18-22)
        pages = argv[argv.index("--pages") + 1]
        args.remove(pages)
        extra = ["--pages", pages]
    os.environ["MODE"] = mode
    out = Path(args[0])
    import config
    for n in ("INPUT_DIR", "OUTPUT_DIR", "LOG_DIR", "CACHE_DIR"):
        setattr(config, n, out / n.lower())
        getattr(config, n).mkdir(parents=True, exist_ok=True)
    config.CACHE_FILE = config.CACHE_DIR / "c.sqlite"
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-selftest")
    import engines.translation_engine as TE
    TE.make_client = lambda key, max_retries=3: FakeClient()
    TE.time.sleep = lambda s: None  # 재시도 대기 생략
    import engines.pdf_engine as P
    for kind, f in (("regular", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
                    ("bold", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf")):
        if Path(f).exists():
            P.FONT_CANDIDATES[kind].insert(0, f)
    import main as M
    rc = M.main(["--once", "--no-open"] + ([] if "--ocr" in argv else ["--no-ocr"]) + extra + args[1:])
    print("RC", rc, "| fake API calls", Stats.calls, "items", Stats.items, "| injected faults", Stats.kinds)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
