"""CPU read contracts: incremental projections, sealed archives and streaming integrity."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from auriga.core.storage.read_models import record_updates
from auriga.apps.library.experiments import allocate_run, catalog_id
from auriga.apps.library.service import Library
from auriga.core.storage.store import Store, open_read_store


class ReadModelsTest(unittest.TestCase):
    def test_sealed_wal_reads_include_commits_without_source_sidecars(self):
        from auriga.apps.evaluation.audit import audit
        from auriga.apps.evaluation.report import summarize
        from auriga.apps.library.task_review import _recorded_instruction
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = Store(root / 'writer', dict(run_id='wal-archive', episode_id='episode',
                env_instance_id='env', env_generation=0))
            self.addCleanup(writer.close)
            writer.db.execute('PRAGMA wal_autocheckpoint=0')
            reference = writer.put_artifact(b'wal-evidence', 'trace', 'application/octet-stream')
            writer.event('fixture.wal_committed', {})
            writer.save('observation', 'obs', {'task_instruction': 'Use the WAL instruction'})
            writer.save('tool_call', 'call', {'params': {'tool': 'robot_observe'}})
            archive = root / 'archive'
            archive.mkdir()
            for name in ('events.sqlite', 'events.sqlite-wal'):
                shutil.copyfile(writer.root / name, archive / name)
            shutil.copytree(writer.blobs, archive / 'blobs')
            (archive / 'manifest.json').write_text(json.dumps({'run_id': 'wal-archive'}))
            (archive / 'summary.json').write_text(json.dumps({'task_success': None}))

            def fingerprint():
                return {str(p.relative_to(archive)): (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in archive.rglob('*') if p.is_file()}

            before = fingerprint()
            entry = {'path': archive.resolve(), 'metadata': {}, 'item': {'live': False}}
            library = Library(root)
            with library.store(entry) as reader:
                self.assertEqual(reader.events()[0]['type'], 'fixture.wal_committed')
                self.assertEqual(reader.resolve_artifact(reference)[1], b'wal-evidence')
                temporary_database = Path(reader.db.execute('PRAGMA database_list').fetchone()[2])
                self.assertNotEqual(temporary_database.parent, archive)
                with self.assertRaisesRegex(ValueError, 'store_is_read_only'):
                    reader.save('observation', 'forbidden', {})
            self.assertFalse(temporary_database.parent.exists())
            self.assertEqual(_recorded_instruction(archive), 'Use the WAL instruction')
            self.assertEqual(summarize(archive)['actor_tools'], {'robot_observe': 1})
            self.assertTrue(audit(archive)['passed'])
            self.assertEqual(fingerprint(), before)
            self.assertFalse((archive / 'events.sqlite-shm').exists())

    def test_sealed_wal_snapshot_rejects_concurrent_change_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = Store(root / 'writer', dict(run_id='wal-archive', episode_id='episode',
                env_instance_id='env', env_generation=0))
            self.addCleanup(writer.close)
            writer.event('fixture.committed', {})
            archive = root / 'archive'
            archive.mkdir()
            for name in ('events.sqlite', 'events.sqlite-wal'):
                shutil.copyfile(writer.root / name, archive / name)
            copy = shutil.copyfile

            def changing_copy(source, destination):
                copy(source, destination)
                if Path(source).name.endswith('-wal'):
                    with Path(source).open('ab') as stream:
                        stream.write(b'concurrent-change')

            with patch('auriga.core.storage.store.shutil.copyfile', side_effect=changing_copy):
                with self.assertRaisesRegex(ValueError, 'archived_database_changed_during_snapshot'):
                    with open_read_store(archive):
                        self.fail('unstable evidence must not be read')
            self.assertFalse((archive / 'events.sqlite-shm').exists())
            (archive / 'events.sqlite-wal').unlink()
            (archive / 'events.sqlite-wal').symlink_to(writer.root / 'events.sqlite-wal')
            with self.assertRaisesRegex(ValueError, 'database_symlink_denied'):
                with open_read_store(archive):
                    self.fail('symlink evidence must not be read')

    def test_actor_turn_queries_filter_evaluator_and_other_turns_in_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, dict(run_id='fixture', episode_id='episode', env_instance_id='env', env_generation=0))
            self.addCleanup(store.close)
            correlation = dict(thread_id='actor-thread', turn_id='actor-turn')
            first = store.event('motion.cost_preview', {'visible': 1}, correlation)
            store.event('observer.evaluated', {'privileged_marker': True}, correlation)
            for kind in ('action.batch_preview', 'program.call_started'):
                store.event(kind, {'privileged_marker': True}, {**correlation, 'turn_id': 'other'})
                store.event(kind, {'privileged_marker': True}, {**correlation, 'thread_id': 'other'})
                with patch.dict(store.identity, episode_id='other'):
                    store.event(kind, {'privileged_marker': True}, correlation)
            request = store.event('program.call_started', {'visible': 'request'}, correlation)
            last = store.event('action.batch_preview', {'visible': 2}, correlation)
            decode = json.loads

            def allowed_decode(body):
                self.assertNotIn('privileged_marker', body, 'excluded event reached actor-side Python decoding')
                return decode(body)

            with patch('auriga.core.storage.store.json.loads', side_effect=allowed_decode):
                events = store.turn_preview_events(thread_id='actor-thread', turn_id='actor-turn', episode_id='episode')
            self.assertEqual([value['server_seq'] for value in events],
                             [first['server_seq'], request['server_seq'], last['server_seq']])

            item = dict(thread_id='actor-thread', turn_id='actor-turn', type='agentMessage',
                        status='completed', phase='final_answer', text='first final')
            for index, updates in enumerate(({}, {'phase': 'commentary'}, {'type': 'userMessage'},
                    {'status': 'running'}, {'thread_id': 'other'}, {'turn_id': 'other'}, {'text': 42},
                    {'text': '   '}, {'text': 'last final'})):
                store.set_state('codex_item', str(index), {**item, **updates})
            self.assertEqual(store.completed_final_texts('actor-thread', 'actor-turn'), ['first final', 'last final'])

    def test_record_sequence_keeps_numeric_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, dict(run_id='fixture', episode_id='episode', env_instance_id='env', env_generation=0))
            self.addCleanup(store.close)
            receipt = {'status': 'unknown', 'executed_native_steps': 1}
            store.save('action_receipt', 'a2', receipt)
            store.save('action_request', 'a999', receipt)
            for identifier in ('a10', 'a300tail', 'pa999', 'a', 'a²'):
                store.save('action_receipt', identifier, receipt)
            self.assertEqual(store.record_sequence('action_receipt', 'a'), 10)
            self.assertEqual(store.record_sequence('action_request', 'a'), 999)
            self.assertEqual(store.record_sequence('unified_action_plan', 'p'), 0)
            for identifier in ('a2', 'a2', 'a10', None):
                store.event('action.accepted', {}, {'action_id': identifier})
            store.event('action.completed', {}, {'action_id': 'excluded'})
            self.assertEqual(store.accepted_action_count(), 2)

    def test_scene_job_read_preserves_sealed_archive_without_service_startup(self):
        from auriga.perception.scene.scene_service import read_scene_job
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root, {})
            with store.db:
                store.db.execute('CREATE TABLE scene_jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
                store.db.execute('INSERT INTO scene_jobs VALUES (?,?)', ('job', json.dumps({'status': 'failed'})))
            store.close()
            before = (root / 'events.sqlite').read_bytes()
            store = Store(root, {}, read_only=True, immutable=True)
            try:
                with patch('auriga.perception.scene.scene_service.SceneService.__init__', side_effect=AssertionError('read cannot start service')):
                    self.assertEqual(read_scene_job(store, 'job'), {'status': 'failed'})
                    with self.assertRaises(KeyError):
                        read_scene_job(store, 'missing')
            finally:
                store.close()
            self.assertEqual((root / 'events.sqlite').read_bytes(), before)
            self.assertFalse((root / 'events.sqlite-wal').exists())
            self.assertFalse((root / 'events.sqlite-shm').exists())

    def test_event_type_queries_keep_global_sequence_and_bound_results(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, dict(run_id='fixture', episode_id='episode', env_instance_id='env', env_generation=0))
            self.addCleanup(store.close)
            for kind in ('observer.evaluated', 'action.completed', 'observer.evaluated'):
                store.event(kind, {})
            self.assertEqual([value['server_seq'] for value in store.events_by_type(['observer.evaluated'])], [1, 3])
            self.assertEqual(store.events_by_type(['observer.evaluated'], after=1, newest_first=True, limit=1)[0]['server_seq'], 3)
            with self.assertRaisesRegex(ValueError, 'invalid_event_query'):
                store.events_by_type('observer.evaluated')

    def test_mutable_paging_preserves_updates_and_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, {})
            self.addCleanup(store.close)
            for index in range(4):
                store.set_state('codex_item', str(index), {'id': str(index), 'text': 'initial'})
            first = store.state_page('codex_item', limit=2)
            store.set_state('codex_item', '0', {'id': '0', 'text': 'updated'})
            second = store.state_page('codex_item', first['cursor'], limit=2)
            third = store.state_page('codex_item', second['cursor'], limit=2)
            self.assertEqual([row['id'] for row in first['entries'] + second['entries'] + third['entries']], ['0', '1', '2', '3', '0'])
            self.assertEqual(first['entries'][0]['position'], third['entries'][0]['position'])
            store.set_state('codex_item', '0', {'id': '0', 'text': 'updated'})
            self.assertEqual(store.state_page('codex_item', third['cursor'])['entries'], [])
            self.assertTrue(store.state_page('codex_item', 9999)['reset'])
            old = store.review_page(before=3, limit=1)
            self.assertEqual(old['items'][0]['id'], '1')
            self.assertTrue(old['has_more'])

    def test_legacy_readonly_projection_does_not_upgrade_or_create_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root / 'events.sqlite') as db:
                db.executescript("CREATE TABLE state(kind TEXT,id TEXT,body TEXT); CREATE TABLE records(kind TEXT,id TEXT,body TEXT); CREATE TABLE events(seq INTEGER,body TEXT);")
                db.execute("INSERT INTO state VALUES ('codex_item','old','{\"id\":\"old\"}')")
            before = (root / 'events.sqlite').read_bytes()
            store = Store(root, {}, read_only=True, immutable=True)
            try:
                page = record_updates(store, {})
                self.assertFalse(page['items']['incremental'])
                self.assertEqual(page['items']['entries'][0]['id'], 'old')
            finally:
                store.close()
            self.assertEqual(before, (root / 'events.sqlite').read_bytes())
            self.assertEqual([p.name for p in root.iterdir()], ['events.sqlite'])

    def test_streamed_digest_cache_detects_same_size_mutation_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, {})
            self.addCleanup(store.close)
            ref = store.put_artifact(b'original-video', 'video', 'video/mp4', 'observer')
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-file allocation')), patch('hashlib.file_digest', wraps=hashlib.file_digest) as digest:
                _, path = store.artifact_file(ref, ('observer',))
                store.artifact_file(ref, ('observer',))
                self.assertEqual(digest.call_count, 1)
                with self.assertRaisesRegex(ValueError, 'scope_denied'):
                    store.artifact_file(ref)
                path.write_bytes(b'corruptd-video')
                with self.assertRaisesRegex(ValueError, 'digest_mismatch'):
                    store.artifact_file(ref, ('observer',))


class LibrarySnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_background_scan_preserves_snapshot_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocate_run(root, category='synthetic', task='test', run_id='snapshot')
            library = Library(root, scan_interval=.01)
            async with TestClient(TestServer(library.app())) as client:
                original_refresh = library.refresh
                snapshot, observed_at = library.catalog, library.refreshed_at
                recovered = asyncio.Event()
                during_failure = {}
                attempts = 0

                async def refresh():
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise OSError('temporary scan failure')
                    if attempts == 2:
                        during_failure.update(catalog=library.catalog, observed_at=library.refreshed_at,
                                              listing=await (await client.get('/v1/experiments')).json())
                    await original_refresh()
                    recovered.set()

                with patch.object(library, 'refresh', side_effect=refresh):
                    await asyncio.wait_for(recovered.wait(), 2)
                self.assertGreaterEqual(attempts, 2)
                self.assertIs(during_failure['catalog'], snapshot)
                self.assertEqual(during_failure['observed_at'], observed_at)
                self.assertEqual(during_failure['listing']['total'], 1)
                self.assertEqual(during_failure['listing']['errors'][-1]['error'], 'catalog_refresh_failed')
                self.assertIsNone(library.refresh_error)
                self.assertEqual((await (await client.get('/v1/experiments')).json())['errors'], [])

    async def test_gateway_and_scene_use_streaming_media_with_their_original_scopes(self):
        from auriga.apps.gateway.workbench import Workbench
        from auriga.perception.scene.scene_service import SceneService
        with tempfile.TemporaryDirectory() as directory:
            for kind in ('gateway', 'scene'):
                with self.subTest(kind=kind):
                    root = Path(directory) / kind
                    owner = (Workbench(root, robot=SimpleNamespace(close=lambda: None)) if kind == 'gateway'
                             else SceneService(root))
                    try:
                        internal = owner.store.put_artifact(b'0123456789', 'video', 'video/mp4', 'internal')
                        observer = owner.store.put_artifact(b'observer-video', 'video', 'video/mp4', 'observer')
                        async with TestClient(TestServer(owner.app())) as client:
                            with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-file allocation')):
                                url = '/v1/artifacts/' + internal.removeprefix('artifact:')
                                self.assertEqual((await client.head(url)).status, 200)
                                response = await client.get(url, headers={'Range': 'bytes=2-4'})
                                self.assertEqual(response.status, 206)
                                self.assertEqual(await response.read(), b'234')
                                response = await client.get('/v1/artifacts/' + observer.removeprefix('artifact:'))
                                self.assertEqual(response.status, 200 if kind == 'gateway' else 422)
                            if kind == 'gateway':
                                response = await client.get('/v1/records')
                                self.assertEqual(response.status, 200)
                                self.assertEqual((await response.json())['schema_version'], '1')
                    finally:
                        if kind == 'gateway':
                            await owner.close()

    async def test_requests_share_background_snapshot_and_stream_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = allocate_run(root, category='synthetic', task='test', run_id='snapshot')
            store = Store(run, {})
            ref = store.put_artifact(b'0123456789', 'video', 'video/mp4', 'observer')
            store.close()
            library = Library(root, scan_interval=3600)
            async with TestClient(TestServer(library.app())) as client:
                identifier = catalog_id(run.relative_to(root).as_posix())
                with patch.object(library, 'refresh', side_effect=AssertionError('request must not scan')):
                    for _ in range(3):
                        response = await client.get('/v1/experiments')
                        self.assertEqual(response.status, 200)
                        self.assertEqual((await response.json())['total'], 1)
                    url = f'/runs/{identifier}/v1/artifacts/{ref.removeprefix("artifact:")}'
                    with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-file allocation')):
                        response = await client.head(url)
                        self.assertEqual(response.status, 200)
                        response = await client.get(url, headers={'Range': 'bytes=2-4'})
                        self.assertEqual(response.status, 206)
                        self.assertEqual(await response.read(), b'234')
                    # Catalog caching must not authorize a directory replaced after publication.
                    held = run.with_name('held-snapshot')
                    run.rename(held)
                    run.symlink_to(held, target_is_directory=True)
                    response = await client.get(f'/runs/{identifier}/v1/records')
                    self.assertEqual(response.status, 400)
                    self.assertIn('symlink', (await response.json())['error'])


if __name__ == '__main__':
    unittest.main()
