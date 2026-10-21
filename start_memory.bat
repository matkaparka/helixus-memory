@echo off
chcp 65001 >nul
title helixus_memory
rem Helixus 记忆服务（http://127.0.0.1:8790）。只用标准库，系统自带的 Python 3.14 就能跑。
set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=py"
"%PY%" "%~dp0memory_service.py" %*
echo.
echo 记忆服务已退出。
pause
