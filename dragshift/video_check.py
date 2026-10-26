"""Self-test for the actual frozen/installed build, using synthetic media only."""
import subprocess
import tempfile
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

from . import datamosh, video


def selftest():
    try:
        with tempfile.TemporaryDirectory(prefix="dragshift-video-check-") as folder:
            a, b = Path(folder) / "A.mp4", Path(folder) / "B.mp4"
            for p, source in [(a, "testsrc2=size=160x120:rate=24:duration=2"),
                              (b, "testsrc=size=192x108:rate=30:duration=2")]:
                video.capture([video.executable("ffmpeg"), "-v", "error", "-nostdin", "-f", "lavfi", "-i", source,
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-y", str(p)])
            for target in video.TARGETS:
                assert video.convert_video(a, target)[0].stat().st_size > 100
            edited = video.transform(a, start=.25, end=1.75, speed=2, rotation=90, max_side=80)[0]
            info = video.probe(edited)
            assert (info.width, info.height) == (60, 80) and .6 < info.duration < .95
            assert video.snapshot(a, .5)[0].is_file()
            resized = video.transform(a,crop_box=(.125,0,.875,1),output_size=(80,80))[0]
            info = video.probe(resized)
            assert (info.width,info.height)==(80,80) and info.has_audio
            for preset in datamosh.PRESETS:
                out = datamosh.transition(a, b, tail=.5, head=1.5, duration=.5, max_side=160, preset=preset)[0]
                info = video.probe(out)
                assert abs(info.duration - 2) < .15 and info.has_audio
            from .melt_timeline import Timeline, render
            timeline = Timeline()
            for source in (a,b,a):
                timeline.add(source,2.,0.,1.)
            timeline.cut(0).duration, timeline.cut(0).strength = .3,30
            timeline.cut(1).duration, timeline.cut(1).strength = .5,85
            result = render(timeline,max_side=160)[0]
            info = video.probe(result)
            assert abs(info.duration-3.) < .15 and info.has_audio
            from .video_tools import DatamoshWindow
            from .video_geometry import VideoResizeWindow
            windows = [DatamoshWindow([str(a), str(b)], lambda *_: None), VideoResizeWindow([str(a)], lambda *_: None)]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                QApplication.processEvents()
                if windows[0].first_info and windows[0].second_info and windows[1].info:
                    break
                time.sleep(.01)
            assert all(w.primary_btn.isEnabled() for w in windows)
            for w in windows:
                w.close()
            QApplication.processEvents()
        print("VIDEO SELFTEST OK · 8 exports, editing, frame, classic A/B Melt without clean blending, Qt editors", flush=True)
        return 0
    except Exception:
        import traceback
        traceback.print_exc()
        print("VIDEO SELFTEST FAILED", flush=True)
        return 1
