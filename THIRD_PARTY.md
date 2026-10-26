# Third-party engines and local Windows build

DragShift's application source remains under the upstream MIT license in LICENSE.
The source contribution contains no engine binaries. DragShift discovers optional
engines locally and runs them as separate command-line programs. The versions
below describe the tested private Windows package, not an official upstream release.

## FFglitch 0.10.2

Copyright 2017–2024 Ramiro Polla and the FFmpeg contributors. GPL, as indicated
by the supplied binaries' `-version` output (`--enable-gpl`).

- Project and documentation: https://ffglitch.org/
- Original Windows distribution: https://ffglitch.org/pub/bin/win64/ffglitch-0.10.2-windows-x86_64.zip
- Corresponding complete source archive: https://ffglitch.org/pub/src/ffglitch-0.10.2.tar.xz
- Source repository and license: https://github.com/ramiropolla/ffglitch-core/tree/ffedit-0.10

`assets/datamosh.js` is original code for this DragShift extension, based on the
documented FFglitch 0.10.2 motion-vector API. No third-party effect scripts were copied.

## FFmpeg / FFprobe

This computer's Gyan full static FFmpeg build is copied as a sidecar to make the
installed custom build independent of PATH. Version: 2026-06-04-git-c27a3b12e3.
Its build configuration enables GPL and version 3. FFmpeg and FFprobe are GPLv3
in this configuration; they remain independent executables.

- Project, licensing and source: https://ffmpeg.org/legal.html
- FFmpeg source revision: https://github.com/FFmpeg/FFmpeg/tree/c27a3b12e3
- Build provider, build configuration and external library source links: https://www.gyan.dev/ffmpeg/builds/
- GPLv3 license: https://www.gnu.org/licenses/gpl-3.0.html

Linux CI stages the full static GPL build of the FFmpeg 9.0 release branch from
https://github.com/BtbN/FFmpeg-Builds/releases/tag/latest, with FFprobe from the
same archive. The archive is verified against the provider's published
`checksums.sha256`; its URL and SHA-256 are recorded in `EngineVersions.json`.
The provider targets glibc 2.28 / Linux 4.18 and newer. Its build scripts,
dependency sources and configuration are available at
https://github.com/BtbN/FFmpeg-Builds; original archive license notices are staged
beside the engines. macOS stages the selected full Homebrew build. Precise video
editing requires FFmpeg 7 or newer; the older Debian 12 system FFmpeg is not used
for the packaged media engine.

The Intel macOS package also stages Homebrew's SDL3 library beside the media
executables, using the `libSDL3.dylib` name expected by SDL2-compat's dynamic
loader. SDL3 uses the Zlib license; its notice is included as `SDL3-LICENSE.txt`.
This runtime dependency is not present in SDL2-compat's Mach-O import table.
Sources: https://github.com/libsdl-org/SDL and
https://github.com/libsdl-org/sdl2-compat.

This is a private local custom build, not an upstream DragShift release.
Before redistributing the bundled engines, provide the complete corresponding
source and notices required by their licenses, including configured external libraries.

## 7-Zip

The existing Windows installation's `7z.exe` and `7z.dll` are included as separate
archive-engine sidecars with their original license notice. The core is LGPL 2.1+
with BSD components and an unRAR restriction. This application reads RAR and never
creates RAR files.

- Source: https://github.com/ip7z/7zip
- Formats and downloads: https://www.7-zip.org/
- License: https://www.7-zip.org/license.txt

## ImageMagick / Liquid Rescale

The Windows package includes **ImageMagick 7.1.2-34 Q16 x64** (2026-10-14),
official portable static build with the built-in **lqr** delegate. The engine's
LICENSE and NOTICE files are bundled beside `magick.exe`. Media is decoded by
Pillow/FFmpeg; ImageMagick receives only generated PNG frames under a restrictive
PNG-only policy with local memory/disk/thread/time limits.

- Official download: https://imagemagick.org/download/
- Tested package: https://github.com/ImageMagick/ImageMagick/releases/download/7.1.2-34/ImageMagick-7.1.2-34-portable-Q16-x64.7z
- Package SHA-256: `52cc6a441c7884f5ff4274b410c9b90c4b7d54245dcdf4d32988bb4e4df4e5d1`
- Source and license: https://github.com/ImageMagick/ImageMagick/tree/7.1.2-34
- liblqr source (LGPL): https://github.com/ImageMagick/lqr
- Original liblqr project: https://liblqr.wikidot.com/

The independently implemented shrink/resize pipeline follows the documented
ImageMagick command in https://github.com/rupansh/OpenDistortBot, which credits
@Mezgrman, author of @DistortBot. No GPL bot application code is incorporated.
The current hosted service's implementation/settings have not been verified.

## Python dependencies

Large print rasters use tifffile (BSD-3-Clause) for strip-based TIFF/BigTIFF
containers: https://github.com/cgohlke/tifffile. Deflate strips use Python's
standard-library zlib; no optional imagecodecs runtime is required.

PySide6 / Qt (LGPLv3), PyMuPDF (AGPLv3), Pillow (HPND), pillow-heif, NumPy,
python-docx and pywin32 retain their respective upstream licenses. Their versions
are recorded in requirements-windows-lock.txt. PyInstaller also collects package
metadata and available license files in the frozen distribution.
