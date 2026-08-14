@echo off
cd /d %~dp0
title 语润 Yurun - 开发模式
echo.
echo  正在启动语润 Yurun（开发模式）...
echo  日志文件：%APPDATA%\Yurun\logs\yurun.log
echo.
python src\main.py
echo.
echo  程序已退出。
pause