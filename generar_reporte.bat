@echo off
chcp 65001 >nul
cd /d "%~dp0"
"C:\Users\Procesos\AppData\Local\Python\pythoncore-3.14-64\python.exe" generar_reporte.py
git add index.html
git commit -m "Actualizar reporte"
git push
echo.
pause
