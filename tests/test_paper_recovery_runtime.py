"""Offline real-runtime check: five failures, exact scratch, historical rewind."""
import asyncio,gzip,json,os,shutil,tarfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

def test_failed_endpoint_recovery(tmp_path,monkeypatch):
    from feedback_runner.agent_entry import episode
    from feedback_runner.ledger import Ledger,atomic
    from mettle_runner.recovery_experiment import prepare_episode,sha
    from mettle_runner.core import tree_hash
    repo=Path('/testbed');repo.mkdir(exist_ok=True)
    (repo/'state.txt').write_text('original');(repo/'empty').mkdir();(repo/'state.txt').chmod(0o640)
    (repo/'link').symlink_to('state.txt')
    namespace='paper-fixture';scratch=Path('/bench-scratch')/namespace/'attempt-0'
    root=tmp_path/'live/ipc';archive=tmp_path/'seed';mode={'strategy':'seed','count':0}
    monkeypatch.setenv('JWS_FEEDBACK_ROOT',str(root));monkeypatch.setenv('JWS_EVAL_FEEDBACK','1')
    monkeypatch.setenv('JWS_PAIRED','1');monkeypatch.setenv('JWS_PAPER_RECOVERY','1')
    monkeypatch.setenv('JWS_BENCHMARK','mettlebench');monkeypatch.setenv('MEMORY_ENGINE','none')
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR','/bench-runtime/'+namespace)
    stop=threading.Event();errors=[];calls=[];ledger=[Ledger(tmp_path/'seed-ledger',max_submissions=100,repeated_limit=5)]
    def evaluator():
        try:
            while not stop.wait(.02):
                for request in (root/'requests').glob('*.json'):
                    if (root/'responses'/request.name).exists():continue
                    good=(repo/'state.txt').read_text()=='good'
                    feedback={'status':'passed' if good else 'product_failure','resolved':good,
                        'failure_id':None if good else 'fixture','message':'state must be good; prior decision rejected'}
                    entry=ledger[0].record(request.stem,'fixture','fixture-sha',feedback)
                    atomic(root/'public-ledger.json',[e['feedback'] for e in ledger[0].entries])
                    atomic(root/'responses'/request.name,entry['feedback'])
        except Exception as e:errors.append(repr(e))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            try:
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                strategy=mode['strategy'];mode['count']+=1;n=mode['count'];calls.append((strategy,body))
                assert body.get('temperature')==0.0,body.get('temperature')
                names={t['function']['name'] for t in body['tools']}
                assert ('backtrack_commit' in names)==(strategy=='rewind')
                if strategy=='seed':
                    if n==1:name,args='read_file',{'file_path':'/testbed/state.txt'}
                    elif n==2:name,args='bash',{'command':f'cp /testbed/state.txt {scratch}/backup.txt; printf damaged > /testbed/state.txt','workdir':'/testbed'}
                    elif 3<=n<=6:name,args='submit_patch',{}
                    elif n==7:name,args=None,{}
                    elif n==8:
                        assert 'stagnated' in json.dumps(body['messages'])
                        name,args='bash',{'command':'printf NEVER > /testbed/state.txt','workdir':'/testbed'}
                    else:raise AssertionError('Seed did not stop at five failures')
                else:
                    if n==1:
                        assert (repo/'state.txt').read_text()=='damaged'
                        assert (scratch/'backup.txt').read_text()=='original'
                        assert (repo/'empty').is_dir() and (repo/'link').is_symlink()
                        assert (repo/'state.txt').stat().st_mode&0o777==0o640
                        assert 'five consecutive official' in json.dumps(body['messages'])
                        assert len(ledger[0].entries)==0
                    if strategy=='rewind' and n==1:name,args='backtrack_candidates',{'reason':'undo destructive decision in failed Continue prefix'}
                    elif strategy=='rewind' and n==2:
                        candidates=json.loads(next(m['content'] for m in reversed(body['messages']) if m.get('role')=='tool'))
                        records=[json.loads(l) for l in (root/'recovery/traces'/f'{namespace}-a1.jsonl').read_text().splitlines()]
                        target=[r for r in records if r.get('kind')=='llm'][1]['record_uid']
                        assert target in [r['record_uid'] for r in candidates],(target,candidates)
                        name,args='backtrack_commit',{'record_uid':target,'memory_summary':'observed_fact: five failures after damaged. Local repair impossible without backup. next_plan: preserve backup, write good and submit.','reason':'restore original before damaging write'}
                    elif n==(3 if strategy=='rewind' else 1):
                        if strategy=='rewind':assert (repo/'state.txt').read_text()=='original'
                        assert (scratch/'backup.txt').read_text()=='original'
                        name,args='bash',{'command':'printf good > /testbed/state.txt','workdir':'/testbed'}
                    elif n==(4 if strategy=='rewind' else 2):name,args='submit_patch',{}
                    else:raise AssertionError('Unexpected live request')
                message={'role':'assistant','content':''}
                if name:
                    message['tool_calls']=[{'id':f'call-{strategy}-{n}','type':'function',
                        'function':{'name':name,'arguments':json.dumps(args)}}]
                else:
                    message['content']='Ready for assessment.'
                result={'id':f'fixture-{strategy}-{n}','object':'chat.completion','created':1,'model':'fixture',
                    'choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if name else 'stop'}],
                    'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
                self.send_response(200)
                if body.get('stream'):
                    self.send_header('Content-Type','text/event-stream');self.end_headers()
                    c=result['choices'][0];c['delta']=c.pop('message')
                    if name:c['delta']['tool_calls'][0]['index']=0
                    self.wfile.write(('data: '+json.dumps(result)+'\n\ndata: [DONE]\n\n').encode())
                else:self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                errors.append(repr(e));self.send_error(500,str(e))
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    host=threading.Thread(target=evaluator,daemon=True);host.start()
    settings={'episode':namespace,'strategy':'seed','paired':True,'paper_recovery':True,'benchmark':'mettlebench',
        'started':time.time(),'deadline':time.time()+240,'token_limit':10000,'max_rewinds':10,
        'workspace':'/testbed','baseline':None,'prompt':'Repair state.txt to good; use official submit_patch.'}
    def prepare(current):
        for name in ('private','requests','responses'):(root/name).mkdir(parents=True,exist_ok=True)
        atomic(root/'private/model.json',{'base_url':f'http://127.0.0.1:{server.server_port}/v1','api_key':'fixture','model_name':'fixture'})
        atomic(root/'settings.json',current);atomic(root/'public-ledger.json',[])
        monkeypatch.setenv('JWS_FEEDBACK_ROOT',str(root));monkeypatch.setenv('JWS_FEEDBACK_STRATEGY',current['strategy'])
    try:
        prepare(settings);report=asyncio.run(episode(settings));assert report['state']=='SEED_READY',(report,errors)
        assert len(ledger[0].entries)==5 and (repo/'state.txt').read_text()=='damaged'
        seed=json.loads((root/'paired-seed.json').read_text());endpoint=archive/'endpoint';endpoint.mkdir(parents=True)
        shutil.copytree(root,archive/'ipc')
        for name,path in [('workspace',repo),('scratch',Path('/bench-scratch'))]:
            with tarfile.open(endpoint/(name+'.tar'),'w') as tf:tf.add(path,arcname='.')
        manifest={'workspace_tree_sha256':tree_hash(repo),'scratch_tree_sha256':tree_hash('/bench-scratch'),
            'workspace_archive_sha256':sha(endpoint/'workspace.tar'),'scratch_archive_sha256':sha(endpoint/'scratch.tar')}
        atomic(endpoint/'manifest.json',manifest);atomic(endpoint/'failures.json',[e['feedback'] for e in ledger[0].entries])
        results={};audits=[]
        for strategy in ['continue','rewind']:
            shutil.rmtree(root);mode.update(strategy=strategy,count=0)
            ledger[0]=Ledger(tmp_path/(strategy+'-ledger'),max_submissions=100,repeated_limit=5)
            current=prepare_episode({**settings,'strategy':strategy,'started':time.time(),'deadline':time.time()+240},
                {'seed':seed,'source':str(archive)},root)
            prepare(current);report=asyncio.run(episode(current));results[strategy]=report
            detail='\n'.join(p.read_text()[-12000:] for p in root.rglob('process.log'))
            assert report['state']=='RESOLVED',(strategy,report,errors,detail)
            assert report['rewinds']==int(strategy=='rewind')
            assert len(ledger[0].entries)==1
            audits.append(json.loads((root/'paired-environment-audit.json').read_text()))
            assert json.loads((root/'paired-first-input.json').read_text())['context_equal']
        assert audits[0]==audits[1] and not errors
        atomic(Path('/evidence')/'smoke-results.json',{'passed':True,'seed_submissions':5,'arms':results,'environment':audits[0]})
    finally:
        stop.set();host.join(timeout=2);server.shutdown();server.server_close()
        shutil.copytree(tmp_path,Path('/evidence')/('run-'+str(time.time_ns())))
