@echo off
rem 双击 = 局域网模式：同一 WiFi 的手机/平板可以访问（会要求 4 位访问码）
rem 只想自己用就关掉本窗口，双击 start.bat（本地模式，不需要访问码）。
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0start.bat" lan
