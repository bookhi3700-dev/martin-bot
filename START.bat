@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 마틴봇

rem ---- 1. 진짜 파이썬 찾기 (스토어 가짜 python 제외) ----
set PY=
py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY python -c "import sys" >nul 2>nul && set PY=python
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3
if defined PY goto HAVE_PY

echo.
echo  [!] 이 PC에 파이썬이 설치되어 있지 않습니다.
echo.
where winget >nul 2>nul
if errorlevel 1 goto MANUAL
choice /c YN /m " 지금 파이썬을 자동으로 설치할까요"
if errorlevel 2 goto MANUAL
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
echo.
echo  설치가 끝났습니다. 이 창을 닫고 START.bat 을 다시 실행해 주세요.
pause
exit /b

:MANUAL
echo  아래 페이지에서 Python 을 설치한 뒤 START.bat 을 다시 실행하세요.
echo  설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
start "" https://www.python.org/downloads/
pause
exit /b

:HAVE_PY
echo  사용하는 파이썬:
%PY% --version

rem ---- 2. 가상환경 + 패키지 설치 (처음 한 번) ----
if exist ".venv\Scripts\python.exe" goto CHECK_PKG
if exist .venv rmdir /s /q .venv
echo  [1/2] 처음 실행: 필요한 프로그램을 설치합니다. 1~2분 걸립니다...
%PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto FAIL

:CHECK_PKG
".venv\Scripts\python.exe" -c "import flask, jwt, requests" >nul 2>nul
if not errorlevel 1 goto RUN
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto FAIL

:RUN
echo  [2/2] 마틴봇을 시작합니다. 이 창을 닫으면 봇도 멈춥니다.
".venv\Scripts\python.exe" app.py
pause
exit /b

:FAIL
echo.
echo  [!] 설치 중 문제가 생겼습니다. 이 창의 내용을 캡처해서 보내주세요.
if exist .venv rmdir /s /q .venv
pause
exit /b
