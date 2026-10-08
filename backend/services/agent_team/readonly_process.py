"""Linux-only child launcher for fixed read-only inspection programs.

Invoked with isolated Python (-I); never imported from the task checkout.
Landlock denies persistent filesystem writes and restricts executable entry
points to the selected binary and the system ELF loader. Seccomp
closes Landlock's metadata/IPC gaps using an allowlist (no network, chmod,
utime, ioctl, ptrace, io_uring, or mount). This is write/execute isolation,
not a replacement for the Docker backend's filesystem confidentiality boundary.
Unsupported kernels/libraries fail closed before executing the requested tool.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import sys


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _layout_ = "ms"
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


class _ArgComparison(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


# All other native and alternate-ABI syscalls are denied by libseccomp.
# open/openat are safe only in conjunction with the enforced Landlock domain.
_READ_SYSCALLS = (
    "read",
    "write",
    "readv",
    "writev",
    "pread64",
    "close",
    "close_range",
    "open",
    "openat",
    "openat2",
    "stat",
    "lstat",
    "fstat",
    "newfstatat",
    "statx",
    "statfs",
    "fstatfs",
    "access",
    "faccessat",
    "faccessat2",
    "readlink",
    "readlinkat",
    "getdents",
    "getdents64",
    "lseek",
    "mmap",
    "mprotect",
    "munmap",
    "mremap",
    "madvise",
    "brk",
    "arch_prctl",
    "set_tid_address",
    "set_robust_list",
    "rseq",
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    "sigaltstack",
    "restart_syscall",
    "getpid",
    "getppid",
    "gettid",
    "getuid",
    "geteuid",
    "getgid",
    "getegid",
    "getgroups",
    "getcwd",
    "chdir",
    "fchdir",
    "uname",
    "sysinfo",
    "getrandom",
    "clock_gettime",
    "clock_getres",
    "gettimeofday",
    "time",
    "times",
    "getrusage",
    "getrlimit",
    "sched_getaffinity",
    "sched_yield",
    "futex",
    "nanosleep",
    "clock_nanosleep",
    "poll",
    "ppoll",
    "select",
    "pselect6",
    "pipe",
    "pipe2",
    "dup",
    "dup2",
    "dup3",
    "fcntl",
    "flock",
    "clone",
    "clone3",
    "fork",
    "vfork",
    "wait4",
    "waitid",
    "execve",
    "execveat",
    "exit",
    "exit_group",
)


def restrict_process(executable: str) -> None:
    """Irreversibly constrain this child before its exec; no parent preexec_fn."""
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise RuntimeError("read-only local execution requires Linux x86_64/arm64")
    libc = ctypes.CDLL(None, use_errno=True)
    # These Landlock syscall numbers are identical on the two supported ABIs.
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError(
            "read-only local execution requires enabled Landlock ABI >= 3"
        )
    # Bits 0..14 include EXECUTE and all write/create/remove/refer/truncate
    # operations through ABI 3; READ_FILE/READ_DIR remain unhandled.
    rights = ((1 << 15) - 1) & ~((1 << 2) | (1 << 3))
    attr = _Ruleset(rights)
    ruleset = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "read-only Landlock ruleset unavailable")
    try:
        loader = (
            "/lib64/ld-linux-x86-64.so.2"
            if platform.machine() == "x86_64"
            else "/lib/ld-linux-aarch64.so.1"
        )
        # ELF interpreter permission is required by execve as well. Even if a
        # Git helper invokes this loader directly, write/network restrictions
        # remain inherited and cannot be relaxed.
        for path, access in ((executable, 1), (loader, 1), ("/dev/null", 1 << 1)):
            executable_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathRule(access, executable_fd)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(
                        ctypes.get_errno(), "read-only Landlock executable rule failed"
                    )
            finally:
                os.close(executable_fd)
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "read-only no_new_privs failed")
        if libc.syscall(446, ruleset, 0) != 0:
            raise OSError(ctypes.get_errno(), "read-only Landlock enforcement failed")
    finally:
        os.close(ruleset)

    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_ArgComparison),
    ]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x00050000 | errno.EPERM)
    if not context:
        raise RuntimeError("read-only seccomp initialization failed")
    try:
        for name in _READ_SYSCALLS:
            number = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number >= 0 and seccomp.seccomp_rule_add(context, 0x7FFF0000, number, 0):
                raise RuntimeError("read-only seccomp rule failed")
        # glibc implements getrlimit using prlimit64. Admit only queries;
        # a non-null new_limit could mutate this or another same-UID process.
        query_only = _ArgComparison(2, 4, 0, 0)  # SCMP_CMP_EQ, new_limit == NULL
        number = seccomp.seccomp_syscall_resolve_name(b"prlimit64")
        if number >= 0 and seccomp.seccomp_rule_add_array(
            context, 0x7FFF0000, number, 1, ctypes.byref(query_only)
        ):
            raise RuntimeError("read-only seccomp prlimit rule failed")
        if seccomp.seccomp_load(context):
            raise RuntimeError("read-only seccomp enforcement failed")
    finally:
        seccomp.seccomp_release(context)


def main() -> int:
    try:
        executable = sys.argv[1]
        restrict_process(executable)
        os.execv(executable, sys.argv[1:])
    except Exception as exc:
        # No traceback, environment, or repository contents in this diagnostic.
        print(f"READ_ONLY_UNAVAILABLE: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 126
    return 126


if __name__ == "__main__":
    sys.exit(main())
