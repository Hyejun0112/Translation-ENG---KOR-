# Plant O&M Translator v2.0 (Ollama 로컬 번역)

발전플랜트 O&M Manual(Boiler, Steam Turbine, Fuel Handling, Mechanical, Electrical, Instrument) PDF를
**원본 레이아웃(그림·표·도면·배경·선) 그대로 두고 O&M 대상 문구만 한국어로 바꾼 PDF**로 자동 번역합니다.

- 번역은 **이 PC의 Ollama + Qwen2.5 14B**로 합니다. **무료**이고 **문서 내용이 외부로 전송되지 않습니다.**
- Word 원본이 없어도 되고, 스캔 PDF / 글자가 깨져 복사되는 PDF도 OCR로 처리합니다.

```
run.bat 더블클릭  →  input\pdf 폴더에 PDF 넣기  →  output\원본파일명_KR_O&M.pdf 자동 생성
```

---

## 1. run.bat 이란?

Windows에서 **더블클릭하면 프로그램을 실행해 주는 파일**입니다. Python 명령어를 몰라도 됩니다.
`run.bat`을 더블클릭하면 다음을 자동으로 합니다.

1. (처음 한 번) 프로그램 전용 Python 환경과 부품 설치 — 1~3분
2. (처음 한 번) **Ollama 설치** → **Qwen2.5 14B 모델 다운로드(약 9GB)** — 네트워크에 따라 10분~1시간
3. (처음 한 번) 스캔 PDF용 **Tesseract OCR** 설치 여부 질문 → `Y`
4. `input\pdf` 폴더의 PDF를 번역해서 `output` 폴더에 저장
5. 창을 열어 두면 `input\pdf`에 새로 넣는 PDF도 계속 자동 번역

> 실행은 **파일 탐색기에서 `run.bat` 더블클릭**입니다. (VS Code에서 열면 내용만 보이고 실행되지 않습니다)

## 2. 필요한 것

| 항목 | 내용 |
|---|---|
| Python 3.12 | https://www.python.org/downloads/ — 설치 화면에서 **"Add python.exe to PATH" 체크** |
| 메모리 | **RAM 16GB 이상** 권장 (부족하면 더 작은 `qwen2.5:7b` 사용을 자동 제안) |
| GPU (선택) | NVIDIA 12GB 이상이면 빠름. 없으면 CPU로 번역 (느리지만 동작) |
| 디스크 | 여유 공간 15GB 이상 (모델 약 9GB) |
| 인터넷 | **처음 설치·모델 다운로드 때만** 필요. 이후 번역은 인터넷 없이 동작 |

## 3. 사용 방법

**방법 1: 폴더에 넣기**
1. `run.bat` 더블클릭 → 검은 창이 뜹니다. (닫지 마세요)
2. `input\pdf` 폴더에 PDF를 넣습니다. (여러 개, 하위 폴더 가능)
3. 번역이 끝나면 `output` 폴더가 열립니다.

**방법 2: 끌어다 놓기**
- PDF 파일(여러 개)이나 폴더를 `run.bat` 아이콘 위로 끌어다 놓습니다.

**번역 속도**: 진행률과 남은 시간이 화면에 표시됩니다. GPU 없는 노트북에서는 수백 페이지 매뉴얼이 몇 시간 걸릴 수 있습니다.
중간에 창을 닫아도 번역한 문구는 저장되어 있어서, 다시 실행하면 **이어서** 진행합니다.
먼저 `run.bat --pages 1-5`로 몇 페이지만 번역해 품질과 속도를 확인하는 것을 권장합니다.

## 4. 결과물 (`output` 폴더)

| 파일 | 내용 |
|---|---|
| `원본파일명_KR_O&M.pdf` | 한국어 O&M 문서. 원본과 같은 페이지 수·크기·순서, 텍스트 검색 가능 |
| `원본파일명_Translation_Log.xlsx` | **번역로그**(원문/번역/상태/비고) · **페이지**(번역/원문유지와 사유) · **최종검사** |
| `Plant_O&M_Glossary.xlsx` | 적용된 용어집 + 자주 나오는 미등록 용어 후보 (누적) |

**번역 수정**: `Translation_Log.xlsx`의 **`수정 번역(입력)`** 열에 고쳐 쓰고 저장 → `run.bat` 실행 → 해당 PDF만 다시 만들어지고,
수정한 번역은 번역 메모리에 저장되어 다른 문서에도 똑같이 적용됩니다.

## 5. 번역 규칙

- **번역**: 제목, 본문, 글머리표, 절차, WARNING/CAUTION/NOTE, 표 머리글·지시문, 그림 제목, 북마크
- **페이지 전체 원문 유지 (Drawing / Datasheet 제외)**: 제목이 Datasheet, Technical Data, Parts List, Bill of Material, Certificate,
  P&ID, PFD, Logic/Wiring/Single Line Diagram, GA/Arrangement/Dimensional Drawing 등인 페이지, A3 이상 도면 페이지
  (본문 속 "P&ID No. 0061" 같은 참조는 무시하고, 문장 위주 페이지는 번역)
- **표**: 셀 단위로 번역, 셀 안에서 줄바꿈. 머리글이 Tag/Model/Parameter/Code/Value/Unit 인 **열은 원문 유지**
- **항상 원문 유지 (Tag 보호)**: Tag No., 모델/부품/도면/문서번호, Rev., 수치+단위, 2oo3, 화학식, 규격, 약어(PLC, DCS…), URL.
  번역 결과에서 보호 값이 빠지면 잘못된 번역을 넣지 않고 원문을 유지합니다.
- **용어집** `data\glossary.csv`(Excel로 편집 가능): operation=운전, maintenance=정비, start-up=기동, trip=트립 …,
  WARNING=경고, CAUTION=주의, NOTE=참고. `ko`가 `KEEP`이면 영어 유지.
- 추가 설정: `data\keep_patterns.txt`(영어 유지 정규식), `data\scope_keywords.txt`(제외 페이지 제목), `config.py`(모델·속도·OCR)

## 6. 고급 사용

| 명령 | 설명 |
|---|---|
| `run.bat --pages 1-5` | 각 PDF의 1~5페이지만 샘플 번역 |
| `run.bat --once` | 일괄 처리만 하고 종료 |
| `run.bat --force` | 이미 번역된 파일도 다시 생성 |
| `run.bat --model qwen2.5:7b` | 더 작은(빠른) 모델 사용 |
| `run.bat --estimate` | 번역 분량과 예상 시간 |
| `run.bat --check` | 설치 상태 점검 (패키지 / Tesseract / Ollama / 모델 / RAM / GPU) |
| `run.bat --mock --once` | 번역 없이 레이아웃만 테스트 (글자 앞에 `[번역]`) |

## 7. 폴더 구조

```
├─ run.bat                     실행 (더블클릭 / 끌어다 놓기)
├─ main.py                     전체 흐름: 준비 → 일괄 처리 → 폴더 감시
├─ config.py                   설정 (모델, 속도, OCR, 번역 범위)
├─ requirements.txt
├─ engines\
│   ├─ ollama_manager.py       Ollama 자동 설치·실행, 모델 다운로드, 사양 확인
│   ├─ translation_engine.py   Ollama 번역, 번역 메모리(SQLite), 진행률
│   ├─ pdf_engine.py           페이지 진단, 텍스트 추출, 레이아웃 유지 한글 삽입
│   ├─ glossary_engine.py      용어집, Tag/숫자/단위 보호
│   ├─ table_engine.py         표 셀 인식, Tag/Value 열 원문 유지
│   ├─ scope_engine.py         Drawing / Datasheet 제외 판단
│   ├─ ocr_engine.py           스캔 PDF OCR (Tesseract + OpenCV)
│   └─ qa_engine.py            최종 검사, Translation Log, Glossary Excel
├─ data\                       glossary.csv, keep_patterns.txt, scope_keywords.txt
├─ tools\install_tesseract.py  Tesseract 자동 설치
├─ input\pdf\                  ← 원본 PDF를 넣는 곳
├─ output\                     ← 결과
├─ cache\                      번역 메모리 / OCR 캐시
└─ logs\                       실행 로그 (문제가 생기면 이 파일을 확인)
```

## 8. 문제 해결

| 증상 | 해결 |
|---|---|
| "Python 이 설치되어 있지 않습니다" | Python 3.12 설치 (PATH 체크) 후 다시 실행 |
| 모델 다운로드 실패 | 회사망에서 `registry.ollama.ai`가 막힌 경우. 다른 네트워크에서 `run.bat`을 한 번 실행해 모델만 받아 두면 이후엔 인터넷 없이 사용 |
| 너무 느림 | `run.bat --model qwen2.5:7b` 또는 `config.py`의 `DEFAULT_MODEL`을 `qwen2.5:7b`로 |
| 결과 PDF가 열려 있어 저장 실패 | Bluebeam 등에서 닫거나, 자동으로 `_시간` 붙은 이름으로 저장됨 |
| 목차/스캔 페이지가 영어로 남음 | Tesseract 설치: `run.bat`을 다시 실행해 설치 질문에 `Y` |

**주의**: 오프라인 모델(Qwen2.5 14B)은 상용 번역 서비스보다 품질이 낮을 수 있습니다.
**WARNING/CAUTION, 설정값, 인터록, 트립 조건 등 안전·운전 직결 문구는 반드시 사람이 검수**하세요.
