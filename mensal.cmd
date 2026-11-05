@echo off
rem Rotina automatica: coleta, categoriza e gera o painel do ultimo mes fechado.
rem Feita para o Agendador de Tarefas do Windows. So usa comandos de leitura do projeto.
rem O log fica junto dos dados reais (fora da pasta do projeto) e so tem contagens e numeros mascarados.
rem Entrada nula (<nul) e saida no log: a rotina nunca e o terminal do dono e nunca confirma regras.
rem Codigo de saida: 0 tudo certo, 3 regras nao confirmadas, 1 erro (o codigo original fica no log).
rem Sem rotulos nem goto: assim o arquivo funciona com qualquer final de linha.
setlocal
cd /d "%~dp0"
echo Rotina mensal do painel de financas rodando. Nao feche esta janela: ela fecha sozinha.
set "LOGS=%USERPROFILE%\financas-dados\logs"
if not exist "%LOGS%" mkdir "%LOGS%"
set "LOG=%LOGS%\mensal.log"
rem rotacao simples: acima de 1 MB, o log vira mensal.anterior.log
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 1000000 move /y "%LOG%" "%LOGS%\mensal.anterior.log" >nul
echo ==== %date% %time% ==== >> "%LOG%"

call "%~dp0financas.cmd" coletar --perfil real <nul >> "%LOG%" 2>&1
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo ERRO na coleta, codigo %RC%. Se havia painel, ele ganhou o aviso Painel desatualizado. >> "%LOG%"
  exit /b 1
)

call "%~dp0financas.cmd" categorizar --perfil real <nul >> "%LOG%" 2>&1
set "RC=%errorlevel%"
if "%RC%"=="3" (
  echo PARADO: regras ou ajustes mudaram e ainda nao foram confirmados. Se havia painel, ele ganhou um aviso. >> "%LOG%"
  echo Para liberar: no seu terminal, rode .\financas.cmd categorizar --perfil real, digite CONFIRMO >> "%LOG%"
  echo e depois rode .\financas.cmd relatorio --perfil real. >> "%LOG%"
  exit /b 3
)
if not "%RC%"=="0" (
  echo ERRO na categorizacao, codigo %RC%. Painel mantido como estava. >> "%LOG%"
  exit /b 1
)

call "%~dp0financas.cmd" relatorio --perfil real <nul >> "%LOG%" 2>&1
set "RC=%errorlevel%"
if "%RC%"=="3" (
  echo PARADO: regras ou ajustes mudaram durante a rotina. Rode .\financas.cmd categorizar --perfil real no seu terminal. >> "%LOG%"
  exit /b 3
)
if not "%RC%"=="0" (
  echo ERRO no relatorio, codigo %RC%. Painel mantido como estava. >> "%LOG%"
  exit /b 1
)
echo OK: painel atualizado. >> "%LOG%"
exit /b 0
