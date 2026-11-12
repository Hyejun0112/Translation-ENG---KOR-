# OM 영→한 번역기 (PDF 양식 유지 · API 없이 Copilot으로 번역)

발전소 O&M 매뉴얼 PDF를 **원본 양식(그림·표·도면·배경·선·링크) 그대로 두고, O&M 대상 문구만 한국어로 교체한 개별 PDF**로 만듭니다.
Word 원본이 없어도 되고, 스캔 PDF / 글자가 깨져 복사되는 PDF도 OCR로 처리합니다.
번역은 **유료 API 없이** 회사 Copilot(또는 다른 챗봇)으로 합니다. 프로그램이 문장을 뽑아 프롬프트를 만들고, 답변을 붙여넣으면 PDF를 만들어 줍니다.

### 결과물 (결과 폴더, 기본 `OM_KO`)

| 파일 | 내용 |
|---|---|
| `[원본파일명]_KR_O&M.pdf` | 한국어 O&M 문서 (원본과 같은 페이지 수·크기, 한국어만 표시, 텍스트 검색 가능) |
| `[원본파일명]_Translation_Log.xlsx` | 번역로그(원문/번역/상태/비고) · 페이지(번역/원문유지 사유) · 최종검사 |
| `Plant_O&M_Glossary.xlsx` | 문서에 적용된 용어집 + 자주 나오는 미등록 용어 후보 |

---

## 1. 설치 (처음 한 번)

1. 저장소 받기 → VS Code로 열기
   ```powershell
   git clone https://github.com/Hyejun0112/Translation-ENG---KOR-.git
   cd Translation-ENG---KOR-
   code .
   ```
2. **Tesseract OCR** 설치 (스캔/글자깨짐 페이지, 목차 등에 필요)
   https://github.com/UB-Mannheim/tesseract/wiki → `tesseract-ocr-w64-setup-xxx.exe` → 기본 경로 `C:\Program Files\Tesseract-OCR`에 설치
3. 파이썬 패키지는 처음 실행할 때 프로젝트 폴더의 `.venv`에 **자동 설치**됩니다(1~3분).

한글 폰트는 `C:\Windows\Fonts\malgun.ttf`(맑은 고딕)를 자동 사용합니다.

## 2. 실행

- 탐색기에서 **`OM번역기 실행.bat`** 더블클릭 (바탕화면에 바로가기를 만들어 두면 편합니다)
- 또는 VS Code에서 `om_gui.py` / `om_translate.py`를 열고 ▶, 또는 F5 → **"OM 번역기 (GUI)"**

## 3. 번역 순서 (GUI)

**① 설정 탭**
- `PDF 파일 추가…`로 원본 PDF 첨부 (여러 개 가능, 끌어다 놓기 가능)
- 용어집 `glossary.csv`(기본 선택됨), 결과 저장 폴더 확인
- `O&M 대상만 번역`은 켜 둔 상태로 둡니다 (데이터시트·부품목록·도면 등은 원문 유지)

**② Copilot 번역 탭**
1. **[① 번역할 문장 추출]** → 번역이 필요한 문구만 뽑아 배치(묶음)로 나눕니다. (이미 번역된 문구, 숫자/기호만 있는 문구, 번역 범위 밖 문구는 제외)
2. 왼쪽 목록에서 배치를 고르고 **[📋 프롬프트 복사]**
3. Copilot 채팅창에 **Ctrl+V → 보내기**
4. Copilot 답변 전체를 복사 → **[클립보드에서 붙여넣기]** → **[✔ 답변 저장]**
   - 답변 형식(`[0001] 번역문`)을 자동으로 읽고, **원문의 Tag·숫자·단위가 바뀐 번역은 거부**합니다.
   - 저장 결과(저장 개수, 빠진 번호, 거부 사유)가 아래에 표시됩니다.
5. 다음 배치로 자동 이동 → 2~4 반복
6. 빠지거나 거부된 문구가 있으면 **[① 번역할 문장 추출]**을 다시 누르면 남은 것만 새 배치로 만들어집니다.
7. **[③ 한국어 PDF 만들기]** → 실행 로그에서 결과 확인 → **[한국어 PDF 열기]**

> Copilot이 "너무 길다"고 하면 `배치 크기(글자)`를 4000 또는 3000으로 줄이고 다시 추출하세요.
> 한 번 번역한 문구는 `translation_cache.sqlite`(번역 메모리)에 저장되어 다른 문서에서도 재사용됩니다.

**③ 수정·재생성**
- `[원본파일명]_Translation_Log.xlsx`의 **`수정 번역(입력)`** 열에 고칠 번역을 씁니다.
- 설정 탭의 **수정 목록**에 그 엑셀 파일을 지정 → 작업 `한국어 PDF 만들기` → ▶ 실행. (수정 내용은 번역 메모리에도 저장됩니다)

### 기타 작업 (설정 탭 → 작업 선택 → ▶ 실행)
| 작업 | 설명 |
|---|---|
| 분량 추정 + 페이지 진단 | 페이지별 종류(네이티브/스캔/글자깨짐), 번역 범위, 번역 문구 수 |
| OCR 결과 확인 | PDF 1개의 한 페이지를 OCR해서 인식 박스 이미지로 확인 |
| 레이아웃 테스트 | 번역 없이 `[번역]` 접두를 붙여 배치만 확인 (납품용 아님, `레이아웃_테스트` 폴더) |
| API 자동 번역 | (선택) Anthropic API 키가 있을 때만. 유료 |

---

## 4. 번역 규칙

**번역 범위** — `O&M 대상만 번역`(기본):
- 페이지 제목이 Datasheet, Technical/Performance/Design Data, Equipment/Valve/Line/Instrument/Cable/I/O List, (Spare) Parts List, Bill of Material, Catalog, Brochure, Ordering Information, Material/Fuel Specification, Certificate, Inspection/Test Report, Packing/Shipping List, P&ID, PFD, Logic/Wiring/Single Line/Loop Diagram, GA/Arrangement/Dimensional Drawing, Hook-up, DCS/PLC/HMI Screen 이면 **페이지 전체 원문 유지**
- A3 이상 대형 페이지, 선이 아주 많고 짧은 라벨 위주인 페이지 → 도면으로 판단해 원문 유지
- 표 머리글이 Tag / Model / Parameter / Code / Value / Unit / Signal / Part No. 등인 **열은 원문 유지** (머리글 자체는 번역)
- 그림(래스터 이미지) 내부 문자는 기본적으로 원문 유지 (`그림(사진) 속 글자도 번역`으로 켤 수 있음)
- 추가 제외 키워드는 `scope_keywords.txt`(한 줄에 하나, 정규식 가능)를 만들어 넣으면 됩니다.
- 판단 결과는 `Translation_Log.xlsx`의 **페이지** 시트에 사유와 함께 기록됩니다.

**항상 원문 유지** — 회사/제작사/Vendor/모델명, Tag/Equipment/Instrument/Loop/Part/Drawing/Document/Revision/Serial No., Alarm Code, Signal/Parameter Name, Plant/Project Name, URL, E-mail, 화학식, 수식, 단위, 숫자.
코드가 자동 인식하는 패턴(규격, Tag, 모델/부품번호, Rev., 수치+단위, 날짜, URL)에 프로젝트 전용 정규식은 `keep_patterns.txt`에 추가합니다.

**용어집** `glossary.csv` — 고정 용어(operation=운전, maintenance=정비, start-up=기동, trip=트립, forced draft fan=FD Fan …), 경고어(DANGER=위험, WARNING=경고, CAUTION=주의, NOTICE=유의사항, IMPORTANT=중요, NOTE=참고). `ko`에 `KEEP`이면 영어 유지.
배치마다 그 배치에 나오는 용어만 프롬프트에 넣습니다.

**Copilot 프롬프트** `copilot_prompt.txt` — 역할, 원문 유지 항목, 경고어, 명령문(Check/Verify → 확인하십시오 …), 문체(~합니다/~하십시오), 품질 기준. 문구를 직접 고칠 수 있습니다(`{glossary}`, `{count}`, `{items}`는 유지).

## 5. PDF 편집 방식

- 원본 PDF를 직접 편집합니다. Word/PPT 재작성, 페이지 전체 이미지화 없음.
- 네이티브 텍스트: 영문 글자만 지우고(선·그림·배경 보존) 같은 위치에 한글 삽입
- 스캔/글자깨짐 페이지: OCR(400 dpi, 기울기 보정, 표 괘선 인식) → 원본 배경색으로 영문 영역만 덮고 한글 삽입
- 길이 조정: 원래 박스 → 자동 줄바꿈 → 옆/아래 빈 공간(셀 경계·다른 글자 직전까지) → 글자 축소(하한 60%, 5pt)
- 셀 밖 초과·텍스트 겹침·페이지 밖은 자동 검사해 로그에 `OVERFLOW` / `TEXT_OVERLAP` / `PAGE_OVER`로 표시
- 페이지 크기·순서 유지, 회전 페이지는 보이는 모습 그대로, 링크·북마크 유지(북마크도 번역)

## 6. 최종 검사 (`Translation_Log.xlsx` → 최종검사 시트)

PDF 열림, 페이지 누락, 페이지 순서, 페이지 크기/방향, 그림/사진 유지(페이지별 이미지 수), 한글 출력, 텍스트 겹침, 셀/박스 초과, Tag/숫자/단위 유지, 상태별 건수.
번역로그 시트의 노란색 행(미번역/실패/초과/겹침/Tag 변경/OCR 저신뢰)을 우선 검수하세요.

## 7. 명령줄 사용 (선택)

```powershell
py om_translate.py estimate  .\OM_EN                                      # 분량/페이지 진단
py om_translate.py export    .\OM_EN -o .\OM_KO --glossary glossary.csv   # Copilot 배치 프롬프트 생성
#   → OM_KO\Copilot_작업\batch_001_프롬프트.txt 를 Copilot에 보내고 답변을 batch_001_답변.txt 로 저장
py om_translate.py import    -o .\OM_KO --glossary glossary.csv           # 답변 반영(검증 후 번역 메모리 저장)
py om_translate.py translate .\OM_EN -o .\OM_KO --glossary glossary.csv   # 한국어 PDF 생성 (기본 --backend memory)
py om_ocr.py .\OM_EN\file.pdf --page 4 --debug                            # OCR 확인
```

## 8. 주의

- **WARNING/CAUTION, 설정값, 인터록, 트립 조건 등 안전·운전 직결 문구는 반드시 사람이 검수**하세요.
- Copilot에 문서 문구를 붙여넣는 것이 사내 보안 정책·고객/제작사 NDA상 허용되는지 확인하세요. (OCR과 PDF 생성은 PC 안에서만 실행됩니다)
- 스캔 품질이 나쁘면 OCR 오류가 생깁니다. 로그의 `OCR_LOW_CONF` 행은 원문을 꼭 확인하세요.
