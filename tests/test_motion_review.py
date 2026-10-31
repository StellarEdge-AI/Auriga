"""Geometric counterexamples for swept box clearance, without simulation."""
import math
import unittest

import numpy as np

from auriga.robot.motion.motion_review import _segment_box_clearance


class MotionReviewTests(unittest.TestCase):
    def test_box_distance_does_not_invent_collision_at_expanded_cube_corner(self):
        a = np.array([[1.8, 1.8, 0.], [2., -2., 0.]])
        b = np.array([[1.8, 1.8, 0.], [2., 2., 0.]])
        clearance = _segment_box_clearance(a, b, np.array([-1.]*3), np.array([1.]*3), np.array([1., .9]))
        np.testing.assert_allclose(clearance, [math.sqrt(1.28)-1, .1], atol=1e-12)


if __name__ == "__main__":
    unittest.main()
