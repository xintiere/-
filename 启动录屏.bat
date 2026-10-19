@echo off
cd /d "%~dp0"
start "" "%~dp0runtime\python\pythonw.exe" "%~dp0screen_recorder.py"
exit
