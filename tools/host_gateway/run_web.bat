@echo off
title Air780EPV 智能网关 - Web 控制台
chcp 65001 >nul
echo 正在启动 Air780EPV Web 管理服务 (监听 http://0.0.0.0:17801)...
python "%~dp0gateway_web.py" --port 17801
pause
