@echo off
chcp 65001 >nul
cd /d E:\tk-miaoshou-rework\clean_rebuild
echo 重试上次未成功的采集箱上架…
E:\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI\.venv\Scripts\python.exe 2>nul
C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe run_collect_split_retry.py
echo.
echo 清单文件: data\previews\待重试_未上架清单.txt
pause
