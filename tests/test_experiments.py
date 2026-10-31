"""Synthetic evidence fixtures; real loopback HTTP/SSE/WS and archive byte checks."""
import asyncio
import base64
import fcntl
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from aiohttp import ClientSession, ContentTypeError, WSMsgType, WSServerHandshakeError, web
from auriga.apps.library.experiments import (Catalog, allocate_run, atomic_json, catalog_id, endpoint_origin,
                                 file_hash, finish_run, import_run, register_run, write_runtime)
from auriga.apps.library.service import Library
from auriga.core.storage.store import Store, open_read_store


def hashes(root):
    return {p.relative_to(root).as_posix(): file_hash(p) for p in root.rglob('*') if p.is_file()}


def fixture(path, name):
    identity = dict(run_id=path.name, episode_id='synthetic-episode', env_instance_id='synthetic-env', env_generation=1)
    store = Store(path, identity)
    ref = store.put_artifact(b'synthetic-image-' + name.encode(), 'rgb', 'image/png')
    denied = store.put_artifact(b'private-fixture', 'secret', 'application/octet-stream', 'private')
    observation = dict(schema_version='0.1', **identity, observation_id=name, data_origin='synthetic_fixture', cameras=[dict(rgb_ref=ref)])
    store.save('observation', name, observation)
    store.save('action_request', 'action-' + name, dict(action_id='action-' + name, **identity))
    store.save('action_receipt', 'action-' + name, dict(status='completed', data_origin='synthetic_fixture'))
    store.set_state('runtime', 'identity', identity)
    store.set_state('runtime', 'capabilities', {'data_origin': 'synthetic_fixture'})
    store.event('observation.available', {'observation_id': name, 'data_origin': 'synthetic_fixture'})
    store.close()
    return ref.removeprefix('artifact:'), denied.removeprefix('artifact:')


class CatalogCheck(unittest.TestCase):
    def test_run_times_are_evidence_based_utc_and_cached_without_changing_sealed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'catalog'
            run = allocate_run(root, category='validation', task='pick', run_id='sealed')
            identity = dict(run_id=run.name, env_instance_id='fixture', env_generation=1)
            writer = Store(base / 'writer', identity)
            try:
                writer.event('observation.available', {}, occurred_at='2026-10-10T01:00:00Z')
                writer.event('run.started', {}, occurred_at='2026-10-10T09:01:02+08:00')
                writer.event('run.stopped', {}, occurred_at='2026-10-10T01:02:00Z')
                for name in ('events.sqlite', 'events.sqlite-wal'):
                    shutil.copy2(writer.root / name, run / name)
            finally:
                writer.close()
            atomic_json(run / 'summary.json', dict(status='stopped', finished_at='2026-10-10T01:03:04Z'))
            before = {file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in run.iterdir() if file.is_file()}
            catalog = Catalog(root)
            with patch('auriga.apps.library.experiments.open_read_store', wraps=open_read_store) as opened:
                catalog.scan()
                item = catalog.list()['experiments'][0]
                self.assertEqual(item['started_at'], '2026-10-10T01:01:02.000000Z')
                self.assertEqual(item['finished_at'], '2026-10-10T01:03:04.000000Z')
                self.assertEqual(item['started_at_source'], 'events.sqlite:run.started#2')
                self.assertEqual(item['finished_at_source'], 'summary.json:finished_at')
                self.assertNotEqual(item['registered_at'], item['started_at'])
                self.assertEqual(item['time_coverage'], dict(run_count=1, started_count=1, finished_count=1))
                self.assertEqual(item['time_warnings'], [])
                self.assertFalse(catalog.scan())
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(opened.call_args.kwargs, dict(live=False))
            self.assertEqual(before, {file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in run.iterdir() if file.is_file()})

    def test_import_times_come_from_original_report_not_import_lifecycle_or_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = allocate_run(root, category='legacy', task='pick', run_id='imported')
            metadata = json.loads((run / 'experiment.json').read_text())
            metadata.update(imported_from='/original', evidence_root='evidence', date_source='report_mtime',
                            created_at='2026-10-12T00:00:00Z', imported_at='2026-10-14T00:00:00Z')
            atomic_json(run / 'experiment.json', metadata)
            atomic_json(run / 'runtime.json', dict(finished_at='2026-10-14T00:00:01Z', status='imported'))
            atomic_json(run / 'summary.json', dict(finished_at='2026-10-14T00:00:01Z', status='imported'))
            atomic_json(run / 'evidence' / 'validation-report.json', dict(
                started_at='2026-09-30T23:59:58-04:00', finished_at='2026-10-01T00:00:03-04:00'))
            catalog = Catalog(root); catalog.scan()
            item = catalog.list()['experiments'][0]
            self.assertEqual(item['started_at'], '2026-10-01T03:59:58.000000Z')
            self.assertEqual(item['finished_at'], '2026-10-01T04:00:03.000000Z')
            self.assertEqual(item['started_at_source'], 'evidence/validation-report.json:started_at')
            self.assertIsNone(item['registered_at'])
            self.assertEqual(item['imported_at'], '2026-10-14T00:00:00.000000Z')
            atomic_json(run / 'evidence' / 'experiment.json', dict(created_at='2026-09-30T23:59:50-04:00'))
            catalog.scan()
            self.assertEqual(catalog.list()['experiments'][0]['registered_at'], '2026-10-01T03:59:50.000000Z')

    def test_missing_invalid_and_reversed_times_remain_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, report in (
                ('missing', {}), ('naive', dict(started_at='2026-10-14T00:00:00', finished_at='bad')),
                ('epoch', dict(started_at=1791538189.6109202, finished_at=True)),
                ('reversed', dict(started_at='2026-10-14T01:00:00Z', finished_at='2026-10-14T00:00:00Z'))):
                run = allocate_run(root, category='validation', task='pick', run_id=name)
                atomic_json(run / 'summary.json', dict(report, elapsed_wall_s=100))
            catalog = Catalog(root); catalog.scan()
            items = {item['run_id']: item for item in catalog.list()['experiments']}
            self.assertEqual(catalog.errors, [])
            for name in ('missing', 'naive', 'epoch'):
                self.assertIsNone(items[name]['started_at'])
                self.assertIsNone(items[name]['finished_at'])
            self.assertTrue(items['naive']['time_warnings'])
            self.assertIsNone(items['reversed']['finished_at'])
            self.assertIsNone(items['reversed']['finished_at_source'])
            self.assertIn('finished_before_started:summary.json:finished_at', items['reversed']['time_warnings'])

    def test_start_sort_and_date_filter_use_execution_time_with_unknown_last(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, start in (('early', '2026-10-10T15:59:59Z'), ('late', '2026-10-10T16:00:00Z'), ('unknown', None)):
                run = allocate_run(root, category='validation', task='pick', run_id=name)
                if start:
                    atomic_json(run / 'summary.json', dict(started_at=start))
            catalog = Catalog(root); catalog.scan()
            self.assertEqual([item['run_id'] for item in catalog.list()['experiments']], ['late', 'early', 'unknown'])
            self.assertEqual([item['run_id'] for item in catalog.list(dict(sort='oldest'))['experiments']], ['early', 'late', 'unknown'])
            items = catalog.list(dict(date_from='2026-10-11', date_to='2026-10-11'))['experiments']
            self.assertEqual([item['run_id'] for item in items], ['late'])

    def test_event_time_cache_tracks_live_wal_changes_and_ignores_unrelated_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = allocate_run(root, category='validation', task='pick', run_id='active')
            writer = Store(run, dict(run_id=run.name, env_instance_id='fixture', env_generation=1))
            try:
                writer.event('observation.available', {}, occurred_at='2026-10-10T01:00:00Z')
                write_runtime(run, dict(status='running'))
                catalog = Catalog(root)
                with patch('auriga.apps.library.experiments.open_read_store', wraps=open_read_store) as opened:
                    catalog.scan()
                    self.assertIsNone(catalog.list()['experiments'][0]['started_at'])
                    self.assertTrue(opened.call_args.kwargs['live'])
                    writer.event('run.started', {}, occurred_at='2026-10-10T01:01:02Z')
                    catalog.scan()
                    self.assertEqual(catalog.list()['experiments'][0]['started_at'], '2026-10-10T01:01:02.000000Z')
                    self.assertEqual(opened.call_count, 2)
                    atomic_json(run / 'summary.json', dict(finished_at='2026-10-10T01:02:03Z'))
                    catalog.scan()
                    self.assertEqual(opened.call_count, 2)
                    self.assertEqual(catalog.list()['experiments'][0]['finished_at'], '2026-10-10T01:02:03.000000Z')
            finally:
                writer.close()

    def test_wrong_run_events_cannot_supply_times_and_stop_event_is_not_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, identity_run in (('correct', 'correct'), ('wrong', 'different-run')):
                run = allocate_run(root, category='validation', task='pick', run_id=name)
                writer = Store(run, dict(run_id=identity_run, env_instance_id='fixture', env_generation=1))
                writer.event('run.started', {}, occurred_at='2026-10-10T01:00:00Z')
                writer.event('run.stopped', {}, occurred_at='2026-10-10T02:00:00Z')
                writer.close()
            catalog = Catalog(root); catalog.scan()
            items = {item['run_id']: item for item in catalog.list()['experiments']}
            self.assertEqual(items['correct']['started_at'], '2026-10-10T01:00:00.000000Z')
            self.assertIsNone(items['correct']['finished_at'])
            self.assertIsNone(items['wrong']['started_at'])
            self.assertIsNone(items['wrong']['finished_at'])
            self.assertIn('run_started_identity_conflict', items['wrong']['time_warnings'])

    def test_discover_custom_layouts_preserving_ids_and_run_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = allocate_run(root, category='main', task='pick', run_id='canonical')
            benchmark = root / 'benchmark' / 'group' / 'batch' / 'runs' / 'case'
            experiment = root / 'experiments' / 'image-interface'
            for path in (benchmark, experiment):
                register_run(path, category='validation', task='pick')
            # Archived originals and private metadata must not become extra runs.
            for relative in ('evidence/original', 'blobs/fixture', 'private/nested'):
                register_run(benchmark / relative, category='legacy', task='pick')
            expected = {catalog_id(path.relative_to(root).as_posix()): path
                        for path in (canonical, benchmark, experiment)}
            catalog = Catalog(root)
            self.assertTrue(catalog.scan())
            self.assertEqual({key: entry['path'] for key, entry in catalog.entries.items()}, expected)
            self.assertEqual(catalog.errors, [])
            self.assertFalse(catalog.scan())
            self.assertEqual(set(catalog.entries), set(expected))
            # New externally located runs are picked up on subsequent scans.
            later = root / 'benchmark' / 'group' / 'next-batch' / 'runs' / 'case'
            register_run(later, category='benchmark', task='pick')
            self.assertTrue(catalog.scan())
            self.assertEqual(catalog.list()['total'], 4)
            self.assertEqual(catalog.get(catalog_id(later.relative_to(root).as_posix()))['path'], later)

    def test_discovery_excludes_private_paths_and_symlink_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'catalog'
            valid = root / 'experiments' / 'visible'
            external = base / 'external'
            for path in (valid, external, root / 'private' / 'hidden',
                         root / 'experiments' / '.aws' / 'hidden',
                         root / 'experiments' / 'credentials.json' / 'hidden',
                         root / 'experiments' / '.env' / 'hidden'):
                register_run(path, category='validation', task='pick')
            (root / 'external-link').symlink_to(external, target_is_directory=True)
            (root / 'internal-link').symlink_to(valid, target_is_directory=True)
            linked_metadata = root / 'experiments' / 'linked-metadata'
            linked_metadata.mkdir()
            (linked_metadata / 'experiment.json').symlink_to(external / 'experiment.json')
            catalog = Catalog(root)
            catalog.scan()
            self.assertEqual([entry['path'] for entry in catalog.entries.values()], [valid])
            self.assertEqual(catalog.errors, [dict(path='experiments/linked-metadata/experiment.json',
                                                 error='symlink_metadata_denied')])

    def test_catalog_import_validation_and_preserve_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = allocate_run(base / 'catalog', category='reference', task='stack_bowls', title='Synthetic first', tags=['cpu'], run_id='one')
            write_runtime(first, dict(status='running', source='recorded_stream'))
            finish_run(first, status='completed', summary=dict(task_success=False, normalized_score=0))
            catalog = Catalog(base / 'catalog')
            self.assertTrue(catalog.scan())
            listing = catalog.list({'category': 'reference', 'tag': 'cpu', 'q': 'synthetic'})
            self.assertEqual(listing['total'], 1)
            self.assertIs(listing['experiments'][0]['summary']['task_success'], False)
            self.assertEqual(listing['experiments'][0]['summary']['score'], 0)
            self.assertEqual(catalog.list({'task': 'missing'})['total'], 0)
            stable = catalog.list()['experiments'][0]['id']
            catalog.scan()
            self.assertEqual(stable, catalog.list()['experiments'][0]['id'])
            for bad in ('../x', 'x/y', '/tmp/x', '.', '..', 'x\\y'):
                with self.assertRaises(ValueError): allocate_run(base / 'catalog', category=bad, task='stack_bowls')
            for bad in ('http://example.com:42', 'http://127.0.0.1:42/private', 'http://user@127.0.0.1:42', 'http://127.0.0.1:42/?target=http://other'):
                with self.assertRaises(ValueError): endpoint_origin(bad)
            source = base / 'legacy'; source.mkdir(); fixture(source, 'legacy-observation')
            (source / 'private' / 'codex-home').mkdir(parents=True)
            (source / 'private' / 'codex-home' / 'auth.json').write_text('private credential fixture')
            (source / 'link').symlink_to(source / 'events.sqlite')
            (source / 'reference-report.json').write_text(json.dumps(dict(passed=True, evaluation=dict(task_success=False, normalized_score=0))))
            with (source / 'gateway.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, 'unowned'): import_run(source, base / 'catalog')
            before = hashes(source)
            copied = import_run(source, base / 'catalog', category='reference', task='stack_bowls')
            self.assertEqual(before, hashes(source))
            inventory = json.loads((copied / 'import-inventory.json').read_text())
            self.assertIn('private/codex-home/auth.json', inventory['excluded']); self.assertIn('link', inventory['excluded'])
            self.assertFalse((copied / 'evidence' / 'private' / 'codex-home' / 'auth.json').exists())
            self.assertEqual(file_hash(source / 'events.sqlite'), file_hash(copied / 'evidence' / 'events.sqlite'))
            catalog.scan(); self.assertEqual(catalog.list()['total'], 2)


class LibraryCheck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.first = allocate_run(self.root, category='main', task='stack_bowls', run_id='synthetic-first')
        self.second = allocate_run(self.root, category='reference', task='stack_bowls', run_id='synthetic-second')
        self.artifact, self.denied = fixture(self.first, 'obs-first'); fixture(self.second, 'obs-second')
        (self.first / 'terminal.bin').write_bytes(b'synthetic terminal bytes')
        atomic_json(self.first / 'terminal.json', dict(session_id='synthetic-pty', cols=80, rows=24, exit_code=0))
        self.before = hashes(self.root); self.library = Library(self.root, scan_interval=.1); self.runners = []
        self.url = await self.start_server(self.library.app()); self.client = ClientSession()
        self.id1 = catalog_id(self.first.relative_to(self.root).as_posix()); self.id2 = catalog_id(self.second.relative_to(self.root).as_posix())

    async def start_server(self, app):
        runner = web.AppRunner(app, shutdown_timeout=.2); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start(); self.runners.append(runner)
        return f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

    async def asyncTearDown(self):
        await self.client.close()
        for runner in reversed(self.runners): await runner.cleanup()
        self.tmp.cleanup()

    async def get(self, path, **kwargs):
        async with self.client.get(self.url + path, **kwargs) as response:
            try: value = await response.json()
            except (ValueError, ContentTypeError): value = {'error': await response.text()}
            return response.status, value

    async def sse_event(self, response):
        lines = []
        async with asyncio.timeout(3):
            while True:
                line = await response.content.readline()
                if not line: raise AssertionError('SSE ended before expected event')
                if line == b'\n' and lines:
                    if any(line.startswith(b'data:') for line in lines): return b''.join(lines)
                    lines = []
                else: lines.append(line)

    async def test_malformed_metadata_and_symlinks_cannot_leak_other_runs(self):
        runtime = self.first / 'runtime.json'; runtime.write_text('[]')
        await self.library.refresh()
        status, listing = await self.get('/v1/experiments'); self.assertEqual(status, 200)
        self.assertEqual(listing['total'], 1); self.assertTrue(listing['errors']); runtime.unlink()
        secret = self.second / 'auth.json'; secret.write_text(json.dumps(dict(secret='synthetic-private-value')))
        runtime.symlink_to(secret)
        await self.library.refresh()
        status, listing = await self.get('/v1/experiments'); self.assertEqual(status, 200); self.assertEqual(listing['total'], 1)
        self.assertNotIn('synthetic-private-value', json.dumps(listing)); runtime.unlink()
        database = self.first / 'events.sqlite'; held_database = self.first / 'events-held.sqlite'
        database.rename(held_database); database.symlink_to(self.second / 'events.sqlite')
        status, value = await self.get(f'/runs/{self.id1}/v1/observations/latest')
        self.assertIn(status, (400,403,404)); self.assertNotIn('obs-second', json.dumps(value))
        database.unlink(); held_database.rename(database)
        await self.library.refresh()
        terminal = self.first / 'terminal.bin'; held_terminal = self.first / 'terminal-held.bin'
        terminal.rename(held_terminal); terminal.symlink_to(secret)
        try: ws = await self.client.ws_connect(self.url + f'/runs/{self.id1}/v1/terminal')
        except WSServerHandshakeError as error: self.assertIn(error.status, (400,403,404))
        else:
            await ws.close(); self.fail('terminal symlink accepted')
        terminal.unlink(); held_terminal.rename(terminal)

    async def test_malformed_nested_summary_does_not_block_library_startup(self):
        atomic_json(self.first / "summary.json", dict(status="unknown", scoring_policy=None))
        library = Library(self.root)
        url = await self.start_server(library.app())
        async with self.client.get(url + "/v1/experiments") as response:
            self.assertEqual(response.status, 200)
            listing = await response.json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["experiments"][0]["id"], self.id2)
        self.assertEqual(len(listing["errors"]), 1)
        self.assertIn("summary_field_requires_object:scoring_policy", listing["errors"][0]["error"])

    async def test_experiment_groups_are_readonly_published_snapshots(self):
        manifest = self.root / 'benchmark' / 'fixture' / 'manifest.json'
        atomic_json(manifest, dict(method='fixture', protocol_cases=[dict(task='stack_bowls', variant='standard',
            eval_seed=0, layout_id=0)], cases=[dict(task='stack_bowls', seed=0, layout_id=0,
            cwd='/fixture-source', run_dir=str(self.first))]))
        await self.library.refresh()
        before = hashes(self.root)
        status, value = await self.get('/v1/experiment-groups')
        self.assertEqual(status, 200)
        self.assertEqual(value['groups'][0]['cases'][0]['run']['id'], self.id1)
        self.assertEqual(value['ungrouped_count'], 1)
        self.assertEqual(value['revision'], self.library.revision)
        self.assertIsNotNone(value['observed_at'])
        self.assertEqual(before, hashes(self.root))
        async with self.client.post(self.url + '/v1/experiment-groups', json={}) as response:
            self.assertEqual(response.status, 405)

    async def test_readonly_replay_isolated_range_scope_and_sha(self):
        reader = Store(self.first, {}, read_only=True, immutable=True)
        try:
            self.assertEqual(reader.records('observation')[0]['observation_id'], 'obs-first')
            with self.assertRaisesRegex(ValueError, 'read_only'): reader.event('forbidden', {})
        finally: reader.close()
        prefix = f'/runs/{self.id1}/v1/'
        status, observation = await self.get(prefix + 'observations/latest')
        self.assertEqual(status, 200); self.assertEqual(observation['observation_id'], 'obs-first')
        self.assertEqual((await self.get(prefix + 'observations/obs-second'))[0], 404)
        self.assertEqual((await self.get(prefix + 'runs/synthetic-second'))[0], 404)
        status, run = await self.get(prefix + 'run')
        self.assertTrue(run['read_only']); self.assertFalse(run['writable']); self.assertTrue(run['terminal_available'])
        async with self.client.post(self.url + prefix + 'control', json=dict(request_id='no-write', command='prompt')) as response: self.assertEqual(response.status,403)
        async with self.client.get(self.url + prefix + 'artifacts/' + self.artifact, headers={'Range':'bytes=0-8'}) as response:
            self.assertEqual(response.status,206); self.assertEqual(await response.read(), b'synthetic'); self.assertIn('X-Artifact-SHA256',response.headers)
        self.assertEqual((await self.get(prefix + 'artifacts/' + self.denied))[0],400)
        async with self.client.get(self.url + prefix + 'events') as response:
            event = await self.sse_event(response); self.assertIn(b'obs-first',event); self.assertNotIn(b'obs-second',event)
        async with self.client.ws_connect(self.url + prefix + 'terminal') as ws:
            ready=await ws.receive_json(); output=await ws.receive_json()
            self.assertEqual(ready['type'],'ready'); self.assertEqual(base64.b64decode(output['data']),b'synthetic terminal bytes')
        status,detail=await self.get(f'/v1/experiments/{self.id1}'); self.assertEqual(status,200); self.assertTrue(detail['replay_available'])
        self.assertEqual(self.before,hashes(self.root))
        for headers in ({'Host':'attacker.example'},{'Origin':'http://attacker.example'}):
            async with self.client.get(self.url + prefix + 'run',headers=headers) as response: self.assertEqual(response.status,403)
        async with self.client.get(self.url + f'/v1/experiments/{self.id1}/files/../../private/auth.json') as response: self.assertEqual(response.status,404)

    async def test_auto_discovery_recorded_stream_update_and_terminal(self):
        async with self.client.get(self.url + '/v1/experiments/events') as response:
            self.assertIn(b'event: snapshot',await self.sse_event(response))
            third=allocate_run(self.root,category='reference',task='pick',title='Synthetic live stream'); fixture(third,'obs-third')
            write_runtime(third,dict(status='running',source='recorded_stream'))
            self.assertIn(b'event: changed',await self.sse_event(response))
            status,listing=await self.get('/v1/experiments?category=reference&task=pick&sort=active'); self.assertEqual(listing['total'],1)
            item=listing['experiments'][0]; self.assertTrue(item['live']); self.assertTrue(item['read_only']); self.assertFalse(item['terminal_available'])
            prefix=item['view_url']+'v1/'
            async with self.client.get(self.url+prefix+'events?after=1') as stream:
                store=Store(third,dict(run_id=third.name,episode_id='synthetic-episode',env_instance_id='synthetic-env',env_generation=1))
                try:
                    store.event('fixture.updated',dict(data_origin='synthetic_fixture')); self.assertIn(b'fixture.updated',await self.sse_event(stream))
                finally: store.close()
            finish_run(third,status='completed',summary=dict(task_success=False,normalized_score=0))
            await self.library.refresh()
            status,run=await self.get(prefix+'run'); self.assertFalse(run['library']['live']); self.assertTrue(run['read_only']); self.assertEqual(run['view_mode'],'replay')

    async def test_verified_http_sse_ws_proxy_origin_and_fallback(self):
        state={'run_id':self.first.name,'origins':[],'run_headers':[]}; app=web.Application()
        async def run(request): return web.json_response(dict(run_id=state['run_id'],status='running',terminal=dict(session_id='synthetic-live-pty'),data_origin='synthetic_fixture'))
        async def control(request):
            state['origins'].append(request.headers.get('Origin')); state['run_headers'].append(request.headers.get('X-Auriga-Run-ID'))
            return web.json_response(dict(status='synthetic_ack',**await request.json()))
        async def events(request):
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'}); await response.prepare(request)
            await response.write(b'id: 9\ndata: {"type":"synthetic.live","data_origin":"synthetic_fixture"}\n\n'); return response
        async def terminal(request):
            state['origins'].append(request.headers.get('Origin')); state['run_headers'].append(request.headers.get('X-Auriga-Run-ID'))
            ws=web.WebSocketResponse(); await ws.prepare(request); await ws.send_json(dict(type='ready',status=dict(session_id='synthetic-live-pty')))
            async for message in ws:
                if message.type==WSMsgType.TEXT: await ws.send_json(dict(type='synthetic_resize_ack',value=json.loads(message.data)))
            return ws
        app.router.add_get('/v1/run',run); app.router.add_post('/v1/control',control); app.router.add_get('/v1/events',events); app.router.add_get('/v1/terminal',terminal)
        backend=await self.start_server(app); write_runtime(self.first,dict(status='running',endpoint=backend)); prefix=f'/runs/{self.id1}/v1/'
        await self.library.refresh()
        status,value=await self.get(prefix+'run'); self.assertTrue(value['library']['live']); self.assertTrue(value['writable'])
        async with self.client.post(self.url+prefix+'control',headers={'Origin':self.url},json=dict(request_id='synthetic-control',command='prompt')) as response: self.assertEqual(response.status,200)
        self.assertEqual(state['origins'][-1],backend); self.assertEqual(state['run_headers'][-1],self.first.name)
        async with self.client.get(self.url+prefix+'events') as response: self.assertIn(b'synthetic.live',await self.sse_event(response))
        async with self.client.ws_connect(self.url+prefix+'terminal',headers={'Origin':self.url}) as ws:
            self.assertEqual((await ws.receive_json())['type'],'ready'); await ws.send_json(dict(type='resize',cols=90,rows=30))
            self.assertEqual((await ws.receive_json())['type'],'synthetic_resize_ack'); await ws.send_json(dict(type='input',data='forbidden'))
            self.assertEqual((await ws.receive_json())['type'],'error')
        self.assertEqual(state['origins'][-1],backend); self.assertEqual(state['run_headers'][-1],self.first.name)
        state['run_id']='synthetic-other-run'; await self.library.refresh()
        status,value=await self.get(prefix+'run'); self.assertFalse(value['library']['live'])
        self.assertEqual((await self.get(prefix+'observations/latest'))[1]['observation_id'],'obs-first')
        async with self.client.post(self.url+prefix+'control',json=dict(request_id='blocked',command='prompt')) as response: self.assertEqual(response.status,403)


if __name__=='__main__': unittest.main()
