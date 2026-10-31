"""Linux pidfds without requiring recent Python or glibc wrapper symbols."""

import ctypes
import errno
import os
import platform
import signal
import sys


def _syscall(number, *arguments):
    # Linux x86_64 and asm-generic (aarch64) UAPI use these syscall numbers.
    # Reject other ABIs instead of risking a different syscall or PID fallback.
    if (sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}
            or ctypes.sizeof(ctypes.c_void_p) != 8):
        raise OSError(errno.ENOSYS, "pidfd syscall fallback requires Linux x86_64 or aarch64")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(number), *arguments)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def pidfd_open(pid, flags=0):
    native = getattr(os, "pidfd_open", None)
    if native is not None:
        return native(pid, flags)
    return _syscall(434, ctypes.c_int(pid), ctypes.c_uint(flags))


def pidfd_send_signal(descriptor, sig, flags=0):
    native = getattr(signal, "pidfd_send_signal", None)
    if native is not None:
        return native(descriptor, sig, None, flags)
    _syscall(424, ctypes.c_int(descriptor), ctypes.c_int(sig), ctypes.c_void_p(), ctypes.c_uint(flags))
