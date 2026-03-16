@echo off
setlocal EnableExtensions EnableDelayedExpansion

echo [SETUP] Upgrading pip/setuptools/wheel...
py -3 -m pip install -U pip setuptools wheel || goto :pipfail

echo [SETUP] Installing Python dependencies from requirements.txt...
py -3 -m pip install -r requirements.txt || goto :pipfail

echo [CHECK] Looking for FFmpeg on PATH...
where ffmpeg >NUL 2>&1
if %ERRORLEVEL% EQU 0 goto :ffmpeg_ok

echo [INFO] FFmpeg not found. Trying winget/choco/scoop or direct download...

REM winget
where winget >NUL 2>&1
if %ERRORLEVEL% EQU 0 (
  winget install --id=Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
  where ffmpeg >NUL 2>&1 && goto :ffmpeg_ok
)

REM chocolatey
where choco >NUL 2>&1
if %ERRORLEVEL% EQU 0 (
  choco install -y ffmpeg
  where ffmpeg >NUL 2>&1 && goto :ffmpeg_ok
)

REM scoop
where scoop >NUL 2>&1
if %ERRORLEVEL% EQU 0 (
  scoop install ffmpeg
  where ffmpeg >NUL 2>&1 && goto :ffmpeg_ok
)

REM direct portable download to vendor\ffmpeg
set "VENDOR_DIR=%~dp0vendor\ffmpeg"
set "ZIP=%TEMP%\ffmpeg.zip"
echo [INFO] Downloading portable FFmpeg to %VENDOR_DIR% ...
powershell -NoLogo -NoProfile -Command ^
  "New-Item -ItemType Directory -Force -Path '%VENDOR_DIR%' | Out-Null; ^
   $url='https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'; ^
   Invoke-WebRequest -Uri $url -OutFile '%ZIP%'; ^
   Add-Type -AssemblyName System.IO.Compression.FileSystem; ^
   [IO.Compression.ZipFile]::ExtractToDirectory('%ZIP%', '%VENDOR_DIR%'); ^
   Remove-Item '%ZIP%'; ^
   $sub=(Get-ChildItem -Dir '%VENDOR_DIR%' | Select-Object -First 1).FullName; ^
   $bin=Join-Path $sub 'bin'; ^
   Write-Output $bin" > "%TEMP%\ffmpeg_bin_path.txt"

set /p FFMPEGBIN=<"%TEMP%\ffmpeg_bin_path.txt"
del "%TEMP%\ffmpeg_bin_path.txt" >NUL 2>&1

if exist "%FFMPEGBIN%\ffmpeg.exe" (
  echo [INFO] Using portable FFmpeg at: %FFMPEGBIN%
  set "PATH=%FFMPEGBIN%;%PATH%"
) else (
  echo [WARN] Portable FFmpeg download failed. Please install FFmpeg manually and re-open the terminal.
)

:ffmpeg_ok
echo [CHECK] Verifying FFmpeg...
where ffmpeg >NUL 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo [WARN] FFmpeg still not found. WAV may work, but MP3/unusual WAV may fail.
) else (
  ffmpeg -version | findstr /I "ffmpeg"
  echo [INFO] If you installed FFmpeg, open a NEW terminal so PATH refreshes.
)

echo [DONE] Environment ready.
endlocal & exit /b 0

:pipfail
echo [ERROR] Failed to install Python dependencies. See messages above.
endlocal & exit /b 1
