"""Exercise bundled engines and radial editor routing in the frozen app."""
import tempfile
import time
import zipfile
import numpy as np
from pathlib import Path

from PySide6.QtWidgets import QApplication

from . import audio, archives, video


def selftest():
    from .video_check import selftest as video_selftest
    if video_selftest() != 0:
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="dragshift-files-check-") as folder:
            # macOS /var aliases and Windows short temp names may resolve to a
            # different spelling when a saved project's sources are reopened.
            base = Path(folder).resolve()
            sound = base / "audio.wav"
            video.capture([video.executable("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=2", "-ar", "44100", "-y", str(sound)])
            for target in audio.TARGETS:
                result = audio.convert_audio(sound, target)[0]
                assert audio.probe(result).duration > 1.9
            edited = audio.transform(sound, target="FLAC", normalize=True, mono=True,
                                     speed=2, fade_in=.1, fade_out=.1)[0]
            info = audio.probe(edited)
            assert .9 < info.duration < 1.1 and info.channels == 1 and info.codec == "flac"
            blended=audio.transition([sound,sound,sound],duration=.25,target='WAV')[0]
            assert abs(audio.probe(blended).duration-5.5)<.02
            from .audio_glitch import export as glitch_audio
            glitched=glitch_audio(sound,strength=60,target='WAV',curve=[(0,0),(1,1)])[0]
            assert abs(audio.probe(glitched).duration-2)<.02
            from PIL import Image
            from . import imaging,convert,telegram,distortion
            gif=base/'animated.gif'
            Image.new('RGBA',(80,60),'red').save(gif,save_all=True,
                append_images=[Image.new('RGBA',(80,60),'blue')],duration=[200,400],loop=0,disposal=2)
            cropped=imaging.crop_image(gif,(5,5,65,45))
            with Image.open(cropped) as image:
                assert image.format=='GIF' and image.n_frames==2 and image.size==(60,40)
            for target in ('PNG', 'JPG'):
                frames_folder = convert.image_to(gif, target)[0]
                assert frames_folder.parent == gif.parent and frames_folder.is_dir()
                frames = sorted(frames_folder.iterdir())
                assert len(frames) == 2
                with Image.open(frames[1]) as image:
                    assert image.getpixel((40,30))[:3][2] > 240
            from . import lenticular as lens
            from .lenticular_tools import LenticularWindow
            first_image, second_image = base/'first.png', base/'second.webp'
            Image.new('RGB',(120,80),'red').save(first_image)
            Image.new('RGB',(80,120),'blue').save(second_image,lossless=True)
            project = lens.Project([lens.Frame(first_image), lens.Frame(second_image)],
                                   lens.Settings(width_mm=25.4, quality=1))
            interlace, geometry = lens.render(project)
            assert interlace.size == (120,80)
            assert interlace.getpixel((0,0)) == (0,0,255) and interlace.getpixel((1,0)) == (255,0,0)
            assert lens.printer_raster(interlace,geometry,(600,600)).size == (600,400)
            project_path = base/'test.dshift-print'
            lens.save_project(project,project_path)
            assert lens.load_project(project_path) == project
            assert len(lens.export(project)) == 2
            from . import lenticular_stream, raster_export
            with lenticular_stream.prepared(project, folder=base) as (streamed, _):
                assert np.array_equal(streamed.read_rows(0, 80), np.asarray(interlace))
                streamed_path = base/'streamed.tif'
                raster_export.write(streamed, streamed_path, 'TIFF', geometry.dpi)
                with Image.open(streamed_path) as image:
                    assert image.size == interlace.size
            animated_video=convert.image_to(gif,'WEBM')[0]
            assert .55<video.probe(animated_video).duration<.65
            emoji=telegram.export_emoji(gif)[0]
            assert telegram.validate_export(emoji).width==100
            distorted=distortion.export(gif,strength=60,target='GIF')[0]
            with Image.open(distorted) as image:
                assert image.n_frames==2 and image.size==(80,60)
            frame=distortion.export(gif,strength=40,target='PNG',at=.3)[0]
            with Image.open(frame) as image:
                assert image.getpixel((40,30))[:3]==(0,0,255)
            source = base / "archive.zip"
            with zipfile.ZipFile(source, "w") as z:
                z.writestr("folder/hello.txt", "DragShift test")
            for target in archives.TARGETS:
                result = archives.convert_archive(source, target)[0]
                extracted = archives.extract(result)[0]
                assert (extracted / "folder" / "hello.txt").read_text() == "DragShift test"
            selected_folder = base / "selected.v1"
            (selected_folder / "empty").mkdir(parents=True)
            (selected_folder / "hello.txt").write_bytes(b"Pack check")
            for target in ("ZIP", "7Z"):
                packed = archives.pack([selected_folder, sound], target)[0]
                unpacked = archives.extract(packed)[0]
                assert (unpacked / "selected.v1" / "hello.txt").read_bytes() == b"Pack check"
                assert (unpacked / "selected.v1" / "empty").is_dir()
                assert (unpacked / sound.name).read_bytes() == sound.read_bytes()
            from .app import DragShiftApp
            from .audio_tools import AudioToolWindow
            from .archive_tools import ArchiveContentsWindow, ArchivePackWindow
            from .radial import RadialMenu
            menu = RadialMenu(lambda paths: DragShiftApp.build_rings(None, paths), lambda paths: "1 audio file")
            menu._load([str(sound)])
            assert "FLAC" in {item.label for item in menu.items}
            menu.next_ring()
            assert "Normalize" in {item.label for item in menu.items}
            assert "Pack" in {item.label for item in menu.items}
            before = menu.ring_idx
            menu._set_hover(0)
            menu._set_hover(-2)
            until = time.monotonic() + .65
            while time.monotonic() < until:
                QApplication.processEvents()
                time.sleep(.01)
            assert menu.ring_idx == before
            assert not hasattr(DragShiftApp, "open_converter")
            windows = [AudioToolWindow([str(sound)], lambda *_: None),
                       ArchiveContentsWindow(str(source), lambda *_: None),
                       ArchivePackWindow([selected_folder, sound], lambda *_: None),
                       LenticularWindow([project_path], lambda *_: None)]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                QApplication.processEvents()
                if windows[0].info is not None and windows[1].table.rowCount() and windows[3].primary_btn.isEnabled():
                    break
                time.sleep(.01)
            assert windows[0].primary_btn.isEnabled() and windows[1].table.rowCount() == 1
            assert windows[2].primary_btn.isEnabled() and windows[2].target.currentText() == "ZIP"
            assert windows[3].primary_btn.isEnabled() and len(windows[3].project.frames) == 2
            for window in windows:
                window.close()
            menu.close()
            QApplication.processEvents()
        print("FILES SELFTEST OK · distortion, audio glitch/transitions, GIF animation/crop/all frames, Telegram emoji, archives, ZIP/7Z, deliberate ring input, pixel resize/crop, Melt series, lenticular project/export/printer raster/editor", flush=True)
        return 0
    except Exception:
        import traceback
        traceback.print_exc()
        print("FILES SELFTEST FAILED", flush=True)
        return 1
