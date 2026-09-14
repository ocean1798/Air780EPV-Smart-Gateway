@echo off
chcp 65001 >nul
title Air780EPV 智能随身通信网关控制中枢 (Smart Gateway CLI)
cd /d "%~dp0"
python gateway_client.py
pause
