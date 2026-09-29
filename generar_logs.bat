@echo off
rem Genera los logs de las 10 ultimas partidas de MTGO en la carpeta "output".
cd /d "%~dp0"
py -m mtgo_replay --last 10
pause
