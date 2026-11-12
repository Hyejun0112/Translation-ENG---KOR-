# Plant O&M Translator v2.0

발전플랜트 O&M Manual(Boiler, Steam Turbine, Fuel Handling, Mechanical, Electrical, Instrument) PDF를
**원본 레이아웃(그림·표·도면·배경·선) 그대로 두고 O&M 대상 문구만 한국어로 바꾼 PDF**로 자동 번역합니다.
Word 원본이 없어도 되고, 스캔 PDF / 글자가 깨져 복사되는 PDF도 OCR로 처리합니다.

```
run.bat 더블클릭  →  input\pdf 폴더에 PDF 넣기  →  output\원본파일명_KR_O&M.pdf 자동 생성
```

---

## 1. 처음 한 번 준비

| 준비물 | 방법 |
|---|---|
| **Python 3.12** | https://www.python.org/downloads/ → 설치 화면에서 **"Add python.exe to PATH" 체크** |
| **Claude API 키** | https://console.anthropic.com → API Keys → Create Key (`sk-ant-`로 시작). 첫 실행 때 붙여넣으면 저장됨 |
| **Tesseract OCR** | 첫 실행 때 "자동 설치할까요?" → `Y` (스캔/글자깨짐 페이지, 목차 등에 필요) |

패키지 설치는 `run.bat`이 자동으로 합니다(처음 1~3분).

## 2. 사용 방법

**방법 1: 폴더에 넣기**
1. `run.bat` 더블클릭 → 검은 창이 뜹니다. (창을 닫지 마세요)
2. `input\pdf` 폴더에 PDF를 넣습니다. (여러 개 가능, 하위 폴더 가능)
3. 자동으로 번역되고, 끝나면 `output` 폴더가 열립니다.
4. 창을 열어 두는 동안 PDF를 더 넣으면 계속 자동 번역합니다. 종료는 창 닫기.

**방법 2: 끌어다 놓기 (Drag & Drop)**
- PDF 파일(여러 개)이나 폴더를 탐색기에서 `run.bat` 아이콘 위로 끌어다 놓습니다.
  자동으로 `input\pdf`에 복사되어 번역됩니다.

## 3. 결과물 (`output` 폴더)

| 파일 | 내용 |
|---|---|
| `원본파일명_KR_O&M.pdf` | 한국어 O&M 문서. 원본과 같은 페이지 수·크기·순서, 텍스트 검색 가능 |
| `원본파일명_Translation_Log.xlsx` | **번역로그**(원문/번역/상태/비고) · **페이지**(번역/원문유지와 사유) · **최종검사** |
| `Plant_O&M_Glossary.xlsx` | 적용된 용어집 + 자주 나오는 미등록 용어 후보 (누적) |

### 번역 수정하기
1. `원본파일명_Translation_Log.xlsx` → 번역로그 시트의 **`수정 번역(입력)`** 열에 고칠 번역을 씁니다 → 저장
2. `run.bat` 실행(또는 열려 있으면 그대로) → 해당 PDF만 다시 만들어지고 수정이 반영됩니다.
   수정한 번역은 번역 메모리에 저장되어 다른 문서에도 똑같이 적용됩니다.

### 다시 만들기
- 결과 PDF를 지우면 다음 확인 때 다시 만듭니다. 같은 문구는 번역 메모리(`cache`)에서 가져오므로 **API 비용이 다시 들지 않습니다.**

## 4. 번역 규칙

**번역함**: 제목, 소제목, 본문, 글머리표, 절차(Step), WARNING/CAUTION/NOTE, 표 머리글과 지시문, 그림 제목, 북마크

**페이지 전체 원문 유지 (Drawing / Datasheet 제외)**: 페이지 제목이 Datasheet, Technical/Performance/Design Data,
Equipment/Valve/Line/Instrument/Cable/I/O List, (Spare) Parts List, Bill of Material, Catalog, Brochure,
Ordering Information, Material/Fuel Specification, Certificate, Inspection/Test Report, Packing/Shipping List,
P&ID, PFD, Logic/Wiring/Single Line/Loop Diagram, GA/Arrangement/Dimensional Drawing, Hook-up, DCS/PLC/HMI Screen인 페이지,
A3 이상 대형 도면 페이지, 선이 많고 짧은 라벨 위주인 도면 페이지.
(본문 속 "P&ID No. 0061" 같은 참조는 무시하고, 문장 위주 페이지는 번역합니다. 판단 결과는 로그의 **페이지** 시트에 있습니다.)

**표**: 셀 단위로 번역하고 셀 안에서 줄바꿈합니다. 머리글이 Tag / Model / Parameter / Code / Value / Unit / Signal / Part No.인 **열은 원문 유지**.

**항상 원문 유지 (Tag 보호)**: 회사·제작사·모델명, Tag/Equipment/Instrument/Loop/Part/Drawing/Document/Revision/Serial No.,
Alarm/Error Code, Signal/Parameter Name, 규격(ASME, API, ISO…), 수치+단위, 2oo3, 화학식, 날짜, URL/E-mail, 약어(PLC, DCS, ESD…).
번역 결과에서 보호 값이 빠지면 **잘못된 번역을 넣지 않고 원문을 유지**하고, 변경이 의심되면 로그에 표시합니다.

**용어집** `data\glossary.csv` (Excel로 편집 가능, `data\glossary.xlsx`로 저장해도 됨)
- 열: `en, ko, note`. `ko`가 `KEEP`이면 영어 유지.
- 고정 용어: operation=운전, maintenance=정비, inspection=점검, commissioning=시운전, start-up=기동, shutdown=정지, trip=트립, alarm=경보, troubleshooting=고장진단, forced draft fan=FD Fan …
- 경고어: DANGER=위험, WARNING=경고, CAUTION=주의, NOTICE=유의사항, IMPORTANT=중요, NOTE=참고
- 명령문: Check/Verify → 확인하십시오, Open → 여십시오, Close → 닫으십시오, Do not → 하지 마십시오 …

**추가 설정 파일**
- `data\keep_patterns.txt`: 영어로 남길 프로젝트 전용 정규식 (문서번호 체계, 제작사명 등)
- `data\scope_keywords.txt`: 원문 유지할 페이지 제목 키워드 추가
- `config.py`: 모델, 동시 호출 수, OCR 해상도, 번역 범위(`TRANSLATE_SCOPE`), 감시 주기 등

## 5. PDF 처리 방식

- 원본 PDF를 직접 편집합니다. 페이지를 이미지로 바꾸거나 Word로 다시 만들지 않습니다.
- 텍스트 PDF: 영문 글자만 지우고(선·그림·배경 보존) 같은 위치에 한글(맑은 고딕) 삽입
- 스캔/글자깨짐 페이지: 400 dpi OCR(기울기 보정, 표 괘선 인식) → 원본 배경색으로 영문만 덮고 한글 삽입
- 한국어가 길면: 원래 칸 → 자동 줄바꿈 → 옆/아래 빈 공간(셀 경계·다른 글자 직전까지) → 글자 축소(하한 60%, 5pt)
- 최종 검사: PDF 열림, 페이지 누락/순서/크기, 그림 유지, 한글 출력, 텍스트 겹침, 셀 초과, Tag·숫자·단위 유지, 번역 실패

## 6. 고급 사용 (명령줄)

`run.bat` 뒤에 옵션을 붙이거나, VS Code에서 F5로 실행합니다.

| 명령 | 설명 |
|---|---|
| `run.bat --once` | 일괄 처리만 하고 종료 (감시 안 함) |
| `run.bat --pages 1-5` | 각 PDF의 1~5페이지만 샘플 번역 |
| `run.bat --force` | 이미 번역된 파일도 다시 생성 |
| `run.bat --estimate` | 번역 분량 / API 토큰 추정 (번역 안 함) |
| `run.bat --mock --once` | API 없이 레이아웃만 테스트 (글자 앞에 `[번역]`) |
| `run.bat --check` | 설치 상태 점검 |
| `run.bat --set-key` | API 키 다시 입력 |
| `run.bat --scope all` | 도면/데이터시트 페이지까지 전부 번역 |

## 7. 폴더 구조

```
Plant O&M Translator v2.0
├─ run.bat                    실행 (더블클릭 / 끌어다 놓기)
├─ main.py                    전체 흐름: 준비 → 일괄 처리 → 폴더 감시
├─ config.py                  설정
├─ requirements.txt
├─ engines\
│   ├─ pdf_engine.py          페이지 진단, 텍스트 추출, 레이아웃 유지 한글 삽입, 폰트
│   ├─ translation_engine.py  Claude API 번역, 번역 메모리(SQLite)
│   ├─ glossary_engine.py     용어집, Tag/숫자/단위 보호
│   ├─ table_engine.py        표 셀 인식, Tag/Value 열 원문 유지
│   ├─ scope_engine.py        Drawing / Datasheet 제외 판단
│   ├─ ocr_engine.py          스캔 PDF OCR (Tesseract + OpenCV)
│   └─ qa_engine.py           최종 검사, Translation Log, Glossary Excel
├─ data\                      glossary.csv, keep_patterns.txt, scope_keywords.txt
├─ tools\install_tesseract.py Tesseract 자동 설치
├─ input\pdf\                 ← 원본 PDF를 넣는 곳
├─ output\                    ← 결과
├─ cache\                     번역 메모리 / OCR 캐시 (지우면 다시 번역 = 비용 발생)
└─ logs\                      실행 로그 (문제 발생 시 이 파일을 확인)
```

## 8. 주의

- **WARNING/CAUTION, 설정값, 인터록, 트립 조건 등 안전·운전 직결 문구는 반드시 사람이 검수**하세요.
- 문서 텍스트가 Claude API(Anthropic)로 전송됩니다. 고객/제작사 NDA 및 사내 보안 규정상 허용되는지 확인하세요.
- API 사용량에 따라 요금이 발생합니다. `run.bat --estimate`로 미리 분량을 확인하세요.
- API 키는 프로젝트 폴더의 `.env` 파일에만 저장됩니다. 이 파일은 다른 사람과 공유하지 마세요.
