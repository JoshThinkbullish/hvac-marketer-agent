@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo Virtual environment not found. Run setup first - see README.md
  pause
  exit /b 1
)
echo Starting HVAC Marketer Agent...
.venv\Scripts\python.exe app.py
pause
