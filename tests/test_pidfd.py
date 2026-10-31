"""Wrapper compatibility; the real syscall probe only opens self and sends signal 0."""

import ctypes
import errno
import os
import platform
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auriga.cortex.session import pidfd


class PidfdTests(unittest.TestCase):
    def test_prefers_standard_library_without_loading_libc(self):
        with patch.object(pidfd.os, "pidfd_open", return_value=17, create=True) as opened, \
                patch.object(pidfd.signal, "pidfd_send_signal", return_value=None, create=True) as sent, \
                patch.object(pidfd.ctypes, "CDLL") as loader:
            self.assertEqual(pidfd.pidfd_open(123, 0), 17)
            self.assertIsNone(pidfd.pidfd_send_signal(17, 0))
        opened.assert_called_once_with(123, 0)
        sent.assert_called_once_with(17, 0, None, 0)
        loader.assert_not_called()

    def test_syscall_arguments_on_supported_architectures(self):
        for machine in ("x86_64", "aarch64"):
            with self.subTest(machine=machine):
                syscall = Mock(side_effect=[17, 0])
                with patch.object(pidfd.os, "pidfd_open", None, create=True), \
                        patch.object(pidfd.signal, "pidfd_send_signal", None, create=True), \
                        patch.object(pidfd.sys, "platform", "linux"), \
                        patch.object(pidfd.platform, "machine", return_value=machine), \
                        patch.object(pidfd.ctypes, "CDLL", return_value=SimpleNamespace(syscall=syscall)):
                    self.assertEqual(pidfd.pidfd_open(123), 17)
                    self.assertIsNone(pidfd.pidfd_send_signal(17, 0))
                self.assertEqual([[arg.value for arg in call.args] for call in syscall.call_args_list],
                                 [[434, 123, 0], [424, 17, 0, None, 0]])
                self.assertIs(syscall.restype, ctypes.c_long)

    def test_errno_is_preserved_without_retry_or_pid_signal(self):
        for error in (errno.ESRCH, errno.EPERM, errno.ENOSYS, errno.EBADF):
            def fail(*arguments):
                ctypes.set_errno(error)
                return -1
            for operation, args in ((pidfd.pidfd_open, (123,)), (pidfd.pidfd_send_signal, (17, 0))):
                with self.subTest(error=error, operation=operation.__name__), \
                        patch.object(pidfd.os, "pidfd_open", None, create=True), \
                        patch.object(pidfd.signal, "pidfd_send_signal", None, create=True), \
                        patch.object(pidfd.sys, "platform", "linux"), \
                        patch.object(pidfd.platform, "machine", return_value="x86_64"), \
                        patch.object(pidfd.ctypes, "CDLL", return_value=SimpleNamespace(syscall=Mock(side_effect=fail))), \
                        patch.object(pidfd.os, "kill") as kill:
                    with self.assertRaises(OSError) as caught:
                        operation(*args)
                    self.assertEqual(caught.exception.errno, error)
                    if error == errno.ESRCH:
                        self.assertIsInstance(caught.exception, ProcessLookupError)
                    kill.assert_not_called()

    def test_unknown_platform_and_architecture_fail_closed(self):
        for system, machine in (("linux", "riscv64"), ("darwin", "x86_64")):
            with self.subTest(system=system, machine=machine), \
                    patch.object(pidfd.os, "pidfd_open", None, create=True), \
                    patch.object(pidfd.signal, "pidfd_send_signal", None, create=True), \
                    patch.object(pidfd.sys, "platform", system), \
                    patch.object(pidfd.platform, "machine", return_value=machine), \
                    patch.object(pidfd.ctypes, "CDLL") as loader:
                for operation, args in ((pidfd.pidfd_open, (123,)), (pidfd.pidfd_send_signal, (17, 0))):
                    with self.assertRaises(OSError) as caught:
                        operation(*args)
                    self.assertEqual(caught.exception.errno, errno.ENOSYS)
                loader.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux" and platform.machine() in {"x86_64", "aarch64"},
                         "Linux pidfd syscall architecture required")
    def test_real_fallback_self_descriptor_and_signal_zero(self):
        with patch.object(pidfd.os, "pidfd_open", None, create=True), \
                patch.object(pidfd.signal, "pidfd_send_signal", None, create=True):
            descriptor = pidfd.pidfd_open(os.getpid())
            try:
                self.assertFalse(os.get_inheritable(descriptor))
                self.assertIsNone(pidfd.pidfd_send_signal(descriptor, 0))
            finally:
                os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
