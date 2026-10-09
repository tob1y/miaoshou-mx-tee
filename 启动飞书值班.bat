@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 飞书值班：每小时检查，满200自动对半上架，单店日限300
echo 关闭本窗口即停止值班
"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" -u run_feishu_watcher.py
pause
