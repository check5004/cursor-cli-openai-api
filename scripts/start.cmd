@echo off
rem Windows (ネイティブ) 用の起動スクリプト。ダブルクリックでも起動できます。
rem 引数はそのままサーバーに渡されます。例: scripts\start.cmd --port 8765
setlocal
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 -m cursor_openai_api %*
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 が見つかりません。https://www.python.org/downloads/windows/ からインストールしてください。
    pause
    exit /b 1
  )
  python -m cursor_openai_api %*
)
if errorlevel 1 pause
