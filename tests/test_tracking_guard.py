"""CPU-only checks for measured active-arm following-error detection."""

import unittest

from auriga.robot.motion.tracking_guard import TrackingGuard


class TrackingGuardTest(unittest.TestCase):
    def test_sustained_error_triggers_on_fifth_native_row(self):
        guard = TrackingGuard()
        command = [0, 0, 0, 0, 0, 0]
        measured = [0, 0, 0, -0.6, 0, 0]
        for _ in range(4):
            self.assertIsNone(guard.observe(command, measured))
        self.assertEqual(guard.observe(command, measured), {
            "max_joint_error_rad": 0.6, "threshold_rad": 0.5,
            "consecutive_steps": 5, "joint_index": 3,
            "commanded_q_rad": command, "measured_q_rad": measured})

    def test_transient_and_exact_threshold_reset_consecutive_count(self):
        for recovery in (0.1, 0.5):
            with self.subTest(recovery=recovery):
                guard = TrackingGuard()
                for _ in range(4):
                    self.assertIsNone(guard.observe([0] * 6, [0.6] * 6))
                self.assertIsNone(guard.observe([0] * 6, [recovery] * 6))
                for _ in range(4):
                    self.assertIsNone(guard.observe([0] * 6, [0.6] * 6))
                self.assertEqual(guard.observe([0] * 6, [0.6] * 6)["consecutive_steps"], 5)

    def test_compares_same_row_target_and_does_not_wrap_joint_coordinates(self):
        guard = TrackingGuard(consecutive_steps=1)
        self.assertIsNone(guard.observe([2.5] * 6, [2.4] * 6))
        result = guard.observe([3.1] * 6, [-3.1] * 6)
        self.assertAlmostEqual(result["max_joint_error_rad"], 6.2)
        self.assertEqual(result["joint_index"], 0)

    def test_invalid_configuration(self):
        for value in (0, -1, True, '0.5', float('nan'), float('inf')):
            with self.subTest(threshold=value), self.assertRaises(ValueError):
                TrackingGuard(threshold_rad=value)
        for value in (0, -1, True, 1.5, '5', float('inf')):
            with self.subTest(steps=value), self.assertRaises(ValueError):
                TrackingGuard(consecutive_steps=value)

    def test_invalid_samples_raise_without_updating_streak(self):
        invalid = (None, [], [0] * 5, [0] * 7, [0, 0, 0, 0, 0, True],
                   [0, 0, 0, 0, 0, '1'], [0, 0, 0, 0, 0, float('nan')],
                   [0, 0, 0, 0, 0, float('inf')])
        for sample in invalid:
            for side in ('command', 'measured'):
                with self.subTest(sample=sample, side=side):
                    guard = TrackingGuard(consecutive_steps=2)
                    self.assertIsNone(guard.observe([0] * 6, [0.6] * 6))
                    arguments = {'command': [0] * 6, 'measured': [0.6] * 6, side: sample}
                    with self.assertRaises(ValueError):
                        guard.observe(**arguments)
                    self.assertIsNotNone(guard.observe([0] * 6, [0.6] * 6))
        with self.assertRaises(ValueError):
            TrackingGuard().observe([1e308] * 6, [-1e308] * 6)

    def test_inputs_and_diagnostic_arrays_are_independent(self):
        command, measured = [0] * 6, [0.6] * 6
        guard = TrackingGuard(consecutive_steps=1)
        result = guard.observe(command, measured)
        self.assertEqual(command, [0] * 6)
        self.assertEqual(measured, [0.6] * 6)
        result['commanded_q_rad'][0] = 99
        result['measured_q_rad'][0] = 99
        self.assertEqual(command, [0] * 6)
        self.assertEqual(measured, [0.6] * 6)
        self.assertIsNone(TrackingGuard().observe(command, measured))


if __name__ == '__main__':
    unittest.main()
