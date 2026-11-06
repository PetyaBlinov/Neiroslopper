"""Build only. Never opens a serial port or writes firmware to hardware."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile


BASE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare():
    manifest = json.loads((BASE / 'build-lock.json').read_text(encoding='utf-8'))
    archive = BASE / 'source.zip'
    if sha(archive) != manifest['source_zip_sha256']:
        raise RuntimeError('Source SHA256 mismatch')
    work = BASE / 'build/source'
    if work.exists():
        raise RuntimeError('Use a fresh build/source directory; do not overwrite build inputs')
    work.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            relative = Path(entry.filename)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Unsafe source path')
            target = work / relative
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(entry))
    module = work / 'FluidNC/src/WebUI/JDDgusBridge.cpp'
    if sha(module) != manifest['dgus_module_sha256']:
        raise RuntimeError('DWIN module differs from reviewed original')
    config = (work / 'platformio_override.ini').read_text(encoding='utf-8')
    for marker in ('+<BTConfig.cpp>', '-DCONFIG_BT_ENABLED', 'i2s_engine_fpermissive.py'):
        if marker not in config:
            raise RuntimeError('Missing combined build setting: ' + marker)
    return work, manifest


def main():
    output = BASE / 'output'
    output.mkdir(exist_ok=True)
    work, manifest = prepare()
    command = [sys.executable, '-m', 'platformio', 'run', '--project-dir', str(work),
               '--environment', 'jd_wifibt', '--jobs', '2']
    with (output / 'build.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace')
        for line in process.stdout:
            print(line, end='', flush=True)
            log.write(line)
        code = process.wait()
    if code:
        raise SystemExit(code)
    products = work / '.pio/build/jd_wifibt'
    firmware = products / 'firmware.bin'
    data = firmware.read_bytes()
    if not 1000000 < len(data) < 0x300000:
        raise RuntimeError('Unexpected candidate firmware size')
    if data[0] != 0xE9 or data[3] >> 4 != 3:
        raise RuntimeError('Expected ESP32 image with 8MB flash header')
    for marker in (b'v4.1.1-JD-DGUS1-BT1', b'Bluetooth/Enable', b'JD-DGUS/1'):
        if marker not in data:
            raise RuntimeError('Missing compiled marker: ' + repr(marker))
    for module in ('BTConfig.cpp.o', 'WebUI/JDDgusBridge.cpp.o'):
        if not (products / 'src' / module).exists():
            raise RuntimeError('Expected module not compiled: ' + module)
    files = {}
    for name in ('firmware.bin', 'bootloader.bin', 'partitions.bin'):
        source = products / name
        dest = output / ('firmware-candidate.bin' if name == 'firmware.bin' else name)
        shutil.copyfile(source, dest)
        files[dest.name] = {'bytes': dest.stat().st_size, 'sha256': sha(dest)}
    shutil.copyfile(BASE / 'source.zip', output / 'source.zip')
    shutil.copyfile(BASE / 'build-lock.json', output / 'build-lock.json')
    shutil.copyfile(BASE / 'BUILD.md', output / 'BUILD.md')
    report = {'candidate': manifest['candidate'], 'files': files,
              'hardware_tested': False, 'automatic_flashing': False,
              'partition_table_requires_device_backup_review': True,
              'source_zip_sha256': manifest['source_zip_sha256']}
    (output / 'build-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write(f'## {manifest["candidate"]}\n\nBuild completed; no hardware tested.\n\n')
            stream.write(f'Firmware: {len(data)} bytes. SHA256 `{files["firmware-candidate.bin"]["sha256"]}`.\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
