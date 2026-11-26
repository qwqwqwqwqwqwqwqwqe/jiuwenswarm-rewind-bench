"""MettleBench pilot: pristine independent Continue / Restart / Rewind runs."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime
import json,os,shutil,subprocess,time,traceback,uuid
from pathlib import Path
from types import SimpleNamespace
from .core import docker,capture,capture_scratch,restore,evaluate,IMAGE,tree_hash
from bench_tools.jiuwenswarm_runtime.host import setup,ROOT,_ai_values
from bench_tools.jiuwenswarm_runtime.ledger import Ledger,atomic

def episode(layout,args,row,strategy,endpoint,network):
    eid='mb-'+uuid.uuid4().hex[:12];container=eid;task_id=row['task_id']
    recovery_trial=getattr(args,'recovery_trial',None)
    if recovery_trial and recovery_trial.get('seed'):
        eid=recovery_trial['seed']['namespace'];container=eid+'-'+strategy
    out=Path(args.output_root)/task_id/strategy;out.mkdir(parents=True)
    shared=out/'ipc';(shared/'requests').mkdir(parents=True);(shared/'responses').mkdir();(shared/'private').mkdir()
    (shared/'private').chmod(0o700)
    isolated=bool(getattr(args,'isolated',False))
    if isolated:shared.chmod(0o700)
    _,model,key=_ai_values(SimpleNamespace(env_file=Path(args.env_file),base_url_env='JWS_BASE_URL',model_name_env='JWS_MODEL_NAME',api_key_env='API_KEY'))
    atomic(shared/'private/model.json',{'base_url':endpoint,'model_name':model,'api_key':key});(shared/'private/model.json').chmod(0o600)
    pristine=Path(args.repo)/row['task_dir']/'workspace'
    ledger=Ledger(out,max_submissions=8,repeated_limit=5)
    start=time.time();deadline=start+1800;process=None;stream=None
    if recovery_trial:
        ledger.maximum=__import__('sys').maxsize;deadline=start+10**12
    summary={'task_id':task_id,'strategy':strategy,'state':'FAILED','resolved':False,'rewinds':0,
        'started':start,'stratum':row['structural_stratum'],'container':container,'feedback_host_root':str(shared)}
    atomic(out/'run.json',summary)
    try:
        caps=['--cap-add','CHOWN','--cap-add','SETUID','--cap-add','SETGID'] if isolated else []
        docker('run','-d','--name',container,'--network',network,'--cap-drop','ALL','--cap-add','DAC_OVERRIDE',*caps,'--security-opt','no-new-privileges',
            '-v',str(layout.runtime)+':/opt/jws_runtime:ro','-v',str(layout.runtime)+':'+str(layout.runtime)+':ro',
            '-v',str(shared)+':/shared/feedback/'+eid+':rw','-w','/testbed',IMAGE,'sleep','infinity')
        restore(container,pristine)
        docker('exec',container,'mkdir','-p','/feedback-code','/bench-runtime','/bench-scratch')
        docker('cp',ROOT/'bench_tools/jiuwenswarm_runtime',container+':/feedback-code/feedback_runner')
        if isolated:
            docker('cp',ROOT/'bench_tools/mettlebench',container+':/feedback-code/mettle_runner')
            docker('exec',container,'chmod','700','/bench-runtime','/feedback-code')
            docker('exec',container,'/usr/local/bin/python','-c',
                "import pwd,grp; from pathlib import Path; "
                "p=Path('/etc/passwd'); g=Path('/etc/group'); "
                "p.write_text(p.read_text()+'benchagent:x:10001:10001:Benchmark agent:/bench-scratch:/bin/sh\\n') if not any(u.pw_uid==10001 for u in pwd.getpwall()) else None; "
                "g.write_text(g.read_text()+'benchagent:x:10001:\\n') if not any(v.gr_gid==10001 for v in grp.getgrall()) else None")
        task_prompt=(Path(args.repo)/row['task_dir']/'prompt.md').read_text()
        prompt=task_prompt+('\n\nExecution environment: task workspace /testbed. Project interpreter /usr/local/bin/python; '
            'Python 3.12, git, make and the image dependencies are preinstalled. Do not install dependencies or fetch from the network. '
            'Follow the task repository instructions and native test entry points. Required outputs belong in /testbed; '
            'Use native validation and handoff generators when provided; retain their output schema instead of guessing a replacement. '
            'temporary experiments and logs belong under $JWS_SCRATCH_DIR. Task-required git operations are allowed. '
            'Do not delete the workspace itself. The name submit_patch is a compatibility name: it submits the complete '
            'workspace including data, generated artifacts and internal git state, not a code diff. '
            'Call submit_patch for official acceptance; local tests alone do not establish completion. '
            'Only the first failed official criterion is returned. It may describe requirements whose checks are hidden. '
            'When a hypothesis is disproved or a harmful operation invalidates prior work, consider backtrack_candidates '
            'and backtrack_commit if available. Preserve observed failures and an alternative strategy in recovery memory. '
            'No task user interaction is available.')
        settings={'episode':eid,'strategy':strategy,'benchmark':'mettlebench','started':start,'deadline':deadline,
            'token_limit':200000,'max_rewinds':6,'prompt':prompt,'workspace':'/testbed','baseline':None,'paired':False}
        if recovery_trial:
            from .recovery_experiment import prepare_episode
            settings=prepare_episode(settings,recovery_trial,shared)
        (out/'effective-prompt.txt').write_text(settings['prompt'],encoding='utf-8')
        atomic(shared/'settings.json',settings);atomic(shared/'public-ledger.json',[])
        stream=(out/'agent-process.log').open('w')
        cmd=['docker','exec','-e','PYTHONPATH=/feedback-code','-e','JWS_EVAL_FEEDBACK=1','-e','JWS_BENCHMARK=mettlebench',
            '-e','JWS_METTLE_ISOLATED='+('1' if isolated else '0'),
            '-e','JWS_PAIRED=0','-e','JWS_FEEDBACK_STRATEGY='+strategy,'-e','JWS_FEEDBACK_ROOT=/shared/feedback/'+eid,
            container,'/opt/jws_runtime/bin/python','-B','/feedback-code/feedback_runner/agent_entry.py','/shared/feedback/'+eid+'/settings.json']
        if recovery_trial:
            if settings['paired']:cmd[cmd.index('JWS_PAIRED=0')]='JWS_PAIRED=1'
            cmd[2:2]=['-e','JWS_PAPER_RECOVERY=1']
        process=subprocess.Popen(cmd,stdout=stream,stderr=subprocess.STDOUT)
        print('START',task_id,strategy,flush=True)
        restored=set()
        while process.poll() is None and time.time()<deadline+20:
            request=shared/'restore-request.json'
            if request.exists():
                value=json.loads(request.read_text());nonce=value['nonce']
                if nonce not in restored:
                    restore(container,pristine);restored.add(nonce);atomic(shared/'restore-response.json',{'nonce':nonce})
            for path in sorted((shared/'requests').glob('*.json')):
                request=json.loads(path.read_text());rid=request['request_id']
                if ledger.existing(rid):continue
                sub=out/'submissions'/f'{len(ledger.entries)+1:03d}'
                ws,sha=capture(container,sub)
                try:capture_scratch(container,sub/'recovery-state')
                except Exception as exc:
                    atomic(sub/'recovery-state-error.json',{'error_type':type(exc).__name__,'replayable':False})
                feedback,report=evaluate(args.repo,task_id,ws,sub/'eval')
                entry=ledger.record(rid,sha,sha,feedback)
                atomic(sub/'feedback.json',entry['feedback']);atomic(shared/'public-ledger.json',[e['feedback'] for e in ledger.entries])
                atomic(shared/'responses'/path.name,entry['feedback'])
                print('FEEDBACK',task_id,strategy,len(ledger.entries),feedback['status'],flush=True)
            time.sleep(.25)
        if (shared/'episode-result.json').exists():summary.update(json.loads((shared/'episode-result.json').read_text()))
        elif time.time()>deadline:summary['state']='BUDGET_EXHAUSTED'
        summary.update(submissions=len(ledger.entries),resolved=bool(ledger.entries and ledger.entries[-1]['feedback'].get('resolved')))
        if recovery_trial and summary['state']=='SEED_READY':
            from .recovery_experiment import capture_endpoint
            summary['endpoint']=capture_endpoint(container,out,shared,ledger.entries)
    except Exception:
        if summary['state']=='SEED_READY':summary['state']='SEED_CAPTURE_FAILED'
        (out/'host-error.log').write_text(traceback.format_exc())
    finally:
        docker('exec',container,'chmod','-R','a+rX','/shared/feedback/'+eid+'/recovery',check=False)
        if isolated:
            docker('exec',container,'chown','-R',f'{os.getuid()}:{os.getgid()}',
                '/shared/feedback/'+eid+'/recovery',check=False)
        docker('rm','-f',container,check=False)
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        if stream:stream.close()
        for p in (shared/'private').glob('*'):p.unlink(missing_ok=True)
        summary['finished']=time.time();atomic(out/'summary.json',summary)
        if strategy=='rewind':
            from .recovery_audit import write
            try:write(out)
            except Exception as error:atomic(out/'recovery-audit-error.json',{'error_type':type(error).__name__})
        print('END',task_id,strategy,summary['state'],flush=True)
    return summary

def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--env-file',required=True);p.add_argument('--root',required=True);p.add_argument('--runtime-source',required=True);args=p.parse_args()
    if shutil.disk_usage(ROOT).free<8*1024**3:raise RuntimeError('Mettle pilot refused: free at least 8 GiB on the workspace/WSL backing volume first.')
    args.repo=str(ROOT/'vendor/MettleBench');args.output_root=str(Path(args.root)/'results'/datetime.now().strftime('%Y%m%d_%H%M%S'))
    out=Path(args.output_root);out.mkdir(parents=True)
    layout=setup(args)
    shutil.copytree(ROOT/'bench_tools/mettlebench',out/'adapter-source');shutil.copytree(ROOT/'bench_tools/jiuwenswarm_runtime',out/'feedback-source',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(layout.runtime/'feedback-overlay.json',out/'runtime-overlay.json')
    atomic(out/'image.json',json.loads(docker('image','inspect',IMAGE).stdout))
    (out/'image-packages.txt').write_text(docker('run','--rm','--network','none',IMAGE,'pip','freeze').stdout)
    sampling=json.loads((ROOT/'local/mettlebench/sampling.json').read_text());atomic(out/'sampling.json',sampling)
    from bench_tools.batch import _closed_book
    handle=_closed_book.setup_batch_egress(Path(args.env_file),out)
    results=[];completed=False
    atomic(ROOT/'local/mettlebench/latest.json',{'output_root':str(out),'state':'RUNNING'})
    try:
        for row in sampling['selected']:
            ws=Path(args.repo)/row['task_dir']/'workspace'
            if (ws/'.git').exists():raise RuntimeError('Root-Git recovery validation is pending; do not run root-Git tasks until the metadata regression passes.')
            feedback,report=evaluate(args.repo,row['task_id'],ws,out/'preflight'/row['task_id'])
            if report is None or report.get('status')!='fail':raise RuntimeError('Initial-state preflight failed: '+row['task_id'])
        atomic(out/'experiment.json',{'model_source':str(args.env_file),'strategies':['continue','restart','rewind'],
            'design':'independent pristine starts','repetitions':1,'budget':{'uncached_tokens':200000,'seconds':1800,'submissions':8,'same_failure_limit':5},
            'difficulty':'structural proxy tertiles; not measured model difficulty','source_lock':sampling['source_lock']})
        for row in sampling['selected']:
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending=[pool.submit(episode,layout,args,row,s,handle.endpoint_url,handle.network) for s in ('continue','restart','rewind')]
                for future in as_completed(pending):results.append(future.result());atomic(out/'summary.json',results)
        completed=True
    finally:
        _closed_book.teardown_batch_egress(handle)
        atomic(ROOT/'local/mettlebench/latest.json',{'output_root':str(out),'state':'COMPLETED' if completed else 'FAILED','episodes':len(results)})
if __name__=='__main__':main()
