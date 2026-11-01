@echo off
set DEST=%APPDATA%\Autodesk\Revit\Addins\2027
del /Q "%DEST%\Cad2Revit.addin" 2>nul
rmdir /S /Q "%DEST%\Cad2Revit" 2>nul
echo cad2revit desinstalado. Reinicia Revit si estaba abierto.
pause
