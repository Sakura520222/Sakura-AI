"""Native Windows workspace handles and locks outside repository content.

Directory handles pin admitted nodes; LockFileEx coordinates processes without
interfering with Git's directory access. File reads check the final handle path
before accessing content. Lock files are keyed by the native directory identity.
"""

from __future__ import annotations

import ctypes
import errno
import msvcrt
import os
import stat
from contextlib import ExitStack
from ctypes import wintypes
from pathlib import Path

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.SHGetFolderPathW.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
]
_shell32.SHGetFolderPathW.restype = ctypes.c_long
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
]
_kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("access_time", wintypes.FILETIME),
        ("write_time", wintypes.FILETIME),
        ("volume_serial", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


_kernel32.GetFileInformationByHandle.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_FileInformation),
]
_kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
_kernel32.GetFileInformationByHandleEx.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]
_kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", wintypes.DWORD),
        ("offset_high", wintypes.DWORD),
        ("event", wintypes.HANDLE),
    ]


_kernel32.LockFileEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
]
_kernel32.LockFileEx.restype = wintypes.BOOL
_kernel32.UnlockFileEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, ctypes.POINTER(_Overlapped),
]
_kernel32.UnlockFileEx.restype = wintypes.BOOL


class _DirectoryInformation(ctypes.Structure):
    """FILE_ID_BOTH_DIR_INFO header; variable-length FileName follows it."""

    _fields_ = [
        ("next_offset", wintypes.DWORD),
        ("index", wintypes.DWORD),
        ("creation_time", ctypes.c_longlong),
        ("access_time", ctypes.c_longlong),
        ("write_time", ctypes.c_longlong),
        ("change_time", ctypes.c_longlong),
        ("end_of_file", ctypes.c_longlong),
        ("allocation_size", ctypes.c_longlong),
        ("attributes", wintypes.DWORD),
        ("name_length", wintypes.DWORD),
        ("ea_size", wintypes.DWORD),
        ("short_name_length", ctypes.c_ubyte),
        ("short_name", wintypes.WCHAR * 12),
        ("file_id", ctypes.c_longlong),
    ]

# Win32 protocol constants: directory data access participates in sharing;
# FILE_READ_ATTRIBUTES/FILE_WRITE_ATTRIBUTES deliberately do not.
_FILE_LIST_DIRECTORY = 0x0001
_FILE_SHARE_READ = 0x0001
_FILE_SHARE_WRITE = 0x0002
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _extended_path(path: str | Path) -> str:
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _handle_path(handle: int) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    length = _kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return os.path.normcase(buffer.value)


def close_handle(handle: int) -> None:
    if not _kernel32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _open(
    path: str | Path, access: int, sharing: int, *, directory: bool,
    creation: int = _OPEN_EXISTING,
) -> int:
    expected = _extended_path(path)
    handle = _kernel32.CreateFileW(
        expected,
        access,
        sharing,
        None,
        creation,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _FileInformation()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or _handle_path(handle) != os.path.normcase(expected)
        ):
            raise OSError(errno.ELOOP, "Workspace reparse points are not admitted")
        if bool(info.attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory:
            if directory:
                raise NotADirectoryError(errno.ENOTDIR, "Workspace directory required")
            raise IsADirectoryError(errno.EISDIR, "Workspace file required")
        if not directory and info.links != 1:
            raise ValueError("Workspace reads require a regular file without hard links")
    except BaseException:
        close_handle(handle)
        raise
    return handle


def open_directory(path: str | Path) -> int:
    return _open(path, _FILE_LIST_DIRECTORY, _FILE_SHARE_READ | _FILE_SHARE_WRITE, directory=True)


def _workspace_lock_root() -> Path:
    # Ask Windows for the current account's known folder, so differing TMP/TEMP
    # variables in worker processes cannot split a workspace's lock domain.
    buffer = ctypes.create_unicode_buffer(32768)
    result = _shell32.SHGetFolderPathW(None, 0x001C, None, 0, buffer)  # CSIDL_LOCAL_APPDATA
    if result != 0:
        raise OSError(f"Windows local app-data lookup failed (HRESULT {result:#x})")
    return Path(buffer.value) / "SakuraAI" / "workspace-locks"


def acquire_workspace_lock(path: str | Path, *, shared: bool) -> ExitStack:
    """Nonblocking shared/exclusive lock with stable workspace and lock identity.

    LockFileEx releases locks on process exit. The user-private OS cache directory
    holds persistent identity files; neither these files nor their directory
    can be deleted while open. No lock file is placed in Agent-writable content.
    """
    with ExitStack() as stack:
        directory = open_directory(path)
        stack.callback(close_handle, directory)
        info = _FileInformation()
        if not _kernel32.GetFileInformationByHandle(directory, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        identity = f"{info.volume_serial:08x}-{info.index_high:08x}{info.index_low:08x}"
        lock_root = _workspace_lock_root()
        lock_root.mkdir(parents=True, exist_ok=True)
        # Packaged Windows apps can redirect LocalAppData transparently.
        # Canonicalize this OS-owned cache location before handle admission.
        lock_root = lock_root.resolve(strict=True)
        lock_directory = open_directory(lock_root)
        stack.callback(close_handle, lock_directory)
        handle = _open(
            lock_root / f"{identity}.lock", _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE, directory=False, creation=_OPEN_ALWAYS,
        )
        stack.callback(close_handle, handle)
        overlap = _Overlapped()
        # LOCKFILE_FAIL_IMMEDIATELY; add LOCKFILE_EXCLUSIVE_LOCK for writers.
        if not _kernel32.LockFileEx(handle, 1 if shared else 3, 0, 1, 0, ctypes.byref(overlap)):
            error = ctypes.get_last_error()
            if error == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError(errno.EAGAIN, "Workspace lock is held")
            raise ctypes.WinError(error)

        def unlock() -> None:
            if not _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap)):
                raise ctypes.WinError(ctypes.get_last_error())

        stack.callback(unlock)
        return stack.pop_all()


def _open_descriptor(root: Path, relative: Path, *, directory: bool) -> int:
    """Admit all parents, then transfer the final handle to a CRT descriptor."""
    if any(":" in part for part in relative.parts):
        raise ValueError("Workspace reads do not admit alternate data streams")
    with ExitStack() as stack:
        current = root
        for part in (None, *relative.parts[:-1]):
            if part is not None:
                current /= part
            handle = open_directory(current)
            stack.callback(close_handle, handle)
        target = root / relative
        handle = _open(
            target, _FILE_LIST_DIRECTORY if directory else _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE, directory=directory,
        )
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close_handle(handle)
            raise
        try:
            info = os.fstat(descriptor)
            if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise ValueError("Workspace reads require a regular file without hard links")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor


def open_workspace_file(root: Path, relative: Path) -> int:
    return _open_descriptor(root, relative, directory=False)


def open_workspace_directory(root: Path, relative: Path) -> int:
    return _open_descriptor(root, relative, directory=True)


def read_workspace_bytes(root: Path, relative: Path) -> tuple[bytes, float]:
    with os.fdopen(open_workspace_file(root, relative), "rb") as stream:
        info = os.fstat(stream.fileno())
        return stream.read(), info.st_mtime


def directory_entries(descriptor: int) -> list[tuple[str, bool]]:
    """Enumerate the admitted directory handle, without reopening its path."""
    handle = msvcrt.get_osfhandle(descriptor)
    # A buffer size for each native batch, not a limit on repository entries.
    buffer = ctypes.create_string_buffer(65536)
    information_class = 11  # FileIdBothDirectoryRestartInfo
    entries = []
    while True:
        if not _kernel32.GetFileInformationByHandleEx(
            handle, information_class, buffer, len(buffer)
        ):
            error = ctypes.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES
                break
            raise ctypes.WinError(error)
        information_class = 10  # FileIdBothDirectoryInfo
        offset = 0
        while True:
            info = _DirectoryInformation.from_buffer(buffer, offset)
            name = ctypes.wstring_at(
                ctypes.addressof(buffer) + offset + ctypes.sizeof(_DirectoryInformation),
                info.name_length // ctypes.sizeof(wintypes.WCHAR),
            )
            if name not in {".", ".."} and not info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                entries.append((name, bool(info.attributes & _FILE_ATTRIBUTE_DIRECTORY)))
            if not info.next_offset:
                break
            offset += info.next_offset
    return sorted(entries)
