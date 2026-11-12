@echo off
chcp 65001 >nul
rem OM 영-한 번역기 실행 (더블클릭)
cd /d "%~dp0"

rem 준비된 .venv가 있으면 콘솔 창 없이 바로 GUI 실행
if exist ".venv\Scripts\pythonw.exe" (
    ".venv\Scripts\python.exe" -c "import pymupdf, cv2, pytesseract, tkinterdnd2" >nul 2>nul && (
        start "" ".venv\Scripts\pythonw.exe" "%~dp0om_gui.py"
        exit /b 0
    )
)

echo 처음 실행 준비 중입니다. 패키지를 설치하므로 1~3분 걸립니다...
where py >nul 2>nul
if %errorlevel%==0 (
    py "%~dp0om_gui.py"
) else (
    python "%~dp0om_gui.py"
)
if errorlevel 1 (
    echo.
    echo 실행에 실패했습니다. 위 메시지를 확인하세요.
    pause
)
