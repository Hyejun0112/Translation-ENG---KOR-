# 작업 규칙 (Claude Code)

## Git
- **모든 코드 변경은 `main` 에 바로 커밋·푸시한다.** (2026-11-16 사용자 지시)
  별도 브랜치나 PR 을 만들지 않는다. 사용자가 명시적으로 요청할 때만 브랜치/PR 을 쓴다.
- 커밋 메시지는 한국어로, 무엇이 왜 바뀌었는지(어느 문서의 어떤 증상을 고쳤는지) 적는다.
- 레이아웃/렌더링 코드가 바뀌면 `main.py` 의 `LAYOUT_VERSION` 을, OCR 로직이 바뀌면
  `engines/ocr_engine.py` 의 `OCR_VERSION` 을 올린다 (기존 결과물·캐시 자동 재생성).

## 프로젝트 성격
- 발전플랜트 O&M Manual PDF 영→한 번역기. 사용자는 플랜트 공정 설계 엔지니어이며 Bluebeam 으로 Markup 한다.
- **가장 중요한 요구: 원본 PDF 레이아웃 유지.** 글자·그림·표·괘선·주석(Markup/스탬프)·링크가 깨지면 안 된다.
- 번역 대상: 표지, 본문, 표 머리글/지시문, 그림 제목. 원문 유지: 데이터시트/도면/부품목록 페이지,
  Tag·모델·도면번호·수치+단위, 표의 Tag/Value/Unit 열. 상세 규칙은 `README.md` 4절.

## 검증 방법
- 여기(클라우드)에는 API 키·맑은 고딕이 없다. 가짜 한글(길이 비슷한 pseudo 텍스트) 백엔드로 전체 문서를 돌려
  레이아웃만 검증한다. 실제 번역문 품질은 사용자 PC 에서 확인한다.
- API 비용 없이 실제 번역 경로(API 호출·재시도·오류 처리 포함) 전체 시험: `python tools/selftest_fake_api.py 결과폴더 원본.pdf --mode chaos`
  (모드: normal/chaos/credit/oldsdk/nostruct/auth/ssl). 번역 내용 대조: `python tools/check_translation.py 원본.pdf 결과.pdf`
- 원본 ↔ 결과를 페이지별로 자동 대조: `python tools/check_layout.py 원본.pdf 결과.pdf --png 폴더`
- 결과 `Translation_Log.xlsx` 의 **최종검사** 시트(그림/괘선/주석/링크/글꼴/겹침)가 모두 OK 인지 본다.
- 코드를 고친 뒤에는 종류가 다른 문서(본문 위주 매뉴얼, 데이터시트 합본, 스캔 카탈로그)를 함께 다시 돌려
  회귀가 없는지 확인한다.
