@echo off
setlocal
rem Instala el add-in cad2revit para Revit 2027 en la carpeta del usuario actual.
set VER=2027
set DEST=%APPDATA%\Autodesk\Revit\Addins\%VER%

tasklist /FI "IMAGENAME eq Revit.exe" | find /I "Revit.exe" >nul
if not errorlevel 1 (
  echo Cierra Revit antes de instalar y vuelve a ejecutar este archivo.
  pause
  exit /b 1
)

if not exist "%DEST%" mkdir "%DEST%"
xcopy /Y /I /Q "%~dp0Cad2Revit" "%DEST%\Cad2Revit" >nul
copy /Y "%~dp0Cad2Revit.addin" "%DEST%\" >nul

rem Windows marca como "bloqueados" los archivos descargados de internet; se desbloquean.
powershell -NoProfile -Command "Get-ChildItem -Recurse -LiteralPath '%DEST%\Cad2Revit' | Unblock-File; Unblock-File -LiteralPath '%DEST%\Cad2Revit.addin'" >nul 2>&1

rem Otra copia instalada para todos los usuarios: Revit podria cargar esa (antigua) en vez de esta.
if exist "%ProgramData%\Autodesk\Revit\Addins\%VER%\Cad2Revit.addin" (
  echo.
  echo  ATENCION: hay otra copia de cad2revit en
  echo    %ProgramData%\Autodesk\Revit\Addins\%VER%
  echo  Revit podria cargar esa version antigua. Borra de esa carpeta Cad2Revit.addin y la carpeta Cad2Revit.
)

echo.
echo  cad2revit instalado en:
echo    %DEST%
echo.
echo  Abre Revit %VER%. La primera vez Revit preguntara si cargar el complemento:
echo  elige "Cargar siempre". Veras la pestana "cad2revit" con el boton "Construir modelo".
echo.
pause
