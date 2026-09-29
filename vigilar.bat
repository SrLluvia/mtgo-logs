@echo off
rem Vigila MTGO y genera los logs automaticamente al terminar cada match (Ctrl+C para parar).
cd /d "%~dp0"
py -m mtgo_replay --watch
