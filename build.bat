@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  电力公司 打包脚本
rem     build.bat            生成 dist\电力公司\ + release\ 里的发行 zip（推荐）
rem     build.bat onefile    生成 dist\电力公司.exe（单文件，首次启动慢）
rem
rem  会自动寻找可用的 Python（跳过 Windows 应用商店的占位程序），
rem  也可以用 set PYTHON=C:\path\to\python.exe 指定。
rem
rem  打包完会：跑一次打包产物的 --selftest（不通过就判失败）
rem            → 写 MANIFEST.json → 压成 release\电力公司-<版本>-win64.zip
rem ============================================================

set "MODE=--onedir"
if /i "%~1"=="onefile" set "MODE=--onefile"

rem ---------- 1. 找 Python ----------
set "PY="
if defined PYTHON set "PY=%PYTHON%"

if not defined PY (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        set "CAND=%%I"
        echo !CAND! | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            if not defined PY (
                "!CAND!" -c "import tkinter" >nul 2>nul
                if not errorlevel 1 set "PY=!CAND!"
            )
        )
    )
)
if not defined PY (
    for /f "delims=" %%I in ('where py 2^>nul') do (
        set "CAND=%%I"
        echo !CAND! | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            if not defined PY (
                "!CAND!" -3 -c "import tkinter" >nul 2>nul
                if not errorlevel 1 set "PY=!CAND!"
            )
        )
    )
)
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "C:\Python3*" "%ProgramFiles%\Python3*" "%LOCALAPPDATA%\Python\pythoncore-3*") do (
        if exist "%%~fD\python.exe" if not defined PY set "PY=%%~fD\python.exe"
    )
)
if not defined PY (
    for /d %%D in ("%USERPROFILE%\.dsh\dsh-runtimes\*\dependencies\python") do (
        if exist "%%~fD\python.exe" if not defined PY set "PY=%%~fD\python.exe"
    )
)

if not defined PY goto :nopython

"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [错误] 这个 Python 没有 tkinter，无法打包图形版：
    echo        %PY%
    echo        请安装官方 python.org 版本，或用 set PYTHON=... 指定别的解释器。
    exit /b 1
)

set "PYTHONPATH=%CD%\build_tools"
echo [1/7] 使用 Python: %PY%
"%PY%" -c "import sys;print('  版本', sys.version.split()[0], sys.executable)"

rem ---------- 2. PyInstaller ----------
rem 优先用已经装好的（pip / CI 环境），都没有才去 pypi 抓 wheel
"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [2/7] 抓取 PyInstaller ...
    "%PY%" tools\fetch_pyinstaller.py || goto :fail
) else (
    echo [2/7] PyInstaller 已就绪
)

rem ---------- 3. 图标 + exe 版本资源 ----------
if not exist "%CD%\assets\app.ico" (
    echo [3/7] 生成图标 ...
    "%PY%" tools\make_icon.py || goto :fail
) else (
    echo [3/7] 图标已就绪
)
"%PY%" tools\make_version_file.py || goto :fail

rem ---------- 4. 源码自检 ----------
echo [4/7] 源码自检 ...
"%PY%" main.py --selftest || goto :fail

rem ---------- 5. 打包 ----------
echo [5/7] 打包中（%MODE%）...
"%PY%" -m PyInstaller --noconfirm --clean %MODE% --windowed ^
    --name "电力公司" ^
    --icon "%CD%\assets\app.ico" ^
    --version-file "%CD%\build\version_info.txt" ^
    --add-data "%CD%\assets;assets" ^
    --distpath dist --workpath build --specpath build ^
    --log-level WARN main.py
if errorlevel 1 goto :fail

rem ---------- 6. 打包产物自检（门禁） ----------
rem 这一步才是真正的验收：跑的是 dist 里那个 exe，不是源码。
if /i "%MODE%"=="--onefile" (
    set "APPEXE=%CD%\dist\电力公司.exe"
    set "APPDIR=%CD%\dist"
) else (
    set "APPEXE=%CD%\dist\电力公司\电力公司.exe"
    set "APPDIR=%CD%\dist\电力公司"
)
rem 注意：批处理里直接调用 GUI 程序，cmd 会等它结束并带回 exit code；
rem 用 start /wait 反而拿不到真实返回值，所以这里不要加 start。
echo [6/7] 产物自检：!APPEXE!
if not exist "!APPEXE!" goto :fail
"!APPEXE!" --selftest
if errorlevel 1 (
    echo.
    echo [失败] 打包产物自检没通过，不要发布这个东西。下面是自检日志：
    echo ----------------------------------------------
    if exist "!APPDIR!\selftest.log" type "!APPDIR!\selftest.log"
    echo ----------------------------------------------
    goto :fail
)
echo   产物自检通过

rem ---------- 7. 安全告知 + 发行包 ----------
echo [7/7] 生成安全告知 + 发行包 ...
"%PY%" tools\make_notice.py || goto :fail
"%PY%" tools\pack_release.py || goto :fail

echo.
if /i "%~1"=="onefile" (
    echo 完成：dist\电力公司.exe
    echo 发行：release\电力公司-*-win64.zip
) else (
    echo 完成：dist\电力公司\电力公司.exe
    echo 发行：release\电力公司-*-win64.zip（把 zip 发出去即可，玩家解压就能玩）
)
goto :eof

:nopython
echo.
echo [错误] 没找到可用的 Python（需要 3.10+ 且带 tkinter）。
echo   1. 安装官方版本：https://www.python.org/downloads/windows/
echo   2. 或者手动指定：set PYTHON=D:\Python312\python.exe  然后重新运行 build.bat
exit /b 1

:fail
echo.
echo 打包失败，请检查上面的报错。
exit /b 1
