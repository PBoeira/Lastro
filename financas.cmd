@echo off
rem Atalho do painel de financas. Uso: financas.cmd <comando> [opcoes]
rem -I isola o Python: ignora variaveis PYTHON* e pacotes do usuario, e nao poe a pasta atual nem a
rem pasta do script no sys.path; -S deixa de fora os pacotes instalados. O iniciador poe a raiz do
rem projeto no fim do sys.path, depois da biblioteca padrao: um json.py deixado na raiz nao e usado.
rem O lancador py e chamado por caminho absoluto, nunca pela pasta atual.
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\py.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not exist "%PY%" set "PY=%SystemRoot%\py.exe"
if not exist "%PY%" (
  echo Lancador py do Python nao encontrado. Instale o Python 3.14 pelo python.org ou pela Microsoft Store.
  exit /b 1
)
"%PY%" -3.14 -I -S -X utf8 "%~dp0financas\_iniciar.py" financas %*
