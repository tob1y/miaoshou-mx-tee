@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 托比改品台
python gui_app.py
if errorlevel 1 (
  echo.
  echo 启动失败。请确认已安装 Python，并在本目录执行:
  echo   python -m pip install -r requirements.txt
  pause
)
