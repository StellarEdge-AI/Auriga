"""Integrated HTTP ownership, scene outbox, restart journal and stale-action checks."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer
from openai_codex.errors import InvalidParamsError, TransportClosedError
from types import SimpleNamespace
from unittest.mock import Mock
from auriga.core.config import Config
from auriga.apps.gateway.workbench import Workbench
from auriga.perception.scene.scene_service import SceneService
from auriga.cortex.session.terminal import Terminal
from auriga.core.contracts import digest
from geometry_fixture import IDENTITY, recorded_plane
from test_harness import FixtureRobot

class ExpandedGatewayCheck(unittest.IsolatedAsyncioTestCase):
    async def test_native_prompt_rejection_is_terminal_but_transport_loss_stays_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            robot = FixtureRobot(root); robot.close = lambda: None
            config = Config.from_dict({'features': {'grasp_memory_dir': str(root/'memory'),
                'observation_profile': 'rgb_proprio_v1', 'motion_review': False}})
            workbench = Workbench(root/'run', robot=robot, config=config)
            workbench.harness.start()
            workbench.starting = False
            workbench.status = 'running'
            workbench.session.thread_id = 'fixture-thread'
            workbench.session.state = 'idle'
            calls = Mock(side_effect=[InvalidParamsError(-32602, 'fixture native rejection'),
                SimpleNamespace(turn=SimpleNamespace(id='fixture-turn')),
                TransportClosedError('fixture lost acknowledgement')])
            workbench.session.client = SimpleNamespace(turn_start=calls)
            client = TestClient(TestServer(workbench.app()))
            try:
                await client.start_server()
                origin = str(client.make_url('')).rstrip('/')
                owner = await (await client.post('/v1/control', headers={'Origin':origin}, json=dict(
                    request_id='claim', command='claim_input', client_id='fixture'))).json()
                async def prompt(identifier):
                    response = await client.post('/v1/control', headers={'Origin':origin}, json=dict(
                        request_id=identifier, command='prompt', prompt='fixture prompt',
                        client_id='fixture', writer_token=owner['writer_token']))
                    return response.status, await response.json()
                status, rejected = await prompt('native-rejection')
                self.assertEqual(status, 400)
                self.assertEqual(rejected['status'], 'rejected')
                self.assertEqual(rejected['native_rpc_code'], -32602)
                self.assertEqual(workbench.session.state, 'idle')
                self.assertTrue(workbench.harness.accepting)
                self.assertEqual(await prompt('native-rejection'), (status, rejected))
                self.assertEqual(calls.call_count, 1, 'same control id must not dispatch again')
                status, accepted = await prompt('next-prompt')
                self.assertEqual(status, 200)
                self.assertEqual(accepted['turn_id'], 'fixture-turn')
                workbench.session.state = 'completed'
                status, uncertain = await prompt('transport-loss')
                self.assertEqual(status, 409)
                self.assertEqual(uncertain['status'], 'unknown')
                self.assertEqual(workbench.session.state, 'disconnected')
                self.assertFalse(workbench.harness.accepting)
                self.assertEqual(await prompt('transport-loss'), (status, uncertain))
                self.assertEqual(calls.call_count, 3)
                self.assertEqual(robot.executions, 0)
            finally:
                workbench.session.client = None
                await client.close()
                await workbench.close()

    async def test_closed_run_replay_preserves_records_and_rejects_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'run'
            robot = FixtureRobot(temp); robot.close = lambda: None
            recorded = Workbench(root, robot=robot, observation_profile="rgb_proprio_v1", motion_review=False)
            thread_id = 'thread-synthetic-replay'
            program_id = 'program-synthetic-replay'
            try:
                observation = recorded.harness.start()
                recorded.starting = False; recorded.session.state = 'completed'
                recorded.session.thread_id = thread_id
                recorded.store.set_state('runtime', 'codex', {'thread_id': thread_id})
                item = dict(id='item-synthetic-replay', type='agentMessage', status='completed',
                            thread_id=thread_id, text='synthetic recorded reply')
                recorded.store.set_state('codex_item', item['id'], item)
                stdout_ref = recorded.store.put_artifact(b'synthetic program output', 'program_stdout', 'text/plain')
                program = dict(job_id=program_id, status='succeeded', returncode=0, stdout_ref=stdout_ref)
                recorded.store.set_state('program', program_id, program)
                (root/'manifest.json').write_text(json.dumps(dict(recorded.identity, data_origin='synthetic_fixture')))
                # An actual owned PTY, with a synthetic process instead of Codex/model.
                recorded.terminal = Terminal(root, root, command_prefix=(sys.executable, '-c',
                    "import sys; print('SYNTHETIC REPLAY TERMINAL', flush=True); sys.stdin.read()"))
                await recorded.terminal.start(f'unix://{recorded.session.socket_path}', thread_id)
                await asyncio.wait_for(recorded.terminal.changed.wait(), 2)
                session_id = recorded.terminal.session_id
            finally:
                await recorded.close()
            terminal_bytes = (root/'terminal.bin').read_bytes()
            self.assertIn(b'SYNTHETIC REPLAY TERMINAL', terminal_bytes)

            replay_robot = FixtureRobot(Path(temp)/'replay-robot'); replay_robot.close = lambda: None
            replay_robot.start = lambda: self.fail('replay must not start a robot worker')
            replay = Workbench(root, robot=replay_robot, replay=True)
            replay.session.start = lambda *args: self.fail('replay must not start Codex')
            client = TestClient(TestServer(replay.app()))
            try:
                await replay.start('must not initiate a turn')
                await client.start_server()
                run = await (await client.get('/v1/run')).json()
                self.assertEqual(run['status'], 'replay')
                self.assertEqual(run['thread_id'], thread_id)
                self.assertFalse(run['accepting_actions'])
                self.assertTrue(replay.terminal._closed)
                self.assertEqual(run['terminal']['thread_id'], thread_id)
                self.assertEqual(run['terminal']['session_id'], session_id)
                self.assertIsNone(run['terminal']['pid'])
                self.assertEqual(await (await client.get('/v1/observations/latest')).json(), observation)
                self.assertEqual(await (await client.get('/v1/items/'+item['id'])).json(), item)
                response = await client.get('/v1/jobs/'+program_id)
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.json(), program)
                output = await client.get('/v1/artifacts/'+stdout_ref.removeprefix('artifact:'))
                self.assertEqual(await output.read(), b'synthetic program output')
                ranged=await client.get('/v1/artifacts/'+stdout_ref.removeprefix('artifact:'),headers={'Range':'bytes=0-8'})
                self.assertEqual(ranged.status,206)
                self.assertEqual(await ranged.read(),b'synthetic')
                self.assertEqual(ranged.headers['Content-Range'],'bytes 0-8/24')
                head=await client.head('/v1/artifacts/'+stdout_ref.removeprefix('artifact:'))
                self.assertEqual(int(head.headers['Content-Length']),24)
                self.assertEqual(await head.read(),b'')
                async with client.ws_connect('/v1/terminal') as socket:
                    ready = await socket.receive_json(timeout=2)
                    self.assertEqual(ready['status']['thread_id'], thread_id)
                    self.assertEqual(ready['status']['session_id'], session_id)
                    output = await socket.receive_json(timeout=2)
                    self.assertEqual(base64.b64decode(output['data']), terminal_bytes)
                    self.assertEqual((await socket.receive_json(timeout=2))['type'], 'status')
                for command in ['claim_input', 'prompt', 'reset', 'stop']:
                    response = await client.post('/v1/control', json=dict(request_id='replay-'+command,
                        command=command, client_id='observer', prompt='must not execute'))
                    self.assertEqual(response.status, 400)
                    self.assertEqual((await response.json())['error'], 'replay_is_read_only')
                for path in ['/v1/observer/evaluate', '/v1/observer/record', '/v1/observer/video',
                             '/v1/jobs/'+program_id+'/cancel', '/v1/scene/update']:
                    response = await client.post(path, json={})
                    self.assertEqual(response.status, 400, path)
                response = await client.get('/v1/observer/snapshot')
                self.assertEqual(response.status, 400)
                self.assertEqual(replay_robot.executions, 0)
                self.assertIsNone(replay.session.process)
            finally:
                await client.close(); await replay.close()

    async def test_writer_journal_scene_jobs_and_global_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            robot=FixtureRobot(temp);robot.close=lambda:None
            workbench=Workbench(Path(temp)/'run',robot=robot, observation_profile="rgb_proprio_v1", motion_review=False)
            workbench.identity.update(IDENTITY)
            workbench.harness.start()
            workbench.starting=False;workbench.status='running'
            workbench.session.thread_id='thread';workbench.session.state='completed'
            turns=[]
            def prompt(value): turns.append(value);return 'turn'
            workbench.session.run_turn=prompt
            workbench.scene_service=SceneService(workbench.root/'twin-service')
            await workbench.scene_service.start()
            workbench.harness.scene=workbench.scene_service.scene
            for obs in recorded_plane(workbench.store): workbench.scene_service.import_bundle(workbench.store,obs)
            workbench.scene_forwarder=asyncio.create_task(workbench.forward_scene_events())
            client=TestClient(TestServer(workbench.app()))
            try:
                await client.start_server();origin=str(client.make_url('')).rstrip('/')
                async def control(body):
                    response=await client.post('/v1/control',json=body,headers={'Origin':origin})
                    return response.status,await response.json()
                status,owner=await control(dict(request_id='claim',command='claim_input',client_id='one'))
                self.assertEqual(status,200)
                status,_=await control(dict(request_id='claim-two',command='claim_input',client_id='two'))
                self.assertEqual(status,409)
                status,_=await control(dict(request_id='steal-same-client',command='claim_input',client_id='one'))
                self.assertEqual(status,409)
                request=dict(request_id='prompt-one',command='prompt',prompt='one',client_id='one',writer_token=owner['writer_token'])
                self.assertEqual((await control(request))[0],200)
                self.assertEqual((await control(request))[0],200)
                self.assertEqual(turns,['one'])
                unknown=dict(request_id='lost-response',command='prompt',prompt='do not resend',client_id='one',writer_token=owner['writer_token'])
                workbench.store.save('control_request','lost-response',unknown)
                workbench.store.set_state('control_response','lost-response',dict(request_id='lost-response',status='unknown'))
                self.assertEqual((await control(unknown))[0],409)
                self.assertEqual(turns,['one'])
                body=dict(request_id='build',static_scene_assumption=True,roi=[100,80,400,320],base_scene_version=0,observation_ids=['obs-0','obs-1'],camera_id='head',entity_id='table',primitive='plane')
                response=await client.post('/v1/scene/update',json=body,headers={'Origin':origin})
                self.assertEqual(response.status,200,await response.text())
                job=await response.json(); identifier=job['job_id']
                deadline=asyncio.get_running_loop().time()+10
                while job['status'] not in ('succeeded','failed','cancelled','expired') and asyncio.get_running_loop().time()<deadline:
                    await asyncio.sleep(.05); job=await (await client.get('/v1/jobs/'+identifier)).json()
                self.assertEqual(job['status'],'succeeded',job)
                head=await (await client.get('/v1/scene/head')).json()
                self.assertEqual(head['scene_version'],1)
                version=await (await client.get('/v1/scene/versions/1')).json()
                self.assertEqual(version['scene_version'],1)
                self.assertIn('obs-1',version['observation_ids'])
                response=await client.post('/v1/scene/snapshot',json=dict(request_id='snapshot',scene_version=1,anchor_observation_id='obs-1'))
                snapshot=await response.json();self.assertIn('snapshot_id',snapshot)
                await asyncio.sleep(.1)
                events=workbench.store.events(limit=1000)
                self.assertEqual([e['server_seq'] for e in events],list(range(1,len(events)+1)))
                self.assertTrue(any(e['type']=='job.completed' and e['source']=='scene_api' for e in events))
                source=json.loads(workbench.scene_service.db.execute("SELECT body FROM events LIMIT 1").fetchone()[0])
                before=workbench.store.event_cursor();workbench.store.ingest_event(source)
                self.assertEqual(before,workbench.store.event_cursor())
                foreign=await client.post('/v1/scene/update',json=dict(body,request_id='foreign',env_generation=99))
                self.assertEqual(foreign.status,409)
                sse=await client.get('/v1/events?after=99999')
                self.assertEqual((await sse.content.readline()).strip(),b'event: gap')
                sse.close()
            finally:
                await client.close();await workbench.close()

    async def test_observer_sampling_and_prompt_are_serialized(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            robot=FixtureRobot(temp);robot.close=lambda:None
            workbench=Workbench(Path(temp)/'run',robot=robot, observation_profile="rgb_proprio_v1", motion_review=False)
            workbench.harness.start();workbench.starting=False;workbench.status='running'
            workbench.session.state='completed';workbench.session.thread_id='thread'
            entered,release=threading.Event(),threading.Event();turns=[]
            def evaluate(final_check=True):
                entered.set();release.wait(3);return {'task_success':False}
            robot.evaluate=evaluate
            workbench.session.run_turn=lambda prompt:turns.append(prompt) or 'turn'
            client=TestClient(TestServer(workbench.app()))
            try:
                await client.start_server()
                score=asyncio.create_task(client.post('/v1/observer/evaluate',json={}))
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                prompt=asyncio.create_task(client.post('/v1/control',json=dict(request_id='prompt',command='prompt',prompt='after score')))
                await asyncio.sleep(.05);self.assertEqual(turns,[])
                release.set()
                response=await score;self.assertEqual(response.status,200)
                result=await response.json();self.assertEqual(result['env_generation'],1)
                response=await prompt;self.assertEqual(response.status,200)
                self.assertEqual(turns,['after score'])
                event=next(e for e in workbench.store.events() if e['type']=='observer.evaluated')
                self.assertEqual(event['env_generation'],result['env_generation'])
            finally:release.set();await client.close();await workbench.close()

if __name__=='__main__':unittest.main()
