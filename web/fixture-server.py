"""Synthetic UI fixture, no Codex/model/GPU. Never use as runtime evidence.

Run: mamba run -n Auriga python web/fixture-server.py --port 8778
"""
import argparse
import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path

from aiohttp import web, WSMsgType

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNI7SwEAAK2AWBQR1eEAAAAAElFTkSuQmCC')
IDENTITY = dict(run_id='synthetic-web', episode_id='ep-fixture', env_instance_id='env-fixture', env_generation=1)


def application():
    blobs = {}
    def artifact(value, binary=False):
        data = value if binary else json.dumps(value).encode()
        key = hashlib.sha256(data).hexdigest()
        blobs[key] = (data, 'image/png' if binary else 'application/json')
        return 'artifact:' + key
    image = artifact(PNG, True)
    observations = [dict(**IDENTITY, observation_id=f'obs-{index}', step_domain='physics_fixture', sim_step=step,
                         data_origin='synthetic_fixture', calibration_id=None, last_action_id=action,
                         cameras=[dict(camera_id='head', frame_id=f'frame-{index}', rgb_ref=image if index < 5 else 'artifact:missing', width=1, height=1)])
                    for index, step, action in [(1, 0, None), (2, 0, None), (3, 4, 'action-failed'), (4, 10, 'action-completed'), (5, 11, None)]]
    raw = artifact(dict(jsonrpc='2.0', method='item/completed', params=dict(item=dict(id='item-motion',type='dynamicToolCall',status='completed'))))
    items = [dict(id='item-text', type='agentMessage', status='completed', text='合成可见消息，不包含隐藏推理', thread_id='thread-fixture', turn_id='turn-fixture', raw_refs=[raw]),
             dict(id='item-motion', type='dynamicToolCall', status='completed', arguments={'target':[.1,.2,.3]}, result={'status':'failed'}, raw_refs=[raw]),
             dict(id='item-file', type='fileChange', status='completed', changes=[{'path':'fixture.py','diff':'-old\n+new'}], raw_refs=[raw]),
             dict(id='item-error', type='error', status='failed', error={'message':'synthetic failure'}, raw_refs=[raw])]
    actions = {}
    for name, status, start, end in [('action-failed','failed',1,3), ('action-completed','completed',3,4)]:
        actions[name] = dict(request=dict(**IDENTITY,action_id=name,based_on_observation_id=f'obs-{start}',path={'target':[.1,.2,.3]},data_origin='synthetic_fixture'),
                             receipt=dict(status=status,execution_start_observation_id=f'obs-{start+1}' if start==1 else 'obs-3',result_observation_id=f'obs-{end}',
                                          planned_trace_ref=artifact({'kind':'planned','points':[[0,0,0],[.1,.2,.3]]}),dispatched_trace_ref=artifact({'kind':'dispatched','points':[[0,0,0],[.08,.2,.3]]}),
                                          actual_trace_ref=artifact({'kind':'actual','points':[[0,0,0],[.07,.2,.3]]}) if status=='completed' else None,before_state_ref=None))
    pending = [dict(request_id=21,method='item/commandExecution/requestApproval',status='pending',thread_id='thread-fixture',turn_id='turn-fixture',item_id='item-approval',params={'command':'synthetic echo','availableDecisions':['decline','accept']},deadline_at=None),
               dict(request_id=22,method='item/tool/requestUserInput',status='pending',thread_id='thread-fixture',turn_id='turn-fixture',item_id='item-question',params={'questions':[{'id':'direction','question':'合成测试方向','options':[{'label':'left','description':'左侧'}]}]},deadline_at=None)]
    events = []
    for event_type, correlation, payload, world in [
        ('observation.available',{'observation_id':'obs-1'},{},'observed'),
        ('observation.delivered',{'observation_id':'obs-1'},{'audience':'astra_model','delivered_to_model':True},'observed'),
        ('observation.delivered',{'observation_id':'obs-3','program_id':'program-fixture'},{'audience':'actor_program','delivered_to_model':False},'observed'),
        ('action.failed',{'action_id':'action-failed','observation_id':'obs-1','item_id':'item-motion','tool_call_id':'item-motion'},actions['action-failed']['receipt'],'observed'),
        ('action.completed',{'action_id':'action-completed','observation_id':'obs-3','item_id':'item-motion','tool_call_id':'item-motion'},actions['action-completed']['receipt'],'observed'),
        ('job.completed',{'job_id':'job-internal'},{'status':'succeeded','scene_version':1},'internal')]:
        events.append(dict(**IDENTITY,event_id=f'event-{len(events)+1}',server_seq=len(events)+1,type=event_type,world_kind=world,correlation=correlation,payload=payload,data_origin='synthetic_fixture'))
    state = dict(owner=None, token=None, expires=0, codex_status='completed', active_action=None, prompts=0, replies=0, resizes=0, terminal_inputs=0, ws_gap=False, sse_gap=False)
    cached = {}
    sockets = set()
    streams = set()
    terminal_data = '\x1b[32mSYNTHETIC native terminal fixture\x1b[0m\r\n'.encode()
    terminal = dict(session_id='pty-fixture',thread_id='thread-fixture',pid=101,cursor=len(terminal_data),cols=100,rows=32,mode='read_only',exit_code=None)
    def owner():
        if state['expires'] < time.time(): state.update(owner=None,token=None)
        return dict(owner_client_id=state['owner'],expires_at=state['expires'],mode='gateway',can_claim=not state['owner'])
    def run():
        return dict(**IDENTITY,status='running',thread_id='thread-fixture',turn_id='turn-fixture',codex_status=state['codex_status'],accepting_actions=True,controller=owner(),motion={'active_action':state['active_action'],'status':'running' if state['active_action'] else 'idle'},terminal=terminal,data_origin='synthetic_fixture')
    @web.middleware
    async def guard(request,handler):
        if request.method=='POST' or request.path=='/v1/terminal':
            if request.headers.get('Origin') not in (None,f'http://{request.host}'):
                return web.json_response({'error':'same_origin_required'},status=403)
        return await handler(request)
    app=web.Application(middlewares=[guard])
    async def api(request):
        path=request.path
        if path=='/v1/run': result=run()
        elif path=='/v1/observations': result={'observations':observations}
        elif path.startswith('/v1/observations/'): result=observations[3] if path.endswith('/latest') else next((item for item in observations if item['observation_id']==request.match_info['id']),None)
        elif path=='/v1/items': result={'items':items,'cursor':len(events)}
        elif path.startswith('/v1/items/'): result=next((item for item in items if item['id']==request.match_info['id']),None)
        elif path=='/v1/requests': result={'requests':pending}
        elif path.startswith('/v1/actions/'): result=actions.get(request.match_info['id'])
        elif path=='/v1/scene/head': result={'scene_version':2,'world_kind':'internal','capabilities':{'physics':'unsupported','rgb_reconstruction':'synthetic_only'}}
        elif path=='/v1/scene/versions': result={'versions':[1,2]}
        elif path.startswith('/v1/scene/versions/'): result={'scene_version':int(request.match_info['id']),'entities':[],'observation_ids':['obs-3','obs-4'],'world_kind':'internal'}
        elif path.startswith('/v1/jobs/'): result={'job_id':request.match_info['id'],'status':'succeeded','result':{'status':'ok','data_origin':'synthetic_fixture'}}
        elif path=='/fixture/stats': result=state
        else: result=None
        return web.json_response(result if result is not None else {'error':'missing'},status=200 if result is not None else 404)
    async def controls(request):
        value=await request.json(); key=value.get('request_id'); command=value.get('command'); owner()
        if key in cached:
            old,result=cached[key]
            if old!=value: return web.json_response({'error':'request_content_conflict'},status=409)
            return web.json_response(result)
        if command=='claim_input':
            if state['owner'] and state['owner']!=value.get('client_id'): return web.json_response({'error':'writer_conflict'},status=409)
            if state['owner'] and state['token']!=value.get('writer_token'): return web.json_response({'error':'writer_conflict'},status=409)
            state.update(owner=value['client_id'],token='token-'+value['client_id'],expires=time.time()+30)
            result={'writer_token':state['token'],'expires_at':state['expires']}
        elif command in ('prompt','reply','renew_input','release_input'):
            if value.get('client_id')!=state['owner'] or value.get('writer_token')!=state['token'] or not state['owner']: return web.json_response({'error':'writer_required'},status=409)
            if command=='renew_input': state['expires']=time.time()+30; result={'expires_at':state['expires']}
            elif command=='release_input': state.update(owner=None,token=None); result={'status':'released'}
            elif command=='prompt': state['prompts']+=1; result={'thread_id':'thread-fixture','turn_id':'turn-next','status':'running'}
            else:
                native=next((item for item in pending if item['request_id']==value['native_request_id']),None)
                if not native or native['status']!='pending': return web.json_response({'error':'request_expired'},status=409)
                native.update(status='resolved',response=value['response']); state['replies']+=1; result={'status':'resolved','native_request_id':native['request_id']}
        else: result={'status':'stopped' if command=='stop' else 'unsupported'}
        cached[key]=(value,result)
        return web.json_response(result)
    async def media(request):
        value=blobs.get(request.match_info['id'])
        return web.Response(body=value[0],content_type=value[1]) if value else web.Response(status=404)
    async def stream(request):
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-cache'}); await response.prepare(request); streams.add(response)
        after=max(int(request.query.get('after',0)),int(request.headers.get('Last-Event-ID',0)))
        try:
            if state['sse_gap']:
                state['sse_gap']=False
                await response.write(('event: gap\ndata: '+json.dumps({'current_seq':len(events),'reason':'synthetic_upstream_gap'})+'\n\n').encode())
            for event in events:
                if event['server_seq']>after:
                    data=(f"id: {event['server_seq']}\ndata: "+json.dumps(event)+'\n\n').encode(); await response.write(data); await response.write(data)
            await response.write(b'retry: 250\n\n')
            while True: await response.write(b': fixture\n\n'); await asyncio.sleep(.5)
        except (ConnectionResetError,asyncio.CancelledError,RuntimeError): pass
        finally: streams.discard(response)
        return response
    async def ws(request):
        socket=web.WebSocketResponse(); await socket.prepare(request); sockets.add(socket)
        after=int(request.query.get('after',0)); await socket.send_json({'type':'ready','status':terminal})
        if state['ws_gap'] or after>len(terminal_data):
            state['ws_gap']=False; await socket.send_json({'type':'gap','cursor':0,'reason':'synthetic_cursor_gap'}); await socket.close()
        elif after<len(terminal_data): await socket.send_json({'type':'output','after':after,'cursor':len(terminal_data),'data':base64.b64encode(terminal_data[after:]).decode()})
        async for message in socket:
            if message.type==WSMsgType.TEXT:
                value=json.loads(message.data)
                if value.get('type')=='resize': state['resizes']+=1; terminal.update(cols=value['cols'],rows=value['rows'])
                else: state['terminal_inputs']+=1; await socket.send_json({'type':'error','message':'terminal_input_is_read_only'})
        sockets.discard(socket); return socket
    async def fault(request):
        state.update(await request.json())
        for socket in list(sockets): await socket.close()
        for response in list(streams): await response.write_eof()
        return web.json_response({'status':'injected'})
    async def scene_post(request):
        value=await request.json()
        if request.path.endswith('/snapshot'): result={'snapshot_id':'snapshot-fixture','scene_version':value['scene_version'],'anchor_observation_id':value.get('anchor_observation_id'),'world_kind':'internal','scope':'geometry_only'}
        elif request.path.endswith(('/alignment','/update')): result={'job_id':'job-alignment','status':'queued','world_kind':'internal','request':value}
        else: result={'status':'ok','value':{'scene_version':value.get('scene_version'),'hit':None},'world_kind':'internal'}
        return web.json_response(result)
    for route in ['/v1/run','/v1/observations','/v1/observations/{id}','/v1/items','/v1/items/{id}','/v1/requests','/v1/actions/{id}','/v1/scene/head','/v1/scene/versions','/v1/scene/versions/{id}','/v1/jobs/{id}','/fixture/stats']: app.router.add_get(route,api)
    app.router.add_get('/v1/events',stream); app.router.add_get('/v1/terminal',ws); app.router.add_get('/v1/artifacts/{id}',media)
    app.router.add_post('/v1/control',controls); app.router.add_post('/fixture/fault',fault)
    for route in ['/v1/scene/query','/v1/scene/snapshot','/v1/scene/alignment','/v1/scene/update','/v1/jobs/{id}/cancel']: app.router.add_post(route,scene_post)
    dist=Path(__file__).parent/'dist'
    async def index(request): return web.FileResponse(dist/'index.html')
    app.router.add_get('/',index); app.router.add_static('/',dist)
    return app


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8778); args=parser.parse_args()
    web.run_app(application(),host='127.0.0.1',port=args.port)
