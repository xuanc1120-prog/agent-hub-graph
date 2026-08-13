"""Race-resistant file handles for workspace capture and restoration."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class SecureFileError(OSError):
    """A workspace path could not be bound to a safe regular-file handle."""


class SecureWorkspaceRoot:
    """Pin one trusted workspace root for race-resistant file operations."""

    def __init__(self, root: Path) -> None:
        lexical = root.expanduser()
        if not lexical.is_absolute():
            lexical = Path.cwd() / lexical
        metadata = os.lstat(lexical)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SecureFileError("workspace root is not a plain directory")
        _assert_not_reparse(metadata, label="workspace root")
        resolved = lexical.resolve(strict=True)
        if resolved != lexical:
            raise SecureFileError("workspace root path contains a symbolic link or reparse point")
        self.path = resolved
        self._root_metadata = metadata
        self._closed = False
        self._windows_root_handle: int | None = None
        self._posix_root_fd: int | None = None
        if os.name == "nt":
            self._windows_root_handle = _open_windows_directory_handle(resolved)
        else:
            self._posix_root_fd = _open_posix_root_fd(
                resolved,
                expected=self._root_metadata,
            )

    def __enter__(self) -> SecureWorkspaceRoot:
        self._assert_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._posix_root_fd is not None:
            os.close(self._posix_root_fd)
            self._posix_root_fd = None
        if self._windows_root_handle is not None:
            _close_windows_handle(self._windows_root_handle)
            self._windows_root_handle = None

    def assert_root_identity(self) -> None:
        """Require the lexical repo root to still name the pinned directory."""

        self._assert_open()
        try:
            current = os.lstat(self.path)
        except OSError as error:
            raise SecureFileError("workspace root identity is unavailable") from error
        _assert_not_reparse(current, label="workspace root")
        if not stat.S_ISDIR(current.st_mode) or not os.path.samestat(
            self._root_metadata,
            current,
        ):
            raise SecureFileError("workspace root identity changed")

    @contextmanager
    def open_binary(
        self,
        relative: str,
        *,
        write_attributes: bool = False,
        deny_mutation: bool = False,
    ) -> Iterator[BinaryIO]:
        """Open a regular file relative to this root without following links."""

        self._assert_open()
        self.assert_root_identity()
        parts = _relative_parts(relative)
        expected = self.path.joinpath(*parts)
        if os.name == "nt":
            with self._windows_parent(parts, create=False):
                self.assert_root_identity()
                fd = _open_windows_fd(
                    expected,
                    self.path,
                    write_attributes=write_attributes,
                    deny_mutation=deny_mutation,
                )
        else:
            assert self._posix_root_fd is not None
            fd = _open_posix_fd(self._posix_root_fd, parts)
        try:
            stream = os.fdopen(fd, "rb", closefd=True)
        except BaseException:
            os.close(fd)
            raise
        try:
            metadata = os.fstat(stream.fileno())
            _assert_regular_single_link(metadata)
            if os.name == "nt":
                _assert_windows_handle_path(stream.fileno(), expected, root=self.path)
            yield stream
        finally:
            stream.close()

    def assert_path_identity(self, relative: str, expected: os.stat_result) -> None:
        with self.open_binary(relative) as stream:
            current = os.fstat(stream.fileno())
            if not os.path.samestat(expected, current):
                raise SecureFileError("workspace path identity changed")

    def set_mode(self, relative: str, mode: int) -> None:
        if mode < 0 or mode > 0o777:
            raise SecureFileError("workspace file mode is invalid")
        with self.open_binary(relative, write_attributes=True) as stream:
            if os.name == "nt":
                _set_windows_mode(stream.fileno(), mode)
            else:
                os.fchmod(stream.fileno(), mode)
        with self.open_binary(relative) as stream:
            restored = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
        if restored != mode:
            raise SecureFileError("workspace file mode could not be restored")

    def unlink_regular(self, relative: str, *, missing_ok: bool = True) -> bool:
        """Delete one exact regular file without following a replaced parent."""

        parts = _relative_parts(relative)
        if os.name == "nt":
            return self._unlink_windows(parts, missing_ok=missing_ok)
        return self._unlink_posix(parts, missing_ok=missing_ok)

    def replace_regular(self, relative: str, content: bytes, *, mode: int) -> None:
        """Atomically replace one file through a parent bound to this root."""

        if mode < 0 or mode > 0o777:
            raise SecureFileError("workspace file mode is invalid")
        parts = _relative_parts(relative)
        if os.name == "nt":
            self._replace_windows(parts, content, mode=mode)
        else:
            self._replace_posix(parts, content, mode=mode)

    def remove_directory(self, relative: str, *, missing_ok: bool = True) -> bool:
        """Remove one exact empty directory without following a directory link."""

        parts = _relative_parts(relative)
        if os.name == "nt":
            with self._windows_parent(parts, create=False):
                target = self.path.joinpath(*parts)
                try:
                    metadata = os.lstat(target)
                except FileNotFoundError:
                    if missing_ok:
                        return False
                    raise SecureFileError("workspace directory is absent") from None
                _assert_not_reparse(metadata, label=relative)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise SecureFileError("workspace cleanup path is not a directory")
                try:
                    target.rmdir()
                except OSError as error:
                    raise SecureFileError("workspace directory removal failed") from error
                return True
        with self._posix_parent(parts, create=False) as (parent_fd, name):
            try:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise SecureFileError("workspace directory is absent") from None
            _assert_not_reparse(metadata, label=relative)
            if not stat.S_ISDIR(metadata.st_mode):
                raise SecureFileError("workspace cleanup path is not a directory")
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError as error:
                raise SecureFileError("workspace directory removal failed") from error
            return True

    def _assert_open(self) -> None:
        if self._closed:
            raise SecureFileError("workspace root handle is closed")

    @contextmanager
    def _posix_parent(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> Iterator[tuple[int, str]]:
        self._assert_open()
        self.assert_root_identity()
        assert self._posix_root_fd is not None
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if nofollow is None or directory is None:
            raise SecureFileError("platform lacks no-follow directory handle support")
        flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        current = os.dup(self._posix_root_fd)
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, flags, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise SecureFileError("workspace parent directory is absent") from None
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    child = os.open(part, flags, dir_fd=current)
                except OSError as error:
                    raise SecureFileError("no-follow workspace parent open failed") from error
                os.close(current)
                current = child
            yield current, parts[-1]
        finally:
            os.close(current)

    @contextmanager
    def _windows_parent(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> Iterator[Path]:
        self._assert_open()
        handles: list[int] = []
        current = self.path
        try:
            handles.append(
                _open_windows_directory_handle(
                    self.path,
                    deny_delete=True,
                )
            )
            self.assert_root_identity()
            for part in parts[:-1]:
                child = current / part
                try:
                    handle = _open_windows_directory_handle(
                        child,
                        deny_delete=True,
                    )
                except FileNotFoundError:
                    if not create:
                        raise SecureFileError("workspace parent directory is absent") from None
                    with suppress(FileExistsError):
                        os.mkdir(child, mode=0o700)
                    handle = _open_windows_directory_handle(
                        child,
                        deny_delete=True,
                    )
                handles.append(handle)
                current = child
            yield current
        finally:
            for handle in reversed(handles):
                _close_windows_handle(handle)

    def _unlink_posix(self, parts: tuple[str, ...], *, missing_ok: bool) -> bool:
        with self._posix_parent(parts, create=False) as (parent_fd, name):
            try:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise SecureFileError("workspace file is absent") from None
            _assert_regular_single_link(metadata)
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError as error:
                raise SecureFileError("workspace file removal failed") from error
            return True

    def _unlink_windows(self, parts: tuple[str, ...], *, missing_ok: bool) -> bool:
        with self._windows_parent(parts, create=False):
            target = self.path.joinpath(*parts)
            try:
                metadata = os.lstat(target)
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise SecureFileError("workspace file is absent") from None
            _assert_regular_single_link(metadata)
            try:
                target.unlink()
            except OSError as error:
                raise SecureFileError("workspace file removal failed") from error
            return True

    def _replace_posix(self, parts: tuple[str, ...], content: bytes, *, mode: int) -> None:
        with self._posix_parent(parts, create=True) as (parent_fd, name):
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                _assert_regular_single_link(existing)
            temporary = f".{name}.agent-hub-{uuid.uuid4().hex}.tmp"
            descriptor = -1
            try:
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
                descriptor = os.open(temporary, flags, mode, dir_fd=parent_fd)
                _write_all(descriptor, content)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                os.replace(
                    temporary,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                _verify_posix_file(parent_fd, name, content=content, mode=mode)
            except OSError as error:
                raise SecureFileError("workspace preimage replacement failed") from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent_fd)

    def _replace_windows(self, parts: tuple[str, ...], content: bytes, *, mode: int) -> None:
        with self._windows_parent(parts, create=True) as parent:
            target = self.path.joinpath(*parts)
            if target.exists() or target.is_symlink():
                fd = _open_windows_fd(target, self.path, write_attributes=True)
                try:
                    _assert_regular_single_link(os.fstat(fd))
                    _set_windows_mode(fd, mode | stat.S_IWUSR)
                finally:
                    os.close(fd)
            temporary = parent / f".{parts[-1]}.agent-hub-{uuid.uuid4().hex}.tmp"
            descriptor = -1
            renamed = False
            try:
                descriptor = _create_windows_temporary_fd(temporary, root=self.path)
                _write_all(descriptor, content)
                _set_windows_mode(descriptor, mode)
                os.fsync(descriptor)
                temporary_identity = _windows_file_identity(descriptor)
                _rename_windows_handle(
                    descriptor,
                    destination=target,
                    replace=True,
                )
                renamed = True

                if _windows_file_identity(descriptor) != temporary_identity:
                    raise SecureFileError("workspace preimage identity changed during replacement")
                fd = _open_windows_fd(target, self.path, write_attributes=False)
                try:
                    reopened_identity = _windows_file_identity(fd)
                    if reopened_identity != temporary_identity:
                        raise SecureFileError(
                            "workspace preimage replacement identity mismatch: "
                            f"expected {temporary_identity!r}, got {reopened_identity!r}"
                        )
                    with os.fdopen(fd, "rb", closefd=True) as stream:
                        restored = stream.read()
                        restored_mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
                    fd = -1
                finally:
                    if fd >= 0:
                        os.close(fd)
                if restored != content or restored_mode != mode:
                    raise SecureFileError("workspace preimage verification failed")
            except OSError as error:
                if isinstance(error, SecureFileError):
                    raise
                raise SecureFileError("workspace preimage replacement failed") from error
            finally:
                if descriptor >= 0:
                    if not renamed:
                        with suppress(SecureFileError, OSError):
                            _mark_windows_handle_for_delete(descriptor)
                    os.close(descriptor)


@contextmanager
def open_verified_binary(
    root: Path | SecureWorkspaceRoot,
    relative: str,
    *,
    write_attributes: bool = False,
) -> Iterator[BinaryIO]:
    """Open a repo-relative regular file without following a path escape."""

    if isinstance(root, SecureWorkspaceRoot):
        with root.open_binary(relative, write_attributes=write_attributes) as stream:
            yield stream
        return
    with (
        SecureWorkspaceRoot(root) as secure_root,
        secure_root.open_binary(relative, write_attributes=write_attributes) as stream,
    ):
        yield stream


def assert_path_identity(
    root: Path | SecureWorkspaceRoot,
    relative: str,
    expected: os.stat_result,
) -> None:
    """Require the current path to resolve to the same safe file identity."""

    if isinstance(root, SecureWorkspaceRoot):
        root.assert_path_identity(relative, expected)
        return
    with SecureWorkspaceRoot(root) as secure_root:
        secure_root.assert_path_identity(relative, expected)


def set_verified_mode(
    root: Path | SecureWorkspaceRoot,
    relative: str,
    mode: int,
) -> None:
    """Restore mode bits through a verified file handle."""

    if isinstance(root, SecureWorkspaceRoot):
        root.set_mode(relative, mode)
        return
    with SecureWorkspaceRoot(root) as secure_root:
        secure_root.set_mode(relative, mode)


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


def _assert_not_reparse(metadata: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise SecureFileError(f"{label} is a symbolic link")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(metadata, "st_file_attributes", 0) & reparse_flag:
        raise SecureFileError(f"{label} is a reparse point")


def _open_posix_root_fd(root: Path, *, expected: os.stat_result) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None or os.open not in os.supports_dir_fd:
        raise SecureFileError("platform lacks no-follow directory handle support")
    flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as error:
        raise SecureFileError("no-follow workspace root open failed") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SecureFileError("workspace root handle is not a directory")
        _assert_not_reparse(metadata, label="workspace root")
        if not os.path.samestat(expected, metadata):
            raise SecureFileError("workspace root identity changed during open")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_posix_fd(root_fd: int, parts: tuple[str, ...]) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None or os.open not in os.supports_dir_fd:
        raise SecureFileError("platform lacks no-follow directory handle support")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | directory | nofollow | cloexec
    file_flags = os.O_RDONLY | nofollow | cloexec
    descriptors: list[int] = []
    try:
        current = os.dup(root_fd)
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


def _open_windows_directory_handle(
    path: Path,
    *,
    deny_delete: bool = False,
) -> int:
    import ctypes
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
    desired_access = 0x00000080  # FILE_READ_ATTRIBUTES
    share_mode = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | WRITE | DELETE
    if deny_delete:
        desired_access |= 0x00010000  # DELETE
        share_mode &= ~0x4
    handle = create_file(
        _extended_windows_path(path),
        desired_access,
        share_mode,
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error_code = ctypes.get_last_error()
        if error_code in {2, 3}:
            raise FileNotFoundError(error_code, "workspace directory is absent", path)
        raise SecureFileError("no-follow Windows workspace directory open failed") from (
            ctypes.WinError(error_code)
        )
    native = int(handle)
    try:
        _assert_windows_directory_handle(native, path)
    except BaseException:
        _close_windows_handle(native)
        raise
    return native


def _assert_windows_directory_handle(handle: int, expected: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    get_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_info.restype = wintypes.BOOL
    info = FileAttributeTagInfo()
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise SecureFileError("failed to inspect Windows directory handle") from ctypes.WinError(
            ctypes.get_last_error()
        )
    if not info.FileAttributes & 0x10:
        raise SecureFileError("workspace parent handle is not a directory")
    if info.FileAttributes & 0x400:
        raise SecureFileError("workspace parent handle is a reparse point")
    if _windows_path_key(Path(_windows_final_path_from_handle(handle))) != _windows_path_key(
        expected
    ):
        raise SecureFileError("Windows directory handle resolved outside its expected path")


def _close_windows_handle(handle: int) -> None:
    import ctypes

    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def _create_windows_temporary_fd(path: Path, *, root: Path) -> int:
    """Create a private temporary file and keep its native handle identity pinned."""

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
    desired_access = 0x80000000 | 0x40000000 | 0x00010000  # READ | WRITE | DELETE
    share_mode = 0x1 | 0x2  # keep delete/rename exclusive while the handle is open
    flags = 0x00200000 | 0x08000000  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
    handle = create_file(
        _extended_windows_path(path),
        desired_access,
        share_mode,
        None,
        1,  # CREATE_NEW
        flags,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error_code = ctypes.get_last_error()
        if error_code in {2, 3, 80}:
            raise FileExistsError(error_code, "temporary workspace file already exists", path)
        raise SecureFileError(
            "Windows temporary workspace file creation failed"
        ) from ctypes.WinError(error_code)
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    try:
        _assert_windows_handle_path(descriptor, path, root=root)
        _assert_regular_single_link(os.fstat(descriptor))
    except BaseException:
        with suppress(SecureFileError, OSError):
            _mark_windows_handle_for_delete(descriptor)
        os.close(descriptor)
        raise
    return descriptor


def _windows_file_identity(fd: int) -> tuple[int, int, int]:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("CreationTime", wintypes.FILETIME),
            ("LastAccessTime", wintypes.FILETIME),
            ("LastWriteTime", wintypes.FILETIME),
            ("VolumeSerialNumber", wintypes.DWORD),
            ("FileSizeHigh", wintypes.DWORD),
            ("FileSizeLow", wintypes.DWORD),
            ("NumberOfLinks", wintypes.DWORD),
            ("FileIndexHigh", wintypes.DWORD),
            ("FileIndexLow", wintypes.DWORD),
        ]

    get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation))
    get_info.restype = wintypes.BOOL
    info = ByHandleFileInformation()
    handle = msvcrt.get_osfhandle(fd)
    if not get_info(handle, ctypes.byref(info)):
        raise SecureFileError("failed to inspect Windows file identity") from ctypes.WinError(
            ctypes.get_last_error()
        )
    return info.VolumeSerialNumber, info.FileIndexHigh, info.FileIndexLow


def _rename_windows_handle(
    fd: int,
    *,
    destination: Path,
    replace: bool,
) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOL),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    filename = os.path.abspath(destination).encode("utf-16-le")
    header = FileRenameInfo()
    header.ReplaceIfExists = bool(replace)
    header.RootDirectory = None
    header.FileNameLength = len(filename)
    size = FileRenameInfo.FileName.offset + len(filename) + ctypes.sizeof(wintypes.WCHAR)
    buffer = (ctypes.c_byte * size)()
    ctypes.memmove(buffer, ctypes.byref(header), FileRenameInfo.FileName.offset)
    ctypes.memmove(
        ctypes.addressof(buffer) + FileRenameInfo.FileName.offset, filename, len(filename)
    )

    set_info = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    set_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    set_info.restype = wintypes.BOOL
    if not set_info(
        msvcrt.get_osfhandle(fd),
        3,  # FileRenameInfo
        ctypes.byref(buffer),
        size,
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        raise SecureFileError(
            f"Windows handle-based workspace replacement failed: {error}"
        ) from error


def _mark_windows_handle_for_delete(fd: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    info = FileDispositionInfo(True)
    set_info = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    set_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    set_info.restype = wintypes.BOOL
    if not set_info(
        msvcrt.get_osfhandle(fd),
        4,  # FileDispositionInfo
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise SecureFileError(
            "failed to remove temporary Windows workspace file"
        ) from ctypes.WinError(ctypes.get_last_error())


def _open_windows_fd(
    path: Path,
    root: Path,
    *,
    write_attributes: bool,
    deny_mutation: bool = False,
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
    share_mode = 0x1 if deny_mutation else 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
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
    import msvcrt

    return _windows_final_path_from_handle(msvcrt.get_osfhandle(fd))


def _windows_final_path_from_handle(handle: int) -> str:
    import ctypes
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


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise SecureFileError("workspace preimage write made no progress")
        offset += written


def _verify_posix_file(
    parent_fd: int,
    name: str,
    *,
    content: bytes,
    mode: int,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        metadata = os.fstat(descriptor)
        _assert_regular_single_link(metadata)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        if b"".join(chunks) != content:
            raise SecureFileError("workspace preimage content verification failed")
        if stat.S_IMODE(metadata.st_mode) != mode:
            raise SecureFileError("workspace preimage mode verification failed")
    finally:
        os.close(descriptor)


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
    "SecureWorkspaceRoot",
    "assert_path_identity",
    "open_verified_binary",
    "set_verified_mode",
]
