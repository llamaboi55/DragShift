# DragShift — local file tools

## Custom build 1.1.0+files.7

This local extension is based on [llamaboi55/DragShift](https://github.com/llamaboi55/DragShift), upstream commit `88abe0cf392b80ad2db805ef12b020c76a6cf458` retrieved on 2026-10-19. It is not an official upstream release. The custom Windows package adds video, audio, experimental Datamosh and archive workflows to the original Shift-drag radial menu, with neutral matte glass.

The supplied deliverables are `DragShift-Files-Setup.exe`, `DragShift-Files-Source.zip` and the Russian `DragShift-Руководство.md`. Upstream GitHub releases do not contain these local additions. The delivered installer targets Windows x64. The same tools and translation catalog are included in macOS/Linux builds; native engine staging and bilingual frozen media checks are configured in CI. Those native builds still require a successful platform CI run.

The Windows installer offers **Русский / English**. The selected language is saved for the application, including radial menus, tools, settings and standard Qt dialogs. macOS/Linux follow the system language initially. Change it in **Settings → Language**, then restart; `DRAGSHIFT_LANGUAGE=ru` or `en` overrides it for a launch.

## Start and convert

Launch DragShift once; it stays in the system tray. There is no standalone file queue or upload window.

1. Begin dragging a file in Explorer or on the desktop.
2. Press **Shift**, then drop it on a menu sector.
3. Tap **Shift** again while dragging to switch between **Convert** and **Tools**.

Choose a format in **Convert**, or an operation in **Tools**. Editors open only after choosing a tool. Several files of the same type can be dragged together. The tray icon opens its control menu, with **Settings…**, **How to use** and **Quit DragShift**. The per-user installer can add Explorer's **DragShift…** and **Send to → DragShift** entries, which also use the radial menu. Ctrl or Alt can replace Shift in Settings.

For nonadjacent files in a Windows Explorer folder, select them with Ctrl, release Ctrl, then **tap and release Shift without clicking**. A click radial menu opens for that exact selection. Holding Shift while clicking a file retains Explorer's normal range-selection behavior. The tap shortcut requires at least two items in an Explorer folder and a Shift trigger; it does not read desktop selection and refuses ambiguous tabbed views. Normal file dragging works on every supported platform and uses the final dropped file list.

## Supported workflows

| Input | Output formats | Tools |
|---|---|---|
| Images: PNG, JPG/JPEG, WebP, HEIC/HEIF, AVIF, TIFF, BMP, GIF, ICO | PNG, JPG, WebP, HEIC, AVIF, TIFF, BMP, GIF, ICO, PDF | Compress, resize, crop, colour/light edits, annotate, redact, add background, view/remove metadata, Liquid Rescale distortion |
| Video: MP4, MKV, MOV, AVI, WebM, M4V, WMV, FLV, TS/MTS/M2TS, MPEG/MPG, 3GP, OGV, VOB | MP4, MKV, MOV, WebM, AVI, animated GIF, MP3, WAV | Trim, compress, resize, crop/rotate/mirror, speed, colour, mute, PNG frame, metadata, Datamosh, Liquid Rescale distortion, Audio extraction |
| Audio: MP3, WAV, FLAC, M4A/M4B, AAC, OGG/OGA, Opus, AIFF/AIF, WMA, APE, ALAC, AMR | MP3, WAV, FLAC, M4A, AAC, OGG, Opus, AIFF | Trim, edit, normalize, fade, compress, reverse, metadata |
| Archives: ZIP, 7Z, RAR, TAR | ZIP, 7Z, TAR | Contents, extract into a new folder |
| PDF | JPG/PNG per page, DOCX, TXT | Compress, split, rotate, merge, password protection, extract images, metadata |
| Word DOCX | PDF, TXT | Conversion |
| Text: TXT, MD, CSV, LOG, JSON, XML, HTML/HTM | PDF, DOCX | Plain-text conversion |

Available decoders and encoders determine whether a particular file is readable. Unsupported extensions have no conversion actions in the radial menu. This is not a general Office, spreadsheet, slide, OCR or 3D converter. Text formats are imported as text, without evaluating HTML, Markdown or spreadsheet formulas. Animated GIF editing preserves composited frames, frame durations, transparency and loop metadata. Animation-capable image outputs (GIF, WebP, PNG/APNG, TIFF, AVIF) retain the sequence; static formats such as JPG use its first frame. GIF also offers MP4 and WebM conversion with its original animation, without a generated camera effect.

GIF-to-video conversion chooses a frame grid compatible with its centisecond delays (at least 30 fps, up to 100 fps) instead of dropping very short source frames. Telegram's separate profile is capped at 30 fps by its specification, so faster animations necessarily lose temporal detail there.

Numeric slider values are editable with the keyboard; Enter confirms the value without exporting. Open tools accept local-file drops: source editors replace the current input, while Melt/audio sequence lists append files. Drop onto either Melt preview to replace that clip. Sequence rows can be reordered by dragging or with the up/down buttons. Pack accepts additional files and folders.

DOCX → PDF uses Microsoft Word when available, otherwise a basic local renderer. PDF → DOCX rebuilds paragraphs and images. Complex layouts, fonts, tables, headers, charts and scanned pages may not reproduce accurately; scanned text is not recognized with OCR.

## Video and Datamosh

MP4/MKV/MOV use H.264 and AAC, WebM uses VP9 and Opus, and AVI uses MPEG-4 video. GIF exports from video use 12 fps and an edge up to 640 px. Video edits save a new MP4. Speed ranges from 0.25× to 4× and preserves audio pitch. The frame preview updates with rotation, centre crop, mirror and colour settings using the same filters as export. Trim has two range handles synchronized with its start/end numeric fields; the frame slider also allows scrubbing.

**Tools → Resize** opens a crop frame with draggable edges/corners, numeric Left/Right/Top/Bottom amounts, output Width/Height in pixels and Keep proportions. Scrub to inspect another frame. Crop coordinates follow the displayed orientation and square-pixel geometry; export uses the same crop, preserves audio and writes a new MP4. Output dimensions are rounded to even pixels, 16–7680. In a batch the same relative crop and output size apply to every video.

**Tools → Audio** extracts the first audio track from a video into any supported audio output format. **Mute** saves an MP4 without audio; **Frame** saves a full-resolution PNG.

For a classic Melt transition or an ordered video series:

1. Open **Tools → Datamosh** on a video, or select several together. Add/drop clips and set their order (2–16 clips).
2. With two clips, select excerpts using **Last seconds of A** / **First seconds of B**, or uncheck the excerpt option to include both full clips. Series of three or more include every clip once in full.
3. Set transition duration, intensity, longest edge, fps and whether to retain audio. Leave at least two clean frames after the transition in each following clip. **Melt** is the only effect.
4. Choose a simple strength shape: **Even throughout**, **Gradually stronger**, **Gradually weaker**, or **Burst in the middle**. The intensity slider controls the maximum strength. Drawing is optional: **Custom points…** reveals the curve; click to add a point, drag it or enter its time in seconds and strength. Right-click/Delete removes interior points. Zero is clean video; 100% uses the selected intensity. Markers show clip changes.
5. **Render preview** exports the same timeline and curve at 480 px. Open it from the result notification. **Create Melt video** saves the selected full-resolution result as MP4.

Each bridge uses FFglitch motion-vector editing and I-frame removal, then recovers clean footage. A final FFmpeg pass produces a playable H.264 MP4. Removed frames are compensated at their original timeline positions so later cuts and sound stay aligned. Audio segments are joined; silence fills missing tracks. Datamosh is experimental: motion-rich clips tend to produce stronger artifacts, and source material affects every result.

### Liquid Rescale distortion

Choose **Tools → Distort** on a GIF, video or still image. The engine uses ImageMagick with liblqr: content-aware shrink followed by ordinary resize back to the processing canvas, as documented by [OpenDistortBot](https://github.com/rupansh/OpenDistortBot), which credits the author of @DistortBot. No bot source code was copied. The default intensity **40** corresponds to its documented **60×60%** shrink. Zero is unchanged; 95 is the strongest setting. A synthetic frame is pixel-identical to the reference ImageMagick command; the current hosted bot's version/settings are not public, so exact equivalence with that service is not established.

Adjust intensity by slider or keyboard, scrub to a frame, compare the original, and select the output size (longest edge 64–1920 px, default 640, no upscale). Preview and export use the same processing canvas and effect. Choose **PNG** for the selected frame, or **GIF / MP4 / WebM** for the selected segment using numeric in/out fields or range handles. GIF input frames retain their delays and loop count; video-to-GIF delays are rounded cumulatively to its centisecond timebase. WebM and PNG retain alpha, GIF retains binary transparency, and MP4 uses opaque video. Video audio is retained in MP4/WebM. Each frame is distorted independently, so animated distortion can flicker as its content changes.

Drop media into the tool to replace its source. Batch settings use each file's full duration when the range is unchanged, otherwise the same time range is applied to every source. Processing is local and cancellable, uses temporary decoded PNG frames, checks disk space, and publishes unique results only on success. The limit is 15,000 frames (GIF rendering also has a 200-million-pixel memory limit); shorten a long segment or lower its output size. The Windows installer includes the engine; source-only builds need ImageMagick 7 with the **lqr** delegate.

Independent frame jobs run in up to four bounded workers with lossless intermediate PNG compression. Cancellation stops and joins active engines before removing temporary frames. Preview reuses its decoded source when only strength changes. On this Windows PC, a synthetic 16-frame 640×480 test at strength 60 took 3.282 s with the old batch engine versus 1.216 s with production workers (2.70×), with identical decoded RGBA pixels. This measures the distortion stage; total export speed also depends on decoding, encoding, size and CPU.

### Telegram custom emoji

For GIF or video, choose **Convert → TG Emoji**. Select a segment up to three seconds using the range handles or numeric fields, then choose full-frame fit with transparent padding or a square centre crop. The export uses **WebM / VP9, exactly 100×100, no audio stream, at most 30 fps and 256,000 bytes**. Encoding retries at smaller quality when necessary and verifies the resulting streams, dimensions, duration, fps and size before publishing the file. The segment is preserved as animation; Telegram recommends choosing a seamless loop. [Official requirements](https://core.telegram.org/stickers/webm-vp9-encoding).

## Audio

**Edit** combines trimming, volume in dB, pitch-preserving tempo, reverse, mono, fades, normalization, sample rate and output format. Edits save directly to **Save as**, so WAV or FLAC can be chosen without an intermediate AAC encode. The initial format follows the input when supported; otherwise it is M4A. **Compress** starts with MP3 at 128 kb/s.

**Tools → Transition** blends an ordered series of 2–16 audio files with overlapping linear crossfades. The same tool is available from **Datamosh → Audio file transitions…**. Drop files, reorder them, set overlap duration and output format. Middle clips need room for both incoming and outgoing fades. Export duration equals the sum of clip lengths minus one overlap per cut; the result is stereo at 48 kHz (with encoder-compatible rate selection).

For digital crunch, choose **Datamosh → Audio · Glitch** in the same dialog as video Melt. Drop an audio file or a video with audio, select a segment, intensity and simple time shape, then preview the first ten seconds or save the audio. FFmpeg's `acrusher` reduces bit depth and sample rate, mixed with the original signal according to the selected shape. Zero is the dry signal; this is a separate operation from transitions between audio files. Video input exports its first audio track as an audio file.

Normalization measures the processed signal, then applies FFmpeg's two-pass EBU R128 `loudnorm` at −16 LUFS, −1.5 dBTP and LRA 11. Fades are included in the measurement; a volume gain follows normalization, so a positive gain can exceed that normalization peak target. Silence or material shorter than the measurement gate is exported without invalid infinite measurements.

MP3 supports up to 320 kb/s. OGG uses Vorbis VBR quality 5. WAV/AIFF use 16-bit PCM; FLAC uses lossless compression. Bitrate controls apply to MP3, M4A/AAC and Opus. Opus output uses 48 kHz; explicitly incompatible sample rates are rejected. Automatic rate selection retains the source rate when the encoder supports it. Only the first audio track is exported; cover art, video, subtitles and chapters are excluded. Reverse buffers the selected audio in memory, so trim long material before reversing it.

## Archives, outputs and cancellation

**Tools → Pack** creates one **ZIP** (default) or **7Z** archive from selected files and folders, including mixed file types and unsupported conversion extensions. Start dragging the selection, press Shift, switch to Tools with another Shift press, then drop on Pack and choose the archive format. Folders and unknown file types show the Pack tool directly. Moving the pointer over the hub never switches rings; click menus also accept deliberate hub clicks.

Folder structure, empty folders and modification times are retained. Overlapping selections are included once. Conflicting top-level names and links/junctions are rejected with an explanation. Limits are 100,000 items and 20 GiB of source data. Packing first creates a cancellable temporary copy, so enough free space for that copy and the archive is required.

The archive is saved beside the first remaining top-level selection, or in its parent's DragShift subfolder according to Settings. If that would place the result inside a selected folder, it is saved beside that folder instead. Existing results receive a numbered name. Choosing Pack for an existing archive puts that archive file into a new archive; Convert changes the format of its contents.


Archive conversion uses the local 7-Zip engine. RAR is read-only; RAR creation and encrypted archives are not supported. Contents are inspected before extraction. Unsafe paths, links, special files and colliding names are rejected. Limits are 100,000 listed entries and 20 GiB of declared uncompressed data; extraction also checks free disk space and the actual extracted tree.

Conversions and edits use **Settings → Save results**: beside the original, or in a `DragShift` subfolder. Archive extraction always creates a unique **`<name> Extracted`** folder beside the original archive. Existing results receive a numbered name rather than being replaced.

Video, audio and archive processing runs in background workers with a progress window and **Cancel**. Their batch conversions roll back results from earlier files when a later file fails or the batch is cancelled. Original files are kept. Closing a busy progress window requests cancellation. Result notifications offer **Open**, **Show** and **Undo**; Undo sends newly created results to the Recycle Bin.

All conversion engines process local files; DragShift does not upload media or download an engine during a job. **Listen to original** and result **Open** launch the system's associated app. The glass retains local background snapshots, blur and refraction, with a neutral light tint and simple controls. Snapshots stay in memory for decoration and are not saved or sent. On Windows, native acrylic is the fallback when a snapshot is unavailable. Set `DRAGSHIFT_RECORDING=1` before launch to keep windows visible to screen-recording tools.

## Run, verify and build from source

Use a Python virtual environment. The Windows dependency snapshot is in `requirements-windows-lock.txt`; the base runtime requirements are in `requirements.txt`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows-lock.txt
.\.venv\Scripts\python.exe -m dragshift
```

Place FFmpeg/FFprobe, FFglitch 0.10.2 (`ffgac` and `ffedit`), 7-Zip, and ImageMagick 7 (`magick`, built with `lqr`) in `bin/`, with their notices. For a Windows 7-Zip sidecar, keep `7z.dll` beside `7z.exe`. Source archives omit these engine binaries; obtain them separately from the sources below.

Engine discovery accepts `DRAGSHIFT_FFMPEG`, `DRAGSHIFT_FFPROBE`, `DRAGSHIFT_FFGAC`, `DRAGSHIFT_FFEDIT`, `DRAGSHIFT_7Z` and `DRAGSHIFT_MAGICK`, then bundled `bin/` and PATH. The archive engine also checks an existing `C:\Program Files\7-Zip\7z.exe` installation.

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m dragshift --selftest
.\.venv\Scripts\python.exe -m dragshift --video-selftest
.\.venv\Scripts\python.exe -m dragshift --files-selftest
.\.venv\Scripts\python.exe -m PyInstaller DragShift.spec --noconfirm
ISCC.exe installer\DragShift.iss
```

The Inno Setup recipe creates `dist/DragShift-Setup.exe`; the delivered custom installer is named `DragShift-Files-Setup.exe`. Synthetic integration checks exercise real codecs and archive round trips when their engines are installed. Run the self-test switches on the frozen executable too when validating a package. Tests that require optional media engines skip when those engines are absent. The custom build has been verified on Windows; macOS and Linux need platform validation.

Release CI uses `python packaging/media_engines.py --stage` before tests: native FFmpeg/FFprobe, FFglitch 0.10.2, 7-Zip, ImageMagick 7.1.2-34 Q16 with PNG/lqr. Windows downloads Gyan essentials and the verified official ImageMagick archive. On Unix, install FFmpeg, 7-Zip, a compiler, make, pkg-config, libpng and liblqr development packages first; the helper builds ImageMagick without external coder modules. macOS uses native Homebrew packages; Intel dependencies may require a longer source build. PyInstaller scans ELF/Mach-O engines and relocates their shared-library dependencies. The helper records download hashes in bundled `EngineVersions.json` and fails when required engines/delegates are absent. Both languages and actual frozen media operations are checked on Windows, macOS Intel/ARM and Debian 12 jobs. Configuring those jobs does not establish that native runs have passed; inspect the PR's CI results.

Windows logs are written to `%LOCALAPPDATA%\DragShift\log.txt`. Settings are available from the tray menu; closing an editor leaves the tray app running. A normal launch or second launch only shows a short reminder to use Shift-drag; `--quiet` launches silently.

## Engines and licenses

- [FFmpeg documentation](https://ffmpeg.org/documentation.html), [FFmpeg source](https://github.com/FFmpeg/FFmpeg), and [Gyan Windows builds](https://www.gyan.dev/ffmpeg/builds/).
- [FFglitch documentation](https://ffglitch.org/), [FFglitch 0.10 source repository](https://github.com/ramiropolla/ffglitch-core/tree/ffedit-0.10), and [0.10.2 source archive](https://ffglitch.org/pub/src/ffglitch-0.10.2.tar.xz).
- [7-Zip downloads](https://www.7-zip.org/), [7-Zip source](https://github.com/ip7z/7zip), and [license](https://www.7-zip.org/license.txt).
- [ImageMagick](https://imagemagick.org/), [ImageMagick source](https://github.com/ImageMagick/ImageMagick), and [liblqr](https://github.com/ImageMagick/lqr).
- Application source: upstream [MIT license](LICENSE). Bundled engines and Python dependencies retain their own licenses; see [THIRD_PARTY.md](THIRD_PARTY.md) before redistributing a package.
