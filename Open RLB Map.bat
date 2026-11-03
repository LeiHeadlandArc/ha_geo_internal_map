@echo off
title Geophysics RLB Map - keep this window open
cd /d "%~dp0"

set "PY="
rem 1) QGIS Python first - it has GDAL, needed to read uploaded files
for /d %%D in ("C:\Program Files\QGIS*") do (
    if exist "%%D\bin\python-qgis-ltr.bat" set "PY=%%D\bin\python-qgis-ltr.bat"
    if exist "%%D\bin\python-qgis.bat" set "PY=%%D\bin\python-qgis.bat"
)
if not defined PY if exist "C:\OSGeo4W\bin\python-qgis.bat" set "PY=C:\OSGeo4W\bin\python-qgis.bat"

rem 2) Fall back to normal Python - the map works, file preview will not
if not defined PY (python -c "import sys" >nul 2>nul && set "PY=python")
if not defined PY (py -c "import sys" >nul 2>nul && set "PY=py")

if not defined PY (
    echo Could not find QGIS or Python on this computer.
    echo Please contact the GIS team.
    pause
    exit /b 1
)

call "%PY%" "%~dp0serve.py"
pause