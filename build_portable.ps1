# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    打包「数学题库工作台」便携版（PyInstaller onedir + qbank 题库 + 一键 zip）。

.DESCRIPTION
    产出：
      dist\数学题库工作台\             ← 双击「数学题库工作台.exe」即可运行
        ├─ 数学题库工作台.exe
        ├─ _internal\                 ← Python 运行时 / 依赖 / templates / static / data\app.db
        ├─ qbank\                     ← 22,898 份题目 docx（从 E:\OCR 只读复制，**不塞进 PyInstaller**）
        └─ README-便携版.txt
      dist\数学题库工作台-v1.1.zip     ← 拷到 U 盘带走（-NoZip 可跳过）

    可重复执行：每次先清 dist\<名>\ 与 build\<名>\，再重新打包；题库用 robocopy /MIR 增量同步。

.PARAMETER Name      产物名（默认 数学题库工作台）
.PARAMETER Version   zip 里的版本号（默认 v1.1）
.PARAMETER Python    用哪个解释器（默认 python）
.PARAMETER SkipQbank 跳过复制题库（只验代码打包，速度快）
.PARAMETER NoZip     不生成 zip
.PARAMETER NoClean   不清理旧的 dist/<名>（增量覆盖，调试用）

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File app\build_portable.ps1
    powershell -ExecutionPolicy Bypass -File app\build_portable.ps1 -SkipQbank -NoZip
#>
param(
    [string]$Name = "数学题库工作台",
    [string]$Version = "v1.1",
    [string]$Python = "python",
    [switch]$SkipQbank,
    [switch]$NoZip,
    [switch]$NoClean
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$App = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Root = Split-Path -Parent $App                      # E:\OCR
$Dist = Join-Path $Root "dist"
$Work = Join-Path $Root "build"
$Target = Join-Path $Dist $Name
$QbankSrc = $Root                                  # E:\OCR\*.docx（只读复制）
$QbankDst = Join-Path $Target "qbank"
$ZipPath = Join-Path $Dist ("$Name-$Version.zip")

function Step([string]$Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Ok([string]$Text) { Write-Host "  [OK] $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "  [!] $Text" -ForegroundColor Yellow }

$script:Started = Get-Date
Step "0) 环境检查"
& $Python -c "import PyInstaller, sys; print('PyInstaller', PyInstaller.__version__, '| Python', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 不可用：$Python -m pip install pyinstaller" }
foreach ($need in @("server.py", "launcher.py", "templates", "static", "config.json", "data\app.db")) {
    if (-not (Test-Path (Join-Path $App $need))) { throw "缺 $need（要在 app\ 目录里构建）" }
}
Ok "源码与资源齐全（$App）"

Step "1) 清理旧产物"
if (-not $NoClean) {
    foreach ($p in @($Target, (Join-Path $Work $Name))) {
        if (Test-Path $p) { Remove-Item -Recurse -Force $p; Warn "已删 $p" }
    }
}
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
Ok "dist = $Dist"

Step "2) PyInstaller onedir 打包（不要 onefile：300MB 每次解压要十几秒）"
Push-Location $App
try {
    $pyArgs = @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", $Name, "--distpath", $Dist, "--workpath", $Work,
        "--add-data", "templates;templates",
        "--add-data", "static;static",
        "--add-data", "data\app.db;data",
        "--add-data", "config.json;.",
        # uvicorn 是动态 import，必须显式声明，否则打包后启动报 "No module named uvicorn.loops.auto"
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.protocols.websockets.websockets_impl",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "uvicorn.lifespan.off",
        # Word COM（render.py 里是 try/import，静态分析看不到）
        "--hidden-import", "win32com.client",
        "--hidden-import", "pythoncom",
        "--hidden-import", "pywintypes",
        "--hidden-import", "win32api",
        "--hidden-import", "multipart",
        "--hidden-import", "jinja2.ext",
        # 减体积：这些都没用到
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        "--exclude-module", "torch",
        "launcher.py"
    )
    & $Python @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（退出码 $LASTEXITCODE）" }
} finally {
    Pop-Location
}
$Exe = Join-Path $Target "$Name.exe"
if (-not (Test-Path $Exe)) { throw "没生成 exe：$Exe" }
Ok ("exe: {0}  ({1:N1} MB)" -f $Exe, ((Get-Item $Exe).Length / 1MB))

Step "3) 复制题库 docx → qbank\（只读复制，不动 E:\OCR 原件）"
if ($SkipQbank) {
    Warn "按 -SkipQbank 跳过题库复制（卷面/PDF 会因为没有 docx 而不可用）"
} else {
    New-Item -ItemType Directory -Force -Path $QbankDst | Out-Null
    # robocopy 处理两万个 5KB 小文件比 Copy-Item 快一个数量级；/MIR 让重跑变增量
    $rc = Start-Process -FilePath "robocopy" `
        -ArgumentList @("`"$QbankSrc`"", "`"$QbankDst`"", "*.docx", "/MIR", "/MT:16", "/NFL", "/NDL", "/NJH", "/NJS", "/NP") `
        -Wait -PassThru -NoNewWindow
    if ($rc.ExitCode -ge 8) { throw "robocopy 复制题库失败（退出码 $($rc.ExitCode)）" }
    $qCount = (Get-ChildItem $QbankDst -Filter *.docx -File | Measure-Object).Count
    $qMB = (Get-ChildItem $QbankDst -Filter *.docx -File | Measure-Object Length -Sum).Sum / 1MB
    Ok ("qbank: {0:N0} 份 docx / {1:N1} MB" -f $qCount, $qMB)
    if ($qCount -lt 20000) { Warn "题库只有 $qCount 份，明显偏少，确认 E:\OCR 原件是否完整" }
}

Step "4) 写 README-便携版.txt（UTF-8 无 BOM）"
$Readme = @'
数学题库工作台 · 便携版 v1.1
============================================================

一、怎么用
  1) 把整个「数学题库工作台」文件夹拷到目标电脑（U 盘 / 桌面都行，路径别太深）。
  2) 双击「数学题库工作台.exe」。等几秒，浏览器会自动打开
     http://127.0.0.1:8710/ ——没自动开就手动输这个地址。
  3) 关掉黑色命令行窗口 = 停止服务。

二、前提
  * 目标电脑是 Windows 10/11（64 位）。
  * **需要 Microsoft Word 2016 或更新版本**：卷面 docx → PDF 靠它。
    没装 Word 也能开程序、组卷、判卷，但「导 PDF / 打印」会失败并提示降级。
  * 不需要装 Python、不需要联网（OCR 云端识别才需要网，可关）。

三、目录里都是什么
  数学题库工作台.exe     双击这个
  _internal\             程序本体（Python 运行时 + 依赖 + 模板），别动
  qbank\                 题库：22,898 份题目 docx（只读，别改/别删）
  data\app.db            题库索引 + 你的卷子记录（**第一次启动会从包内复制一份到这里**）
  config.json            配置：端口、访问码、OCR token（**含密钥，别外传/别发群里**）
  out\                   生成的卷子/答题卡/判分图都在这（第一次运行自动创建，可随时删）

四、手机拍照判卷（局域网）
  默认只允许本机访问。要让手机连，命令行运行：
      数学题库工作台.exe --lan
  窗口会打印「手机/同 WiFi 打开」的地址和 4 位访问码；手机扫码/输地址后输入访问码即可。
  * 只在自己家的 WiFi 用；公用 WiFi 别开。
  * Windows 防火墙弹窗要允许（专用网络）。
  * 关掉 --lan 就恢复只本机。

五、常见问题
  * 端口 8710 被占用：程序会自动 +1（看窗口里的实际地址）。
  * 想换端口：--port 8800；想改默认值：编辑 config.json 的 server.port。
  * 打不开/白屏：命令行运行「数学题库工作台.exe --check」，会打印题库/数据库/Word/端口自检。
  * 想从头开始：删掉 data\app.db 与 out\（下次启动会重新从包内复制数据库）。
  * 换电脑后题库更新：把新的 qbank\ 整个覆盖过去即可（data\ 保留你的卷子记录）。

六、隐私
  全部在本机运行；只有「填空/解答 OCR 识别」会把你裁的作答图发到云端识别服务
  （config.json 里的 OCR token）。不想联网就把 config.json 的 ocr.backend 改成 "manual"。
'@
$ReadmePath = Join-Path $Target "README-便携版.txt"
[System.IO.File]::WriteAllText($ReadmePath, $Readme, (New-Object System.Text.UTF8Encoding($false)))
Ok "已写 $ReadmePath"

Step "5) 统计体积"
$files = Get-ChildItem $Target -Recurse -File
$totalMB = ($files | Measure-Object Length -Sum).Sum / 1MB
Ok ("解压后：{0:N0} 个文件 / {1:N1} MB" -f $files.Count, $totalMB)
foreach ($sub in @("_internal", "qbank", "data")) {
    $p = Join-Path $Target $sub
    if (Test-Path $p) {
        $s = (Get-ChildItem $p -Recurse -File | Measure-Object Length -Sum)
        Ok ("  {0,-10} {1,8:N1} MB" -f $sub, ($s.Sum / 1MB))
    }
}

Step "6) 打 zip"
if ($NoZip) {
    Warn "按 -NoZip 跳过"
} else {
    if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $t0 = Get-Date
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $Target, $ZipPath, [System.IO.Compression.CompressionLevel]::Optimal, $true)
    $zipMB = (Get-Item $ZipPath).Length / 1MB
    Ok ("zip: {0}  ({1:N1} MB, 用时 {2:N1}s)" -f $ZipPath, $zipMB, ((Get-Date) - $t0).TotalSeconds)
}

$elapsed = ((Get-Date) - $script:Started).TotalSeconds
Write-Host ""
Write-Host ("打包完成：{0}（总用时 {1:N1}s）" -f $Target, $elapsed) -ForegroundColor Green
Write-Host "下一步：解压 zip 到干净目录 → 双击 exe → 组一份卷 → 看 PDF 是否生成"
