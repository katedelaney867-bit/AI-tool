<#
  AI 小工具 · 第一次运行自动准备环境

  客户只需要双击「一键启动.bat」，这个脚本负责把环境弄好：

    1. 已经准备好 → 跳过（幂等，日常启动不会重装）
    2. 下载官方嵌入式 Python（11MB，**二进制均由 Python 软件基金会签名**）
       —— 不用客户自己装 Python，也不会碰到「忘了勾 Add to PATH」那个经典坑
    3. 解压 + 接上 site-packages（嵌入式包默认是隔离的，要改 ._pth）
    4. 装 pip
    5. 按 requirements.lock.txt 装依赖（**版本钉死**，国内镜像优先）

  ## 为什么用嵌入式 Python 而不是让客户装

  · 不用管理员权限（解压到自己目录就行）
  · 不用配置 PATH
  · 版本确定 —— 客户装什么版本我们说了算，行为可复现
  · ★ 官方嵌入式包里每个 exe/dll/pyd 都是**已签名**的，
    所以 Win11 的「智能应用控制」不会拦（PyInstaller 打出来的未签名 exe 必被拦）

  ## 为什么依赖要钉死版本

  cloakbrowser 是 stealth Chromium 的封装，它换个版本浏览器指纹行为就可能变，
  表现成「注册成功率莫名下降」，而且极难归因。详见 requirements.lock.txt 顶部注释。
#>
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

# ★ 关掉「用户级 site-packages」（2026-10-06 实测踩到）。
#   嵌入式 Python 的 ._pth 启用了 `import site`，于是
#   %APPDATA%\Python\Python312\site-packages 也会进 sys.path。
#   后果很实际：客户以前用 `pip install --user` 装过 fastapi/pydantic 之类，
#   pip 就会认为「已满足」而**跳过安装**，我们的运行时变成依赖客户那份 —— 他一删就崩。
#   实测本机被 paramiko/scipy/tqdm 污染：pip check 报一堆假冲突，
#   而且 pip 安装会因为那些假冲突报错退出（于是白跑一个镜像源才成功）。
#   ★ 这里设了之后，后面所有 pip 调用都在这个环境变量下跑，能传到子进程。
$env:PYTHONNOUSERSITE = '1'

# ---- 版本与路径（改这里就能换 Python 版本） ----
$PyVersion   = '3.12.10'
$RuntimeDir  = Join-Path $Root 'runtime'
$PyExe       = Join-Path $RuntimeDir 'python.exe'
$Marker      = Join-Path $RuntimeDir '.ready'
$LockFile    = Join-Path $Root 'requirements.lock.txt'

# ---- 下载源：国内镜像优先，官方兜底 ----
$EmbedUrls = @(
    "https://mirrors.huaweicloud.com/python/$PyVersion/python-$PyVersion-embed-amd64.zip",
    "https://registry.npmmirror.com/-/binary/python/$PyVersion/python-$PyVersion-embed-amd64.zip",
    "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
)
$GetPipUrls = @(
    "https://bootstrap.pypa.io/get-pip.py"
)
$PipIndexes = @(
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://mirrors.cloud.tencent.com/pypi/simple",
    ""     # 空串 = pip 官方源
)

function Info($m) { Write-Host "    $m" }
function Ok($m)   { Write-Host "    [OK] $m"   -ForegroundColor Green }
function Warn($m) { Write-Host "    [!!] $m"   -ForegroundColor Yellow }

function Download-WithFallback {
    param([string[]]$Urls, [string]$Dest, [string]$Label)
    foreach ($u in $Urls) {
        if (-not $u) { continue }
        try {
            Info "下载$Label : $u"
            Invoke-WebRequest -Uri $u -OutFile $Dest -UseBasicParsing -TimeoutSec 900
            if ((Test-Path $Dest) -and (Get-Item $Dest).Length -gt 0) {
                Ok ("下载完成（{0:N1} MB）" -f ((Get-Item $Dest).Length / 1MB))
                return $true
            }
        } catch {
            Warn "这个源不行，换下一个（$($_.Exception.Message)）"
        }
    }
    return $false
}

Write-Host ""
Write-Host "============================================================"
Write-Host "  AI 小工具 · 第一次运行准备（只需跑一次）"
Write-Host "============================================================"
Write-Host ""

if (-not (Test-Path $LockFile)) {
    Write-Host "  找不到 requirements.lock.txt，请确认解压了**整个文件夹**。" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 已经好了？
if ((Test-Path $Marker) -and (Test-Path $PyExe) -and -not $Force) {
    try {
        $probe = & $PyExe -c "import fastapi, uvicorn, cloakbrowser; print('ok')" 2>&1
        if ($probe -match 'ok') {
            Ok "环境已就绪，跳过准备"
            exit 0
        }
    } catch { }
    Warn "环境标记还在但依赖不完整，重新准备"
}

# ------------------------------------------------------- 1. 嵌入式 Python
Write-Host "  [1/5] 准备 Python 运行环境"
if (-not (Test-Path $PyExe) -or $Force) {
    $tmpZip = Join-Path $env:TEMP "py-embed-$PyVersion.zip"
    if (-not (Download-WithFallback -Urls $EmbedUrls -Dest $tmpZip -Label "Python（约 11MB）")) {
        Write-Host ""
        Write-Host "  下载 Python 失败（所有源都试过了）。" -ForegroundColor Red
        Write-Host "  请检查网络后重试；或手动下载下面这个文件，改名放到本目录，再重跑：" -ForegroundColor Red
        Write-Host "      $($EmbedUrls[0])" -ForegroundColor Yellow
        Write-Host "      -> $tmpZip" -ForegroundColor Yellow
        exit 1
    }
    if (Test-Path $RuntimeDir) { Remove-Item $RuntimeDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    Expand-Archive -Path $tmpZip -DestinationPath $RuntimeDir -Force
    Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
    Ok "已解压到 runtime\"

    # 嵌入式包默认是隔离的：必须显式把 site-packages 加进搜索路径并启用 site
    $pth = Get-ChildItem $RuntimeDir -Filter '*._pth' | Select-Object -First 1
    if (-not $pth) {
        Write-Host "  runtime 里没找到 ._pth，嵌入式包结构可能变了。" -ForegroundColor Red
        exit 1
    }
    $ver = ($pth.Name -replace '\._pth$', '')
    Set-Content -Path $pth.FullName -Encoding ASCII `
        -Value @("$ver.zip", ".", "Lib\site-packages", "import site")
    Ok "已配置模块搜索路径（$($pth.Name)）"
} else {
    Ok "Python 运行时已存在"
}

# ------------------------------------------------------------------ 2. pip
Write-Host "  [2/5] 准备 pip"
$hasPip = $false
try {
    $v = & $PyExe -m pip --version 2>&1
    if ($v -match 'pip') { $hasPip = $true; Ok "pip 已存在（$v）" }
} catch { }

if (-not $hasPip) {
    $getPip = Join-Path $env:TEMP "get-pip.py"
    if (-not (Download-WithFallback -Urls $GetPipUrls -Dest $getPip -Label "pip 安装器")) {
        Write-Host "  下载 pip 安装器失败，请检查网络后重试。" -ForegroundColor Red
        exit 1
    }
    Info "正在安装 pip（约需 1 分钟）…"
    & $PyExe $getPip --no-warn-script-location --disable-pip-version-check 2>&1 |
        ForEach-Object { if ($_ -match 'Successfully|error|ERROR') { Info $_ } }
    Remove-Item $getPip -Force -ErrorAction SilentlyContinue
    try {
        $v = & $PyExe -m pip --version 2>&1
        if ($v -notmatch 'pip') { throw "pip 仍然不可用" }
        Ok "pip 安装完成"
    } catch {
        Write-Host "  pip 安装失败：$_" -ForegroundColor Red
        exit 1
    }
}

# ------------------------------------------------------------- 3. 装依赖
Write-Host "  [3/5] 安装依赖（约 200MB，第一次会慢，请耐心等）"
$installed = $false
foreach ($idx in $PipIndexes) {
    $label = if ($idx) { $idx } else { 'pip 官方源' }
    Info "用 $label 安装…"
    # ★ 变量名别用 $args —— 它是 PowerShell 的自动变量（脚本参数），
    #   赋值会跟它打架。踩过一次，记下来。
    $pipArgs = @('-m', 'pip', 'install', '-r', $LockFile,
                 '--no-warn-script-location', '--disable-pip-version-check')
    if ($idx) { $pipArgs += @('-i', $idx) }
    try {
        & $PyExe @pipArgs 2>&1 | ForEach-Object {
            if ($_ -match 'Successfully installed|ERROR|error:') { Info $_ }
        }
        if ($LASTEXITCODE -eq 0) { $installed = $true; Ok "依赖安装完成（$label）"; break }
        Warn "$label 安装失败（退出码 $LASTEXITCODE），换下一个源"
    } catch {
        Warn "$label 出错：$($_.Exception.Message)"
    }
}
if (-not $installed) {
    Write-Host ""
    Write-Host "  依赖安装失败（所有源都试过了）。" -ForegroundColor Red
    Write-Host "  手动重试命令（在本目录下执行）：" -ForegroundColor Yellow
    Write-Host "      runtime\python.exe -m pip install -r requirements.lock.txt -i https://pypi.tuna.tsinghua.edu.cn/simple" -ForegroundColor Yellow
    exit 1
}

# --------------------------------------------------------------- 4. 验证
Write-Host "  [4/5] 验证依赖是否可用"
$probe = & $PyExe -c "import fastapi, uvicorn, pydantic, requests, curl_cffi, cloakbrowser, playwright, selenium; print('ALL_IMPORT_OK')" 2>&1
if ($probe -notmatch 'ALL_IMPORT_OK') {
    Write-Host "  依赖装上了但导入失败：" -ForegroundColor Red
    Write-Host "  $probe" -ForegroundColor Red
    exit 1
}
Ok "关键依赖全部可导入"

# ------------------------------------------------------------- 5. 收尾
Write-Host "  [5/5] 收尾"
Set-Content -Path $Marker -Value (Get-Date -Format 'o') -Encoding ASCII
Ok "环境准备完成 —— 下次启动会直接跳过这一步"
Write-Host ""
exit 0
