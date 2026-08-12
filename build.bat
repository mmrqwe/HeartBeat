@echo off
chcp 65001 >nul
cd /d %~dp0
set PY=
py -3.12 -m pip --version >nul 2>&1
if not errorlevel 1 set PY=py -3.12
if defined PY goto :have_py
py -3 -m pip --version >nul 2>&1
if not errorlevel 1 set PY=py -3
if defined PY goto :have_py
python -m pip --version >nul 2>&1
if not errorlevel 1 set PY=python
if defined PY goto :have_py
goto :fail

:have_py
echo [1/3] 安装依赖...
%PY% -m pip install --quiet -r requirements.txt pyinstaller pillow -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto :fail
echo [2/3] 打包 exe...
%PY% -m PyInstaller --noconfirm --clean --workpath "%LOCALAPPDATA%\Temp\HeartBeat-build" --distpath "%~dp0dist" HeartBeat.spec
if errorlevel 1 goto :fail
if not exist "%~dp0dist\HeartBeat\HeartBeat.exe" goto :fail
echo [3/3] 校验完成...
echo.
echo 构建完成: %~dp0dist\HeartBeat\HeartBeat.exe
echo 首次运行会在用户数据目录（%%APPDATA%%\HeartBeat）自动生成 config.json，也可以放 plugins\ 扩展内容源。
pause
exit /b 0

:fail
echo 构建失败，请查看上方错误信息。
pause
exit /b 1
