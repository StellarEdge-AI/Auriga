import unittest
from copy import deepcopy
from scripts.benchmark_queue import eligible_gpu_sample, command
from auriga.core.config import Config, cli_overrides
import test_benchmark_queue as queue_fixture


class ManipulationQueueConfiguration(unittest.TestCase):
    def test_gpu_allowlist_changes_only_admission_candidates(self):
        original=dict(gpus=[dict(index=i,uuid=f'GPU-{i}') for i in range(4)],error=None,processes=[{'pid':1}])
        before=deepcopy(original)
        value=eligible_gpu_sample(original,[1,2,3])
        self.assertEqual([gpu['index'] for gpu in value['gpus']],[1,2,3])
        self.assertEqual(value['all_observed_gpu_indices'],[0,1,2,3])
        self.assertEqual(value['processes'],original['processes'])
        self.assertEqual(original,before)
        self.assertIs(eligible_gpu_sample(original,None),original)
        self.assertEqual(eligible_gpu_sample(original,[5])['gpus'],[])
        for invalid in ([],[True],[1,1],[-1],{},'1'):
            with self.assertRaises(ValueError): eligible_gpu_sample(original,invalid)

    def test_queue_feature_flags_map_to_effective_gateway_config(self):
        fixture=queue_fixture.QueueCheck(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        case=dict(fixture.case,arm_queue=False,motion_cost_preview=True)
        args=command(case,1,config=Config())
        self.assertIn('--no-arm-queue',args)
        self.assertIn('--motion-cost-preview',args)
        cfg=Config().with_overrides(cli_overrides(case))
        self.assertFalse(cfg.features.arm_queue)
        self.assertTrue(cfg.features.motion_cost_preview)
        for key in ('arm_queue','motion_cost_preview'):
            with self.assertRaises(ValueError): command(dict(case,**{key:1}),1,config=Config())

    def test_new_protocol_switches_roundtrip_strictly_and_remain_independent(self):
        fixture=queue_fixture.QueueCheck(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        keys=('bounded_continuation','motion_safety_guard','trajectory_preview','manipulation_protocol')
        for key in keys:
            self.assertIs(getattr(Config().features,key),True)
            args=command(dict(fixture.case,**{key:False}),1,config=Config())
            self.assertIn('--no-'+key.replace('_','-'),args)
            config=Config().with_overrides(cli_overrides({key:False}))
            self.assertIs(getattr(config.features,key),False)
            for other in keys:
                if other!=key:self.assertIs(getattr(config.features,other),True)
            with self.assertRaises(ValueError):command(dict(fixture.case,**{key:1}),1,config=Config())
