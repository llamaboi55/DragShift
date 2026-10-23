import importlib.util
from pathlib import Path
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
