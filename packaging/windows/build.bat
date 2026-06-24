@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0..\.."
setlocal enabledelayedexpansion

echo.
echo   ========================================
echo     Flow^Craft Windows Portable Build
echo          (Embedded Python ^& Extract-and-Run)
echo   ========================================
echo.

set "PYTHON_VERSION=3.12.9"
set "PYTHON_EMBED_ZIP=python-%PYTHON_VERSION%-embed-amd64.zip"
set "PYTHON_DOWNLOAD_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/%PYTHON_EMBED_ZIP%"
set "STAGING_DIR=release\FlowCraft-Portable"
set "RELEASE_DIR=%STAGING_DIR%\FlowCraft"
set "PACKAGE_NAME=FlowCraft-v0.1.2-portable-windows-x64"
set "OUTPUT_ZIP=%~dp0%PACKAGE_NAME%.zip"
set "BUILD_CACHE=%TEMP%\flowcraft-build"

:: Step 1: Clean
echo   [1/7] Cleaning previous build...
if exist "%STAGING_DIR%" rmdir /s /q "%STAGING_DIR%" 2>nul
mkdir "%RELEASE_DIR%" 2>nul
if not exist "%RELEASE_DIR%" (
    echo   [ERROR] Cannot create release directory: "%RELEASE_DIR%"
    pause
    exit /b 1
)

::  Step 2: Download Embedded Python 
set "EMBED_CACHE=%BUILD_CACHE%\%PYTHON_EMBED_ZIP%"
if not exist "%EMBED_CACHE%" (
    echo   [2/7] Downloading Python %PYTHON_VERSION% embeddable...
    if not exist "%BUILD_CACHE%" mkdir "%BUILD_CACHE%"

    :: Try PowerShell with TLS 1.2 enabled
    powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYTHON_DOWNLOAD_URL%' -OutFile '%EMBED_CACHE%'" 2>nul

    if not exist "%EMBED_CACHE%" (
        :: Fallback: curl (Windows 10+ built-in)
        curl -L -o "%EMBED_CACHE%" "%PYTHON_DOWNLOAD_URL%" 2>nul
    )

    if not exist "%EMBED_CACHE%" (
        echo   [ERROR] Failed to download Python embeddable.
        echo   URL: %PYTHON_DOWNLOAD_URL%
        echo   Please download manually and place it at:
        echo   "%EMBED_CACHE%"
        pause
        exit /b 1
    )
) else (
    echo   [2/7] Using cached Python %PYTHON_VERSION% embeddable.
)

::  Step 3: Extract Embedded Python 
echo   [3/7] Extracting Python runtime...
set "PYTHON_RUNTIME_DIR=%RELEASE_DIR%\python"
mkdir "%PYTHON_RUNTIME_DIR%" 2>nul

:: Try tar first (Windows 10+), then PowerShell
tar -xf "%EMBED_CACHE%" -C "%PYTHON_RUNTIME_DIR%" 2>nul
if not exist "%PYTHON_RUNTIME_DIR%\python.exe" (
    powershell -NoProfile -Command "Expand-Archive -Path '%EMBED_CACHE%' -DestinationPath '%PYTHON_RUNTIME_DIR%' -Force" 2>nul
)
if not exist "%PYTHON_RUNTIME_DIR%\python.exe" (
    echo   [ERROR] Failed to extract Python embeddable.
    pause
    exit /b 1
)

::  Step 4: Configure Embedded Python 
echo   [4/7] Configuring embedded Python...
set "PTH_FILE=%PYTHON_RUNTIME_DIR%\python312._pth"
(
echo python312.zip
echo .
echo Lib\site-packages
echo ..\core
echo import site
) > "%PTH_FILE%"
mkdir "%PYTHON_RUNTIME_DIR%\Lib\site-packages" 2>nul

::  Step 5: Bootstrap pip and Install Dependencies 
echo   [5/7] Installing pip and Flow^Craft dependencies...

:: Download get-pip.py (skip ensurepip -- it always fails in embedded Python)
echo   Downloading get-pip.py...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%BUILD_CACHE%\get-pip.py'" 2>nul
if not exist "%BUILD_CACHE%\get-pip.py" (
    curl -L -o "%BUILD_CACHE%\get-pip.py" "https://bootstrap.pypa.io/get-pip.py" 2>nul
)
if not exist "%BUILD_CACHE%\get-pip.py" (
    echo   [ERROR] Failed to download get-pip.py. Check your network.
    pause
    exit /b 1
)

:: Install pip
echo   Installing pip...
"%PYTHON_RUNTIME_DIR%\python.exe" "%BUILD_CACHE%\get-pip.py" --no-warn-script-location
if %errorlevel% neq 0 (
    echo   [ERROR] Failed to install pip.
    pause
    exit /b 1
)

:: Install project dependencies
echo   Installing Python packages...
"%PYTHON_RUNTIME_DIR%\python.exe" -m pip install pydantic fastapi uvicorn httpx chromadb pyyaml --quiet --no-warn-script-location
if %errorlevel% neq 0 (
    echo   [WARNING] Batch install failed. Installing individually...
    "%PYTHON_RUNTIME_DIR%\python.exe" -m pip install pydantic --quiet --no-warn-script-location
    "%PYTHON_RUNTIME_DIR%\python.exe" -m pip install fastapi --quiet --no-warn-script-location
    "%PYTHON_RUNTIME_DIR%\python.exe" -m pip install uvicorn --quiet --no-warn-script-location
    "%PYTHON_RUNTIME_DIR%\python.exe" -m pip install httpx --quiet --no-warn-script-location
    "%PYTHON_RUNTIME_DIR%\python.exe" -m pip install chromadb --quiet --no-warn-script-location
    "%PYTHON_RUNTIME_DIR%\python.exe" -m pip install pyyaml --quiet --no-warn-script-location
)

::  Step 6: Copy Source 
echo   [6/7] Copying source code...
xcopy /s /e /y "core\flowcraft_core" "%RELEASE_DIR%\core\flowcraft_core\" >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Failed to copy core\flowcraft_core. Check that the source exists.
    pause
    exit /b 1
)
copy /y "core\pyproject.toml" "%RELEASE_DIR%\core\" >nul 2>nul
copy /y "README.md" "%RELEASE_DIR%\" >nul 2>nul
copy /y "README_zh.md" "%RELEASE_DIR%\" >nul 2>nul
echo   OK - Source files copied.

::  Step 7: Create Launch Script and Package 
echo   [7/7] Creating launch script and packaging...

set "LAUNCHER=%RELEASE_DIR%\FlowCraft.bat"

:: Generate launch script line-by-line
:: NOTE: Flow^Craft in the generated script means the & is escaped,
:: so at runtime it displays as "FlowCraft" correctly.
> "%LAUNCHER%" (
echo @echo off
echo cd /d "%%~dp0core"
echo.
echo echo   ========================================
echo echo     Flow^^^&Craft v0.1.2
echo echo   ========================================
echo echo.
echo :: Quick pre-check: can Python even run?
echo "%%~dp0python\python.exe" -c "exit(0)" ^>nul 2^>^&1
echo if errorlevel 1 ^(
echo     echo   [ERROR] Python runtime cannot execute.
echo     echo   Please install VC++ Redistributable:
echo     echo   https://aka.ms/vs/17/release/vc_redist.x64.exe
echo     pause
echo     exit /b 1
echo ^)
echo.
echo for /f "tokens=5" %%%%a in ^('netstat -ano ^^^| find ":8765" ^^^| find "LISTENING"'^) do taskkill /f /pid %%%%a ^>nul 2^>nul
echo echo   Starting Flow^^^&Craft server...
echo start "FlowCraft Server" /D "%%~dp0core" cmd /k ""%%~dp0python\python.exe" -m flowcraft_core.simple_server"
echo.
echo echo   Waiting for server to be ready...
echo for /l %%%%i in ^(1,1,30^) do ^(
echo     ping 127.0.0.1 -n 2 ^>nul
echo     netstat -ano ^| find ":8765" ^| find "LISTENING" ^>nul
echo     if not errorlevel 1 goto :ready
echo ^)
echo echo   [ERROR] Server did not start within 30 seconds.
echo echo   Check the server window for error details.
echo pause
echo exit /b 1
echo.
echo :ready
echo echo   Server is ready.
echo start http://127.0.0.1:8765
echo echo   Flow^^^&Craft running at http://127.0.0.1:8765
echo echo   Close this window to stop.
echo pause
)

if not exist "%LAUNCHER%" (
    echo   [ERROR] Failed to create launch script.
    pause
    exit /b 1
)

:: Package into zip (outer wrapper so extract creates FlowCraft\ folder)
echo   Creating zip package...
if exist "%OUTPUT_ZIP%" del "%OUTPUT_ZIP%" 2>nul
powershell -NoProfile -Command "Compress-Archive -Path '%STAGING_DIR%\*' -DestinationPath '%OUTPUT_ZIP%' -Force" 2>nul
if not exist "%OUTPUT_ZIP%" (
    echo   Trying tar fallback...
    tar -a -cf "%OUTPUT_ZIP%" -C "%STAGING_DIR%" . 2>nul
)
if not exist "%OUTPUT_ZIP%" (
    echo   [ERROR] Failed to create zip package.
    pause
    exit /b 1
)

::  Done 
echo.
echo   ========================================
echo     Build Complete!
echo   ========================================
echo.
echo   Package:  "%OUTPUT_ZIP%"
echo.
echo   To test:
echo     1. Extract "%OUTPUT_ZIP%"
echo     2. Open FlowCraft folder, double-click FlowCraft.bat
echo     3. Browser opens at http://127.0.0.1:8765
echo.
echo   No Python installation required!
echo.
pause
