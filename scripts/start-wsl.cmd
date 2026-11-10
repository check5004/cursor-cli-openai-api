@echo off
rem Cursor CLI を WSL 側に入れている場合に、Windows から WSL 内でサーバーを起動するスクリプト。
rem このリポジトリを Windows 側のフォルダ（C:\...）に置いている前提です（WSL からは /mnt/c/... で見えます）。
rem WSL 内に clone した場合は、WSL のターミナルで ./scripts/start.sh を実行してください。
rem 引数はそのままサーバーに渡されます。例: scripts\start-wsl.cmd --port 8765
setlocal
cd /d "%~dp0.."
wsl.exe -e bash -lc "python3 -m cursor_openai_api %*"
if errorlevel 1 pause
