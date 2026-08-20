@echo off
chcp 65001 >nul
cd /d %~dp0

echo === build_windows.bat v3 (1.0.0) ===

taskkill /f /im XianYuApis.exe >nul 2>&1

REM 自动寻找 Python
set "PY_CMD="
where python >nul 2>&1
if not errorlevel 1 set "PY_CMD=python"
if not defined PY_CMD (
    where py >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)
if not defined PY_CMD (
    where python3 >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python3"
)
if not defined PY_CMD (
    echo ERROR: 未找到 Python，请安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)
echo Python: %PY_CMD%

%PY_CMD% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo ERROR: 依赖安装失败
    pause
    exit /b 1
)

findstr /C:"APP_VERSION = '1.0.0'" gui.py >nul
if errorlevel 1 (
    echo ERROR: gui.py is outdated, missing APP_VERSION marker
    pause
    exit /b 1
)

if not exist node_bin\node.exe (
    echo Downloading Node.js for Windows...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "if (-not (Test-Path node_bin)) { New-Item -ItemType Directory -Force node_bin | Out-Null }; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $v='v20.18.0'; $url='https://nodejs.org/dist/'+$v+'/node-'+$v+'-win-x64.zip'; $zip=Join-Path $env:TEMP 'node-win-x64.zip'; Invoke-WebRequest -Uri $url -OutFile $zip; Expand-Archive -Path $zip -DestinationPath $env:TEMP -Force; Copy-Item (Join-Path $env:TEMP ('node-'+$v+'-win-x64\node.exe')) 'node_bin\node.exe' -Force"
    if not exist node_bin\node.exe (
        echo ERROR: failed to download node.exe, please manually put node.exe into node_bin\node.exe
        pause
        exit /b 1
    )
)

findstr /C:"def check_chrome_installed" cookie_auth.py >nul
if errorlevel 1 (
    echo ERROR: cookie_auth.py is outdated, missing check_chrome_installed
    pause
    exit /b 1
)

set CLEAN_RETRY=0
:cleanup
set /a CLEAN_RETRY+=1
if exist dist\XianYuApis.exe del /f /q dist\XianYuApis.exe >nul 2>&1
if exist dist rmdir /s /q dist >nul 2>&1
if exist build rmdir /s /q build >nul 2>&1
if exist dist (
    if %CLEAN_RETRY% GEQ 5 (
        echo ERROR: 无法删除旧的 dist 目录，文件可能被占用。
        echo 请关闭 XianYuApis.exe、退出安全软件扫描后重试。
        pause
        exit /b 1
    )
    echo WARNING: dist 仍存在，2 秒后重试删除...
    timeout /t 2 /nobreak >nul
    goto cleanup
)

%PY_CMD% -m PyInstaller --clean --noconfirm --distpath dist --workpath build xianyu.spec > build_output.log 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller 构建失败，日志已保存到 build_output.log
    type build_output.log
    pause
    exit /b 1
)

if not exist dist\XianYuApis\XianYuApis.exe (
    echo ERROR: 构建失败，未生成 dist\XianYuApis\XianYuApis.exe
    pause
    exit /b 1
)

echo.
echo Build done: dist\XianYuApis\XianYuApis.exe
for %%F in (dist\XianYuApis\XianYuApis.exe) do echo Build time: %%~tF  Size: %%~zF bytes
echo Version: 1.0.0
pause
