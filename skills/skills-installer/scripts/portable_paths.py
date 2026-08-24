"""Portable path rules used by standalone closed-network skill tooling.

This file is vendored into each standalone tool. Keep the copies byte-for-byte
equal so either skill remains self-contained after installation.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

MAX_COMPONENT_UTF16_UNITS = 255
MAX_RELATIVE_PATH_UTF16_UNITS = 240
MAX_WINDOWS_ABSOLUTE_PATH_UTF16_UNITS = 240
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
WINDOWS_RESERVED_CHARS = frozenset('<>:"/\\|?*')
WINDOWS_DEVICE_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|"
    r"COM(?:[1-9]|[¹²³])|LPT(?:[1-9]|[¹²³]))$",
    re.IGNORECASE,
)


class PortablePathError(ValueError):
    """Raised when a path cannot be represented safely on supported hosts."""


def utf16_units(value: str) -> int:
    """Return the Windows UTF-16 code-unit length without a BOM."""

    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise PortablePathError("path contains an invalid Unicode scalar") from exc


def portable_component_key(component: str) -> str:
    """Return a conservative cross-platform comparison key."""

    return unicodedata.normalize("NFC", component).casefold()


def validate_portable_component(component: str, *, label: str = "path component") -> None:
    if not isinstance(component, str) or not component or component in {".", ".."}:
        raise PortablePathError(f"{label} must be a non-empty ordinary name")
    if unicodedata.normalize("NFC", component) != component:
        raise PortablePathError(
            f"{label} must use NFC Unicode normalization: {component!r}"
        )
    if component[-1] in {".", " "}:
        raise PortablePathError(f"{label} must not end with a dot or space: {component!r}")
    if any(
        ord(character) < 32 or character in WINDOWS_RESERVED_CHARS
        for character in component
    ):
        raise PortablePathError(
            f"{label} contains a Windows-reserved character: {component!r}"
        )
    stem = component.split(".", 1)[0].rstrip(" .")
    if WINDOWS_DEVICE_RE.fullmatch(stem):
        raise PortablePathError(f"{label} is a Windows-reserved device name: {component!r}")
    if utf16_units(component) > MAX_COMPONENT_UTF16_UNITS:
        raise PortablePathError(
            f"{label} exceeds {MAX_COMPONENT_UTF16_UNITS} UTF-16 code units: {component!r}"
        )


def validate_portable_relative_path(value: str, *, label: str = "path") -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise PortablePathError(f"{label} must be a non-empty string")
    if "\\" in value:
        raise PortablePathError(f"{label} must use '/' separators: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PortablePathError(f"unsafe {label}: {value!r}")
    for component in path.parts:
        validate_portable_component(component, label=f"{label} component")
    if utf16_units(value) > MAX_RELATIVE_PATH_UTF16_UNITS:
        raise PortablePathError(
            f"{label} exceeds {MAX_RELATIVE_PATH_UTF16_UNITS} UTF-16 code units: {value!r}"
        )
    return path


def portable_path_key(path: PurePosixPath) -> str:
    return "/".join(portable_component_key(part) for part in path.parts)


def is_windows_reparse_point(file_stat: object) -> bool:
    """Return true for Windows symlinks, junctions, and other reparse points."""

    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_file_link_count(path: Path) -> int:
    """Read the hard-link count from an opened Windows file handle."""

    try:
        import ctypes
        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ByHandleFileInformation),
        )
        get_information.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        open_reparse_point = 0x00200000
        handle = create_file(
            str(path),
            0,
            share_all,
            None,
            open_existing,
            open_reparse_point,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            information = ByHandleFileInformation()
            if not get_information(handle, ctypes.byref(information)):
                raise OSError(
                    ctypes.get_last_error(), "GetFileInformationByHandle failed"
                )
            if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise PortablePathError(
                    f"cannot determine hard-link count through a reparse point: {path}"
                )
            count = int(information.nNumberOfLinks)
            if count < 1:
                raise OSError("Windows returned an invalid hard-link count")
            return count
        finally:
            close_handle(handle)
    except PortablePathError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise PortablePathError(
            f"cannot determine hard-link count safely: {path}"
        ) from exc


def file_link_count(
    path: Path, file_stat: object, *, platform: str | None = None
) -> int:
    """Return a trustworthy hard-link count, compensating for DirEntry on Windows."""

    initial = getattr(file_stat, "st_nlink", 0)
    if isinstance(initial, int) and initial > 0:
        return initial
    try:
        refreshed = os.stat(path, follow_symlinks=False)
    except OSError:
        refreshed = None
    refreshed_count = getattr(refreshed, "st_nlink", 0)
    if isinstance(refreshed_count, int) and refreshed_count > 0:
        return refreshed_count
    if (platform or os.name) == "nt":
        return _windows_file_link_count(path)
    raise PortablePathError(f"cannot determine hard-link count safely: {path}")


def full_file_stat(path: Path, file_stat: object) -> object:
    """Refresh incomplete DirEntry metadata before it is used as an identity."""

    initial = getattr(file_stat, "st_nlink", 0)
    if isinstance(initial, int) and initial > 0:
        return file_stat
    try:
        return os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise PortablePathError(f"cannot obtain complete file metadata: {path}") from exc
