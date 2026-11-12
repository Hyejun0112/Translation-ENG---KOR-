# OM 영→한 번역 도구 (PDF 레이아웃 유지 + OCR)

발전소 OM(Operating & Maintenance Manual) PDF를 **원본 양식(그림·표·배경·선·링크·북마크) 그대로 두고 글자만 한글로 교체**합니다.
Word 원본이 없어도 됩니다. 텍스트가 살아있는 PDF, 스캔 PDF, 글자가 깨져서 복사되는 PDF, 그림 속 글자까지 처리합니다.
규격, 모델명, Tag No., 도면번호, Rev., 수치+단위, 약어, 브랜드명은 영어로 남깁니다.

| 파일 | 역할 |
|---|---|
| `om_translate.py` | 메인 프로그램 (추정 / 용어 추출 / 번역) |
| `om_ocr.py` | OCR 엔진 (Tesseract + OpenCV). 단독 실행으로 페이지별 OCR 확인 가능 |
| `check_env.py` | 설치 상태 점검 |
| `glossary_sample.csv` | 용어집 예시 → 복사해서 `glossary.csv`로 사용 |
| `keep_patterns.txt` | 영어로 남길 프로젝트 전용 정규식 |
| `.vscode/` | VS Code 실행(F5) 구성, 작업(Task), 설정 |

---

## 1. 설치 (Windows + VS Code)

### 1-1. 저장소 받기
```powershell
git clone https://github.com/Hyejun0112/Translation-ENG---KOR-.git
cd Translation-ENG---KOR-
code .
```
VS Code에서 권장 확장(Python, Pylance, Rainbow CSV) 설치 안내가 뜨면 설치합니다.

### 1-2. 파이썬 패키지
VS Code 메뉴 **터미널 → 작업 실행 → "환경 설정: 가상환경 생성 + 패키지 설치"**
(또는 직접)
```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
`Ctrl+Shift+P → Python: Select Interpreter → .venv` 선택.

### 1-3. Tesseract OCR (스캔 PDF / 그림 속 글자용)
1. https://github.com/UB-Mannheim/tesseract/wiki 에서 Windows 설치파일(64bit) 다운로드
2. 기본 경로 `C:\Program Files\Tesseract-OCR` 에 설치 (영어 데이터 `eng`는 기본 포함)
3. 자동으로 찾습니다. 다른 경로면 `--tesseract "D:\Tools\Tesseract-OCR\tesseract.exe"` 지정

### 1-4. API 키
```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."     # 설정 후 VS Code를 완전히 껐다 켜야 반영됩니다
```

### 1-5. 점검
**작업 실행 → "점검: Tesseract / 한글 폰트 / 패키지"** 또는 `py check_env.py` → 모두 `[OK]`이면 준비 완료.
한글 폰트는 `C:\Windows\Fonts\malgun.ttf`(맑은 고딕)를 자동 사용합니다.

> 고객 문서는 저장소에 올라가지 않도록 `.gitignore`에서 `*.pdf`, `OM_EN/`, `OM_KO/` 등을 제외해 두었습니다.

---

## 2. 작업 순서 (VS Code에서 F5 → 구성 선택)

원본 PDF를 프로젝트 안 `OM_EN` 폴더에 넣습니다. `glossary_sample.csv`를 복사해 `glossary.csv`를 만듭니다.

| F5 구성 | 명령 | 하는 일 |
|---|---|---|
| 1) 분량 추정 + 페이지 진단 | `py om_translate.py estimate .\OM_EN --glossary glossary.csv` | 페이지 종류(네이티브/스캔/글자깨짐), 번역 분량, 토큰 추정. API 호출 없음 |
| 2) 용어 후보 추출 | `py om_translate.py terms .\OM_EN -o terms_candidates.csv` | 자주 나오는 용어 → `ko` 열을 채워 `glossary.csv`에 합침 (스캔 페이지는 OCR해서 포함) |
| 3) OCR 결과 확인 | `py om_ocr.py .\OM_EN\P-101_OM.pdf --page 12 --debug` | 해당 페이지 인식 결과 출력 + 인식 박스(빨강)/표 셀(초록)을 그린 `ocr_debug_p12.png` |
| 4) 레이아웃 테스트 (mock) | `... translate ... --backend mock --pages 1-5 --compare` | API 없이 `[번역]` 접두로 배치/표/그림 처리만 확인 |
| 5) 샘플 번역 | `... translate .\OM_EN\P-101_OM.pdf -o .\OM_SAMPLE --glossary glossary.csv --keep-patterns keep_patterns.txt --pages 1-5 --compare` | 5페이지만 실제 번역 + 원본|번역 비교본 |
| 6) 전체 번역 | `... translate .\OM_EN -o .\OM_KO --glossary glossary.csv --keep-patterns keep_patterns.txt` | 폴더 전체. 이미 만든 파일은 건너뛰고 이어서 진행 |

중단 후 다시 실행해도 `translation_cache.sqlite`에 **번역 결과와 OCR 결과**가 저장되어 있어 다시 계산하지 않습니다.
같은 문구(머리글/바닥글/표 헤더 등)는 문서 전체에서 한 번만 번역합니다.

---

## 3. PDF 종류별 처리 방식

`estimate`가 페이지마다 자동 진단하고, `translate`가 진단 결과에 따라 처리합니다.

| 페이지 종류 | 판단 기준 | 처리 |
|---|---|---|
| **네이티브** | 텍스트 레이어 정상 | 원문 글자만 리댁션으로 지우고(선·그림·배경 보존) 같은 자리에 한글 삽입. 선택/검색 가능 |
| **스캔(이미지)** | 텍스트 없음 + 이미지 | 400 dpi OCR → 원본 배경색 패치로 영어를 덮고 한글 삽입 |
| **글자깨짐** | 복사하면 `�`/기호로 나오는 PDF (폰트 매핑 누락) | 화면에 보이는 대로 OCR해서 처리 |
| **스캔+숨은OCR층** | 스캔 이미지 위에 투명 텍스트(Acrobat OCR 등) | 숨은 층은 지우고 자체 OCR로 처리 |
| **그림 속 글자** | 네이티브 페이지 안의 래스터 그림(사진/스캔 도면/캡처) | 그림 영역만 OCR(희소 텍스트 모드)해서 라벨/범례 번역. 벡터 글자와 겹치는 부분은 제외 |

### OCR 정확도를 높이는 처리 (`om_ocr.py`)
- **400 dpi 렌더링**, 그림은 500 dpi
- **기울기 자동 보정(deskew)**: ±3° 범위에서 0.05° 단위로 찾아 보정 후 인식, 좌표는 원래 페이지로 되돌림
- **표 격자 인식**: 가로/세로 괘선을 형태학 연산으로 찾아 셀 단위로 글자를 묶음 → 번역문이 셀 밖으로 넘치지 않음, 패치가 괘선을 덮지 않음
- **누락 복구 패스**: Tesseract가 그림으로 오인해 건너뛴 글자(색 배경 표 헤더 등)를 인식된 단어를 지운 이미지에서 다시 찾음
- **빈 셀 재인식**: 글자가 있는데 비어 있는 셀은 셀만 잘라 확대/반전 후 다시 OCR
- **신뢰도 낮은 줄 재인식**: 해당 줄만 잘라 이진화·반전·확대 후 단일 줄 모드로 다시 인식, 더 나은 결과 채택
- **배경색/글자색/굵기/크기 추정**: 색 배경 위 글자는 같은 배경색으로 덮고, 빨간 WARNING은 빨간색으로 씀
- 번역 모델에 "OCR 오인식(rn↔m, 0↔O 등)은 의도한 단어로 추정해서 번역"하도록 지시

### OCR 옵션
| 옵션 | 설명 |
|---|---|
| `--ocr auto` | (기본) 스캔/글자깨짐 페이지만 페이지 전체 OCR |
| `--ocr force` | 모든 페이지를 OCR 방식으로 처리 (텍스트 레이어가 있지만 이상할 때) |
| `--ocr off` | OCR 사용 안 함 |
| `--no-ocr-figures` | 그림 속 글자 OCR 끄기 (도면 기호가 많아 잡음이 생길 때) |
| `--ocr-dpi 400` | 해상도. 작은 글자가 많으면 500~600 (느려짐) |
| `--ocr-min-conf 50` / `--ocr-fig-min-conf 70` | 이 신뢰도 미만 블록은 번역하지 않고 원본 유지 |
| `--ocr-lang eng` | 원문 언어 (Tesseract 언어 데이터) |
| `--ocr-workers N` | OCR 동시 처리 수 (기본: CPU 코어 수의 절반) |
| `--no-deskew` | 기울기 보정 끄기 |

---

## 4. 표 / 그림 / 레이아웃

- **표(네이티브)**: PyMuPDF `find_tables`로 괘선 표의 셀 경계를 찾아 셀 단위로 번역하고 셀 안에서 줄바꿈합니다. 괘선 없는 표는 열 정렬로 묶습니다.
- **표(스캔)**: 위 OCR 표 격자 인식 사용.
- **그림**: 그림 자체(벡터 도면, 사진, 래스터 이미지)는 손대지 않습니다. 글자만 교체합니다. 그림 속 라벨은 원래 자리를 거의 벗어나지 않게 글자를 줄여서 맞춥니다.
- **배치 규칙**: ① 원래 박스에서 92%까지 축소 → ② 옆 글자/셀 테두리 직전까지 폭 확장 → ③ 셀 바닥(또는 한 줄)까지 높이 확장 → ④ 85% → ⑤ 하한(`--min-scale 0.6`, `--min-size 5`)까지 축소. 그래도 넘치면 `OVERFLOW` 표시.
- **회전 페이지**: 보이는 모습 그대로 회전값만 정규화해서 한글이 똑바로 들어갑니다. 페이지 안에서 회전된 글자는 `ROTATED_SKIPPED`로 표시하고 건너뜁니다.
- **링크/북마크**: 링크는 유지, 북마크 제목도 번역합니다.
- **출력 검증**: 저장 후 다시 열어 페이지 수, 한글 추출 가능 여부, 깨진 글자를 확인하고 문제가 있으면 경고합니다.

---

## 5. 용어집 (glossary.csv / .xlsx)

| en | ko | note |
|---|---|---|
| Feed Water Pump | 급수 펌프 | |
| Induced Draft Fan | 유인통풍기(ID Fan) | 약어 병기 예시 |
| PLC | KEEP | 영어 유지 |

- `ko`에 **KEEP**(또는 `=`, `-`, `유지`)이면 해당 용어는 영어로 유지합니다. `ko`가 빈 행은 무시합니다.
- 복수형(pumps 등)과 대소문자는 자동 처리됩니다. 긴 용어가 우선 적용됩니다.
- CSV는 UTF-8 또는 Excel 기본 저장(cp949) 모두 읽습니다.
- 번역 후 용어집 용어가 반영되지 않은 블록은 리포트에 `GLOSSARY_MISS`로 표시됩니다.

## 6. 영어 유지 규칙

내장 패턴: 규격(ASME B31.1, API 610, ISO 9001:2015 …), Tag No./모델명/부품번호(P-101A, HPK-L 100-250 …),
`Rev. B`, 수치+단위(120 bar(g), 250 °C, 45 m3/h, 75 kW …), 날짜, URL/이메일.
짧은 문구(80자 이하)의 숫자는 모두 보호되어 `Page 12 of 340` 같은 바닥글도 한 번만 번역됩니다.
프로젝트 전용 규칙은 `keep_patterns.txt`에 정규식으로 추가하세요.

번역기가 보호 토큰을 하나라도 빠뜨리거나 바꾸면 **틀린 번역을 넣지 않고 원문 영어를 그대로 두고** `FAILED`로 표시합니다.

---

## 7. 출력물과 검수 (Bluebeam)

| 파일 | 내용 |
|---|---|
| `OM_KO\<이름>_KO.pdf` | 번역본 (원본과 같은 페이지 수·크기, 텍스트 선택/검색 가능) |
| `OM_KO\<이름>_KO_compare.pdf` | `--compare` 시: 왼쪽 원본 / 오른쪽 번역본 나란히 |
| `OM_KO\<이름>_KO.review.csv` | 블록별 페이지/좌표/출처(native, ocr-page, ocr-figure)/OCR 신뢰도/원문/번역/경고 |
| `OM_KO\summary.csv` | 파일별 번역/실패/검토필요/스캔페이지/OCR블록/검증경고 |

`review.csv`의 `notes`로 검수 대상을 찾습니다:
`OVERFLOW`, `FAILED`, `ROTATED_SKIPPED`, `GLOSSARY_MISS`, `OCR_LOW_CONF(신뢰도)`, `RENDER_FAILED`.

**Bluebeam 검수 팁**
- 번역본은 원본과 페이지 크기·좌표가 같아서 Bluebeam **Document → Compare Documents** 또는 **Overlay Pages**로 원본과 바로 겹쳐 볼 수 있습니다.
- `review.csv`의 `page`, `x0,y0`(pt, 좌상단 기준)로 위치를 찾아 Markup 하면 됩니다.
- 수정할 문구는 `overrides.csv`(원문,번역 두 열)에 넣고 `--overrides overrides.csv --force`로 재생성하면 통째로 교체됩니다. (OCR/번역 캐시 덕분에 빠름)

## 8. 기타 옵션

| 옵션 | 설명 |
|---|---|
| `--tone formal\|concise` | `~합니다/~하십시오`(기본) 또는 개조식 `~할 것/~함` |
| `--model` | Claude 모델 (기본 `claude-sonnet-5-5`, 환경변수 `OM_MODEL`) |
| `--workers 4` | 동시 API 호출 수 |
| `--pages 1-5,8` | 일부 페이지만 처리하고 해당 페이지만 출력 (샘플용) |
| `--font`, `--font-bold` | 한글 폰트 직접 지정 |
| `--whiteout` | 네이티브 번역 영역을 흰색으로 덮음 (원본 글자가 비치는 특수 PDF) |
| `--cache` | 캐시 파일 위치 |
| `--force` | 이미 있는 결과 덮어쓰기 |

## 9. 한계 및 주의

- 스캔 품질이 나쁘면(200 dpi 미만, 번짐, 손글씨) OCR 오류가 생깁니다. `OCR_LOW_CONF` 블록은 원문을 꼭 확인하세요. **가능하면 제작사 원본(텍스트가 살아있는) PDF**를 받아 쓰는 것이 가장 좋습니다.
- 스캔 페이지에서는 한글이 원본 배경색 패치 위에 쓰입니다. 종이 질감/얼룩이 있는 스캔은 패치 부분이 약간 매끈하게 보일 수 있습니다.
- 복잡한 P&ID/도면 이미지의 작은 기호는 OCR이 글자로 오인할 수 있어 그림 OCR은 신뢰도 70 이상만 반영합니다. 잡음이 많으면 `--no-ocr-figures`.
- **WARNING/CAUTION, 설정값, 인터록, 트립 조건 등 안전·운전 직결 문구는 반드시 사람이 검수하세요.** 자동 번역본을 그대로 운전원에게 배포하지 마세요.
- **보안**: 문서 텍스트가 외부 API(Anthropic)로 전송됩니다. 사업주와의 계약/NDA상 허용되는지 먼저 확인하세요. (OCR은 PC 안에서만 실행됩니다)
