@echo off
chcp 65001 >nul
rem ======================================================================
rem  Plant O&M Translator v2.0
rem  사용법 1) 더블클릭 → input\pdf 폴더에 PDF를 넣으면 자동 번역 (창을 열어 두세요)
rem  사용법 2) PDF 파일/폴더를 이 run.bat 아이콘 위로 끌어다 놓기
rem  결과   : output\원본파일명_KR_O&M.pdf
rem ======================================================================
title Plant O^&M Translator v2.0
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto RUN

echo [준비] 처음 실행: 프로그램 전용 Python 환경을 만듭니다. (1~3분, 한 번만)
set "BASE_PY="
where py >/dev/null 2>nul
if %errorlevel%==0 (
    py -3.12 -c "import sys" >/dev/null 2>/dev/null && set "BASE_PY=py -3.12"
    if not defined BASE_PY py -3 -c "import sys" >/dev/null 2>/dev/null && set "BASE_PY=py -3"
)
if not defined BASE_PY (
    python -c "import sys" >/dev/null 2>/dev/null && set "BASE_PY=python"
)
if not defined BASE_PY (
    echo.
    echo [오류] Python 이 설치되어 있지 않습니다.
    echo        https://www.python.org/downloads/ 에서 Python 3.12 를 설치하세요.
    echo        설치 화면에서 "Add python.exe to PATH" 를 반드시 체크하세요.
    echo.
    pause
    exit /b 1
)
%BASE_PY% -m venv "%~dp0.venv"
if not exist "%VENV_PY%" (
    echo [오류] Python 환경을 만들지 못했습니다.
    pause
    exit /b 1
)
"%VENV_PY%" -m pip install --upgrade pip -q
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [오류] 패키지 설치 실패. 인터넷 연결/회사 프록시를 확인하세요.
    pause
    exit /b 1
)

:RUN
"%VENV_PY%" "%~dp0main.py" %*
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" echo [오류] 종료 코드 %RC% - logs 폴더의 로그를 확인하세요.
pause
exit /b %RC%
