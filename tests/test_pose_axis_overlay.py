import unittest
import numpy as np

from auriga.perception.overlays.pose_axis_overlay import draw_pose_axis_frame
from auriga.perception.overlays.trajectory_overlay import render_trajectory_overlay


class PoseAxisOverlayTest(unittest.TestCase):
    def test_trajectory_overlay_renders_pose_axis_frame_while_keeping_panel_meta_scalar(self):
        observation = dict(world_kind='observed', observation_id='obs-1', sim_step=10, step_domain='control_step')
        frame = dict(camera_id='cam_head', frame_id='head', sim_step=10, step_domain='control_step',
                     rgb_ref='rgb-1', pixel_transform='identity')
        camera = dict(
            observation=observation,
            frame=frame,
            width=320,
            height=240,
            K=np.array([[250.0, 0.0, 160.0], [0.0, 250.0, 120.0], [0.0, 0.0, 1.0]]),
            D=np.zeros(5),
            T=np.eye(4),
            reference_frame_id='env_origin',
            pose_source='fixed_calibration',
        )
        image = np.zeros((240, 320, 3), dtype=np.uint8)
        arm_data = dict(
            phase='move',
            link_positions_m=dict(link5=[0.0, -0.1, 1.5], link6=[0.0, 0.0, 1.5]),
            link6_pose=dict(
                position_m=[0.0, 0.0, 1.5],
                rotation_matrix=np.eye(3).tolist(),
            ),
        )
        pose_frame = draw_pose_axis_frame(image.copy(), camera, 'left', arm_data, (255, 160, 48), 320, 240)
        self.assertEqual(pose_frame['grasp_center_m'], [0.1226, 0.0, 1.5])
        self.assertEqual(pose_frame['pad_center_m'], [0.1501, 0.0, 1.5])
        self.assertEqual(pose_frame['fingertip_m'], [0.1576, 0.0, 1.5])
        self.assertEqual(pose_frame['approach_axis_world'], [1.0, 0.0, 0.0])
        self.assertEqual(pose_frame['jaw_closing_axis_world'], [0.0, 1.0, 0.0])
        trajectory = dict(
            source_kind='candidate_command_trajectory_not_measurement',
            frame_id='env_origin',
            label='plan-1',
            robot_model=dict(joints=[dict(parent='link5', child='link6')]),
            samples=[dict(native_step=0, arms=dict(left=arm_data))],
        )
        rendered, meta = render_trajectory_overlay(camera, image, trajectory, native_steps=[0])
        self.assertEqual(rendered.shape, (240, 320, 3))
        arm_meta = meta['panels'][0]['arms']['left']
        self.assertNotIn('pose_axis_frame', arm_meta)


if __name__ == '__main__':
    unittest.main()
