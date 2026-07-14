@echo off
title QuillKey - Stop
cd /d "%~dp0"

echo Stopping QuillKey...

taskkill /f /fi "WINDOWTITLE eq quillkey-backend*" >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1

for /f "tokens=2" %%p in ('wmic process where "CommandLine like '%%quillkey%%' and Name='python.exe'" get ProcessId /value 2^>nul ^| find "="') do taskkill /f /pid %%p >nul 2>&1
for /f "tokens=2" %%p in ('wmic process where "CommandLine like '%%quillkey%%' and Name='pythonw.exe'" get ProcessId /value 2^>nul ^| find "="') do taskkill /f /pid %%p >nul 2>&1

docker compose down >nul 2>&1

echo Done. QuillKey is stopped.
timeout /t 3 >nul
