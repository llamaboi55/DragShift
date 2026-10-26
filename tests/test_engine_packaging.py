import importlib.util
import json
from pathlib import Path
import re
import pytest


def engine_module():
    path=Path(__file__).resolve().parents[1]/'packaging/media_engines.py'
    spec=importlib.util.spec_from_file_location('media_engines',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_engine_platform_selection():
    module = engine_module()
    assert 'windows-x86_64.zip' in module.ffglitch_url('Windows','AMD64')
    assert 'macos-x86_64.zip' in module.ffglitch_url('Darwin','x86_64')
    assert 'macos-aarch64.zip' in module.ffglitch_url('Darwin','arm64')
    assert 'linux-x86_64.zip' in module.ffglitch_url('Linux','x86_64')
    with pytest.raises(RuntimeError):
        module.ffglitch_url('Windows','arm64')


def test_linux_ffmpeg_checksum_requires_the_exact_asset_and_valid_hash():
    module=engine_module()
    digest='abcdef01'*8
    assert module.checksum_for_asset(digest+'  ffmpeg.tar.xz\n','ffmpeg.tar.xz')==digest
    assert module.checksum_for_asset(digest.upper()+' *ffmpeg.tar.xz\n','ffmpeg.tar.xz')==digest
    for text in (digest+'  another.tar.xz', 'bad  ffmpeg.tar.xz'):
        with pytest.raises(RuntimeError,match='checksum'):
            module.checksum_for_asset(text,'ffmpeg.tar.xz')


def test_windows_sevenzip_ignores_chocolatey_shim(tmp_path, monkeypatch):
    module = engine_module()
    shim = tmp_path / 'chocolatey' / 'bin' / '7z.exe'
    shim.parent.mkdir(parents=True)
    shim.touch()
    real = tmp_path / 'Program Files' / '7-Zip'
    real.mkdir(parents=True)
    for name in ('7z.exe', '7z.dll'):
        (real / name).touch()
    monkeypatch.setenv('ProgramFiles', str(real.parent))
    monkeypatch.setenv('ProgramFiles(x86)', str(tmp_path / 'absent'))
    monkeypatch.setattr(module.shutil, 'which', lambda name: str(shim))
    assert module.windows_sevenzip() == real / '7z.exe'
    (real / '7z.dll').unlink()
    with pytest.raises(RuntimeError, match='7z.dll'):
        module.windows_sevenzip()


def test_imagemagick_linker_paths_include_pkgconfig_system_dirs(monkeypatch):
    module = engine_module()
    monkeypatch.setenv('LDFLAGS', '-Wl,-dead_strip')
    calls = []
    monkeypatch.setattr(module, 'run', lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(module.subprocess, 'check_output',
                        lambda args, **kwargs: '/opt/homebrew/lib\n')
    env = module.imagemagick_env()
    assert calls == [['pkg-config', '--print-errors', '--exists',
                      'lqr-1 >= 0.1.0', 'libpng', 'glib-2.0']]
    assert env['LDFLAGS'] == '-Wl,-dead_strip -L/opt/homebrew/lib'


def test_intel_mac_stages_sdl3_beside_engine_for_dynamic_compat_loader(tmp_path, monkeypatch):
    module = engine_module()
    prefix, dest = tmp_path / 'sdl3', tmp_path / 'bin'
    (prefix / 'lib').mkdir(parents=True)
    dest.mkdir()
    (prefix / 'lib/libSDL3.dylib').write_bytes(b'native SDL3 library')
    (prefix / 'LICENSE.txt').write_text('SDL zlib license', encoding='utf-8')
    calls = []
    def brew(args, **kwargs):
        calls.append(args)
        return str(prefix) + '\n'
    monkeypatch.setattr(module.subprocess, 'check_output', brew)
    module.stage_macos_sdl3('Darwin', 'x86_64', dest, tmp_path, [])
    assert calls == [['brew', '--prefix', 'sdl3']]
    # SDL2-compat searches @executable_path/libSDL3.dylib, not its versioned basename.
    assert (dest / 'libSDL3.dylib').read_bytes() == b'native SDL3 library'
    assert (dest / 'SDL3-LICENSE.txt').read_text(encoding='utf-8') == 'SDL zlib license'


def test_intel_mac_rejects_missing_dynamic_sdl3_before_packaging(tmp_path, monkeypatch):
    module = engine_module()
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: str(tmp_path))
    with pytest.raises(RuntimeError, match='SDL3'):
        module.stage_macos_sdl3('Darwin', 'x86_64', tmp_path, tmp_path, [])


def test_sdl3_notice_is_supplied_when_package_prefix_omits_it(tmp_path, monkeypatch):
    module = engine_module()
    prefix, dest, cache = tmp_path / 'sdl3', tmp_path / 'bin', tmp_path / 'cache'
    (prefix / 'lib').mkdir(parents=True)
    dest.mkdir()
    cache.mkdir()
    (prefix / 'lib/libSDL3.dylib').write_bytes(b'SDL3')
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: str(prefix))
    calls = []
    def notice(url, target, records):
        calls.append(url)
        target.write_text('SDL zlib notice', encoding='utf-8')
        return target
    monkeypatch.setattr(module, 'download', notice)
    module.stage_macos_sdl3('Darwin', 'AMD64', dest, cache, [])
    assert calls == ['https://raw.githubusercontent.com/libsdl-org/SDL/main/LICENSE.txt']
    assert (dest / 'SDL3-LICENSE.txt').read_text(encoding='utf-8') == 'SDL zlib notice'


@pytest.mark.parametrize('system,machine', [('Windows', 'AMD64'), ('Linux', 'x86_64'), ('Darwin', 'arm64')])
def test_sdl3_compat_dependency_leaves_other_platforms_unchanged(tmp_path, monkeypatch, system, machine):
    module = engine_module()
    def unexpected(*args, **kwargs):
        pytest.fail('SDL3 compatibility staging must be Intel Mac only')
    monkeypatch.setattr(module.subprocess, 'check_output', unexpected)
    module.stage_macos_sdl3(system, machine, tmp_path, tmp_path, [])
    assert not list(tmp_path.iterdir())


def test_macos_stages_full_native_ffmpeg_without_relinking():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text(encoding='utf-8')
    macos = workflow.split('  macos:', 1)[1].split('  linux:', 1)[0]
    matrix = next(line for line in macos.splitlines() if 'include: ${{ fromJSON(' in line)
    assert 'inputs.mac_intel_only &&' in matrix
    isolated, normal = [json.loads(value) for value in re.findall(r"'(\[.*?\])'", matrix)]
    expected = [dict(runner='macos-15', arch='apple-silicon', ffmpeg_formula='ffmpeg-full'),
                dict(runner='macos-15-intel', arch='intel', ffmpeg_formula='ffmpeg@7')]
    assert normal == expected
    assert isolated == [expected[1]]
    assert 'brew install "$FFMPEG_FORMULA"' in macos
    assert 'export PATH="$(brew --prefix "$FFMPEG_FORMULA")/bin:$PATH"' in macos
    assert macos.index('export PATH=') < macos.index('python packaging/media_engines.py --stage')
    assert 'brew link' not in macos


@pytest.mark.parametrize('kind,missing', [('encoders', 'libvorbis'), ('encoders', 'libvpx-vp9'),
    ('encoders', 'libmp3lame'), ('decoders', 'libvpx-vp9'), ('filters', 'loudnorm'),
    ('filters', 'acrossfade'), ('filters', 'acrusher')])
def test_required_ffmpeg_capability_gate_rejects_incomplete_build(monkeypatch, kind, missing):
    from dragshift import audio
    module = engine_module()
    required = module.required_capabilities()['ffmpeg']
    def available(requested, name='ffmpeg'):
        result = required[requested]
        return result - {missing} if requested == kind else result
    monkeypatch.setattr(audio, 'engine_capabilities', available)
    with pytest.raises(RuntimeError, match=missing):
        module.check_media_capabilities('ffmpeg')


def test_required_capability_gate_covers_audio_video_and_melt_recipes(monkeypatch):
    from dragshift import audio
    module = engine_module()
    required = module.required_capabilities()
    assert set(audio.AUDIO_ENCODERS.values()) <= required['ffmpeg']['encoders']
    assert {'libx264', 'libvpx-vp9', 'mpeg4', 'gif', 'png'} <= required['ffmpeg']['encoders']
    assert {'amix', 'asetnsamples', 'asplit', 'volume', 'acrusher'} <= required['ffmpeg']['filters']
    assert {'scale', 'fps', 'setsar', 'tpad', 'trim', 'concat', 'pad'} <= required['ffgac']['filters']
    monkeypatch.setattr(audio, 'engine_capabilities', lambda kind, name='ffmpeg': required[name][kind])
    assert module.check_media_capabilities('ffmpeg')
    assert module.check_media_capabilities('ffgac')
