@echo off
chcp 65001 >NUL
setlocal ENABLEDELAYEDEXPANSION

REM ===========================
REM 进入脚本所在目录
pushd "%~dp0"
title Python 启动器
REM ===========================

REM 选择 Python 解释器：优先系统 python，不存在则用常见路径
set "PYTHON=python"
"%PYTHON%" -V >NUL 2>&1
if errorlevel 1 (
  set "PYTHON=C:\Users\10062\AppData\Local\Programs\Python\Python311\python.exe"
)

"%PYTHON%" -V >NUL 2>&1
if errorlevel 1 (
  echo 未找到可用的 Python。请安装 Python 3.11 或修改本 bat 中的 PYTHON 路径。
  pause
  exit /b 1
)

echo 使用 Python: %PYTHON%

REM 确保 pip 可用
echo 检查 pip...
"%PYTHON%" -m pip -V >NUL 2>&1
if errorlevel 1 (
  echo 未检测到 pip，尝试使用 ensurepip...
  "%PYTHON%" -m ensurepip --upgrade >NUL 2>&1
)

"%PYTHON%" -m pip -V >NUL 2>&1
if errorlevel 1 (
  echo ensurepip 仍未生效，尝试在线获取 get-pip.py...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing https://bootstrap.pypa.io/get-pip.py -OutFile get-pip.py } catch { exit 1 }"
  if errorlevel 1 (
    echo 下载 get-pip.py 失败，请检查网络或代理设置。
    pause
    exit /b 1
  )
  "%PYTHON%" get-pip.py
  if errorlevel 1 (
    echo 运行 get-pip.py 失败，请手动安装 pip 后再试。
    pause
    exit /b 1
  )
)

REM 升级 pip（非必需）
"%PYTHON%" -m pip install --upgrade pip >NUL 2>&1

REM 安装依赖
echo 正在安装依赖（如已安装会快速跳过）...
"%PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo 依赖安装失败，尝试使用清华源加速...
  "%PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo 依赖安装仍失败，请检查网络或稍后重试。
    pause
    exit /b 1
  )
)

REM ===========================
REM 启动程序并最小化 .bat 窗口
REM ===========================
echo 正在启动程序...

REM 方法 1：用 start 命令最小化执行 Python
start "Python运行中..." /min "%PYTHON%" ".\main.py"

REM 方法 2（可选）：如果你想完全隐藏窗口，用以下替换上面一行
REM powershell -WindowStyle Hidden -Command "Start-Process '%PYTHON%' '.\main.py'"

REM ===========================
REM 清理 & 退出
popd
exit /b 0

