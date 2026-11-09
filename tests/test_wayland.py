"""Wayland input tracking (no devices needed): run with python -m pytest -q"""
from types import SimpleNamespace

import pytest

evdev = pytest.importorskip("evdev")
from evdev import ecodes  # noqa: E402

from dragshift import platform_wayland  # noqa: E402


def ev(type_, code, value):
    return SimpleNamespace(type=type_, code=code, value=value)


@pytest.fixture
def inp():
    i = platform_wayland._Input.__new__(platform_wayland._Input)   # skip device scanning
    i.pressed = {"/dev/input/event0": set(), "/dev/input/event1": set()}
    i.motion = [0, 0]
    i._abs_last = {}
    import threading
    i._lock = threading.Lock()
    return i


def test_button_and_shift_across_devices(inp):
    inp._event("/dev/input/event0", ev(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))       # mouse
    inp._event("/dev/input/event1", ev(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1))  # keyboard
    assert inp.any_down(ecodes.BTN_LEFT)
    assert inp.any_down(ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT)
    inp._event("/dev/input/event1", ev(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 2))  # autorepeat
    assert inp.any_down(ecodes.KEY_LEFTSHIFT)
    inp._event("/dev/input/event0", ev(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
    assert not inp.any_down(ecodes.BTN_LEFT)


def test_motion_accumulates_travel(inp):
    for dx in (5, -7, 3):
        inp._event("/dev/input/event0", ev(ecodes.EV_REL, ecodes.REL_X, dx))
    inp._event("/dev/input/event0", ev(ecodes.EV_REL, ecodes.REL_Y, -4))
    assert inp.motion == [15, 4]


def test_touchpad_travel_resets_on_lift(inp):
    p = "/dev/input/event0"
    for x in (100, 110, 130):
        inp._event(p, ev(ecodes.EV_ABS, ecodes.ABS_X, x))
    assert inp.motion[0] == 30
    inp._event(p, ev(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0))   # finger lifted
    inp._event(p, ev(ecodes.EV_ABS, ecodes.ABS_X, 900))     # new touch elsewhere: no jump
    assert inp.motion[0] == 30
