# -*- coding: utf-8 -*-
"""
config.py - Plant O&M Translator v2.0 설정
==========================================
대부분의 사용자는 이 파일을 고칠 필요가 없습니다.
바꿀 수 있는 주요 항목:
  - DEFAULT_MODEL      : 번역에 사용할 Claude 모델
  - TRANSLATE_SCOPE    : "om"(O&M 대상만 번역, 도면/데이터시트 원문 유지) 또는 "all"
  - OCR_*              : 스캔 PDF 인식 설정
  - WATCH_INTERVAL_SEC : input\\pdf 폴더 확인 주기(초)
API 키는 이 파일이 아니라 프로젝트 폴더의 .env 파일에 저장됩니다(프로그램 창에서 입력 → [저장]).
.env 는 .gitignore 에 들어 있어 GitHub 에 올라가지 않습니다. API 키를 코드에 직접 쓰지 마세요.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ----------------------------------------------------------------------------
# 프로그램 정보
# ----------------------------------------------------------------------------
APP_NAME = "Plant O&M Translator"
VERSION = "2.0.0"
APP_TITLE = f"{APP_NAME} v{VERSION}"

# ----------------------------------------------------------------------------
# 폴더 / 파일
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input" / "pdf"          # 여기에 원본 PDF를 넣는다
OUTPUT_DIR = BASE_DIR / "output"                # 번역본/로그/용어집이 생기는 곳
LOG_DIR = BASE_DIR / "logs"                     # 실행 로그(문제 발생 시 확인)
CACHE_DIR = BASE_DIR / "cache"                  # 번역 메모리 + OCR 캐시 (지우면 다시 번역/인식)
DATA_DIR = BASE_DIR / "data"                    # 용어집, 영어 유지 패턴, 제외 키워드
TOOLS_DIR = BASE_DIR / "tools"

GLOSSARY_FILE = DATA_DIR / "glossary.csv"            # 용어집 (en, ko, note) - Excel(xlsx)도 가능
GLOSSARY_XLSX_FILE = DATA_DIR / "glossary.xlsx"      # 있으면 csv 대신 사용
KEEP_PATTERNS_FILE = DATA_DIR / "keep_patterns.txt"  # 영어로 남길 추가 정규식
SCOPE_KEYWORDS_FILE = DATA_DIR / "scope_keywords.txt"  # 원문 유지할 문서 제목 키워드 추가
CACHE_FILE = CACHE_DIR / "translation_cache.sqlite"
ENV_FILE = BASE_DIR / ".env"

OUTPUT_SUFFIX = "_KR_O&M"                       # 원본파일명_KR_O&M.pdf
LOG_SUFFIX = "_Translation_Log"                 # 원본파일명_Translation_Log.xlsx
GLOSSARY_XLSX_NAME = "Plant_O&M_Glossary.xlsx"  # 결과 폴더의 누적 용어집
PROCESSED_DIRNAME = "_done"                     # (선택) 처리 끝난 원본을 옮길 하위 폴더 이름

def load_env() -> None:
    """.env 파일의 KEY=VALUE 를 환경변수로 읽는다 (이미 설정된 값은 유지)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v


load_env()  # 아래 설정들이 .env 값을 쓰도록 먼저 읽는다


# ----------------------------------------------------------------------------
# 번역 (Claude API)
# ----------------------------------------------------------------------------
DEFAULT_MODEL = os.environ.get("OM_MODEL", "claude-sonnet-5-5")
# 프로그램 창에서 고를 수 있는 모델: (모델 ID, 설명)
MODEL_CHOICES = [
    ("claude-sonnet-5-5", "Sonnet - 품질/비용 균형 (권장)"),
    ("claude-haiku-5-5", "Haiku - 빠르고 저렴"),
    ("claude-opus-5-5", "Opus - 최고 품질, 비용 높음"),
]
API_WORKERS = 4                 # 동시 API 호출 수 (속도 제한 오류가 나면 2로)
MAX_CHUNK_CHARS = 4500          # 한 번 호출에 보내는 최대 글자 수
MAX_CHUNK_ITEMS = 40            # 한 번 호출에 보내는 최대 문구 수
TONE = "formal"                 # formal: ~합니다/~하십시오 | concise: 개조식

# ----------------------------------------------------------------------------
# 번역 범위 / 레이아웃
# ----------------------------------------------------------------------------
TRANSLATE_SCOPE = "om"          # om: 도면·데이터시트·부품목록·표 Tag/Value/Unit 열 원문 유지 | all
TRANSLATE_FIGURE_TEXT = False   # 그림(래스터 이미지) 속 글자까지 번역 (도면 내부 문자는 원문 유지 원칙)
MIN_FONT_SCALE = 0.6            # 한글이 길 때 글자 축소 하한 (원래 크기 대비)
MIN_FONT_SIZE = 5.0             # 글자 크기 하한 (pt)

# ----------------------------------------------------------------------------
# OCR (스캔 PDF / 글자깨짐 페이지)
# ----------------------------------------------------------------------------
OCR_MODE = "auto"               # auto: 필요한 페이지만 | force: 모든 페이지 | off
OCR_LANG = "eng"
OCR_DPI = 400
OCR_MIN_CONF = 50.0
OCR_FIG_MIN_CONF = 70.0
OCR_WORKERS = max(1, (os.cpu_count() or 2) // 2)
OCR_DESKEW = True
TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "")   # 비우면 자동 탐색

# ----------------------------------------------------------------------------
# 폴더 감시
# ----------------------------------------------------------------------------
WATCH_INTERVAL_SEC = 5          # input\pdf 폴더 확인 주기
FILE_STABLE_SEC = 3             # 파일 복사가 끝났다고 보는 대기 시간
MOVE_DONE_INPUTS = False        # True면 처리한 원본을 input\pdf\_done 으로 이동


@dataclass
class Config:
    """한 번의 실행에 쓰는 설정 묶음 (엔진들이 공통으로 사용)."""
    backend: str = "anthropic"          # anthropic | mock(레이아웃 테스트)
    model: str = DEFAULT_MODEL
    tone: str = TONE
    workers: int = API_WORKERS
    max_chunk_chars: int = MAX_CHUNK_CHARS
    max_chunk_items: int = MAX_CHUNK_ITEMS
    min_scale: float = MIN_FONT_SCALE
    min_size: float = MIN_FONT_SIZE
    ocr_mode: str = OCR_MODE
    ocr_figures: bool = TRANSLATE_FIGURE_TEXT
    ocr_workers: int = OCR_WORKERS
    ocr_lang: str = OCR_LANG
    ocr_dpi: int = OCR_DPI
    ocr_min_conf: float = OCR_MIN_CONF
    ocr_fig_min_conf: float = OCR_FIG_MIN_CONF
    deskew: bool = OCR_DESKEW
    tesseract_cmd: str = TESSERACT_CMD
    scope: str = TRANSLATE_SCOPE
    whiteout: bool = False
    font: str = ""
    font_bold: str = ""
    pages: Optional[list[int]] = None
    force: bool = False


# ----------------------------------------------------------------------------
# .env (API 키 저장)
# ----------------------------------------------------------------------------
def save_env(key: str, value: str) -> None:
    """.env 에 KEY=VALUE 를 저장(있으면 교체)."""
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = [ln for ln in ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
                 if not ln.strip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def get_api_key() -> str:
    load_env()
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def glossary_path() -> Optional[Path]:
    if GLOSSARY_XLSX_FILE.exists():
        return GLOSSARY_XLSX_FILE
    if GLOSSARY_FILE.exists():
        return GLOSSARY_FILE
    return None


def ensure_dirs() -> None:
    for d in (INPUT_DIR, OUTPUT_DIR, LOG_DIR, CACHE_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
