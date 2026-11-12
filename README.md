# OM 영→한 번역 도구 (PDF 레이아웃 유지)

발전소 OM(Operating & Maintenance Manual) PDF를 **그림·표·배경·링크·북마크는 그대로 두고 텍스트만 한글로 교체**합니다.
규격, 모델명, Tag No., 도면번호, Rev., 수치+단위, 약어, 브랜드명은 영어로 남깁니다.

## 1. 설치 (Windows 기준)

```powershell
py -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."     # 영구 설정: setx ANTHROPIC_API_KEY "sk-ant-..."
```

한글 폰트는 `C:\Windows\Fonts\malgun.ttf`(맑은 고딕)를 자동으로 찾습니다. 다른 폰트는 `--font`, `--font-bold`로 지정합니다.

## 2. 권장 작업 순서

```powershell
# (1) 분량/토큰 추정 - API 호출 없음
py om_translate.py estimate .\OM_EN --glossary glossary.csv

# (2) 용어 후보 추출 - 전체 OM에서 자주 나오는 용어 → terms_candidates.csv
py om_translate.py terms .\OM_EN -o terms_candidates.csv
#     ko 열을 채워서 glossary.csv에 합칩니다. (ko가 빈 행은 무시됩니다)

# (3) 샘플 5페이지만 번역해서 품질/레이아웃 확인  (--pages 지정 시 해당 페이지만 출력)
py om_translate.py translate .\OM_EN\P-101_OM.pdf -o .\OM_SAMPLE --glossary glossary.csv --keep-patterns keep_patterns.txt --pages 1-5

# (4) 문제없으면 전체 실행 (폴더 전체 가능, 이미 만든 파일은 건너뛰고 이어서 진행)
py om_translate.py translate .\OM_EN -o .\OM_KO --glossary glossary.csv --keep-patterns keep_patterns.txt
```

중단 후 다시 실행해도 `translation_cache.sqlite` 덕분에 이미 번역한 문구는 API를 다시 호출하지 않습니다.
같은 문구(머리글/바닥글/표 헤더 등)는 문서 전체에서 한 번만 번역합니다.

## 3. 용어집 (glossary.csv / .xlsx)

| en | ko | note |
|---|---|---|
| Feed Water Pump | 급수 펌프 | |
| Induced Draft Fan | 유인통풍기(ID Fan) | 약어 병기 예시 |
| PLC | KEEP | 영어 유지 |

- `ko`에 **KEEP**(또는 `=`, `-`, `유지`)이면 해당 용어는 영어로 유지합니다.
- 복수형(pumps 등)과 대소문자는 자동 처리됩니다. 긴 용어가 우선 적용됩니다.
- CSV는 UTF-8 또는 Excel 기본 저장(cp949) 모두 읽습니다.
- 번역 후 용어집 용어가 반영되지 않은 블록은 리포트의 `notes`에 `GLOSSARY_MISS`로 표시됩니다.

## 4. 영어 유지 규칙

코드에 내장된 패턴: 규격(ASME B31.1, API 610, ISO 9001:2015 …), Tag No./모델명/부품번호(P-101A, HPK-L 100-250 …),
`Rev. B`, 수치+단위(120 bar(g), 250 °C, 45 m3/h, 75 kW …), 날짜, URL/이메일.
짧은 문구(80자 이하)의 숫자는 모두 보호되어 `Page 12 of 340` 같은 바닥글도 한 번만 번역됩니다.
프로젝트 전용 규칙은 `keep_patterns.txt`에 정규식으로 추가하세요. (제작사명, 문서번호 체계 등)

번역기가 보호 토큰을 하나라도 빠뜨리거나 바꾸면 **틀린 번역을 넣지 않고 원문 영어를 그대로 두고** `FAILED`로 표시합니다.

## 5. 출력물

| 파일 | 내용 |
|---|---|
| `OM_KO\<이름>_KO.pdf` | 번역본 (텍스트 선택/검색 가능, 링크·북마크 유지, 북마크도 번역) |
| `OM_KO\<이름>_KO.review.csv` | 블록별 원문/번역/페이지/좌표/글자크기/경고 (엑셀에서 열기) |
| `OM_KO\summary.csv` | 파일별 번역/실패/검토필요 건수 |

`review.csv`의 `status`/`notes`로 검수 대상을 찾습니다: `OVERFLOW`(박스에 안 들어가 글자를 축소 한계까지 줄임), `FAILED`, `ROTATED_SKIPPED`, `GLOSSARY_MISS`.
수정이 필요한 문구는 `--overrides overrides.csv`(원문,번역 두 열)에 넣고 `--force`로 재생성하면 통째로 교체됩니다.

## 6. 옵션

| 옵션 | 설명 |
|---|---|
| `--tone formal\|concise` | `~합니다/~하십시오`(기본) 또는 개조식 `~할 것/~함` |
| `--model` | 사용할 Claude 모델 (기본 `claude-sonnet-5-5`, 환경변수 `OM_MODEL`로도 지정) |
| `--workers 4` | 동시 API 호출 수 (속도 제한에 걸리면 낮추세요) |
| `--min-scale 0.6 --min-size 5` | 한글이 안 들어갈 때 글자 축소 하한 |
| `--pages 1-5,8` | 일부 페이지만 처리하고 해당 페이지만 출력 (샘플용) |
| `--backend mock` | API 없이 레이아웃/보호 로직만 테스트 (`[번역]` 접두) |
| `--ocr` | 텍스트 레이어가 없는 스캔 페이지 OCR (**실험적**, Tesseract 필요) |
| `--whiteout` | 번역 영역을 흰색으로 덮고 이미지 픽셀까지 지움 (원본 글자가 비치는 경우) |
| `--force` | 이미 있는 결과 덮어쓰기 |

## 7. 한계 및 주의

- **그림(래스터 이미지) 안에 박힌 글자는 번역되지 않습니다.** 벡터로 그려진 도면/P&ID의 텍스트는 번역됩니다. 이미지 속 글자는 `--ocr`로 시도하거나 직접 Markup하세요.
- 스캔본 OCR은 색 배경 위 글자(표 헤더 등)에서 오인식이 나올 수 있습니다. **가능하면 제작사 원본(텍스트가 살아있는) PDF**를 받아 쓰세요.
- 회전된 텍스트는 번역하지 않고 `ROTATED_SKIPPED`로 표시합니다.
- 표는 같은 열에서 바로 이어지는 줄을 한 셀로 묶는 방식이라, 줄 간격이 아주 좁은 표는 셀이 합쳐질 수 있습니다.
- 한글은 가로로 넓어질 수 있어, 이웃 텍스트/셀 테두리 직전까지 폭을 넓히고 그래도 안 되면 글자를 줄입니다. 그래도 넘치면 `OVERFLOW`로 표시합니다.
- **WARNING/CAUTION, 설정값, 인터록, 트립 조건 등 안전·운전 직결 문구는 반드시 사람이 검수하세요.** 자동 번역본을 그대로 운전원에게 배포하지 마세요.
- **보안**: 문서 텍스트가 외부 API(Anthropic)로 전송됩니다. 사업주와의 계약/NDA상 허용되는지, 필요하면 사업주 승인을 먼저 확인하세요.
