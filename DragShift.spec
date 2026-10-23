# PyInstaller spec. ONEFILE=1 -> portable single exe; otherwise a folder for the installer.
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all
from PySide6.QtCore import QLibraryInfo

onefile = os.environ.get("ONEFILE") == "1"
heif_datas, heif_bins, heif_hidden = collect_all("pillow_heif")
engine_names = {"ffmpeg", "ffprobe", "ffgac", "ffedit", "7z", "7zz", "magick"}
engine_datas, engine_bins = [], []
for p in Path("bin").glob("*"):
    if not p.is_file():
        continue
    if p.stem in engine_names and p.suffix.lower() in ("", ".exe") or p.suffix.lower() in (".dll", ".so", ".dylib"):
        # Scan ELF/Mach-O dependencies and relocate them into the frozen bundle.
        # Windows engines are self-contained; retain the already verified layout.
        (engine_datas if sys.platform == "win32" else engine_bins).append((str(p), "bin"))
    elif p.suffix.lower() in (".txt", ".json"):
        engine_datas.append((str(p), "bin"))
qt_russian=Path(QLibraryInfo.path(QLibraryInfo.TranslationsPath))/'qtbase_ru.qm'
if not qt_russian.is_file():
    raise RuntimeError('Qt Russian translation is required for this bilingual build.')
language_datas=[(str(qt_russian),'dragshift/assets')]

a = Analysis(
    ["run.py"],
    datas=[("dragshift/assets", "dragshift/assets"), ("THIRD_PARTY.md", "."), ("LICENSE", ".")] + heif_datas + engine_datas + language_datas,
    binaries=heif_bins + engine_bins,
    hiddenimports=["win32com.client", "PySide6.QtNetwork", "AppKit", "Quartz", "objc"] + heif_hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf", "pandas", "yaml", "PySide6.QtSql", "PySide6.QtTest",
              "PySide6.QtOpenGL", "PySide6.QtSvgWidgets", "PySide6.QtDesigner", "matplotlib", "scipy"],
)

DROP = ("Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6WebEngine", "Qt63D", "Qt6Multimedia", "Qt6Charts",
        "Qt6VirtualKeyboard", "Qt6Designer", "Qt6ShaderTools", "Qt6Sql", "Qt6Test", "opengl32sw",
        "Qt6OpenGL", "Qt6DataVisualization", "Qt6Positioning", "Qt6Sensors", "Qt6SerialPort",
        "Qt6Bluetooth", "Qt6Nfc", "Qt6RemoteObjects", "Qt6Scxml", "Qt6SpatialAudio", "Qt6TextToSpeech",
        "Qt6WebChannel", "Qt6WebSockets", "Qt6Svg", "qtquick", "qml",
        "libgtk-3", "libgdk-3", "libqgtk3", "libatk-")
a.binaries = [b for b in a.binaries if not any(d.lower() in b[0].lower() for d in DROP)]
# Qt on current Windows imports the OS's unversioned ICU API. A Poppler
# installation on PATH can trick the scanner into bundling an unrelated ICU 78
# with suffixed exports, making QtCore fail to load with ERROR_PROC_NOT_FOUND.
if sys.platform == "win32":
    a.binaries = [b for b in a.binaries if Path(b[0]).name.lower() not in ("icuuc.dll", "icudt78.dll")]
a.datas = [d for d in a.datas if "translations" not in d[0].replace("\\", "/")]

pyz = PYZ(a.pure)
IS_MAC = sys.platform == "darwin"
common = dict(name="DragShift", console=os.environ.get("DS_CONSOLE") == "1", upx=False)
IS_LINUX = sys.platform.startswith("linux")
if IS_MAC:
    common.update(icon="dragshift/assets/dragshift.icns")
elif IS_LINUX:
    common.update(name="dragshift")       # icon comes from the .desktop file / hicolor theme
else:
    common.update(icon="dragshift/assets/dragshift.ico", version="installer/version_info.txt")

if IS_MAC:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, argv_emulation=False, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="DragShift", upx=False)
    app = BUNDLE(
        coll,
        name="DragShift.app",
        icon="dragshift/assets/dragshift.icns",
        bundle_identifier="com.dragshift.app",
        info_plist={
            "CFBundleName": "DragShift",
            "CFBundleDisplayName": "DragShift",
            "CFBundleShortVersionString": "1.1.0",
            "CFBundleVersion": "1.1.7",
            "LSUIElement": True,                 # menu-bar app, no Dock icon
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # show up in Finder's "Open With" for any file, and accept drops on the app icon
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Any file",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.item", "public.content", "public.data"],
            }],
        },
    )
elif IS_LINUX:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="dragshift", upx=False)  # no strip: breaks OpenBLAS with Debian 12 binutils
elif onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="DragShift", upx=False)
