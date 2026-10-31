"""Manifest comparison identity, fixed denominators and read-only boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auriga.apps.library.experiment_comparisons import ExperimentGroups, _identity, group_id
from auriga.apps.library.experiments import Catalog, atomic_json, register_run


class ComparisonCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'benchmark' / 'panel' / 'manifest.json'
        self.protocol = [dict(case_id='pick__standard__g0__l0', task='pick', variant='standard', eval_seed=0, layout_id=0),
                         dict(case_id='pick__random__g0__l0', task='pick', variant='random', eval_seed=0, layout_id=0)]

    def run_case(self, name, condition, row, score, *, scored=True):
        run = self.root / 'benchmark' / 'panel' / 'runs' / name
        register_run(run, category='benchmark', task=row['task'], run_id=name)
        atomic_json(run / 'summary.json', dict(normalized_score=score, task_success=score == 1,
                    valid_episode=scored, scored_episode=scored, status='completed', diagnostic_secret='not-for-comparison'))
        return dict(run_dir=str(run), cwd='/source/' + condition, task=row['task'], seed=row['eval_seed'],
                    layout_id=row['layout_id'], tag=[condition, row['case_id']])

    def write(self, cases, **extra):
        manifest = dict(benchmark_group='test', method='paired', protocol_cases=self.protocol, cases=cases,
            experiment_panels={'candidate': [row['case_id'] for row in self.protocol]},
            auriga_config={'model': {'name': 'fixture-model', 'effort': 'high', 'api_key': 'secret-key', 'native_memories': False},
                             'features': {'observation_profile': 'rgb', 'private_path': 'secret-path'}})
        atomic_json(self.path, {**manifest, **extra})

    def snapshot(self):
        catalog = Catalog(self.root)
        catalog.scan()
        return ExperimentGroups(self.root).snapshot(catalog)

    def test_manifest_written_before_the_rename_keeps_its_conditions(self):
        first, second = self.protocol
        self.write([self.run_case('candidate-one', 'candidate', first, 1)])
        manifest = json.loads(self.path.read_text())
        manifest['daedalus_config'] = manifest.pop('auriga_config')
        atomic_json(self.path, manifest)
        groups = {group['condition']: group for group in self.snapshot()['groups']}
        self.assertEqual(groups['candidate']['conditions']['model'], 'fixture-model')

    def test_exact_pairing_missing_denominator_and_ambiguous_attempts(self):
        first, second = self.protocol
        cases = [self.run_case('baseline-standard', 'baseline', first, 0),
                 self.run_case('baseline-random', 'baseline', second, 1),
                 self.run_case('candidate-one', 'candidate', first, .2),
                 self.run_case('candidate-retry', 'candidate', first, 1)]
        self.write(cases)
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        result = self.snapshot()
        groups = {group['condition']: group for group in result['groups']}
        candidate, baseline = groups['candidate'], groups['baseline']
        self.assertEqual(candidate['case_count'], 2)
        self.assertTrue(candidate['protocol_denominator_known'])
        self.assertEqual(candidate['baseline_group_id'], baseline['id'])
        rows = candidate['cases']
        self.assertEqual(rows[0]['selection'], 'ambiguous')
        self.assertIsNone(rows[0]['run'])
        self.assertEqual([run['summary']['normalized_score'] for run in rows[0]['attempts']], [.2, 1])
        self.assertEqual(rows[1]['selection'], 'missing')
        self.assertNotEqual(rows[0]['case_key'], rows[1]['case_key'])
        self.assertEqual(candidate['conditions']['model'], 'fixture-model')
        self.assertIs(candidate['conditions']['native_memories'], False)
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*.json')})

    def test_unscored_zero_and_scored_zero_stay_distinct(self):
        self.write([self.run_case('unscored', 'candidate', self.protocol[0], 0, scored=False),
                    self.run_case('zero', 'candidate', self.protocol[1], 0)])
        rows = self.snapshot()['groups'][0]['cases']
        self.assertIsNone(rows[0]['run']['summary']['normalized_score'])
        self.assertEqual(rows[1]['run']['summary']['normalized_score'], 0)

    def test_legacy_infrastructure_score_is_not_promoted_and_error_text_is_private(self):
        case = self.run_case('legacy', 'candidate', self.protocol[0], .7)
        self.write([case])
        summary_path = Path(case['run_dir']) / 'summary.json'
        atomic_json(summary_path, dict(normalized_score=.7, infrastructure_error='private endpoint secret', status='unknown'))
        summary = self.snapshot()['groups'][0]['cases'][0]['run']['summary']
        self.assertIsNone(summary['normalized_score'])
        self.assertIs(summary['infrastructure_error'], True)
        self.assertNotIn('private endpoint', json.dumps(summary))
        atomic_json(summary_path, dict(normalized_score=.7, score_status='unavailable'))
        self.assertIsNone(self.snapshot()['groups'][0]['cases'][0]['run']['summary']['normalized_score'])

    def test_explicit_roles_supply_identity_when_protocol_missing(self):
        self.write([self.run_case('first', 'history_r1_candidate', self.protocol[0], .5)],
                   protocol_cases=[], experiment_panels={},
                   case_roles={'history_r1_candidate': [row['case_id'] for row in self.protocol]})
        group = self.snapshot()['groups'][0]
        self.assertEqual(group['condition'], 'history_r1_candidate')
        self.assertEqual(group['case_count'], 2)
        self.assertEqual(group['cases'][1]['variant'], 'random')
        self.assertEqual(group['cases'][1]['selection'], 'missing')

    def test_explicit_recovery_subset_preserves_its_selected_missing_case(self):
        first = self.run_case('first', 'candidate', self.protocol[0], .5)
        self.write([first], recovery_selection=[dict(new_run_dir=first['run_dir'], source_run_dir='/old/run')])
        group = self.snapshot()['groups'][0]
        self.assertEqual(group['case_count'], 1)
        self.assertEqual(group['protocol_case_count'], 2)
        self.assertEqual(group['scope'], 'recovery_subset')
        self.assertTrue(group['protocol_denominator_known'])
        # Scheduled recovery cases remain in their subset denominator before creation.
        Path(first['run_dir'], 'experiment.json').unlink()
        group = self.snapshot()['groups'][0]
        self.assertEqual(group['case_count'], 1)
        self.assertEqual(group['cases'][0]['selection'], 'missing')

    def test_curated_relationships_do_not_select_recovery_results(self):
        self.write([self.run_case('candidate', 'candidate', self.protocol[0], .5),
                    self.run_case('baseline', 'baseline', self.protocol[0], 0)])
        relative = self.path.relative_to(self.root).as_posix()
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(version=1, groups=[dict(
            manifest=relative, condition='candidate', label='R1', series='fixture', iteration=1,
            parent=dict(manifest=relative, condition='baseline'), recovery_of=dict(manifest=relative, condition='baseline'),
            evidence=['docs/fixture.md'], source=dict(branch='fixture-r1', revision='revision'),
            note='Same method recovery', secret='never emitted')]))
        candidate = next(group for group in self.snapshot()['groups'] if group['condition'] == 'candidate')
        self.assertEqual(candidate['parent_group_id'], group_id(relative, 'baseline'))
        self.assertEqual(candidate['recovery_of_group_id'], group_id(relative, 'baseline'))
        self.assertEqual(candidate['iteration'], 1)
        self.assertEqual(candidate['relationship_evidence'], ['docs/fixture.md'])
        self.assertEqual(candidate['source']['branch'], 'fixture-r1')
        self.assertEqual(candidate['cases'][1]['selection'], 'missing')

    def test_metadata_and_run_symlinks_are_rejected_after_snapshot(self):
        self.write([self.run_case('candidate', 'candidate', self.protocol[0], 1)])
        catalog = Catalog(self.root); catalog.scan()
        groups = ExperimentGroups(self.root)
        self.assertEqual(len(groups.snapshot(catalog)['groups']), 1)
        external = self.root / 'external.json'
        atomic_json(external, {'secret': 'secret-value'})
        self.path.unlink(); self.path.symlink_to(external)
        result = groups.snapshot(catalog)
        self.assertEqual(result['groups'], [])
        self.assertEqual(len(result['errors']), 1)
        self.assertNotIn('secret-value', json.dumps(result))
        self.path.unlink()
        self.write([dict(run_dir=str(self.root / 'link'), cwd='/source', task='pick', seed=0, layout_id=0,
                         tag=['candidate', self.protocol[0]['case_id']])])
        run = next(iter(catalog.entries.values()))['path']
        (self.root / 'link').symlink_to(run, target_is_directory=True)
        row = groups.snapshot(catalog)['groups'][0]['cases'][0]
        self.assertEqual(row['selection'], 'missing')
        self.assertEqual(row['warnings'], ['run_path_unavailable'])

    def test_archived_identity_mismatch_cannot_attach_score_to_wrong_case(self):
        case = self.run_case('candidate', 'candidate', self.protocol[0], 1)
        self.write([case])
        atomic_json(Path(case['run_dir']) / 'manifest.json', dict(task='pick', seed=0, layout_id=7))
        row = self.snapshot()['groups'][0]['cases'][0]
        self.assertEqual(row['selection'], 'missing')
        self.assertEqual(row['warnings'], ['archived_case_identity_conflict'])

    def test_filename_does_not_create_identity_or_lineage(self):
        self.write([dict(run_dir=str(self.root / 'baseline-R1/pick__random__g0__l0'), cwd='/source',
                         task='pick', seed=0, layout_id=0)], protocol_cases=[], experiment_panels={})
        group = self.snapshot()['groups'][0]
        self.assertIsNone(group['parent_group_id'])
        self.assertIsNone(group['iteration'])
        self.assertFalse(group['protocol_denominator_known'])
        self.assertIsNone(group['cases'][0]['case_key'])
        self.assertIn('case_identity_incomplete', group['cases'][0]['warnings'])

    def test_changed_manifest_invalidates_decode_cache(self):
        self.write([self.run_case('candidate', 'candidate', self.protocol[0], .5)])
        catalog = Catalog(self.root); catalog.scan()
        groups = ExperimentGroups(self.root)
        original = groups.snapshot(catalog)
        with patch('auriga.apps.library.experiment_comparisons.read_json', side_effect=AssertionError('uncached read')):
            self.assertEqual(groups.snapshot(catalog), original)
        self.write([dict(run_dir='/outside', cwd='/source', task='pick', seed=0, layout_id=0)], method='changed')
        updated = groups.snapshot(catalog)
        self.assertEqual(updated['groups'][0]['method'], 'changed')
        self.assertEqual(updated['groups'][0]['cases'][0]['selection'], 'missing')

    def test_explicit_baseline_manifest_links_only_unique_target(self):
        self.write([self.run_case('candidate', 'candidate', self.protocol[0], .5)])
        baseline = self.path.with_name('baseline-manifest.json')
        manifest = json.loads(self.path.read_text())
        atomic_json(baseline, {**manifest, 'method': 'declared-baseline'})
        manifest.update(baseline_manifest=str(baseline), series='declared-series', iteration=1)
        atomic_json(self.path, manifest)
        result = self.snapshot()['groups']
        candidate = next(group for group in result if group['manifest'].endswith('/manifest.json'))
        reference = next(group for group in result if group['manifest'].endswith('/baseline-manifest.json'))
        self.assertEqual(candidate['baseline_group_id'], reference['id'])
        self.assertEqual(candidate['iteration'], 1)
        self.assertEqual(candidate['series'], 'declared-series')

    def test_symlink_benchmark_root_does_not_break_catalog_snapshot(self):
        self.root.joinpath('outside').mkdir()
        self.root.joinpath('benchmark').symlink_to(self.root / 'outside', target_is_directory=True)
        result = self.snapshot()
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['errors'], [dict(path='benchmark', error='comparison_root_unavailable')])

    def test_csv_protocol_numbers_pair_runtime_and_keep_random_variant(self):
        protocol = [{**row, 'eval_seed': '0', 'layout_id': '0', 'reset_seed': '3'} for row in self.protocol]
        cases = [self.run_case('standard', 'candidate', self.protocol[0], .5),
                 self.run_case('random', 'candidate', {**self.protocol[1], 'task': 'pick_random'}, 1)]
        self.write(cases, protocol_cases=protocol)
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        group = self.snapshot()['groups'][0]
        self.assertEqual((group['case_count'], group['protocol_case_count']), (2, 2))
        self.assertEqual([row['selection'] for row in group['cases']], ['single', 'single'])
        self.assertEqual([row['reset_seed'] for row in group['cases']], [3, 3])
        self.assertEqual([row['run']['summary']['normalized_score'] for row in group['cases']], [.5, 1])
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*.json')})

    def test_identity_rejects_non_integer_numbers_and_conflicting_runtime(self):
        for value in (True, False, 0.0, '0.0', ' 0', '0 ', '1e0', '０', None):
            with self.subTest(value=value):
                self.assertIsNone(_identity({**self.protocol[0], 'eval_seed': value})['case_key'])
                self.assertIsNone(_identity({**self.protocol[0], 'layout_id': value})['case_key'])
        case = self.run_case('conflict', 'candidate', self.protocol[0], 1)
        self.write([{**case, 'layout_id': 4}], protocol_cases=[{**self.protocol[0], 'eval_seed': '0', 'layout_id': '0'}])
        group = self.snapshot()['groups'][0]
        self.assertEqual(group['protocol_case_count'], 1)
        self.assertEqual(group['cases'][0]['selection'], 'missing')
        self.assertIsNone(group['cases'][1]['case_key'])

    def test_explicit_case_tags_supply_identity_without_claiming_protocol(self):
        cases = [self.run_case('first', 'candidate', self.protocol[0], .5),
                 self.run_case('second', 'candidate', {**self.protocol[1], 'task': 'pick_random'}, 1)]
        self.write(cases, protocol_cases=[], experiment_panels={})
        group = self.snapshot()['groups'][0]
        self.assertFalse(group['protocol_denominator_known'])
        self.assertIsNone(group['protocol_case_count'])
        self.assertEqual([row['variant'] for row in group['cases']], ['standard', 'random'])
        cases[0]['tag'].append(self.protocol[1]['case_id'])
        self.write(cases, protocol_cases=[], experiment_panels={})
        self.assertIsNone(self.snapshot()['groups'][0]['cases'][0]['case_key'])

    def test_registry_aliases_merge_only_explicit_sources_and_supply_protocol(self):
        protocol = [self.protocol[0], {**self.protocol[0], 'case_id': 'pick__standard__g0__l1', 'layout_id': 1}]
        cases = [self.run_case('first', 'one', protocol[0], .5), self.run_case('second', 'two', protocol[1], 1)]
        for case in cases:
            case['tag'] = []
        self.write(cases, protocol_cases=[], experiment_panels={})
        self.assertEqual(len(self.snapshot()['groups']), 2)
        relative = self.path.relative_to(self.root).as_posix()
        registry = self.root / 'service' / 'experiment-groups.json'
        metadata = dict(manifest=relative, condition='paired', condition_aliases=['source:/source/one', 'source:/source/two'],
                        protocol_cases=protocol)
        atomic_json(registry, dict(groups=[metadata]))
        group = self.snapshot()['groups'][0]
        self.assertEqual(group['id'], group_id(relative, 'paired'))
        self.assertEqual((group['case_count'], group['protocol_case_count']), (2, 2))
        self.assertTrue(group['protocol_denominator_known'])
        self.assertIsNone(group['source']['worktree'])
        self.assertEqual(group['source']['worktrees'], ['/source/one', '/source/two'])
        self.assertEqual([row['selection'] for row in group['cases']], ['single', 'single'])

    def test_registry_protocol_conflicts_and_duplicates_do_not_create_denominator(self):
        case = self.run_case('first', 'candidate', self.protocol[0], .5)
        self.write([case], protocol_cases=[], experiment_panels={})
        relative = self.path.relative_to(self.root).as_posix()
        registry = self.root / 'service' / 'experiment-groups.json'
        atomic_json(registry, dict(groups=[dict(manifest=relative, condition='candidate',
                    protocol_cases=[{**self.protocol[0], 'case_id': 'pick__standard__g0__l8', 'layout_id': 8}])]))
        group = self.snapshot()['groups'][0]
        self.assertFalse(group['protocol_denominator_known'])
        self.assertIn('registry_protocol_case_conflict', group['warnings'])
        atomic_json(registry, dict(groups=[dict(manifest=relative, condition='candidate',
                    protocol_cases=[self.protocol[0], self.protocol[0]])]))
        result = self.snapshot()
        self.assertFalse(result['groups'][0]['protocol_denominator_known'])
        self.assertEqual(len(result['errors']), 1)

    def test_registry_protocol_preserves_explicit_missing_rows_over_old_panel(self):
        self.write([self.run_case('first', 'candidate', self.protocol[0], .5)],
                   protocol_cases=[self.protocol[0]], experiment_panels={'candidate': [self.protocol[0]['case_id']]})
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[dict(
            manifest=self.path.relative_to(self.root).as_posix(), condition='candidate', protocol_cases=self.protocol)]))
        group = self.snapshot()['groups'][0]
        self.assertEqual((group['case_count'], group['protocol_case_count']), (2, 2))
        self.assertEqual([row['selection'] for row in group['cases']], ['single', 'missing'])

    def test_registry_references_missing_filename_shapes_and_isolates_bad_entries(self):
        self.write([self.run_case('first', 'candidate', self.protocol[0], .5)])
        omitted = self.path.with_name('dynamic-manifest-r1.json')
        self.path.rename(omitted)
        self.assertEqual(self.snapshot()['groups'], [])
        relative = omitted.relative_to(self.root).as_posix()
        hidden = omitted.parent / 'source-copy' / 'manifest.json'
        atomic_json(hidden, json.loads(omitted.read_text()))
        link = omitted.with_name('link.json')
        link.symlink_to(omitted)
        malformed = omitted.with_name('invalid.json')
        atomic_json(malformed, [])
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[
            None, dict(manifest='../outside.json', condition='candidate'),
            dict(manifest=link.relative_to(self.root).as_posix(), condition='candidate'),
            dict(manifest=malformed.relative_to(self.root).as_posix(), condition='candidate'),
            dict(manifest=relative, condition='candidate', name_zh='明确登记的补跑')]))
        result = self.snapshot()
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual(result['groups'][0]['name_zh'], '明确登记的补跑')
        self.assertEqual(len(result['errors']), 4)

    def test_registry_metadata_is_allowlisted_and_null_clears_prior_relations(self):
        self.write([self.run_case('candidate', 'candidate', self.protocol[0], .5),
                    self.run_case('baseline', 'baseline', self.protocol[0], 0)])
        relative = self.path.relative_to(self.root).as_posix()
        source = dict(manifest=relative, condition='baseline')
        fields = dict(name_zh='实验组', purpose='验证协议', series_name_zh='系列', experiment_nature=['ablation', 'recovery'],
                      role='treatment', relation_status={'parent': 'not_applicable', 'baseline': 'unresolved'},
                      baseline_reason='未确认基线')
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[
            dict(manifest=relative, condition='candidate', parent=source, recovery_of=source),
            dict(manifest=relative, condition='candidate', baseline=None, parent=None, recovery_of=None,
                 **fields, secret={'token': 'never-visible'})]))
        group = next(group for group in self.snapshot()['groups'] if group['condition'] == 'candidate')
        for relation in ('baseline', 'parent', 'recovery_of'):
            self.assertIsNone(group[relation + '_group_id'])
        self.assertEqual({key: group[key] for key in fields}, fields)
        self.assertNotIn('never-visible', json.dumps(group))

    def test_registry_structured_metadata_rejects_wrong_types_and_extra_status_fields(self):
        self.write([self.run_case('candidate', 'candidate', self.protocol[0], .5)])
        registry = self.root / 'service' / 'experiment-groups.json'
        base = dict(manifest=self.path.relative_to(self.root).as_posix(), condition='candidate')
        for nature, status in [('ablation', 'confirmed'), (['ablation', {'secret': 'never-visible'}], ['confirmed'])]:
            with self.subTest(nature=nature, status=status):
                atomic_json(registry, dict(groups=[dict(base, experiment_nature=nature, relation_status=status)]))
                group = self.snapshot()['groups'][0]
                self.assertNotIn('experiment_nature', group)
                self.assertNotIn('relation_status', group)
        atomic_json(registry, dict(groups=[dict(base, experiment_nature=[], relation_status={
            'parent': 'confirmed', 'baseline': {'secret': 'never-visible'}, 'extra': 'never-visible'})]))
        before = registry.read_bytes()
        group = self.snapshot()['groups'][0]
        self.assertEqual(group['experiment_nature'], [])
        self.assertEqual(group['relation_status'], {'parent': 'confirmed'})
        self.assertNotIn('never-visible', json.dumps(group))
        self.assertEqual(registry.read_bytes(), before)

    def test_explicit_identical_manifest_copy_is_not_a_second_scoring_group(self):
        self.write([self.run_case('candidate', 'candidate', self.protocol[0], .5)])
        copy = self.path.parent / 'revision-v1' / 'continuation-manifest.json'
        atomic_json(copy, json.loads(self.path.read_text()))
        self.assertEqual(len(self.snapshot()['groups']), 2)
        relative = self.path.relative_to(self.root).as_posix()
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[dict(
            manifest=relative, condition='candidate', manifest_copies=[copy.relative_to(self.root).as_posix()])]))
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        result = self.snapshot()
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual(result['groups'][0]['manifest'], relative)
        self.assertEqual(result['groups'][0]['cases'][0]['run']['summary']['normalized_score'], .5)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*.json')})

    def test_different_or_invalid_manifest_copies_report_errors_without_exclusion(self):
        self.write([self.run_case('candidate', 'candidate', self.protocol[0], .5)])
        copy = self.path.parent / 'revision-v1' / 'continuation-manifest.json'
        atomic_json(copy, {**json.loads(self.path.read_text()), 'method': 'different'})
        link = self.path.with_name('linked.json')
        link.symlink_to(self.path)
        relative = self.path.relative_to(self.root).as_posix()
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[dict(
            manifest=relative, condition='candidate', name_zh='原始执行', manifest_copies=[
                copy.relative_to(self.root).as_posix(), '../outside.json', str(link), None])]))
        result = self.snapshot()
        self.assertEqual(len(result['errors']), 4)
        self.assertEqual(len(result['groups']), 2)
        original = next(group for group in result['groups'] if group['manifest'] == relative)
        self.assertEqual(original['name_zh'], '原始执行')

    def test_execution_scope_keeps_reused_run_out_of_original_fixed_denominator(self):
        first = self.run_case('original', 'candidate', self.protocol[0], .5)
        resumed = self.run_case('resumed', 'candidate', self.protocol[1], 1)
        self.write([first, resumed])
        continuation = self.path.with_name('continuation-manifest.json')
        atomic_json(continuation, {**json.loads(self.path.read_text()), 'cases': [resumed], 'protocol_cases': [self.protocol[1]]})
        relative = self.path.relative_to(self.root).as_posix()
        registry = self.root / 'service' / 'experiment-groups.json'
        atomic_json(registry, dict(groups=[dict(manifest=relative, condition='candidate', execution_run_dirs=[first['run_dir']])]))
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        result = self.snapshot()
        original = next(group for group in result['groups'] if group['manifest'] == relative)
        followup = next(group for group in result['groups'] if group['manifest'] != relative)
        self.assertEqual((original['case_count'], original['protocol_case_count']), (2, 2))
        self.assertTrue(original['protocol_denominator_known'])
        self.assertEqual([row['selection'] for row in original['cases']], ['single', 'missing'])
        self.assertEqual(original['cases'][0]['run']['summary']['normalized_score'], .5)
        self.assertEqual(followup['cases'][0]['run']['summary']['normalized_score'], 1)
        self.assertEqual(result['ungrouped_count'], 0)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*.json')})
        atomic_json(registry, dict(groups=[dict(manifest=relative, condition='candidate', execution_run_dirs=[])]))
        original = next(group for group in self.snapshot()['groups'] if group['manifest'] == relative)
        self.assertEqual((original['case_count'], original['protocol_case_count']), (2, 2))
        self.assertEqual([row['selection'] for row in original['cases']], ['missing', 'missing'])

    def test_invalid_execution_scope_isolates_entire_registry_entry(self):
        case = self.run_case('original', 'candidate', self.protocol[0], .5)
        self.write([case])
        link = self.root / 'linked-run'
        link.symlink_to(case['run_dir'], target_is_directory=True)
        base = dict(manifest=self.path.relative_to(self.root).as_posix(), condition='candidate', name_zh='invalid-entry')
        registry = self.root / 'service' / 'experiment-groups.json'
        for run_dirs in ([case['run_dir'], '/outside'], [None], [str(link)], 'not-a-list'):
            with self.subTest(run_dirs=run_dirs):
                atomic_json(registry, dict(groups=[dict(base, execution_run_dirs=run_dirs)]))
                result = self.snapshot()
                self.assertEqual(len(result['errors']), 1)
                self.assertNotIn('name_zh', result['groups'][0])
                self.assertEqual(result['groups'][0]['cases'][0]['selection'], 'single')

    def test_source_checkout_commit_are_projected_only_for_matching_worktree(self):
        case = self.run_case('candidate', 'candidate', self.protocol[0], .5)
        self.write([case], source_checkout=case['cwd'], source_commit='historical-revision')
        source = self.snapshot()['groups'][0]['source']
        self.assertEqual(source['revision'], 'historical-revision')
        self.assertIsNone(source['branch'])
        self.write([case], source_checkout='/different-checkout', source_commit='unrelated-revision')
        self.assertIsNone(self.snapshot()['groups'][0]['source']['revision'])

    def execution_manifest(self, name, cases, **extra):
        path = self.path.with_name(name + '-manifest.json')
        atomic_json(path, {**json.loads(self.path.read_text()), 'cases': cases, **extra})
        return dict(manifest=path.relative_to(self.root).as_posix(), condition='candidate')

    def test_preparation_time_never_becomes_execution_time(self):
        self.write([dict(run_dir=str(self.root / 'not-started'), cwd='/source', task='pick', seed=0, layout_id=0,
                         tag=['candidate', self.protocol[0]['case_id']])], prepared_at='2026-10-10T00:00:00Z')
        group = self.snapshot()['groups'][0]
        self.assertIsNone(group['started_at'])
        self.assertIsNone(group['finished_at'])
        self.assertIsNone(group['created_at'])
        self.assertEqual(group['prepared_at'], '2026-10-10T00:00:00.000000Z')
        self.assertEqual(group['time_coverage'], dict(run_count=0, started_count=0, finished_count=0))

    def test_merged_time_range_covers_unique_attempts_without_changing_score_order(self):
        original = self.run_case('original', 'candidate', self.protocol[0], 0)
        recovery = self.run_case('recovery', 'candidate', self.protocol[0], 1)
        partial = self.run_case('partial', 'candidate', self.protocol[1], .5)
        for case, start, finish in ((original, '2026-10-14T00:00:00Z', '2026-10-14T01:00:00Z'),
                                    (recovery, '2026-10-13T00:00:00Z', '2026-10-13T01:00:00Z'),
                                    (partial, None, '2026-10-15T00:00:00Z')):
            path = Path(case['run_dir']) / 'summary.json'
            atomic_json(path, {**json.loads(path.read_text()), 'started_at': start, 'finished_at': finish})
        self.write([original], prepared_at='2026-10-01T00:00:00Z')
        extra = self.execution_manifest('recovery', [recovery, partial])
        duplicate = self.execution_manifest('copy', [recovery])
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[dict(
            manifest=self.path.relative_to(self.root).as_posix(), condition='candidate', execution_groups=[extra, duplicate])]))
        group = self.snapshot()['groups'][0]
        self.assertEqual(group['started_at'], '2026-10-13T00:00:00.000000Z')
        self.assertEqual(group['finished_at'], '2026-10-15T00:00:00.000000Z')
        self.assertEqual(group['started_at_source'], 'attempts:min(started_at)')
        self.assertEqual(group['time_coverage'], dict(run_count=3, started_count=2, finished_count=3))
        row = group['cases'][0]
        self.assertEqual(row['run']['summary']['normalized_score'], 0)
        self.assertEqual([run['run_id'] for run in row['attempts']], ['original', 'recovery'])
        self.assertEqual(row['run']['started_at'], '2026-10-14T00:00:00.000000Z')
        self.assertEqual(row['run']['started_at_source'], 'summary.json:started_at')
        self.assertEqual(group['execution_groups'][0]['started_at'], '2026-10-14T00:00:00.000000Z')
        self.assertEqual(group['execution_groups'][1]['time_coverage'], dict(run_count=2, started_count=1, finished_count=2))

    def test_execution_merge_fills_unscored_keeps_first_zero_and_deduplicates_runs(self):
        first = self.run_case('failed', 'candidate', self.protocol[0], 0, scored=False)
        zero = self.run_case('official-zero', 'candidate', self.protocol[1], 0)
        missing = dict(case_id='pick__standard__g0__l9', task='pick', variant='standard', eval_seed=0, layout_id=9)
        self.write([first, zero], protocol_cases=[*self.protocol, missing], experiment_panels={})
        recovered = self.run_case('recovered', 'candidate', self.protocol[0], .4)
        improved = self.run_case('later-success', 'candidate', self.protocol[1], 1)
        recovery = self.execution_manifest('recovery', [recovered, improved])
        later = self.execution_manifest('later', [recovered, self.run_case('later-higher', 'candidate', self.protocol[0], 1)])
        relative = self.path.relative_to(self.root).as_posix()
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[dict(
            manifest=relative, condition='candidate', execution_groups=[recovery, later], name_zh='检查点方案', secret='never-visible')]))
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        result = self.snapshot()
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['groups']), 1)
        group = result['groups'][0]
        self.assertEqual(group['id'], group_id(relative, 'candidate'))
        self.assertEqual(group['id_aliases'], [group_id(**recovery), group_id(**later)])
        self.assertEqual((group['case_count'], group['protocol_case_count']), (3, 3))
        self.assertEqual([row['selection'] for row in group['cases']], ['first_scored', 'first_scored', 'missing'])
        self.assertEqual([row['run']['summary']['normalized_score'] for row in group['cases'][:2]], [.4, 0])
        self.assertEqual(len(group['cases'][0]['attempts']), 3)
        self.assertEqual([run['execution_group_id'] for run in group['cases'][0]['attempts']],
                         [group['id'], group_id(**recovery), group_id(**later)])
        self.assertTrue(group['cases'][0]['selection_reason'])
        self.assertEqual([execution['id'] for execution in group['execution_groups']], [group['id'], *group['id_aliases']])
        self.assertTrue(all('cases' not in execution for execution in group['execution_groups']))
        self.assertNotIn('never-visible', json.dumps(group))
        self.assertEqual(result['ungrouped_count'], 0)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*.json')})

    def test_execution_merge_latest_unscored_retains_status_without_score(self):
        first = self.run_case('failed', 'candidate', self.protocol[0], 0, scored=False)
        self.write([first])
        second = self.run_case('invalid-score', 'candidate', self.protocol[0], 1)
        atomic_json(Path(second['run_dir']) / 'summary.json', dict(normalized_score=1, score_status='invalid', status='completed'))
        recovery = self.execution_manifest('recovery', [second])
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[dict(
            manifest=self.path.relative_to(self.root).as_posix(), condition='candidate', execution_groups=[recovery])]))
        row = self.snapshot()['groups'][0]['cases'][0]
        self.assertEqual(row['selection'], 'latest_unscored')
        self.assertEqual(row['run']['run_id'], 'invalid-score')
        self.assertIsNone(row['run']['summary']['normalized_score'])
        self.assertEqual(row['run']['summary']['score_status'], 'invalid')
        self.assertEqual(len(row['attempts']), 2)

    def test_invalid_execution_reference_graphs_preserve_all_original_groups(self):
        case = self.run_case('first', 'candidate', self.protocol[0], .5)
        self.write([case])
        original = dict(manifest=self.path.relative_to(self.root).as_posix(), condition='candidate')
        recovery = self.execution_manifest('recovery', [case])
        other = self.execution_manifest('other', [case])
        missing = dict(manifest='benchmark/missing/manifest.json', condition='candidate')
        entries = [
            [dict(original, execution_groups=[missing])],
            [dict(original, execution_groups=[recovery, recovery])],
            [dict(original, execution_groups=[recovery]), dict(recovery, execution_groups=[original])],
            [dict(original, execution_groups=[recovery]), dict(other, execution_groups=[recovery])],
        ]
        for groups in entries:
            with self.subTest(groups=groups):
                atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=groups))
                result = self.snapshot()
                self.assertEqual(len(result['groups']), 3)
                self.assertEqual(len(result['errors']), 1)
                self.assertTrue(all('id_aliases' not in group for group in result['groups']))
                self.assertTrue(all(group['cases'][0]['run']['summary']['normalized_score'] == .5 for group in result['groups']))

    def test_execution_protocol_and_configuration_conflicts_preserve_original_groups(self):
        case = self.run_case('first', 'candidate', self.protocol[0], .5)
        self.write([case])
        original = dict(manifest=self.path.relative_to(self.root).as_posix(), condition='candidate')
        extra_case = dict(case_id='pick__standard__g0__l9', task='pick', variant='standard', eval_seed=0, layout_id=9)
        changes = [dict(protocol_cases=[*self.protocol, extra_case], experiment_panels={}),
                   dict(auriga_config={'model': {'name': 'different-model', 'effort': 'high'}})]
        for change in changes:
            with self.subTest(change=change):
                recovery = self.execution_manifest('recovery', [case], **change)
                metadata = dict(original, execution_groups=[recovery])
                atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[metadata]))
                result = self.snapshot()
                self.assertEqual(len(result['groups']), 2)
                self.assertEqual(len(result['errors']), 1)
                self.assertEqual(result['errors'][0]['error'], 'comparison_execution_merge_conflict')

    def test_explicit_execution_merge_preserves_different_source_revisions(self):
        case = self.run_case('first', 'candidate', self.protocol[0], .5)
        self.write([case], source_revision='original-source')
        recovery = self.execution_manifest('recovery', [case], source_revision='compatibility-fix')
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[dict(
            manifest=self.path.relative_to(self.root).as_posix(), condition='candidate', execution_groups=[recovery])]))
        result = self.snapshot()
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual([execution['source']['revision'] for execution in result['groups'][0]['execution_groups']],
                         ['original-source', 'compatibility-fix'])

    def test_execution_merge_remaps_relationships_and_clears_self_links(self):
        case = self.run_case('first', 'candidate', self.protocol[0], .5)
        self.write([case])
        original = dict(manifest=self.path.relative_to(self.root).as_posix(), condition='candidate')
        recovery = self.execution_manifest('recovery', [case])
        other = self.execution_manifest('other', [case])
        relations = dict(parent=recovery, baseline=recovery, recovery_of=recovery)
        atomic_json(self.root / 'service' / 'experiment-groups.json', dict(groups=[
            dict(original, execution_groups=[recovery], **relations), dict(other, **relations)]))
        result = self.snapshot()
        self.assertEqual(result['errors'], [])
        groups = {group['id']: group for group in result['groups']}
        identifier = group_id(**original)
        self.assertEqual(len(groups), 2)
        for relation in ('parent_group_id', 'baseline_group_id', 'recovery_of_group_id'):
            self.assertIsNone(groups[identifier][relation])
            self.assertEqual(groups[group_id(**other)][relation], identifier)
