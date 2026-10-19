"""Test-session defaults so the suite runs anywhere and never touches the real desktop.

- Puts the repo root on sys.path, so a bare `pytest` works as well as `python -m pytest`.
- Uses Qt's offscreen platform unless the caller chose one (no DISPLAY/WAYLAND_DISPLAY needed).
- Points the XDG base directories at a throwaway folder, so QSettings, logs and caches go
  there instead of ~/.config/DragShift, ~/.local/state, ~/.local/share or ~/.cache.

This runs when pytest loads the conftest, before any test module (or Qt) is imported.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_XDG_ROOT = Path(tempfile.mkdtemp(prefix="dragshift-tests-"))
for _var, _sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                   ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
    (_XDG_ROOT / _sub).mkdir()
    os.environ[_var] = str(_XDG_ROOT / _sub)


def pytest_unconfigure(config):
    shutil.rmtree(_XDG_ROOT, ignore_errors=True)
