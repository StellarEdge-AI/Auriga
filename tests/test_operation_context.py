"""CPU provenance checks over public observation bundles, without scene truth."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from auriga.cortex.context.task_planning import validate_operation_evidence


class OperationEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.identity=dict(run_id="run",episode_id="ep",env_instance_id="env",env_generation=1)
        self.observations={key:dict(self.identity,observation_id=key,sim_step=step)
            for key,step in (("before",0),("release",10),("clear",20),("current",30))}
        self.reads=[]
        def read(kind,identifier):
            self.assertEqual(kind,"observation")
            self.reads.append(identifier)
            return self.observations[identifier]
        self.store=Mock()
        self.store.read.side_effect=read
        self.context=dict(object_label="actor bottle label",state="retained",release_observation_id="release",
            evidence=dict(observation_id="current",sources=["rgb","proprioception"],
                basis="Visible separation, resting footprint, and giver outside the entry corridor.",
                uncertainty="Back face is hidden; no contact truth is available."),
            supported_transfer=dict(phase="receive",support="Visible central table patch.",
                giver_arm="right",receiver_arm="left",giver_exit="Upward then right corridor.",
                receiver_entry="Left corridor after clearance review.",parallel_work="Right preparation away from landing.",
                clearance_observation_id="clear"))

    def validate(self,context=None,**kwargs):
        return validate_operation_evidence(context or self.context,self.store,self.identity,"current",**kwargs)

    def test_distinct_ordered_episode_observations_validate_references_only(self):
        result=self.validate()
        self.assertTrue(result["observation_references_valid"])
        self.assertFalse(result["physical_claims_verified"])
        self.assertEqual(result["source_kind"],"actor_judgment")
        self.assertEqual(set(self.reads),{"release","clear","current"})
        self.assertEqual([call[0] for call in self.store.method_calls],["read"]*3)

    def test_missing_cross_episode_and_future_citations_are_rejected(self):
        for scenario in ("missing","episode","future"):
            with self.subTest(scenario=scenario):
                old=deepcopy(self.observations)
                if scenario=="missing":del self.observations["clear"]
                elif scenario=="episode":self.observations["clear"]["episode_id"]="other"
                else:self.observations["clear"]["sim_step"]=40
                with self.assertRaises(ValueError):self.validate()
                self.observations=old

    def test_clearance_requires_later_native_observation_not_merely_another_id(self):
        for clear_id,step in (("release",10),("clear",10),("clear",0),("clear",None)):
            with self.subTest(clear_id=clear_id,step=step):
                context=deepcopy(self.context)
                context["supported_transfer"]["clearance_observation_id"]=clear_id
                self.observations[clear_id]["sim_step"]=step
                with self.assertRaisesRegex(ValueError,"clearance_requires_later"):self.validate(context)
                self.observations["release"]["sim_step"]=10
                self.observations["clear"]["sim_step"]=20

    def test_current_evidence_required_for_template_but_old_plan_evidence_may_remain(self):
        context=deepcopy(self.context)
        context["evidence"]["observation_id"]="clear"
        with self.assertRaisesRegex(ValueError,"requires_current_evidence"):self.validate(context)
        self.assertFalse(self.validate(context,require_current=False)["physical_claims_verified"])
        context["evidence"]["observation_id"]="before"
        with self.assertRaisesRegex(ValueError,"release_after_claim"):self.validate(context,require_current=False)

    def test_hidden_sources_and_unsupported_retained_claims_are_rejected(self):
        for source in ("simulator","object_coordinates","contact_truth","score"):
            context=deepcopy(self.context)
            context["evidence"]["sources"]=[source]
            with self.subTest(source=source),self.assertRaisesRegex(ValueError,"invalid_operation_context"):
                self.validate(context)
        context=deepcopy(self.context);context.pop("release_observation_id")
        with self.assertRaisesRegex(ValueError,"release_evidence_required"):self.validate(context)
        context=deepcopy(self.context);context["evidence"]["sources"]=["proprioception"]
        with self.assertRaisesRegex(ValueError,"retained_requires_rgb"):self.validate(context)


if __name__=="__main__":
    unittest.main()
