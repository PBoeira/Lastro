@echo off
rem Roda os testes automaticos (so dados sinteticos, sem rede nem credencial).
rem Mesmo isolamento do financas.cmd: -I -S e o iniciador, que poe a raiz do projeto no fim do sys.path.
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\py.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not exist "%PY%" set "PY=%SystemRoot%\py.exe"
if not exist "%PY%" (
  echo Lancador py do Python nao encontrado.
  exit /b 1
)
"%PY%" -3.14 -I -S -X utf8 -W error::ResourceWarning "%~dp0financas\_iniciar.py" unittest discover -s "%~dp0tests"
