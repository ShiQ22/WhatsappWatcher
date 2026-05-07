@echo off
echo Stopping WhatsApp Watcher...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq WhatsApp Watcher"
echo Done.
pause
