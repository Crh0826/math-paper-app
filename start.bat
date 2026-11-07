@echo off
rem math-paper-app launcher. Chinese output starts after chcp 65001 (UTF-8, no BOM).
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 数学题库工作台 · http://127.0.0.1:8710/
rem 想换端口：改下面的 --port 与浏览器地址两处，并同步 app/config.json 的 server.port。
set "PF86=%ProgramFiles(x86)%"
rem 监听范围：本脚本和 config.example.json 的 server.host 一样，固定监听 0.0.0.0（所有网卡）。
rem **监听 0.0.0.0 不等于允许访问**：默认只允许本机（127.0.0.1）访问，手机要用得去「设置 -> 局域网访问」打开开关。
rem 兼容老习惯：传 lan 或设 MATHAPP_LAN=1，等价于在设置里把那个开关点开（launcher 的 --lan 走的也是它）。
if /i "%~1"=="lan" set "MATHAPP_LAN=1"

echo ============================================================
echo   数学题库工作台 · 组卷 / 答题卡 / 拍照判卷 / 错题训练
echo   题库：沪科版七年级上 · 22,898 题
echo ============================================================
echo.

rem ---------- 1) 找 Python ----------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [x] 没找到 Python。
    echo     请先安装 Python 3.11 或更新版本: https://www.python.org/downloads/
    echo     安装时务必勾选 "Add python.exe to PATH"，装完重新双击本文件。
    echo.
    pause
    exit /b 1
)

set "PYVER=unknown"
for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
echo [OK] Python !PYVER!  (命令: %PY%)

rem ---------- 2) 查依赖 ----------
%PY% -c "import fastapi, uvicorn, jinja2, multipart" >nul 2>nul
if errorlevel 1 (
    echo [!] 缺少运行依赖: fastapi / uvicorn / jinja2 / python-multipart
    echo     需要执行: %PY% -m pip install -r requirements.txt
    echo.
    choice /c YN /n /m "现在自动安装吗? [Y=装 / N=退出] "
    if errorlevel 2 goto :nodeps
    echo [*] 正在安装依赖，第一次装可能要几分钟...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [x] 安装失败。请检查网络后手动执行:
        echo     %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo [OK] 依赖安装完成
) else (
    echo [OK] 运行依赖齐全
)

rem ---------- 题库目录（可移植：环境变量 / config.paths.qbank / qbank\ / 老路径回退） ----------
%PY% -c "import paths;d=paths.describe();print('   题库目录: '+d['qbank']['path']+'  ['+d['qbank']['source']+']')"
echo.

rem ---------- 3) 查 Word（只探路径，不启动 Word） ----------
set "WORD="
for %%p in (
    "%ProgramFiles%\Microsoft Office\root\Office16\WINWORD.EXE"
    "%ProgramFiles%\Microsoft Office\Office16\WINWORD.EXE"
    "%PF86%\Microsoft Office\root\Office16\WINWORD.EXE"
    "%PF86%\Microsoft Office\Office16\WINWORD.EXE"
    "%ProgramFiles%\Microsoft Office\root\Office15\WINWORD.EXE"
    "%ProgramFiles%\Microsoft Office\Office15\WINWORD.EXE"
) do (
    if not defined WORD if exist "%%~p" set "WORD=%%~p"
)
rem 常见路径没命中时，让 server.probe_word() 再查一遍注册表（覆盖装在 D 盘等非默认路径的 Office）
if not defined WORD (
    for /f "delims=" %%w in ('%PY% -c "import server;w=server.probe_word();print(w['path'] or '')" 2^>nul') do set "WORD=%%w"
)
if defined WORD (
    echo [OK] 检测到 Word: !WORD!
) else (
    echo [!] 没检测到 Microsoft Office Word。
    echo     卷面仍能生成，但 docx 转 PDF 会降级，公式与排版可能失真。
    echo     建议装 Word 2016 或更新版本；只想先试组卷可以继续。
)
echo.

rem ---------- 4) 起服务 ----------
rem HOST 必须与 config.example.json 的 server.host 一致（tests/test_packaging.py 锁这一条）
set "HOST=0.0.0.0"
echo [*] 启动命令: python -m uvicorn server:app --host %HOST% --port 8710
%PY% -c "import server;print(server.lan_startup_text())"
echo.
echo     (关掉本窗口 = 停止服务)

if /i "%MATH_NO_BROWSER%"=="1" (
    echo [i] MATH_NO_BROWSER=1，本次不自动开浏览器。
    goto :serve
)

rem 先等服务起来再开浏览器：3 秒后打开 http://127.0.0.1:8710/
start "" /b %PY% -c "import time,webbrowser; time.sleep(3); webbrowser.open('http://127.0.0.1:8710/')"

:serve
%PY% -m uvicorn server:app --host %HOST% --port 8710

echo.
echo [x] 服务已退出。常见原因:
echo     - 端口 8710 被占用: 先关掉旧的黑色窗口，或改本文件里的 --port 与界面地址
echo     - 手机连不上: 先看「设置 -^> 局域网访问」开关有没有打开（默认关）；再确认同一个 WiFi、防火墙放行 8710
echo     - 依赖装了一半: 重新执行 %PY% -m pip install -r requirements.txt
echo     - 想看详细报错: 往上翻本窗口的输出
echo.
pause
exit /b 0

:nodeps
echo.
echo [x] 没有依赖，服务起不来。请先执行:
echo     %PY% -m pip install -r requirements.txt
echo.
pause
exit /b 1
