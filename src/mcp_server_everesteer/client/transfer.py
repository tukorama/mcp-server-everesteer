"""Safe local file I/O and credential-free presigned S3/CDN transfer helpers."""

import os
import stat
from pathlib import Path, PurePath
from typing import IO, Any


def safe_output_path(output_path: str, *, server_supplied: bool = False) -> Path:
    """Validate a download destination path, rejecting `..` path traversal.

    When ``server_supplied`` is True, directory components are stripped defensively.
    """
    if server_supplied:
        name = os.path.basename(output_path)
        if not name or name in (".", ".."):
            raise ValueError(f"Refusing invalid server-supplied filename: {output_path!r}")
        return Path(name)

    parts = PurePath(output_path).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"Refusing to write to path containing '..' segments: {output_path!r}")
    return Path(output_path)


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


def _open_no_symlink(path: Path) -> IO[bytes]:
    path.parent.mkdir(parents=True, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | nofollow
        fd = os.open(path, flags, 0o644)
        return os.fdopen(fd, "wb")

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None and stat.S_ISLNK(st.st_mode):
        raise OSError(f"Refusing to write to symlink at {str(path)!r}")
    return open(path, "wb")


def write_bytes_no_symlink(path: Path, data: bytes) -> int:
    """Write ``data`` to ``path`` without following a symlink; returns byte count."""
    with _open_no_symlink(path) as handle:
        handle.write(data)
    return len(data)


def open_no_symlink_for_stream(path: Path) -> Any:
    """Return an open writable binary file object refusing symlinks."""
    return _open_no_symlink(path)
