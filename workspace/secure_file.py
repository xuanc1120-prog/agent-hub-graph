"""Race-resistant file handles for workspace capture and restoration."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class SecureFileError(OSError):
    """A workspace path could not be bound to a safe regular-file handle."""


@contextmanager
def open_verified_binary(
    root: Path,
    relative: str,
    *,
    write_attributes: bool = False,
) -> Iterator[BinaryIO]:
    """Open a repo-relative regular file without following a path escape."""

    resolved_root = root.expanduser().resolve(strict=True)
    parts = _relative_parts(relative)
    expected = resolved_root.joinpath(*parts)
    fd = (
        _open_windows_fd(expected, resolved_root, write_attributes=write_attributes)
        if os.name == "nt"
        else _open_posix_fd(resolved_root, parts)
    )
    try:
        stream = os.fdopen(fd, "rb", closefd=True)
    except BaseException:
        os.close(fd)
        raise
    try:
        metadata = os.fstat(stream.fileno())
        _assert_regular_single_link(metadata)
        if os.name == "nt":
            _assert_windows_handle_path(stream.fileno(), expected)
        yield stream
    finally:
        stream.close()


def assert_path_identity(
    root: Path,
    relative: str,
    expected: os.stat_result,
) -> None:
    """Require the current path to resolve to the same safe file identity."""

    with open_verified_binary(root, relative) as stream:
        current = os.fstat(stream.fileno())
        if not os.path.samestat(expected, current):
            raise SecureFileError("workspace path identity changed")


def set_verified_mode(
    root: Path,
    relative: str,
    mode: int,
) -> None:
    """Restore mode bits through a verified file handle."""

    if mode < 0 or mode > 0o777:
        raise SecureFileError("workspace file mode is invalid")
    with open_verified_binary(root, relative, write_attributes=True) as stream:
        if os.name == "nt":
            _set_windows_mode(stream.fileno(), mode)
        else:
            os.fchmod(stream.fileno(), mode)
    with open_verified_binary(root, relative) as stream:
        restored = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
    if restored != mode:
        raise SecureFileError("workspace file mode could not be restored")


def _relative_parts(relative: str) -> tuple[str, ...]:
    if not relative or "\\" in relative:
        raise SecureFileError("workspace path is not canonical repo-relative POSIX")
    path = PurePosixPath(relative)
    parts = tuple(path.parts)
    if not parts or path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise SecureFileError("workspace path is not canonical repo-relative POSIX")
    return parts


def _assert_regular_single_link(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SecureFileError("workspace path is not a plain single-link file")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(metadata, "st_file_attributes", 0) & reparse_flag:
        raise SecureFileError("workspace path is a reparse point")


def _open_posix_fd(root: Path, parts: tuple[str, ...]) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None or os.open not in os.supports_dir_fd:
        raise SecureFileError("platform lacks no-follow directory handle support")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | directory | nofollow | cloexec
    file_flags = os.O_RDONLY | nofollow | cloexec
    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        return os.open(parts[-1], file_flags, dir_fd=current)
    except OSError as error:
        raise SecureFileError("no-follow workspace file open failed") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_windows_fd(
    path: Path,
    root: Path,
    *,
    write_attributes: bool,
) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
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
    desired_access = 0x80000000  # GENERIC_READ
    if write_attributes:
        desired_access |= 0x00000100  # FILE_WRITE_ATTRIBUTES
    share_mode = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
    open_existing = 3
    flags = 0x00200000 | 0x08000000  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
    handle = create_file(
        _extended_windows_path(path),
        desired_access,
        share_mode,
        None,
        open_existing,
        flags,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise SecureFileError("no-follow Windows workspace file open failed") from ctypes.WinError(
            ctypes.get_last_error()
        )
    try:
        fd = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    try:
        _assert_windows_handle_path(fd, path, root=root)
        _assert_regular_single_link(os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _assert_windows_handle_path(
    fd: int,
    expected: Path,
    *,
    root: Path | None = None,
) -> None:
    final = _windows_final_path(fd)
    expected_key = _windows_path_key(expected)
    final_key = _windows_path_key(Path(final))
    if final_key != expected_key:
        raise SecureFileError("Windows file handle resolved outside its expected path")
    if root is not None:
        root_key = _windows_path_key(root)
        try:
            common = os.path.commonpath((root_key, final_key))
        except ValueError as error:
            raise SecureFileError("Windows file handle is outside the workspace") from error
        if common != root_key:
            raise SecureFileError("Windows file handle is outside the workspace")


def _windows_final_path(fd: int) -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_final_path = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)
    size = 512
    while size <= 32_768:
        buffer = ctypes.create_unicode_buffer(size)
        result = get_final_path(handle, buffer, size, 0)
        if result == 0:
            raise SecureFileError(
                "failed to resolve Windows file handle path"
            ) from ctypes.WinError(ctypes.get_last_error())
        if result < size:
            return _strip_extended_windows_prefix(buffer.value)
        size = result + 1
    raise SecureFileError("Windows file handle path exceeds the safety limit")


def _set_windows_mode(fd: int, mode: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_info.restype = wintypes.BOOL
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_info.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    info = FileBasicInfo()
    if not get_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise SecureFileError("failed to read Windows file mode") from ctypes.WinError(
            ctypes.get_last_error()
        )
    readonly = 0x1
    normal = 0x80
    if mode & 0o222:
        info.FileAttributes &= ~readonly
    else:
        info.FileAttributes |= readonly
    if info.FileAttributes == 0:
        info.FileAttributes = normal
    if not set_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise SecureFileError("failed to restore Windows file mode") from ctypes.WinError(
            ctypes.get_last_error()
        )


def _extended_windows_path(path: Path) -> str:
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _strip_extended_windows_prefix(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _windows_path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "SecureFileError",
    "assert_path_identity",
    "open_verified_binary",
    "set_verified_mode",
]
