"""Create a local Windows launcher with the project's icon; never opens the UI."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != 'win32':
        return
    required = [ROOT / '.venv/Scripts/pythonw.exe', ROOT / 'launch.py', ROOT / 'assets/icon.ico']
    if not all(path.is_file() for path in required):
        raise SystemExit('Install the application environment before creating its shortcut.')
    script = r'''
$appRoot = $env:NEIROSLOPPER_SHORTCUT_ROOT
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $appRoot 'Neiroslopper.lnk'))
$link.TargetPath = Join-Path $appRoot '.venv\Scripts\pythonw.exe'
$link.Arguments = '"' + (Join-Path $appRoot 'launch.py') + '"'
$link.WorkingDirectory = $appRoot
$link.IconLocation = (Join-Path $appRoot 'assets\icon.ico') + ',0'
$link.Description = 'Neiroslopper — Windows-пульт для FluidNC'
$link.WindowStyle = 1
$link.Save()
'''
    env = dict(os.environ, NEIROSLOPPER_SHORTCUT_ROOT=str(ROOT))
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                   env=env, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    print('Created Neiroslopper.lnk')


if __name__ == '__main__':
    main()
