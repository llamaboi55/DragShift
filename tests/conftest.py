"""Keep one Qt application alive throughout the suite, including headless CI."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def qt_session():
    # Destroying and recreating QApplication between UI modules can crash Qt.
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture(scope="session")
def media_engines():
    from dragshift import video
    from dragshift.util import UserError

    try:
        video.executable("ffmpeg")
        video.executable("ffprobe")
    except UserError as error:
        pytest.skip(str(error))


@pytest.fixture(scope="session")
def rotation_options(media_engines):
    """Use the input option when available; older releases need output metadata."""
    import subprocess
    from dragshift import video

    help_text = subprocess.run([video.executable("ffmpeg"), "-hide_banner", "-h", "full"],
        capture_output=True, check=True, creationflags=video.CREATION_FLAGS).stdout
    if b"-display_rotation" in help_text:
        return ["-display_rotation:v:0", "90"], []
    return [], ["-metadata:s:v:0", "rotate=90"]
