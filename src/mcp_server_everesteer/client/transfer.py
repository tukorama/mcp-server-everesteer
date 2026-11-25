"""Confined local file I/O and atomic, validated download staging for presigned transfers."""

import contextlib
import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import BinaryIO, Literal

from mcp_server_everesteer.errors import Refused

DownloadKind = Literal["parquet", "zip", "binary", "file"]

_SNIFF_BYTES = 16
_MIN_PARQUET_BYTES = 12
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_HTML_PREFIXES = (b"<!doctype html", b"<html")
_STAGE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)


def confine_output_path(
    output_path: str, roots: Sequence[Path], *, server_supplied: bool = False
) -> Path:
    """Resolve a download destination and require it to stay inside one of the allowed `roots`.

    Relative paths land in the first root. Symlinks in existing directories are resolved before
    the check, and `..` segments, a symlink as the final component, and directories are refused.
    When ``server_supplied`` is True, directory components are stripped defensively.
    """
    if server_supplied:
        name = os.path.basename(output_path)
        if not name or name in (".", ".."):
            raise Refused(f"Refusing invalid server-supplied filename: {output_path!r}")
        requested = Path(name)
    else:
        if any(part == ".." for part in PurePath(output_path).parts):
            raise Refused(f"Refusing to write to path containing '..' segments: {output_path!r}")
        requested = Path(output_path)
    candidate = requested if requested.is_absolute() else roots[0] / requested
    if candidate.is_symlink():
        raise Refused(f"Refusing to write to symlink at {str(candidate)!r}")
    resolved = candidate.resolve()
    if resolved.is_dir():
        raise Refused(f"Refusing to write to directory {str(resolved)!r}; name a file in it.")
    if not any(resolved != root and resolved.is_relative_to(root) for root in roots):
        allowed = os.pathsep.join(str(root) for root in roots)
        raise Refused(
            f"Refusing to write {str(resolved)!r} outside the download directories ({allowed}).",
            hint="Choose an output_path inside them, or ask the user to add the directory to "
            + "EVERESTEER_DOWNLOAD_DIR in the server environment.",
        )
    return resolved


def kind_for_name(name: str) -> DownloadKind:
    """Infer the expected artifact format of a named download from its suffix."""
    suffix = PurePath(name).suffix.lower()
    if suffix in (".parquet", ".pq"):
        return "parquet"
    return "zip" if suffix == ".zip" else "file"


def media_problem(kind: DownloadKind, content_type: str) -> str | None:
    """Describe a Content-Type that cannot carry the expected artifact (None when acceptable)."""
    media = content_type.partition(";")[0].strip().lower()
    textual = media in ("text/html", "application/xhtml+xml", "application/json")
    if kind == "file" or not (textual or media.endswith("+json")):
        return None
    return f"{media} content instead of a {kind} file"


def body_problem(kind: DownloadKind, head: bytes, tail: bytes, size: int) -> str | None:
    """Describe why a downloaded body is not the expected artifact (None when it looks right)."""
    if kind == "file":
        return None
    if head.lstrip().lower().startswith(_HTML_PREFIXES):
        return f"an HTML page instead of a {kind} file"
    parquet = size >= _MIN_PARQUET_BYTES and head[:4] == b"PAR1" and tail[-4:] == b"PAR1"
    if kind == "parquet" and not parquet:
        return "a body that is not a parquet file (no PAR1 magic bytes at both ends)"
    if kind == "zip" and not head.startswith(_ZIP_MAGIC):
        return "a body that is not a zip archive"
    return None


@dataclass
class StagedDownload:
    """A temporary file beside `target` that replaces it only once complete and validated."""

    target: Path
    temp: Path
    handle: BinaryIO
    size: int = 0
    head: bytes = b""
    tail: bytes = b""

    @classmethod
    def open(cls, target: Path) -> "StagedDownload":
        """Create the staging file next to an already confined, resolved `target`."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.resolve() != target.parent:
            raise Refused(f"Refusing to write through a symlinked directory: {str(target)!r}")
        temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.part")
        descriptor = os.open(temp, _STAGE_FLAGS, 0o644)
        try:
            handle = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            temp.unlink(missing_ok=True)
            raise
        return cls(target=target, temp=temp, handle=handle)

    def write(self, chunk: bytes) -> None:
        self.handle.write(chunk)
        self.size += len(chunk)
        if len(self.head) < _SNIFF_BYTES:
            self.head = (self.head + chunk[:_SNIFF_BYTES])[:_SNIFF_BYTES]
        self.tail = (self.tail + chunk[-_SNIFF_BYTES:])[-_SNIFF_BYTES:]

    def publish(self) -> int:
        """Flush and fsync the staged bytes, then atomically move them over the target."""
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        if self.target.is_symlink():
            raise Refused(f"Refusing to write to symlink at {str(self.target)!r}")
        os.replace(self.temp, self.target)
        return self.size

    def discard(self) -> None:
        """Drop the staged bytes and leave any previous target file untouched."""
        with contextlib.suppress(OSError):
            self.handle.close()
        self.temp.unlink(missing_ok=True)


def validate_input_file(file_path: str) -> tuple[Path, str]:
    """Validate an existing local file path for upload (rejecting symlinks and directories)."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file does not exist: {file_path}")
    if path.is_dir():
        raise IsADirectoryError(f"Expected a file, got a directory: {file_path}")
    return path, path.name


def read_file_bytes(file_path: str) -> tuple[bytes, str]:
    """Read local file bytes synchronously (invoke via ``asyncio.to_thread``)."""
    path, name = validate_input_file(file_path)
    return path.read_bytes(), name
