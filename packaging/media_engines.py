"""Stage native engines for release CI; no downloads happen in the application.

Unix prerequisites: ffmpeg, 7zz/7z, C/C++ compiler, make, pkg-config,
libpng and liblqr development packages. ImageMagick's coders are compiled in,
so a frozen app cannot accidentally depend on a system coder-module folder.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IM_VERSION = '7.1.2-34'
IM_WINDOWS_SHA256 = '52cc6a441c7884f5ff4274b410c9b90c4b7d54245dcdf4d32988bb4e4df4e5d1'


def ffglitch_url(system, machine):
    arm = machine.lower() in ('arm64', 'aarch64')
    if system == 'Darwin':
        path = 'macos-aarch64/ffglitch-0.10.2-macos-aarch64.zip' if arm else 'mac64/ffglitch-0.10.2-macos-x86_64.zip'
    elif system == 'Windows' and not arm:
        path = 'win64/ffglitch-0.10.2-windows-x86_64.zip'
    elif system == 'Linux' and not arm:
        path = 'linux64/ffglitch-0.10.2-linux-x86_64.zip'
    else:
        raise RuntimeError(f'No configured release engine for {system}/{machine}')
    return 'https://ffglitch.org/pub/bin/' + path


def run(args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def download(url, dest, records, expected=None):
    if not dest.exists():
        with urllib.request.urlopen(url, timeout=120) as source, dest.open('wb') as target:
            shutil.copyfileobj(source, target)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if expected and digest != expected:
        raise RuntimeError(f'Checksum mismatch: {dest.name}')
    records.append(dict(url=url, sha256=digest))
    return dest


def find_copy(folder, name, dest):
    matches = [p for p in folder.rglob(name) if p.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f'Expected one {name} in {folder}, found {len(matches)}')
    shutil.copy2(matches[0], dest / name)
    (dest / name).chmod(0o755)


def windows_sevenzip():
    # Chocolatey's PATH entry can be a shim without the adjacent format DLL.
    candidates = [Path(os.environ.get(key, default)) / '7-Zip' / '7z.exe'
                  for key, default in (('ProgramFiles', r'C:\Program Files'),
                                       ('ProgramFiles(x86)', r'C:\Program Files (x86)'))]
    found = shutil.which('7z')
    if found:
        candidates.append(Path(found))
    for executable in candidates:
        if executable.is_file() and executable.with_name('7z.dll').is_file():
            return executable
    raise RuntimeError('Install 7-Zip with both 7z.exe and 7z.dll before staging')


def checksum_for_asset(text, name):
    for line in text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip('*') == name:
            if re.fullmatch(r'[a-fA-F0-9]{64}', parts[0]):
                return parts[0].lower()
    raise RuntimeError(f'Missing published checksum for {name}')


def stage_linux_ffmpeg(dest, cache, records):
    # Debian 12's FFmpeg 5 can shorten the last retimed frame. Ship the current
    # stable full static build, independently of the host distribution's codec set.
    name = 'ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz'
    base = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/'
    sums = download(base + 'checksums.sha256', cache / 'ffmpeg-linux.sha256', records)
    expected = checksum_for_asset(sums.read_text(encoding='utf-8'), name)
    package = download(base + name, cache / name, records, expected)
    folder = cache / 'ffmpeg-linux'
    with tarfile.open(package) as archive:
        archive.extractall(folder, filter='data')
    for binary in ('ffmpeg', 'ffprobe'):
        find_copy(folder, binary, dest)
    notices = [p for p in folder.rglob('*') if p.is_file()
               and (p.name.startswith(('LICENSE', 'COPYING')) or p.name == 'README.txt')]
    if not notices:
        raise RuntimeError('FFmpeg archive has no license notices')
    for notice in notices:
        shutil.copy2(notice, dest / ('FFmpeg-' + notice.name))


def imagemagick_env():
    packages = ('lqr-1', 'libpng', 'glib-2.0')
    run(['pkg-config', '--print-errors', '--exists', 'lqr-1 >= 0.1.0', *packages[1:]])
    # pkgconf may omit Homebrew's "system" -L directory, although Apple ld
    # does not search it. Preserve explicit delegate paths for the final link.
    folders = dict.fromkeys(subprocess.check_output(
        ['pkg-config', '--variable=libdir', package], text=True).strip()
        for package in packages)
    env = os.environ.copy()
    env['LDFLAGS'] = ' '.join(filter(None, [env.get('LDFLAGS', ''),
        *['-L' + shlex.quote(folder) for folder in folders if folder]]))
    return env


def stage_macos_sdl3(system, machine, dest, cache, records):
    if system != 'Darwin' or machine.lower() not in ('x86_64', 'amd64'):
        return
    # Homebrew's SDL2 compatibility layer dlopens SDL3: it is absent from
    # Mach-O dependency tables, so PyInstaller cannot collect it automatically.
    # The loader explicitly checks @executable_path/libSDL3.dylib.
    prefix = Path(subprocess.check_output(['brew', '--prefix', 'sdl3'], text=True).strip())
    library = prefix / 'lib/libSDL3.dylib'
    if not library.is_file():
        raise RuntimeError('Intel Mac packaging requires the native SDL3 library (brew install sdl3)')
    shutil.copy2(library, dest / 'libSDL3.dylib')
    (dest / 'libSDL3.dylib').chmod(0o755)
    notice = next((path for path in (prefix / 'LICENSE.txt',
        prefix / 'share/licenses/SDL3/LICENSE.txt') if path.is_file()), None)
    if notice is None:
        notice = download('https://raw.githubusercontent.com/libsdl-org/SDL/main/LICENSE.txt',
                          cache / 'SDL3-LICENSE.txt', records)
    shutil.copy2(notice, dest / 'SDL3-LICENSE.txt')


def stage():
    system, machine = platform.system(), platform.machine()
    suffix = '.exe' if system == 'Windows' else ''
    dest = ROOT / 'bin'
    cache = ROOT / 'build' / 'media-engines'
    dest.mkdir(exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    records = []
    ffzip = download(ffglitch_url(system, machine), cache / f'ffglitch-{system}-{machine}.zip', records)
    unpack = cache / 'ffglitch'
    with zipfile.ZipFile(ffzip) as archive:
        archive.extractall(unpack)
    for name in ('ffgac', 'ffedit'):
        find_copy(unpack, name + suffix, dest)
    readmes = list(unpack.rglob('*README*')) + list(unpack.rglob('*readme*'))
    if readmes:
        shutil.copy2(readmes[0], dest / 'FFglitch-readme.txt')
    if system == 'Windows':
        z = windows_sevenzip()
        for name in ('7z.exe', '7z.dll'):
            shutil.copy2(Path(z).parent / name, dest / name)
        ffmpeg = download('https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip', cache / 'ffmpeg.zip', records)
        with zipfile.ZipFile(ffmpeg) as archive:
            archive.extractall(cache / 'ffmpeg')
        for name in ('ffmpeg.exe', 'ffprobe.exe'):
            find_copy(cache / 'ffmpeg', name, dest)
        licenses = list((cache / 'ffmpeg').rglob('LICENSE*'))
        if licenses:
            shutil.copy2(licenses[0], dest / 'FFmpeg-LICENSE.txt')
        im = download(f'https://github.com/ImageMagick/ImageMagick/releases/download/{IM_VERSION}/ImageMagick-{IM_VERSION}-portable-Q16-x64.7z', cache / 'imagemagick.7z', records, IM_WINDOWS_SHA256)
        run([z, 'x', '-y', '-o' + str(cache / 'imagemagick'), im])
        find_copy(cache / 'imagemagick', 'magick.exe', dest)
        for name in ('LICENSE', 'NOTICE'):
            files = list((cache / 'imagemagick').rglob(name))
            if files:
                shutil.copy2(files[0], dest / ('ImageMagick-' + name + '.txt'))
    else:
        if system == 'Linux':
            stage_linux_ffmpeg(dest, cache, records)
        else:
            for name in ('ffmpeg', 'ffprobe'):
                source = shutil.which(name)
                if not source:
                    raise RuntimeError(f'Install native {name} before staging')
                shutil.copy2(source, dest / name)
        z = shutil.which('7zz') or shutil.which('7z')
        if not z:
            raise RuntimeError('Install native 7-Zip before staging')
        shutil.copy2(z, dest / Path(z).name)
        # Debian's 7z wrapper needs its real program and format plug-ins beside it.
        if system == 'Linux' and Path('/usr/lib/p7zip/7z').is_file():
            shutil.copy2('/usr/lib/p7zip/7z', dest / '7z')
            for library in Path('/usr/lib/p7zip').glob('*.so'):
                shutil.copy2(library, dest / library.name)
        source = download(f'https://github.com/ImageMagick/ImageMagick/archive/refs/tags/{IM_VERSION}.tar.gz', cache / 'imagemagick.tar.gz', records)
        with tarfile.open(source) as archive:
            archive.extractall(cache, filter='data')
        folder = cache / ('ImageMagick-' + IM_VERSION)
        prefix = cache / 'imagemagick-install'
        disabled = ('bzlib', 'djvu', 'fftw', 'fontconfig', 'freetype', 'heic', 'jbig', 'jpeg', 'jxl', 'lcms',
                    'lzma', 'openexr', 'openjp2', 'pango', 'raw', 'rsvg', 'tiff', 'webp', 'wmf', 'xml', 'zstd')
        options = ['--disable-shared', '--enable-static', '--without-modules', '--disable-installed',
                   '--disable-hdri', '--with-quantum-depth=16', '--disable-openmp', '--without-magick-plus-plus',
                   '--without-perl', '--without-x', '--with-lqr=yes', '--with-png=yes', '--with-zlib=yes']
        build_env = imagemagick_env()
        run(['./configure', '--prefix=' + str(prefix), *options, *['--without-' + n for n in disabled]], cwd=folder, env=build_env)
        run(['make', '-j' + str(min(4, os.cpu_count() or 2))], cwd=folder, env=build_env)
        run(['make', 'install'], cwd=folder, env=build_env)
        shutil.copy2(prefix / 'bin' / 'magick', dest / 'magick')
        for name in ('LICENSE', 'NOTICE'):
            shutil.copy2(folder / name, dest / ('ImageMagick-' + name + '.txt'))
    stage_macos_sdl3(system, machine, dest, cache, records)
    (dest / 'EngineVersions.json').write_text(json.dumps(dict(system=system, machine=machine, downloads=records), indent=2) + '\n', encoding='utf-8')


def required_capabilities():
    """Features explicitly used by the shipped media recipes, not optional codecs."""
    from dragshift.audio import AUDIO_ENCODERS
    return {
        'ffmpeg': {
            'encoders': frozenset(AUDIO_ENCODERS.values()) | {
                'libx264', 'libvpx-vp9', 'mpeg4', 'gif', 'png', 'pcm_f32le'},
            # The libvpx decoder, rather than native vp9, preserves WebM alpha.
            'decoders': frozenset({'libvpx-vp9'}),
            'filters': frozenset({'acrossfade', 'acrusher', 'afade', 'aformat', 'amix', 'anullsrc',
                'apad', 'aresample', 'areverse', 'asetnsamples', 'asetpts', 'asplit', 'atempo',
                'atrim', 'concat', 'crop', 'eq', 'format', 'fps', 'hflip', 'loudnorm', 'pad',
                'palettegen', 'paletteuse', 'scale', 'setpts', 'setsar', 'tpad', 'transpose',
                'trim', 'vflip', 'volume'}),
        },
        'ffgac': {
            'encoders': frozenset({'mpeg4'}),
            'filters': frozenset({'concat', 'format', 'fps', 'pad', 'scale', 'setpts',
                                  'setsar', 'tpad', 'trim'}),
        },
    }


def check_media_capabilities(name):
    from dragshift.audio import engine_capabilities
    required = required_capabilities()[name]
    for kind, expected in required.items():
        missing = expected - engine_capabilities(kind, name)
        if missing:
            hint = ' Stage a full FFmpeg build; macOS needs the selected keg bin first on PATH.' if name == 'ffmpeg' else ''
            raise RuntimeError(f'{name} is missing required {kind}: {", ".join(sorted(missing))}.' + hint)
    return {kind: sorted(names) for kind, names in required.items()}


def check():
    sys.path.insert(0, str(ROOT))
    from dragshift import video, archives
    versions = {}
    for name in ('ffmpeg', 'ffprobe', 'ffgac', 'ffedit', 'magick'):
        path = video.executable(name)
        value = video.capture([path, '-version']).decode('utf-8', 'replace')
        if name == 'magick' and (' lqr ' not in value or 'png' not in value.lower()):
            raise RuntimeError('ImageMagick requires PNG and Liquid Rescale support:\n' + value)
        versions[name] = value.splitlines()[0]
    # A valid -version can still be a stripped build (e.g. Homebrew ffmpeg
    # without libvorbis). Fail during staging, before editor tests or packaging.
    for name in ('ffmpeg', 'ffgac'):
        versions[name + '_required_features'] = check_media_capabilities(name)
    video.require_precise_timing()
    video.capture([archives.executable(), 'i'])
    print(json.dumps(versions, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', action='store_true')
    args = parser.parse_args()
    if args.stage:
        stage()
    check()
