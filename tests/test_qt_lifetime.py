"""The headless test pump must finish Qt's deferred GUI destruction."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_closed_widget_is_deleted_before_next_worker_gc(tmp_path, media_engines):
    # Isolate a native crash from the parent test process. The real scrub test
    # leaves a closed editor with callbacks and a bound-method Python cycle.
    # A subsequent lookup is a deterministic place for worker-side GC.
    root = Path(__file__).resolve().parents[1]
    runner = tmp_path / 'run_gui_gc_child.py'
    runner.write_text(textwrap.dedent(f'''
        import gc
        import sys
        import threading
        import pytest
        from PySide6.QtCore import Qt
        sys.path.insert(0, {str(root)!r})
        from dragshift.video_tools import _Lookup
        from dragshift.window import GlassWindow
        gui_thread = threading.get_ident()
        wrong_thread = []
        initialize = GlassWindow.__init__
        def init(window, *args, **kwargs):
            initialize(window, *args, **kwargs)
            name = type(window).__name__
            def destroyed(*_):
                if threading.get_ident() != gui_thread:
                    wrong_thread.append(name)
            window.destroyed.connect(destroyed, Qt.DirectConnection)
        GlassWindow.__init__ = init
        run_lookup = _Lookup.run
        def run(job):
            gc.collect()
            return run_lookup(job)
        _Lookup.run = run
        code = pytest.main(['-q', '-s', '--basetemp',
            {str(tmp_path / 'child-temp')!r},
            'tests/test_video_geometry.py::test_scrubbing_never_queues_multiple_frame_decoders[edit]',
            'tests/test_video_geometry.py::test_resize_reset_restores_dimensions_after_unlocked_edit'])
        assert not wrong_thread, wrong_thread
        raise SystemExit(code)
    '''), encoding='utf-8')
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen',
               TEMP=str(tmp_path), TMP=str(tmp_path))
    result = subprocess.run([sys.executable, '-X', 'faulthandler', str(runner)],
        capture_output=True, text=True, errors='replace', timeout=30, env=env, cwd=root,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stdout + result.stderr
