"""Bounded program launcher with optional Linux Landlock/seccomp isolation.

Enabled isolation fails closed without requiring user namespaces.
"""

import ctypes
import errno
import os
import sys
from pathlib import Path

# Linux UAPI syscall numbers on x86_64/aarch64; other platforms fail closed.
CREATE_RULESET, ADD_RULE, RESTRICT_SELF = 444, 445, 446
EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1, 2, 4, 8


def restrict_program_syscalls(*, deny_socket_creation=False, bounded_program=False):
    """Keep an actor on its inherited channel; filters also constrain descendants."""
    if not (deny_socket_creation or bounded_program):
        return
    try:
        seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as error:
        raise RuntimeError("libseccomp.so.2 is required for bounded actor programs") from error
    allow, deny = 0x7fff0000, 0x00050000 | errno.EPERM
    class Argument(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint32), ("op", ctypes.c_uint32),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Argument)]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(allow)
    if not context:
        raise RuntimeError("seccomp filter initialization failed")
    def rule(name, action=deny, argument=None):
        syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
        if syscall < 0:
            return  # This syscall is not present on the active native architecture.
        result = seccomp.seccomp_rule_add_array(context, action, syscall, int(argument is not None),
                                               ctypes.byref(argument) if argument is not None else None)
        if result < 0:
            raise OSError(-result, "seccomp rule failed: " + name)
    try:
        if deny_socket_creation:
            for name in ("socket", "socketpair", "connect", "socketcall", "io_uring_setup", "io_uring_enter", "io_uring_register"):
                rule(name)
        if bounded_program:
            for name in ("fork", "vfork", "kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo",
                         "pidfd_send_signal", "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd"):
                rule(name)
            # glibc falls back to clone only on ENOSYS; the clone rule permits pthreads.
            rule("clone3", 0x00050000 | errno.ENOSYS)
            rule("clone", argument=Argument(0, 7, 0x10000, 0))  # SCMP_CMP_MASKED_EQ, !CLONE_THREAD.
        result = seccomp.seccomp_load(context)
        if result < 0:
            raise OSError(-result, "seccomp filter load failed")
    finally:
        seccomp.seccomp_release(context)


def restrict(read_paths, write_paths, socket_paths=(), tcp_ports=None):
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(CREATE_RULESET, 0, 0, 1)
    if abi < 1:
        raise OSError(ctypes.get_errno(), "Landlock is required for actor isolation")
    handled = (1 << 13) - 1
    # Landlock rejects a ruleset containing rights newer than the kernel ABI.
    if abi >= 2:
        handled |= 1 << 13  # REFER
    if abi >= 3:
        handled |= 1 << 14  # TRUNCATE
    if abi >= 5:
        handled |= 1 << 15  # IOCTL_DEV

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    class PortRule(ctypes.Structure):
        _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]

    def check(result):
        if result < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        return result

    if tcp_ports is not None and abi < 4:
        raise RuntimeError("Landlock ABI >=4 required for actor TCP isolation")
    attrs = Ruleset(handled, 3 if tcp_ports is not None else 0)
    size = ctypes.sizeof(attrs) if abi >= 4 else 8
    ruleset = check(libc.syscall(CREATE_RULESET, ctypes.byref(attrs), size, 0))
    try:
        for paths, access in [(read_paths, EXECUTE | READ_FILE | READ_DIR), (write_paths, handled), (socket_paths, 1 << 9)]:
            for item in paths:
                path = Path(item).resolve()
                if not path.exists():
                    raise FileNotFoundError(path)
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    allowed = access if path.is_dir() else access & (EXECUTE | READ_FILE | WRITE_FILE | (1 << 14) if abi >= 3 else EXECUTE | READ_FILE | WRITE_FILE)
                    rule = PathRule(allowed, fd)
                    check(libc.syscall(ADD_RULE, ruleset, 1, ctypes.byref(rule), 0))
                finally:
                    os.close(fd)
        for port in tcp_ports or []:
            rule = PortRule(2, port)  # CONNECT_TCP only; BIND_TCP remains denied.
            check(libc.syscall(ADD_RULE, ruleset, 2, ctypes.byref(rule), 0))
        check(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
        check(libc.syscall(RESTRICT_SELF, ruleset, 0))
    finally:
        os.close(ruleset)
    return abi


def launch():
    import json
    import platform
    spec = json.loads(Path(sys.argv[1]).read_text())
    if spec.get("limits"):
        import resource
        for name, value in spec["limits"].items():
            resource.setrlimit(getattr(resource, name), (value, value))
    if spec.get("sandbox_enabled", True):
        if platform.machine() not in ("x86_64", "aarch64"):
            raise RuntimeError("unsupported_landlock_architecture")
        restrict(spec["read_paths"], spec["write_paths"], spec.get("socket_paths", []), spec.get("tcp_ports"))
        restrict_program_syscalls(deny_socket_creation=spec.get("deny_socket_creation", False), bounded_program=spec.get("bounded_program", False))
    if spec.get("bounded_program"):
        os.chdir(spec["env"]["HOME"])
    os.execvpe(sys.argv[2], sys.argv[2:], spec["env"])


if __name__ == "__main__":
    launch()
