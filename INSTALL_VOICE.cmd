@echo off
setlocal
cd /d "%~dp0" || exit /b 1
set "PLOTTER_VOICE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not "%~1"=="" if /i not "%~1"=="--check" (
    echo Usage: INSTALL_VOICE.cmd [--check]
    exit /b 2
)
if not exist "%PLOTTER_VOICE_PYTHON%" (
    echo Run INSTALL.cmd first. Voice input uses the same .venv as the application.
    goto failed
)
"%PLOTTER_VOICE_PYTHON%" -X utf8 -c "import struct, sys, sysconfig; sys.exit(0 if sys.platform == 'win32' and struct.calcsize('P') == 8 and (3, 11) <= sys.version_info[:2] <= (3, 14) and not sysconfig.get_config_var('Py_GIL_DISABLED') else 1)"
if errorlevel 1 (
    echo Voice input requires regular Windows x64 Python 3.11 through 3.14.
    echo Python 3.12 is recommended. Free-threaded Python is not supported here.
    goto failed
)
if /i "%~1"=="--check" goto verify
echo Installing optional voice libraries from the Internet into the application .venv.
echo This does not record audio or download speech models.
"%PLOTTER_VOICE_PYTHON%" -m pip install --only-binary=:all: -r "%~dp0requirements-voice.txt"
if errorlevel 1 goto failed
:verify
"%PLOTTER_VOICE_PYTHON%" -m pip check
if errorlevel 1 goto failed
"%PLOTTER_VOICE_PYTHON%" -X utf8 -c "import sounddevice, ctranslate2; from faster_whisper import WhisperModel; assert 'int8' in ctranslate2.get_supported_compute_types('cpu'), 'CPU int8 is unavailable'; print('Voice libraries and CPU int8 are ready.')"
if errorlevel 1 goto failed
echo Restart Neiroslopper and open Voice input in the assistant.
echo The selected speech model is downloaded on the first recognition request.
if /i not "%~1"=="--check" pause
exit /b 0
:failed
echo Voice setup failed. Check Internet access and the Python version in .venv.
echo For DLL load errors, install the Microsoft Visual C++ x64 Redistributable.
if /i not "%~1"=="--check" pause
exit /b 1
