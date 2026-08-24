"""Portable path rules used by standalone closed-network skill tooling.

This file is vendored into each standalone tool. Keep the copies byte-for-byte
equal so either skill remains self-contained after installation.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

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
