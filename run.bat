@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   AI 小工具
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 首次运行，正在创建运行环境...
    python -m venv .venv
    if errorlevel 1 goto venvfail
) else (
    echo [1/3] 运行环境已就绪
)

echo [2/3] 检查依赖...
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, cloakbrowser, curl_cffi, selenium, requests" >nul 2>nul
if errorlevel 1 (
    echo       首次安装依赖，需要几分钟，请耐心等待...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto depfail
) else (
    echo       依赖已就绪
)

echo [3/3] 正在启动，浏览器会自动打开界面...
echo.
".venv\Scripts\python.exe" app.py
goto end

:nopython
echo [错误] 没有找到 Python。
echo        请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
echo        安装时务必勾选 "Add Python to PATH"。
echo.
pause
exit /b 1

:venvfail
echo [错误] 创建运行环境失败。
pause
exit /b 1

:depfail
echo.
echo [错误] 依赖安装失败。若是网络问题，可改用国内镜像重试：
echo        .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:end
echo.
echo 程序已退出。
pause
