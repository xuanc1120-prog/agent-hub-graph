"""Bind CLI execution to the executable object verified at registration."""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import os
import stat
import sys
from pathlib import Path

from adapters.cli.spec import (
    CliAgentSpec,
    CliSpecError,
    FileIdentity,
    _assert_no_reparse_chain,
    _capture_ancestor_binding,
    _open_nofollow,
)

_READ_CHUNK = 65_536

_GENERIC_READ = 0x80000000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Stable Linux UAPI values. Some standalone CPython builds support the syscalls
# but do not export every symbolic constant from ``os``/``fcntl``.
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_F_SEAL_SEAL = 0x0001
_F_SEAL_SHRINK = 0x0002
_F_SEAL_GROW = 0x0004
_F_SEAL_WRITE = 0x0008
_MFD_CLOEXEC = 0x0001
_MFD_ALLOW_SEALING = 0x0002


def _matches_spec(spec: CliAgentSpec, current: FileIdentity) -> bool:
    bound = spec.identity
    return (
        isinstance(bound, FileIdentity)
        and current.sha256 == spec.expected_sha256
        and current.dev == bound.dev
        and current.ino == bound.ino
        and current.size == bound.size
    )


def _read_fd_digest(fd: int) -> tuple[str, int]:
    """Hash all bytes currently frozen in ``fd`` and return hash plus size."""

    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(fd, _READ_CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _create_linux_memfd() -> int:
    """Create a sealable memfd using Python or the glibc compatibility API."""

    if not sys.platform.startswith("linux"):
        raise CliSpecError("immutable memfd execution sealing is unavailable")
    flags = getattr(os, "MFD_CLOEXEC", _MFD_CLOEXEC) | getattr(
        os, "MFD_ALLOW_SEALING", _MFD_ALLOW_SEALING
    )
    creator = getattr(os, "memfd_create", None)
    if creator is not None:
        return creator("agent-hub-cli", flags=flags)

    libc = ctypes.CDLL(None, use_errno=True)
    creator = getattr(libc, "memfd_create", None)
    if creator is None:
        raise CliSpecError("immutable memfd execution sealing is unavailable")
    creator.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    creator.restype = ctypes.c_int
    fd = creator(b"agent-hub-cli", flags)
    if fd < 0:
        error = ctypes.get_errno()
        raise CliSpecError(f"memfd_create failed: errno={error}")
    return int(fd)


def _copy_to_sealed_memfd(source_fd: int, spec: CliAgentSpec) -> int:
    """Copy verified bytes into an immutable Linux memfd.

    A normal read-only fd binds only the inode; another process with write
    access can still truncate that inode in place. The digest is therefore
    calculated from the exact bytes copied into the private memfd, and the
    memfd is sealed against every content mutation before it can be executed.
    """

    try:
        import fcntl

        add_seals = getattr(fcntl, "F_ADD_SEALS", _F_ADD_SEALS)
        get_seals = getattr(fcntl, "F_GET_SEALS", _F_GET_SEALS)
        required_seals = (
            getattr(fcntl, "F_SEAL_WRITE", _F_SEAL_WRITE)
            | getattr(fcntl, "F_SEAL_GROW", _F_SEAL_GROW)
            | getattr(fcntl, "F_SEAL_SHRINK", _F_SEAL_SHRINK)
            | getattr(fcntl, "F_SEAL_SEAL", _F_SEAL_SEAL)
        )
    except ImportError as exc:
        raise CliSpecError("immutable memfd execution sealing is unavailable") from exc

    sealed_fd = _create_linux_memfd()
    try:
        initial = os.fstat(source_fd)
        if not stat.S_ISREG(initial.st_mode):
            raise CliSpecError("executable is not a regular file")
        os.lseek(source_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, _READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(sealed_fd, remaining)
                if written <= 0:
                    raise CliSpecError("short write while sealing executable")
                remaining = remaining[written:]

        final = os.fstat(source_fd)
        if (initial.st_dev, initial.st_ino) != (final.st_dev, final.st_ino):
            raise CliSpecError("executable identity changed while sealing")
        current = FileIdentity(digest.hexdigest(), initial.st_dev, initial.st_ino, copied)
        if final.st_size != copied or not _matches_spec(spec, current):
            raise CliSpecError("executable content drifted while sealing")

        os.fchmod(sealed_fd, 0o500)
        fcntl.fcntl(sealed_fd, add_seals, required_seals)
        applied = fcntl.fcntl(sealed_fd, get_seals)
        if applied & required_seals != required_seals:
            raise CliSpecError("executable memfd seal verification failed")

        # This is the authoritative content check. It must happen after the
        # write/grow/shrink seals are active: before F_ADD_SEALS another
        # same-UID process can reopen /proc/<pid>/fd/<n> and mutate memfd.
        sealed_sha256, sealed_size = _read_fd_digest(sealed_fd)
        sealed_stat = os.fstat(sealed_fd)
        bound = spec.identity
        if (
            not isinstance(bound, FileIdentity)
            or sealed_sha256 != spec.expected_sha256
            or sealed_size != bound.size
            or sealed_stat.st_size != sealed_size
        ):
            raise CliSpecError("sealed executable copy does not match registered content")
        os.lseek(sealed_fd, 0, os.SEEK_SET)
        return sealed_fd
    except BaseException:
        os.close(sealed_fd)
        raise


def _win_api() -> object:
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    api.CreateFileW.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    api.QueryFullProcessImageNameW.restype = ctypes.c_int
    return api


def _win_open_locked(api: object, path: Path, *, directory: bool) -> int:
    access = _FILE_READ_ATTRIBUTES if directory else _GENERIC_READ | _FILE_READ_ATTRIBUTES
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    handle = api.CreateFileW(
        str(path),
        access,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if not handle or int(handle) == invalid:
        error = ctypes.get_last_error()
        raise CliSpecError(f"cannot seal executable path: winerror={error}")
    return int(handle)


def _win_process_image(pid: int) -> str:
    api = _win_api()
    process = api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        raise CliSpecError(f"cannot inspect spawned process: winerror={ctypes.get_last_error()}")
    try:
        capacity = 32_768
        buffer = ctypes.create_unicode_buffer(capacity)
        length = ctypes.c_uint32(capacity)
        if not api.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
            raise CliSpecError(
                f"cannot inspect spawned process image: winerror={ctypes.get_last_error()}"
            )
        return buffer.value
    finally:
        api.CloseHandle(process)


class ExecutionSeal:
    """Short-lived object binding spawn to a verified executable object.

    Linux executes an immutable, sealed memfd copy through an inherited
    descriptor path. Other POSIX platforms fail closed when equivalent kernel
    sealing is unavailable. Windows holds non-share-delete/non-share-write
    handles for the executable and every renameable ancestor until the
    suspended process image has been checked.
    """

    def __init__(
        self,
        spec: CliAgentSpec,
        *,
        spawn_executable: str,
        fd: int | None = None,
        windows_handles: tuple[int, ...] = (),
    ) -> None:
        self._spec = spec
        self.spawn_executable = spawn_executable
        self.pass_fds = (fd,) if fd is not None else ()
        self._fd = fd
        self._windows_handles = windows_handles
        self._closed = False

    @classmethod
    def acquire(cls, spec: CliAgentSpec) -> ExecutionSeal:
        """Acquire the platform seal and revalidate identity while held."""

        path = Path(spec.executable)
        _assert_no_reparse_chain(path)
        if sys.platform.startswith("win"):
            return cls._acquire_windows(spec, path)
        return cls._acquire_posix(spec, path)

    @classmethod
    def _acquire_posix(cls, spec: CliAgentSpec, path: Path) -> ExecutionSeal:
        try:
            source_fd = _open_nofollow(path)
        except OSError as exc:
            raise CliSpecError(f"cannot seal executable: errno={exc.errno}") from exc
        sealed_fd: int | None = None
        try:
            sealed_fd = _copy_to_sealed_memfd(source_fd, spec)
            if tuple(spec.ancestor_binding or ()) != _capture_ancestor_binding(path):
                raise CliSpecError("executable ancestor identity drifted before sealing")
            if Path("/proc/self/fd").is_dir():
                spawn_path = f"/proc/self/fd/{sealed_fd}"
            else:
                raise CliSpecError("fd-bound execution is unavailable")
            return cls(spec, spawn_executable=spawn_path, fd=sealed_fd)
        except BaseException:
            if sealed_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(sealed_fd)
            raise
        finally:
            os.close(source_fd)

    @classmethod
    def _acquire_windows(cls, spec: CliAgentSpec, path: Path) -> ExecutionSeal:
        api = _win_api()
        handles: list[int] = []
        try:
            # Lock outer ancestors first, excluding the volume root which is
            # not renameable. This prevents replacing a validated parent with
            # a junction while the child process is being created.
            ancestors = [parent for parent in path.parents if parent.parent != parent]
            for parent in reversed(ancestors):
                handles.append(_win_open_locked(api, parent, directory=True))
            handles.append(_win_open_locked(api, path, directory=False))
            if not spec.verify_identity():
                raise CliSpecError("executable identity drifted before sealing")
            return cls(
                spec,
                spawn_executable=spec.executable,
                windows_handles=tuple(handles),
            )
        except BaseException:
            for handle in reversed(handles):
                api.CloseHandle(handle)
            raise

    def path_still_bound(self) -> bool:
        """Detect path replacement before spawn; the seal remains authoritative."""

        return not self._closed and self._spec.verify_identity()

    def verify_spawned_process(self, pid: int) -> bool:
        """On Windows verify the suspended image before it can execute."""

        if self._closed:
            return False
        if not sys.platform.startswith("win"):
            return True
        try:
            image = _win_process_image(pid)
        except CliSpecError:
            return False
        expected = os.path.normcase(os.path.realpath(self._spec.executable))
        actual = os.path.normcase(os.path.realpath(image))
        return actual == expected and self._spec.verify_identity()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._windows_handles:
            api = _win_api()
            for handle in reversed(self._windows_handles):
                api.CloseHandle(handle)
            self._windows_handles = ()

    def __enter__(self) -> ExecutionSeal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["ExecutionSeal"]
