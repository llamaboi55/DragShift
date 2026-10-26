"""Local archive conversion and safe extraction using the installed 7-Zip CLI.

Archives are copied into an isolated temporary workspace before inspection, so
the inspected bytes are the bytes extracted. Sources are never modified.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from . import video
from .util import CONFIG, UserError

TARGETS = ["ZIP", "7Z", "TAR"]
INPUT_EXTENSIONS = {".zip", ".7z", ".rar", ".tar"}
MAX_ENTRIES = 100_000
MAX_EXTRACTED_BYTES = 20 * 1024 ** 3
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    size: int
    is_dir: bool


def executable() -> str:
    """Explicit override, bundled sidecar, installed Windows 7-Zip, then PATH."""
    override = os.environ.get("DRAGSHIFT_7Z")
    roots = [Path(sys.executable).parent] if getattr(sys, "frozen", False) else []
    roots.append(Path(__file__).resolve().parent.parent)
    suffix = ".exe" if os.name == "nt" else ""
    candidates = ([Path(override)] if override else [])
    candidates += [root / "bin" / (name + suffix) for root in roots for name in ("7z", "7zz")]
    candidates.append(Path(r"C:\Program Files\7-Zip\7z.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    found = shutil.which("7z") or shutil.which("7zz")
    if found:
        return found
    raise UserError("7-Zip was not found. Install 7-Zip, set DRAGSHIFT_7Z, or put 7z.exe in DragShift/bin.")


def _source(src) -> Path:
    video.check_cancel()
    source = Path(src).resolve()
    if not source.is_file():
        raise UserError("The selected archive was moved or deleted. Choose the file again.")
    if source.suffix.lower() not in INPUT_EXTENSIONS:
        raise UserError("Unsupported archive format. Choose ZIP, 7Z, RAR or TAR.")
    return source


def _safe_name(name: str) -> str:
    normalized = name.replace("\\", "/").rstrip("/")
    if (not normalized or name.startswith(("/", "\\")) or PureWindowsPath(name).drive
            or any(ord(char) < 32 or char in '<>:"|?*' for char in normalized)):
        raise UserError("This archive contains an unsafe file path.")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") or part.endswith((".", " ")) or _RESERVED.match(part) for part in parts):
        raise UserError("This archive contains an unsafe file path.")
    return "/".join(parts)


def _records(raw: bytes) -> list[dict[str, str]]:
    try:
        text = raw.decode("utf-8-sig", "strict")
    except UnicodeError as error:
        raise UserError("The archive contains unreadable file names.") from error
    records = []
    current = {}
    for line in text.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = line.partition(" = ")
        if not separator or not key or key in current:
            raise UserError("The archive listing is ambiguous or contains unsafe file names.")
        current[key] = value
    if current:
        records.append(current)
    return records


def entries(src) -> list[ArchiveEntry]:
    source = _source(src)
    try:
        raw = video.capture([executable(), "l", "-slt", "-ba", "-sccUTF-8",
                             "-t" + source.suffix[1:].lower(), "--", str(source)])
    except video.Cancelled:
        raise
    except UserError as error:
        raise UserError("Couldn't read this archive. It may be damaged, encrypted or unsupported.\n" + str(error).split("\n", 1)[-1]) from error
    records = _records(raw)
    if len(records) > MAX_ENTRIES:
        raise UserError(f"This archive contains too many entries (limit: {MAX_ENTRIES:,}).")
    result = []
    seen = {}
    total = 0
    for record in records:
        video.check_cancel()
        # Reject links before extraction, including Unix modes stored in ZIP.
        for key, value in record.items():
            lower = key.lower()
            if ((lower in ("symbolic link", "hard link") and value)
                    or ("reparse" in lower and value not in ("", "-", "0"))
                    or (lower == "alternate stream" and value == "+")):
                raise UserError("Links and alternate streams in archives are not supported.")
        attributes = record.get("Attributes", "") + " " + record.get("Mode", "")
        if re.search(r"(?:^|\s)[bclps][rwxstST-]{9}(?:\s|$)", attributes):
            raise UserError("Links and special files in archives are not supported.")
        if record.get("Encrypted") == "+":
            raise UserError("Password-protected archives are not supported.")
        try:
            name = _safe_name(record["Path"])
            size = int(record["Size"] or "0")
            if size < 0:
                raise ValueError
        except (KeyError, ValueError) as error:
            raise UserError("The archive contains invalid file information.") from error
        is_dir = record.get("Folder") == "+" or attributes.lstrip().startswith("D") or bool(re.search(r"(?:^|\s)d[rwxstST-]{9}", attributes))
        key = name.casefold()
        if key in seen:
            raise UserError("The archive contains colliding file names.")
        seen[key] = is_dir
        result.append(ArchiveEntry(name, size, is_dir))
        if not is_dir:
            total += size
        if total > MAX_EXTRACTED_BYTES:
            raise UserError(f"This archive is too large to extract safely (limit: {MAX_EXTRACTED_BYTES // 1024 ** 3} GB).")
    for entry in result:
        parts = entry.name.casefold().split("/")
        if any(seen.get("/".join(parts[:i])) is False for i in range(1, len(parts))):
            raise UserError("The archive contains colliding file and directory paths.")
    return result


def _run(args, **kwargs):
    try:
        return video.run_process(args, parse_progress=False, **kwargs)
    except video.Cancelled:
        raise
    except UserError as error:
        raise UserError("Archive processing failed. Original files are unchanged.\n" + str(error).split("\n", 1)[-1]) from error


def _audit(folder: Path, members: list[ArchiveEntry]):
    """Check the actual tree before making any result visible to the user."""
    expected = {entry.name.casefold(): entry for entry in members}
    files = set()
    count = total = 0
    for current, directories, filenames in os.walk(folder, followlinks=False):
        for name in directories + filenames:
            video.check_cancel()
            path = Path(current) / name
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise UserError("Links and special files in archives are not supported.")
            relative = _safe_name(path.relative_to(folder).as_posix()).casefold()
            count += 1
            if count > MAX_ENTRIES:
                raise UserError("This archive creates too many files and folders.")
            if stat.S_ISREG(info.st_mode):
                member = expected.get(relative)
                if member is None or member.is_dir or info.st_size != member.size:
                    raise UserError("The extracted files do not match the archive listing.")
                files.add(relative)
                total += info.st_size
                if total > MAX_EXTRACTED_BYTES:
                    raise UserError("This archive is too large to extract safely.")
    if files != {entry.name.casefold() for entry in members if not entry.is_dir}:
        raise UserError("The archive could not be extracted completely.")
    if any(not (folder / entry.name).is_dir() for entry in members if entry.is_dir):
        raise UserError("The archive could not be extracted completely.")


@contextlib.contextmanager
def _contents(source: Path):
    with tempfile.TemporaryDirectory(prefix=".~archive-", dir=source.parent) as name:
        workspace = Path(name)
        snapshot = workspace / ("input" + source.suffix.lower())
        with source.open("rb") as original, snapshot.open("xb") as copied:
            while chunk := original.read(1024 * 1024):
                video.check_cancel()
                copied.write(chunk)
        members = entries(snapshot)
        required = sum(entry.size for entry in members if not entry.is_dir)
        if shutil.disk_usage(workspace).free < required + 64 * 1024 ** 2:
            raise UserError("There is not enough free disk space to extract this archive.")
        folder = workspace / "contents"
        folder.mkdir()
        _run([executable(), "x", "-y", "-bd", "-bb0", "-sccUTF-8", "-aos",
              "-t" + source.suffix[1:].lower(), "-o" + str(folder), "--", str(snapshot)],
             message="Extracting archive…", offset=.05, span=.45)
        _audit(folder, members)
        yield folder


def extract(src) -> list[Path]:
    source = _source(src)
    with _contents(source) as folder:
        video.check_cancel()
        with video._publish_lock:
            base = source.with_name(source.stem + " Extracted")
            destination = base
            counter = 0
            while destination.exists():
                counter += 1
                destination = base.with_name(f"{base.name} ({counter})")
            # Windows directory rename atomically refuses an existing target.
            try:
                os.rename(folder, destination)
            except FileExistsError as error:
                raise UserError("Another program created this output folder. Please try again.") from error
    return [destination]


def convert_archive(src, target: str) -> list[Path]:
    target = target.upper()
    if target not in TARGETS:
        raise UserError("Unsupported archive export format. Choose ZIP, 7Z or TAR.")
    source = _source(src)
    with _contents(source) as folder:
        with video.export_path(source, ext="." + target.lower()) as (temporary, outputs):
            # 7-Zip creates the archive itself; an empty pre-created file is invalid.
            temporary.unlink()
            args = [executable(), "a", "-t" + target.lower(), "-y", "-bd", "-bb0", "-sccUTF-8"]
            if target != "TAR":
                args += ["-mx=5"]
            _run(args + ["--", str(temporary), "*"], cwd=folder,
                 message=f"Packing archive as {target}…", offset=.5, span=.45)
    return outputs


def _pack_stat(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise UserError("Links and junctions cannot be packed. Select the original files or folders.")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise UserError("Only regular files and folders can be packed.")
    return info


def packing_sources(paths) -> list[Path]:
    """Deduplicate selections and omit children already covered by a selected parent."""
    video.check_cancel()
    sources = list(dict.fromkeys(Path(os.path.abspath(p)) for p in paths))
    if not sources:
        raise UserError("Select at least one file or folder to pack.")
    if len(sources) > MAX_ENTRIES:
        raise UserError("Too many selected items to pack.")
    folders = set()
    try:
        for source in sources:
            video.check_cancel()
            if stat.S_ISDIR(_pack_stat(source).st_mode):
                folders.add(source)
    except OSError as error:
        raise UserError("A selected file or folder is missing or cannot be read. Choose it again.") from error
    sources = [p for p in sources if not any(parent in folders for parent in p.parents)]
    names = [_safe_name(p.name).casefold() for p in sources]
    if len(names) != len(set(names)):
        raise UserError("Selected items have the same name. Rename them or pack their parent folders instead.")
    return sources


def packing_directory(sources: list[Path]) -> Path:
    directory = sources[0].parent
    if CONFIG.get("output") == "folder":
        candidate = directory / "DragShift"
        # A selected folder may itself be called DragShift. Keep its output outside.
        if not any(p.is_dir() and candidate.resolve().is_relative_to(p.resolve()) for p in sources):
            directory = candidate
    return directory


def pack(paths, target: str = "ZIP") -> list[Path]:
    """Copy a selected group into an isolated tree, then create one new ZIP/7Z."""
    target = target.upper()
    if target not in ("ZIP", "7Z"):
        raise UserError("Unsupported archive format. Choose ZIP or 7Z.")
    sources = packing_sources(paths)
    engine = executable()
    members = []
    seen = set()
    total = 0
    pending = [(p, p.name) for p in reversed(sources)]
    try:
        while pending:
            video.check_cancel()
            path, name = pending.pop()
            name = _safe_name(name)
            key = name.casefold()
            if key in seen:
                raise UserError("The selected folders contain colliding file names.")
            seen.add(key)
            info = _pack_stat(path)
            is_dir = stat.S_ISDIR(info.st_mode)
            members.append((path, name, is_dir, info))
            if is_dir:
                children = sorted(path.iterdir(), key=lambda p: p.name.casefold(), reverse=True)
                pending.extend((child, name + '/' + child.name) for child in children)
            else:
                total += info.st_size
            if len(members) + len(pending) > MAX_ENTRIES or total > MAX_EXTRACTED_BYTES:
                raise UserError("The selection is too large to pack (limit: 100,000 items and 20 GB).")

        # Beside a top-level source means the staging tree cannot be inside a selected folder.
        with tempfile.TemporaryDirectory(prefix=".~pack-", dir=sources[0].parent) as workspace:
            staged = Path(workspace)
            copied = 0
            for path, name, is_dir, info in members:
                video.check_cancel()
                video.progress(.5 * copied / max(1, total), "Preparing files…")
                destination = staged / name
                if is_dir:
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    current = _pack_stat(path)
                    if (current.st_size, current.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                        raise UserError("A selected file changed while preparing the archive. Try again.")
                    with path.open('rb') as source, destination.open('xb') as output:
                        while chunk := source.read(1024 * 1024):
                            video.check_cancel()
                            output.write(chunk)
                            copied += len(chunk)
                            if copied > total:
                                raise UserError("A selected file changed while preparing the archive. Try again.")
                            video.progress(.5 * copied / max(1, total), "Preparing files…")
                    current = _pack_stat(path)
                    if (current.st_size, current.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                        raise UserError("A selected file changed while preparing the archive. Try again.")
                    shutil.copystat(path, destination, follow_symlinks=False)
            # Restore directory dates only after their children have been created.
            for path, name, is_dir, _ in reversed(members):
                if is_dir:
                    video.check_cancel()
                    shutil.copystat(path, staged / name, follow_symlinks=False)
            _audit(staged, [ArchiveEntry(name, info.st_size if not is_dir else 0, is_dir)
                            for _, name, is_dir, info in members])
            video.check_cancel()
            directory = packing_directory(sources)
            directory.mkdir(exist_ok=True)
            first = sources[0]
            anchor = first.with_name(first.name + '.pack') if first.is_dir() else first
            label = 'Archive' if len(sources) > 1 else ''
            with video.export_path(anchor, label, '.' + target.lower(), directory=directory) as (temporary, outputs):
                temporary.unlink()
                _run([engine, 'a', '-t' + target.lower(), '-y', '-bd', '-bb0', '-sccUTF-8',
                      '-mx=5', '--', str(temporary), '*'], cwd=staged,
                     message=f"Packing archive as {target}…", offset=.5, span=.5)
        return outputs
    except OSError as error:
        raise UserError("Couldn't pack the selected files. Check access and free disk space. Originals are unchanged.") from error
