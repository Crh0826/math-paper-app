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
.PARAMETER Version   zip 名与包内 README/版本行（默认 v1.2）
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
    [string]$Version = "v1.2",
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
# 题库来源：优先 E:\OCR\qbank（2026-11-01 起的规范布局），老布局才回退到 E:\OCR 根目录
$QbankSrc = if (Test-Path (Join-Path $Root "qbank")) { Join-Path $Root "qbank" } else { $Root }
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

Step "0b) 校验答题卡标识是 5×5 新版（防止把 legacy 4×4 卡打进包）"
$cardPy = Join-Path $App "card.py"
if (-not (Test-Path $cardPy)) { throw "找不到 card.py：$cardPy（答题卡模块缺失，不能打包）" }
$cardSrc = Get-Content $cardPy -Raw -Encoding UTF8
$gridMatch = [regex]::Match($cardSrc, '(?m)^\s*ID_GRID\s*=\s*(\d+)')
if (-not $gridMatch.Success) { throw "card.py 里找不到 ID_GRID 常量，无法确认是 5×5 新卡；打包中止（用户装了会判 legacy_card）" }
$idGrid = [int]$gridMatch.Groups[1].Value
if ($idGrid -ne 5) { throw "答题卡标识是 $idGrid×$idGrid，不是 5×5（t46 新版）；打包中止，免得用户装了被判 legacy_card" }
if ($cardSrc -notmatch 'ID_GRID \* ID_GRID') { throw "card.py 里没有 5×5=25 位的用法（ID_GRID*ID_GRID），疑似旧版；打包中止" }
Ok "PASS: ID_GRID=5 —— 答题卡标识 = 5×5（25 位 = 20 位题号 + 5 位 CRC），不是 legacy 4×4"
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
        "--name", $Name, "--distpath", $Dist, "--workpath", $Work, "--specpath", $Work,
        # 用绝对路径：--specpath 把 spec 放到 build\ 里，相对路径会被按 spec 目录解析
        "--add-data", "$App\templates;templates",
        "--add-data", "$App\static;static",
        "--add-data", "$App\data\app.db;data",
        # config.json 不从这里进包：真实 config 带 OCR token，见 2b) 脱敏后再拷进 _internal
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
        # ocr.pin_stdlib_select() 会重 import select（打包后容易丢），显式声明
        "--hidden-import", "select",
        "--hidden-import", "selectors",
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

Step "2b) 生成打包用配置（脱敏：不带 OCR token）"
$assets = Join-Path $Work "_assets"
New-Item -ItemType Directory -Force -Path $assets | Out-Null
$port = 8710
try {
    $realCfg = Get-Content (Join-Path $App "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($realCfg.server -and $realCfg.server.port) { $port = [int]$realCfg.server.port }
} catch { Warn "读现有 config.json 失败，端口用默认 8710" }
$packCfg = [ordered]@{
    "_note"  = "便携版默认配置：不含任何密钥，OCR 默认本机 manual（填空/解答留待人工确认）。要用云端识别再填 token，见 README-便携版.txt。"
    "ocr"     = [ordered]@{
        "backend"   = "manual"
        "timeout_s" = 120
        "poll_s"    = 2
        "paddle_vl" = [ordered]@{
            "token"   = ""
            "model"   = "PaddleOCR-VL-1.6"
            "job_url" = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
        }
        "vlm"       = [ordered]@{ "base_url" = ""; "key" = ""; "model" = "" }
    }
    "server"  = [ordered]@{ "host" = "127.0.0.1"; "port" = $port; "pin" = "" }
}
[System.IO.File]::WriteAllText((Join-Path $assets "config.json"),
    ($packCfg | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$example = Join-Path $App "config.example.json"
if (Test-Path $example) { Copy-Item $example (Join-Path $assets "config.example.json") -Force }
# 放进 PyInstaller 的 _internal（启动时装进 exe 旁边；launcher.bootstrap_file 只在缺失时复制）
$internal = Join-Path $Target "_internal"
if (-not (Test-Path $internal)) { throw "找不到 $internal：PyInstaller 没按 onedir 产出" }
Copy-Item (Join-Path $assets "config.json") (Join-Path $internal "config.json") -Force
if (Test-Path (Join-Path $assets "config.example.json")) {
    Copy-Item (Join-Path $assets "config.example.json") (Join-Path $internal "config.example.json") -Force
}
Ok "打包用 config.json 已脱敏（ocr.backend=manual，无 token）并放进 _internal"
[System.IO.File]::WriteAllText((Join-Path $internal "VERSION.txt"), $Version.TrimStart("vV") + "`n", (New-Object System.Text.UTF8Encoding($false)))
Ok "包内版本 = $Version（写进 _internal\VERSION.txt，启动横幅/--check 会显示它）"

Step "3) 复制题库 docx → qbank\（只读复制，不动 E:\OCR 原件）"
if ($SkipQbank) {
    Warn "按 -SkipQbank 跳过题库复制（卷面/PDF 会因为没有 docx 而不可用）"
} else {
    # 【关键】qbank 就在 E:\OCR 里面：绝不能递归复制，否则 robocopy 会把 dist 自己拷进 qbank。
    #（第一次构建踩过：/MIR 递归出 6 万多个 qbank/dist/... 目录。）所以清空 + 只拷顶层（/LEV:1）。
    if (Test-Path $QbankDst) {
        $empty = Join-Path $Work "_empty"
        New-Item -ItemType Directory -Force -Path $empty | Out-Null
        $rc0 = Start-Process -FilePath "robocopy" -ArgumentList @("`"$empty`"", "`"$QbankDst`"", "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP") -Wait -PassThru -NoNewWindow
        if ($rc0.ExitCode -ge 8) { throw "清空旧 qbank 失败（退出码 $($rc0.ExitCode)）" }
    }
    New-Item -ItemType Directory -Force -Path $QbankDst | Out-Null
    $rc = Start-Process -FilePath "robocopy" `
        -ArgumentList @("`"$QbankSrc`"", "`"$QbankDst`"", "*.docx", "/LEV:1", "/MT:16", "/NFL", "/NDL", "/NJH", "/NJS", "/NP") `
        -Wait -PassThru -NoNewWindow
    if ($rc.ExitCode -ge 8) { throw "robocopy 复制题库失败（退出码 $($rc.ExitCode)）" }
    $subs = @(Get-ChildItem $QbankDst -Directory -ErrorAction SilentlyContinue)
    if ($subs.Count -gt 0) {
        throw "qbank 里出现了 $($subs.Count) 个子目录（第一个：$($subs[0].Name)）——递归复制了，立刻停"
    }
    $qCount = (Get-ChildItem $QbankDst -Filter *.docx -File | Measure-Object).Count
    $qMB = (Get-ChildItem $QbankDst -Filter *.docx -File | Measure-Object Length -Sum).Sum / 1MB
    Ok ("qbank: {0:N0} 份 docx / {1:N1} MB（无子目录，确认只拷了顶层）" -f $qCount, $qMB)
    if ($qCount -lt 20000) { Warn "题库只有 $qCount 份，明显偏少，确认 E:\OCR 原件是否完整" }
}

Step "4) 写 README-便携版.txt（UTF-8 无 BOM）"
$Readme = @'
数学题库工作台 · 便携版 __VERSION__
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
  config.json            配置：端口、访问码、OCR 设置（**包里这份是脱敏的，不含任何密钥**）
  config.example.json    配置模板（字段说明）
  out\                   生成的卷子/答题卡/判分图都在这（第一次运行自动创建，可随时删）
  README-便携版.txt      就是你正在看的这个

四、要不要开云端 OCR
  便携版默认 ocr.backend = "manual"：填空/解答判卷结果会留在「待人工确认」，不联网、不花钱。
  想让它自动认字：编辑 config.json，把 ocr.backend 改成 "paddle_vl"，
  再把你的 paddle_vl.token 填进去（token 只放这个文件，别外传/别发群里）。
  也可以不写进文件，用环境变量：set OCR_TOKEN=xxxxx 再启动。
  不想让任何作答图外发，就保持 manual。

五、手机拍照判卷（局域网）
  默认只允许本机访问。要让手机连，命令行运行：
      数学题库工作台.exe --lan
  窗口会打印「手机/同 WiFi 打开」的地址和 4 位访问码；手机扫码/输地址后输入访问码即可。
  * 只在自己家的 WiFi 用；公用 WiFi 别开。
  * Windows 防火墙弹窗要允许（专用网络）。
  * 关掉 --lan 就恢复只本机。

六、常见问题
  * 端口 8710 被占用：程序会自动 +1（看窗口里的实际地址）。
  * 想换端口：--port 8800；想改默认值：编辑 config.json 的 server.port。
  * 打不开/白屏：命令行运行「数学题库工作台.exe --check」，会打印题库/数据库/Word/端口自检。
  * 想从头开始：删掉 data\app.db 与 out\（下次启动会重新从包内复制数据库）。
  * 换电脑后题库更新：把新的 qbank\ 整个覆盖过去即可（data\ 保留你的卷子记录）。
  * 首次启动会从包内 _internal\ 复制 120MB 数据库到 data\，慢 1~3 秒属正常。

七、隐私
  全部在本机运行；只有你自己把 ocr.backend 改成云端后端时，才会把裁出的作答图发到识别服务。
  默认 manual 模式不联网。
'@
$Readme = $Readme.Replace('__VERSION__', $Version)
$ReadmePath = Join-Path $Target "README-便携版.txt"
[System.IO.File]::WriteAllText($ReadmePath, $Readme, (New-Object System.Text.UTF8Encoding($false)))
Ok "已写 $ReadmePath"

Step "4b) 自动自检 exe --check（不落盘：不会把 120MB 数据库复制进包里）"
$checkOut = & $Exe --check 2>&1 | Out-String
$checkCode = $LASTEXITCODE
foreach ($line in ($checkOut.Trim() -split "`r?`n")) { Write-Host "    $line" }
if ($checkCode -ne 0) { throw "exe --check 失败（退出码 $checkCode）：题库/数据库/配置至少缺一项，先别发版" }
Ok "exe --check 通过（打包产物可直接运行）"

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
