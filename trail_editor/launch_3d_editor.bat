@echo off
REM Launches the 3D trail editor. MapLibre needs http:// (not file://),
REM so this starts a tiny local web server, then opens the browser.
cd /d "%~dp0"
echo Starting Trail Editor on http://localhost:8777 ...
echo (Leave the server window open while you use the editor.)
start "trail-editor-server" "C:\Program Files\QGIS 4.0.3\bin\python-qgis.bat" -m http.server 8777
timeout /t 3 >nul
start "" "http://localhost:8777/index.html"
echo.
echo If the browser did not open, go to:  http://localhost:8777/index.html
echo To stop the server later, just close the "trail-editor-server" window.
pause
