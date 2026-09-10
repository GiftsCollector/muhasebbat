@echo off
cd /d "%~dp0"
start "Mohasabat" cmd /k "cd /d "%~dp0" && (py -3 run.py || python run.py)"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5000/"
