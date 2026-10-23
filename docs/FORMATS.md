# Supported formats

What the menu offers when you drop each kind of file. A format is never offered as its own target, and a target only appears when the program it needs is installed (the *Converts to* column assumes LibreOffice and FFmpeg are present; without them the *Needs* column tells you what drops out).

| Kind | Reads | Converts to | Needs |
|---|---|---|---|
| Images | JPG, PNG, WEBP, HEIC/HEIF, AVIF, TIFF, BMP, GIF, ICO | JPG, WEBP, HEIC, TIFF, AVIF, BMP, PDF, ICO, GIF, PNG | Built in (Pillow) |
| Vector images | SVG | PNG, JPG, WEBP, TIFF, PDF | Built in (PyMuPDF) |
| PDF | PDF | JPG, PNG, DOCX, TXT, HTML, SVG | Built in (PyMuPDF) |
| Word | DOCX | PDF, TXT, HTML, MD, ODT, RTF, DOC | Built in. Microsoft Word or LibreOffice give a better PDF; ODT, RTF and DOC output need LibreOffice |
| Other word processors | ODT, RTF, DOC, DOT, WPD | PDF, DOCX, RTF, DOC, TXT, HTML, ODT | ODT and RTF to TXT, PDF, DOCX: built in (text only). Everything else, and all DOC/DOT/WPD: LibreOffice |
| Spreadsheets & data | XLSX, XLSM, CSV, TSV, JSON | CSV, TSV, JSON, HTML, MD, PDF, DOCX, ODS, XLS, XLSX | Built in (openpyxl). ODS and XLS output need LibreOffice |
| Old spreadsheets | XLS, ODS | XLSX, CSV, TSV, JSON, HTML, MD, PDF, DOCX, ODS, XLS | LibreOffice |
| Presentations | PPTX, PPSX | PDF, PNG, JPG, TXT, ODP, PPT | PDF/PNG/JPG: LibreOffice gives an exact copy, without it you get a text-and-pictures outline. ODP and PPT output need LibreOffice |
| Old presentations | PPT, PPS, ODP | PDF, PNG, JPG, TXT, PPTX, ODP, PPT | LibreOffice |
| Markdown | MD, MARKDOWN | PDF, DOCX, HTML, TXT | Built in (Python-Markdown) |
| Web pages | HTML, HTM, XHTML | PDF, DOCX, MD, TXT | Built in |
| Plain text | TXT, LOG, XML | PDF, DOCX, HTML | Built in |
| E-books | EPUB, MOBI, FB2 | PDF, TXT, HTML | Built in (PyMuPDF) |
| Page documents | XPS, OXPS, CBZ | PDF | Built in (PyMuPDF) |
| Archives | ZIP, TAR, TAR.GZ (TGZ), TAR.XZ (TXZ), TAR.BZ2 (TBZ2) | TAR.GZ, TAR.XZ, TAR.BZ2, TAR, ZIP | Built in. Files are streamed, never extracted to disk, and unsafe paths are refused |
| Audio | MP3, WAV, FLAC, AAC, M4A, OGG, OPUS, WMA, AIFF, AC3, APE, MKA and about 20 more | MP3, FLAC, M4A, AAC, OGG, OPUS, WMA, AIFF, WAV | FFmpeg (bundled) |
| Video | MP4, MKV, WEBM, MOV, AVI, WMV, FLV, M4V, MPG, 3GP, TS, VOB and about 40 more | MKV, WEBM, MOV, AVI, GIF, MP3, M4A, WAV, FLAC, MP4 | FFmpeg (bundled) |
| Subtitles | SRT, VTT, ASS, SSA | VTT, ASS, SRT | FFmpeg (bundled) |

## Optional programs

- **FFmpeg**: audio, video and subtitle conversion. Bundled through `imageio-ffmpeg`; otherwise `ffmpeg` on PATH or the `DRAGSHIFT_FFMPEG` variable.
- **LibreOffice**: exact PDF export of slides, old Office formats (DOC, XLS, PPT) and OpenDocument/RTF output. Found on PATH, in the usual Windows and Mac install folders, or via `DRAGSHIFT_SOFFICE`. DragShift runs it headless with its own profile, so it never interferes with a LibreOffice you have open.

Without them, everything marked *Built in* still works.

## Tools for audio and video

Audio and video files also get a **Tools** ring (press Shift again, like for images): Compress, Trim, Edit, Merge (when several files are dropped) and Metadata. They never touch the original and need FFmpeg.

## Notes

- Spreadsheets with several sheets become one CSV, TSV or JSON per sheet; HTML, Markdown, PDF, DOCX and XLSX keep them together.
- Slides to PNG/JPG give one image per slide. PDF to SVG gives one file per page.
- CSV and JSON numbers are detected (`31` becomes a number, `0042` stays text so IDs and phone numbers survive).
- JSON must be a list of records (or an object of lists) to become a table; anything else is refused with a clear message.

## Not covered

RAR and 7z archives, Apple iWork files (Pages, Numbers, Keynote), Photoshop and camera RAW images, fonts, databases, and anything encrypted or DRM-protected.
