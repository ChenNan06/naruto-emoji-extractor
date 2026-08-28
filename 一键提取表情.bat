@echo off
chcp 65001 >nul
title 火影忍者手游 表情提取器
cd /d "%~dp0"
python "%~dp0脚本\表情提取器GUI.py"
if errorlevel 1 (
    echo.
    echo 启动失败。请确认已安装 Python 并执行过: pip install pillow numpy unitypy
    pause
)
