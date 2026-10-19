"""Log file location (log_dir / setup_logging) and the --selftest entry point."""
import faulthandler
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dragshift import app


@pytest.fixture
def linux(monkeypatch, tmp_path):
    """Pretend to be Linux with HOME at tmp_path/home, whatever the host is."""
    monkeypatch.setattr(app, "IS_MAC", False)
    monkeypatch.setattr(app, "IS_LINUX", True)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def logging_sandbox(monkeypatch):
    """Let setup_logging run without leaving its excepthook or faulthandler file behind."""
    files = []
    monkeypatch.setattr(faulthandler, "enable", lambda f=None, *a, **k: files.append(f))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    yield files
    for f in files:
        f.close()


def test_log_dir_uses_xdg_state_home(linux, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert app.log_dir() == tmp_path / "state" / "DragShift"


@pytest.mark.parametrize("value", [None, "", "relative/state"])
def test_log_dir_defaults_to_local_state(linux, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_STATE_HOME", value)
    assert app.log_dir() == linux / ".local" / "state" / "DragShift"


def test_log_dir_other_platforms_unchanged(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(app, "IS_LINUX", False)
    monkeypatch.setattr(app, "IS_MAC", True)
    assert app.log_dir() == home / "Library" / "Logs" / "DragShift"
    monkeypatch.setattr(app, "IS_MAC", False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert app.log_dir() == tmp_path / "appdata" / "DragShift"
    monkeypatch.delenv("LOCALAPPDATA")
    assert app.log_dir() == home / ".local" / "share" / "DragShift"


def test_setup_logging_writes_banner_and_rotates(linux, monkeypatch, tmp_path, logging_sandbox):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    base = tmp_path / "state" / "DragShift"
    base.mkdir(parents=True)
    (base / "log.txt").write_bytes(b"x" * 1_000_001)
    path = app.setup_logging()
    assert path == base / "log.txt"
    assert (base / "log.old.txt").stat().st_size == 1_000_001
    assert f"==== DragShift {app.VERSION} started" in path.read_text(encoding="utf-8")
    assert sys.excepthook is not sys.__excepthook__
    assert logging_sandbox, "faulthandler should be pointed at the log file"


def test_unwritable_state_dir_raises_and_main_keeps_going(linux, monkeypatch, tmp_path, logging_sandbox):
    blocker = tmp_path / "state"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    with pytest.raises(OSError):
        app.setup_logging()

    class Reached(Exception):
        pass

    def stop(*a, **k):
        raise Reached
    monkeypatch.setattr(app, "DragShiftQApplication", stop)
    with pytest.raises(Reached):  # setup_logging failed, main() swallowed it and carried on
        app.main(["dragshift", "--selftest"])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux log path")
def test_selftest_offscreen_writes_log_under_xdg_state(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    dirs = {k: tmp_path / k.lower() for k in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                                "XDG_STATE_HOME", "XDG_CACHE_HOME")}
    env.update({k: str(v) for k, v in dirs.items()}, QT_QPA_PLATFORM="offscreen")
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "dragshift", "--selftest"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST OK" in r.stdout
    log = dirs["XDG_STATE_HOME"] / "DragShift" / "log.txt"
    assert "==== DragShift" in log.read_text(encoding="utf-8")
    assert not (dirs["XDG_CONFIG_HOME"] / "DragShift").exists()
    assert not (dirs["HOME"] / ".config" / "DragShift").exists()
    assert not (dirs["HOME"] / ".local" / "share" / "DragShift").exists()
