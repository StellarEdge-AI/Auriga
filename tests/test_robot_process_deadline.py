"""Real pipe backpressure must not bypass request or native-close deadlines."""
import os
import sys
import tempfile
import time
import unittest

from auriga.robot.backends.robodojo.robot_process import RobotProcess
from auriga.robot.backends.robodojo.worker_protocol import WIRE_PREFIX


class RobotProcessDeadlineTest(unittest.TestCase):
    def worker(self, directory, *, stall=False):
        source = f'''
import json, sys, time
prefix = {WIRE_PREFIX!r}
executions = 0
for line in sys.stdin:
    message = json.loads(line)
    operation = message['op']
    if operation == 'execute': executions += 1
    result = {{'closed': True}} if operation == 'close' else {{'size': len((message.get('payload') or {{}}).get('text', ''))}}
    if operation == 'execute': result['executions'] = executions
    print(prefix + json.dumps({{'id': message['id'], 'result': result}}), flush=True)
    if operation == 'close': break
    if operation == 'ready' and {stall!r}: time.sleep(30)
'''
        robot = RobotProcess(directory)
        robot.start_process([sys.executable, '-u', '-c', source], cwd=directory, environment=os.environ.copy())
        def cleanup():
            if robot.process.poll() is None:
                robot.process.kill()
                robot.process.wait(timeout=3)
            robot.close()
        self.addCleanup(cleanup)
        return robot

    def test_full_pipe_times_out_and_close_reaps_without_replaying_request(self):
        with tempfile.TemporaryDirectory() as directory:
            robot = self.worker(directory, stall=True)
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, 'status unknown: worker request write deadline; do not replay action'):
                robot._request('execute', {'text': 'x' * (1024 * 1024)}, timeout=.1)
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(robot.pending)
            self.assertIsNotNone(robot.failure)
            self.assertIsNone(robot.process.poll())
            started = time.monotonic()
            report = robot.close()
            self.assertLess(time.monotonic() - started, 4)
            self.assertTrue(report['process_exit_confirmed'])
            self.assertFalse(report['native_close_acknowledged'])
            self.assertEqual(report['forced_termination'], 'SIGTERM')

    def test_waiting_for_another_writer_uses_the_same_request_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            robot = self.worker(directory)
            with robot.write_lock:
                started = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, 'worker request write deadline'):
                    robot._request('observe', timeout=.05)
                self.assertLess(time.monotonic() - started, .5)
            self.assertFalse(robot.pending)
            self.assertIsNotNone(robot.failure)

    def test_large_request_is_written_once_and_native_close_still_acknowledges(self):
        with tempfile.TemporaryDirectory() as directory:
            robot = self.worker(directory)
            self.assertEqual(robot._request('execute', {'text': 'x' * (1024 * 1024)}, timeout=2),
                             {'size': 1024 * 1024, 'executions': 1})
            self.assertIsNone(robot.failure)
            report = robot.close()
            self.assertTrue(report['native_close_acknowledged'])
            self.assertTrue(report['process_exit_confirmed'])
            self.assertIsNone(report['forced_termination'])


if __name__ == '__main__':
    unittest.main()
