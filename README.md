# Plant O&M Translator v2.0 (Claude API)

발전플랜트 O&M Manual(Boiler, Steam Turbine, Fuel Handling, Mechanical, Electrical, Instrument) PDF를
**원본 레이아웃(그림·표·도면·배경·선) 그대로 두고 O&M 대상 문구만 한국어로 바꾼 PDF**로 번역합니다.
Word 원본이 없어도 되고, 스캔 PDF / 글자가 깨져 복사되는 PDF도 OCR로 처리합니다.

```
OM_Translator.pyw 더블클릭 → API 키 저장(처음 한 번) → [PDF 추가] → [▶ 번역 시작] → output\원본파일명_KR_O&M.pdf
```

---

## 1. 처음 한 번 준비

1. **Python 3.12** 설치: https://www.python.org/downloads/ → 설치 화면에서 **"Add python.exe to PATH" 체크**
2. 프로젝트 폴더에서 **`OM_Translator.pyw` 더블클릭**
   → "처음 실행 준비" 창이 뜨고 필요한 부품을 자동 설치합니다 (1~3분, 한 번만). 끝나면 프로그램 창이 열립니다.
3. **Claude API 키** 입력 → **[저장]**
   - 키 발급: https://console.anthropic.com → API Keys → Create Key (`sk-ant-`로 시작), Billing 에서 크레딧 충전
   - 키는 이 PC의 `.env` 파일에만 저장됩니다. `.env`는 `.gitignore`에 들어 있어 **GitHub에 올라가지 않습니다.**
   - **키를 코드나 채팅, 메일에 붙여넣지 마세요.** (이 저장소는 공개 저장소입니다)
4. 프로그램 창 아래 **[Tesseract(OCR) 설치]** 를 한 번 눌러 둡니다 (스캔/글자깨짐 페이지, 목차 등에 필요)
5. (선택) **[바탕화면 바로가기 만들기]** → 다음부터 바탕화면 'OM 번역기' 아이콘으로 실행

> `.pyw` 파일이 더블클릭으로 열리지 않으면: 파일 우클릭 → 연결 프로그램 → **Python** 선택.

## 2. 사용 방법

1. **[PDF 추가…]** 또는 **[폴더 추가…]** 로 번역할 PDF 선택 (여러 개 가능, `input\pdf` 폴더의 PDF는 자동 표시)
2. 옵션 확인
   - **모델**: Sonnet(권장, 품질/비용 균형) · Haiku(빠르고 저렴) · Opus(최고 품질, 비용 높음)
   - **페이지**: 비우면 전체, 처음에는 `1-5`로 샘플 확인 권장
3. (선택) **[분량 추정]** → 번역할 문구 수와 예상 토큰 확인
4. **[▶ 번역 시작]** → 전체/현재 파일 진행률, 남은 시간 표시
5. 끝나면 **[한국어 PDF 열기]**, **[번역 로그 열기]**, **[결과 폴더]**

- 중간에 **[■ 중지]** 하거나 창을 닫아도 번역된 문구는 저장되어 있어 다시 시작하면 이어서 진행합니다.
- 같은 문구는 번역 메모리에서 재사용하므로 다시 만들 때 **API 비용이 다시 들지 않습니다.**

## 3. 결과물 (`output` 폴더)

| 파일 | 내용 |
|---|---|
| `원본파일명_KR_O&M.pdf` | 한국어 O&M 문서. 원본과 같은 페이지 수·크기·순서, 텍스트 검색 가능 |
| `원본파일명_Translation_Log.xlsx` | **번역로그**(원문/번역/상태/비고) · **페이지**(번역/원문유지와 사유) · **최종검사** |
| `Plant_O&M_Glossary.xlsx` | 적용된 용어집 + 자주 나오는 미등록 용어 후보 (누적) |

**번역 수정**: `Translation_Log.xlsx`의 **`수정 번역(입력)`** 열에 고쳐 쓰고 저장 → 같은 PDF로 다시 [번역 시작]
→ 수정이 반영되고, 번역 메모리에 저장되어 다른 문서에도 똑같이 적용됩니다.

## 4. 번역 규칙

- **번역**: 제목, 본문, 글머리표, 절차, WARNING/CAUTION/NOTE, 표 머리글·지시문, 그림 제목, 북마크
- **페이지 전체 원문 유지 (Drawing / Datasheet 제외)**: 제목이 Datasheet, Technical Data, Parts List, Bill of Material,
  Certificate, P&ID, PFD, Logic/Wiring/Single Line Diagram, GA/Arrangement/Dimensional Drawing 등인 페이지, A3 이상 도면 페이지
  (본문 속 "P&ID No. 0061" 같은 참조는 무시하고, 문장 위주 페이지는 번역)
- **표**: 셀 단위 번역, 셀 안에서 줄바꿈. 머리글이 Tag/Model/Parameter/Code/Value/Unit 인 **열은 원문 유지**
- **항상 원문 유지 (Tag 보호)**: Tag No., 모델/부품/도면/문서번호, Rev., 수치+단위, 2oo3, 화학식, 규격, 약어, URL.
  번역 결과에서 보호 값이 빠지면 잘못된 번역을 넣지 않고 원문을 유지
- **용어집** `data\glossary.csv` ([용어집 열기] 버튼, Excel로 편집): operation=운전, maintenance=정비, start-up=기동, trip=트립 …,
  WARNING=경고, CAUTION=주의, NOTE=참고. `ko`가 `KEEP`이면 영어 유지
- 문체: 설명문 "~합니다", 지시문 "~하십시오" (Check→확인하십시오, Open→여십시오, Do not→하지 마십시오 …)

## 4-1. 레이아웃 보존 규칙 (원본 그대로 유지되는 것)

- **그림·표 괘선·도면·로고**: 글자만 지우고 다시 씁니다. 지울 때는 문구 상자 전체가 아니라 **원문 줄마다 글자가 찍힌 자리만** 지워
  옆의 번호(`2.`), 수식 기호, 이웃 줄이 함께 지워지지 않습니다.
- **주석(Bluebeam Markup, 스탬프, 서명, 글상자)·링크·북마크**: 번역 전에 떼어 두었다가 저장 직전에 원본 그대로 다시 붙입니다.
  주석 안의 글자(검토 스탬프 문구 등)는 번역하지 않습니다.
- **표의 행/열**: 세로선·가로선이 없는 표도 행 단위로 인식합니다 (기호 | 설명 목록, `less than` 같은 반복 열, 칸 안의 숨은 열).
  표의 첫 열이 새 항목으로 시작하면 새 행으로 보고, 애매하면 합치지 않습니다.
- **문단**: 줄 간격이 넓어지는 곳, 줄을 일부러 바꾼 곳(다음 줄 첫 단어가 앞 줄 빈자리에 들어갈 수 있었던 경우),
  글자 크기가 바뀌는 곳(라벨/값), 밑줄 제목에서 문단을 나눕니다.
- **밑줄 제목**: 원래 밑줄을 지우고 번역문 길이에 맞춰 다시 긋습니다 (일부만 밑줄이면 같은 비율로).
- **수식·기호**: 수식 글꼴(Cambria Math 등)과 첨자가 붙은 기호(P wbox, T bed ave)는 원본 그대로 둡니다.
  `cm²`, `Nm³/h`, `H₂O` 같은 위/아래 첨자 숫자는 번역 후에도 첨자로 남습니다.
- **한글 글꼴에 없는 기호**(−, ≤, µ, ₂ …): 대체 글꼴(Arial, Segoe UI Symbol 등)로 그려 □ 로 깨지지 않게 합니다.
  어느 글꼴에도 없으면 같은 뜻의 기본 글자로 바꾸고, 그래도 안 되면 그 문구는 원문을 유지합니다.

**최종검사 시트**(`Translation_Log.xlsx`)에서 `그림/사진 유지`, `표 괘선/도형 유지`, `주석/링크 유지`, `글꼴에 없는 글자`,
`텍스트 겹침`이 모두 OK인지 확인하세요. 여러 문서를 한꺼번에 돌린 뒤에는 아래 도구로 원본과 자동 대조할 수 있습니다.

```
python tools\check_layout.py 원본.pdf output\원본_KR_O&M.pdf --png logs\check
```
이상이 있는 페이지 번호와 사유(그림/선 차이, 주석 개수, 깨진 글자, 글자 겹침, 영어 남음)를 출력하고,
`--png` 폴더에 원본/번역본을 나란히 붙인 그림을 저장합니다 (API 사용 안 함).

## 5. 폴더 구조

```
├─ OM_Translator.pyw           ★ 실행 (더블클릭) - 처음 실행 준비 + 프로그램 창
├─ gui.py                      프로그램 창 (API 키, PDF 선택, 진행률, 결과 열기)
├─ main.py                     번역 파이프라인 (프로그램 창이 내부적으로 실행)
├─ config.py                   설정 (모델, 동시 요청 수, OCR, 번역 범위)
├─ requirements.txt
├─ engines\
│   ├─ translation_engine.py   Claude API 번역, 번역 메모리, 진행률
│   ├─ pdf_engine.py           페이지 진단, 텍스트 추출, 레이아웃 유지 한글 삽입
│   ├─ glossary_engine.py      용어집, Tag/숫자/단위 보호
│   ├─ table_engine.py         표 셀 인식, Tag/Value 열 원문 유지
│   ├─ scope_engine.py         Drawing / Datasheet 제외 판단
│   ├─ ocr_engine.py           스캔 PDF OCR (Tesseract + OpenCV)
│   └─ qa_engine.py            최종 검사, Translation Log, Glossary Excel
├─ data\                       glossary.csv, keep_patterns.txt, scope_keywords.txt
├─ tools\install_tesseract.py  Tesseract 자동 설치
├─ tools\check_layout.py       원본 ↔ 번역본 페이지별 자동 대조 (레이아웃 검사)
├─ input\pdf\                  (선택) 원본 PDF를 넣어 두면 목록에 자동 표시
├─ output\                     결과
├─ cache\                      번역 메모리 / OCR 캐시 / 화면 설정
├─ logs\                       실행 로그 (문제가 생기면 이 파일을 확인)
└─ .env                        API 키 (이 PC에만 있음, GitHub 제외)
```

## 6. 문제 해결

| 증상 | 해결 |
|---|---|
| `.pyw` 더블클릭해도 반응 없음 | 우클릭 → 연결 프로그램 → Python. 또는 Python 3.12 재설치(PATH 체크) |
| "API 키가 올바르지 않습니다" | console.anthropic.com 에서 키를 다시 복사해 [붙여넣기] → [저장] |
| "크레딧(잔액)이 부족합니다" | console.anthropic.com → Billing 에서 충전 |
| 목차/스캔 페이지가 영어로 남음 | [Tesseract(OCR) 설치] 후 [이미 번역된 파일도 다시 만들기] 체크하고 다시 번역 |
| 결과 PDF 저장 실패 | Bluebeam 등에서 결과 PDF를 닫거나, 자동으로 `_시간` 붙은 이름으로 저장됨 |
| 그 밖의 오류 | 진행 기록의 빨간 줄, 또는 [로그 폴더]의 로그 파일 확인 |

**주의**
- **WARNING/CAUTION, 설정값, 인터록, 트립 조건 등 안전·운전 직결 문구는 반드시 사람이 검수**하세요.
- 문서 텍스트가 Claude API(Anthropic)로 전송됩니다. 고객/제작사 NDA 및 사내 보안 규정상 허용되는지 확인하세요.
- API 사용량에 따라 요금이 발생합니다. [분량 추정]과 `1-5` 페이지 샘플로 먼저 확인하세요.
