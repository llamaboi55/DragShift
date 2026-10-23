"""Launching DragShift must keep the original Shift-drag workflow."""
import json
import uuid

import pytest
from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from dragshift import app as application


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture
def controller(qapp, monkeypatch):
    monkeypatch.setattr(application, "IPC_NAME", "DragShift-test-" + uuid.uuid4().hex)
    monkeypatch.setattr(application.DragShiftWatcher, "start", lambda self: None)
    monkeypatch.setattr(application.QSystemTrayIcon, "show", lambda self: None)
    instance = application.DragShiftApp(qapp)
    yield instance
    instance.server.close()
    instance.tray.hide()
    instance.menu.close_menu()
    for window in list(instance.windows):
        window.close()
    instance.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("quiet", [False, True])
def test_launch_without_files_never_opens_a_window(controller, monkeypatch, quiet):
    notices = []
    monkeypatch.setattr(application.QTimer, "singleShot", lambda delay, fn: fn())
    monkeypatch.setattr(controller, "show_ready", lambda: notices.append("ready"), raising=False)
    monkeypatch.setattr(controller, "show_welcome", lambda: pytest.fail("automatic onboarding window"))
    controller.startup([], quiet=quiet)
    assert controller.windows == []
    assert notices == ([] if quiet else ["ready"])


def test_launch_with_files_still_routes_to_radial(controller, monkeypatch):
    calls = []
    monkeypatch.setattr(application.QTimer, "singleShot", lambda delay, fn: fn())
    monkeypatch.setattr(controller, "queue_files", lambda paths: calls.append(paths))
    controller.startup(["clip.mp4"], quiet=False)
    assert calls == [["clip.mp4"]] and controller.windows == []


def test_tray_only_opens_the_control_menu(controller, monkeypatch):
    labels = [action.text() for action in controller.tray_menu.actions()]
    assert "Settings…" in labels
    assert not any("file" in label.lower() or "converter" in label.lower() for label in labels)
    calls = []
    monkeypatch.setattr(controller.tray_menu, "popup", lambda pos: calls.append(pos))
    controller._tray_click(QSystemTrayIcon.Trigger)
    assert len(calls) == 1 and controller.windows == []


def test_second_launch_only_shows_a_short_hint(controller, monkeypatch):
    class Socket(QObject):
        readyRead = Signal()
        disconnected = Signal()
        def readAll(self):
            return json.dumps({"show": True}).encode()
        def disconnectFromServer(self):
            self.disconnected.emit()
    socket = Socket(controller)
    callbacks, notices = [], []
    monkeypatch.setattr(controller.server, "nextPendingConnection", lambda: socket)
    monkeypatch.setattr(application.QTimer, "singleShot", lambda delay, fn: callbacks.append(fn))
    monkeypatch.setattr(controller, "show_ready", lambda: notices.append("ready"), raising=False)
    monkeypatch.setattr(controller, "open_converter", lambda: pytest.fail("unexpected file queue"), raising=False)
    controller._ipc()
    socket.readyRead.emit()
    assert notices == ["ready"] and controller.windows == []


def test_shift_drag_still_opens_the_ring(controller, monkeypatch):
    positions = []
    monkeypatch.setattr(controller.menu, "popup", lambda pos: positions.append(pos))
    controller.watcher.show_menu.emit(QPoint(320, 240))
    assert positions == [QPoint(320, 240)]
    for name, expected in [("clip.mp4", "Datamosh"), ("track.wav", "Normalize"), ("photo.png", "Crop")]:
        ring = controller.build_rings([name])
        assert expected in {item.label for item in ring["Tools"]}
