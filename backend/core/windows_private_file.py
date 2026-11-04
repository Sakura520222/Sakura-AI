"""Create credential handoff files with a protected process-user DACL on Windows."""

import ctypes
import msvcrt
import os
import tempfile
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

from loguru import logger

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("descriptor", ctypes.c_void_p),
        ("inherit_handle", wintypes.BOOL),
    ]


class _TokenUser(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(_SecurityAttributes),
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.GetVolumeInformationByHandleW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR,
    wintypes.DWORD,
]
_kernel32.GetVolumeInformationByHandleW.restype = wintypes.BOOL
_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.GetTokenInformation.restype = wintypes.BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.LPWSTR),
]
_advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
]
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

# Win32 protocol values (not configurable application policy).
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_CREATE_NEW = 1
_GENERIC_READ_WRITE = 0xC0000000
_FILE_ATTRIBUTE_NORMAL = 0x0080
_FILE_PERSISTENT_ACLS = 0x0008
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _process_user_sid() -> str:
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    sid_string = wintypes.LPWSTR()
    try:
        size = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(
            token, _TOKEN_USER, buffer, size, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.sid
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        return sid_string.value
    finally:
        _kernel32.LocalFree(sid_string)
        _kernel32.CloseHandle(token)


def create_private_tempfile(directory: Path, *, prefix: str) -> tuple[int, str]:
    """Return a CRT fd for an exclusively created, non-inheritable private file.

    Apply the owner and protected DACL at creation, before any bytes or readable
    handle can escape. Reject filesystems without persistent ACL support.
    """
    sid = _process_user_sid()
    descriptor = ctypes.c_void_p()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"O:{sid}D:P(A;;FA;;;{sid})", 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes), descriptor, False
        )
        for _ in range(tempfile.TMP_MAX):
            path = Path(os.path.abspath(directory)) / f"{prefix}{uuid4().hex}"
            name = str(path)
            if not name.startswith("\\\\?\\"):
                name = (
                    ("\\\\?\\UNC\\" + name[2:])
                    if name.startswith("\\\\")
                    else "\\\\?\\" + name
                )
            handle = _kernel32.CreateFileW(
                name,
                _GENERIC_READ_WRITE,
                0,
                ctypes.byref(attributes),
                _CREATE_NEW,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle == _INVALID_HANDLE:
                error = ctypes.WinError(ctypes.get_last_error())
                if isinstance(error, FileExistsError):
                    continue
                raise error
            try:
                flags = wintypes.DWORD()
                if not _kernel32.GetVolumeInformationByHandleW(
                    handle, None, 0, None, None, ctypes.byref(flags), None, 0
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not flags.value & _FILE_PERSISTENT_ACLS:
                    raise OSError(
                        "Setup token requires a filesystem with Windows ACL support"
                    )
                fd = msvcrt.open_osfhandle(
                    handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT
                )
            except BaseException:
                _kernel32.CloseHandle(handle)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("无法清理空的 Setup Token 临时文件 {}", path)
                raise
            # Ownership transfers to the CRT fd; os.fdopen/os.close closes it.
            return fd, str(path)
        raise FileExistsError("Could not allocate a private setup token file")
    finally:
        _kernel32.LocalFree(descriptor)
