"""Durable 82-task/246-arm queue; resume retains original terminal records."""
import argparse,concurrent.futures,fcntl,gzip,hashlib,io,json,os,random,shutil,subprocess,sys,tarfile,time,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE=Path(os.environ.get('JWS_METTLE_ROOT', ROOT/'local/jws_mettle_full')).resolve()

def stream_sha256(stream):
    digest=hashlib.sha256()
    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(path)

def compact(episode):
    # Keep canonical submission archives and reports; remove only our expanded
    # evaluator/capture scratch copies after the worker has been fenced.
    for sub in (episode/'submissions').glob('*'):
        for p in (sub/'workspace',sub/'eval/workspace'):
            if p.is_dir() and not p.is_symlink() and p.resolve().is_relative_to(episode.resolve()):shutil.rmtree(p)
    for p in episode.rglob('*.log'):
        target=p.with_suffix('.log.gz');tmp=target.with_suffix('.gz.tmp')
        with p.open('rb') as inp,gzip.open(tmp,'wb') as out:shutil.copyfileobj(inp,out)
        with gzip.open(tmp,'rb') as inp:
            digest=stream_sha256(inp)
        with p.open('rb') as inp:assert digest==stream_sha256(inp)
        tmp.replace(target);p.unlink()

def fetch_workspace(row):
    task=ROOT/'vendor/MettleBench'/row['task_dir'];ws=task/'workspace'
    if not ws.exists():
        path=ROOT/'tmp'/('full-workspace-'+row['task_id']+'.tar.gz')
        url='https://huggingface.co/datasets/KelvinCoffee/MettleBench/resolve/main/workspaces/'+row['task_id']+'.tar.gz'
        path.parent.mkdir(parents=True,exist_ok=True)
        cmd=['curl','-L','--fail','--retry','3','--retry-all-errors','--retry-delay','2','--connect-timeout','20','--max-time','180','--max-filesize','209715200','-sS',url,'-o',str(path)]
        subprocess.run(cmd,check=True)
        row['workspace_tar_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        with tarfile.open(path) as tf:
            if sum(m.size for m in tf.getmembers())>1024**3:raise RuntimeError('Workspace extraction exceeds 1 GiB')
            tf.extractall(task,filter='tar')
        path.unlink()
    return ws

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--resume',type=Path);parser.add_argument('--budget-fix',action='store_true');parser.add_argument('--state-fix',action='store_true');parser.add_argument('--completed-from',type=Path);parser.add_argument('--workers',type=int,choices=(1,2),default=1);args0=parser.parse_args()
    BASE.mkdir(parents=True,exist_ok=True);lock=(BASE/'queue.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    out=args0.resume or BASE/'results'/('20261118_full_'+uuid.uuid4().hex[:8]);out.mkdir(parents=True,exist_ok=bool(args0.resume))
    if args0.budget_fix and not args0.resume:raise ValueError('--budget-fix requires --resume')
    frozen=out/('source-state-fix' if args0.state_fix else 'source-budget-fix' if args0.budget_fix else 'source')
    if args0.budget_fix and not args0.state_fix and not frozen.exists():
        shutil.copytree(out/'source',frozen)
        original=frozen/'bench_tools/jiuwenswarm_runtime/agent_entry.py'
        before=hashlib.sha256(original.read_bytes()).hexdigest()
        shutil.copy2(ROOT/'bench_tools/jiuwenswarm_runtime/agent_entry.py',original)
        save(out/'budget-fix-provenance.json',{'change':'classify deadline TimeoutError as budget exhaustion; task/verifier/budgets unchanged',
            'source':str(frozen),'agent_entry_before':before,'agent_entry_after':hashlib.sha256(original.read_bytes()).hexdigest()})
    if not frozen.exists():
        frozen.mkdir();(frozen/'bench_tools').mkdir()
        for part in ('jiuwenswarm_runtime','mettlebench','batch','net','swebench_runner'):
            shutil.copytree(ROOT/'bench_tools'/part,frozen/'bench_tools'/part,
                ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','.venv','runtime','shared_test','results'))
        shutil.copy2(__file__,frozen/'launch-source.py')
    sys.path.insert(0,str(frozen))
    from bench_tools.mettlebench.runner import episode
    from bench_tools.mettlebench.core import evaluate,tree_hash,docker,IMAGE
    if args0.state_fix:
        save(out/'state-fix-provenance.json',{'source':str(frozen),'image':IMAGE,
            'image_id':json.loads(docker('image','inspect',IMAGE).stdout)[0]['Id'],
            'changes':['exact evaluator git safe.directory','build-time Cython/setuptools','submission scratch archive'],
            'existing_results_preserved':True,'completed_from':str(args0.completed_from) if args0.completed_from else None})
    from bench_tools.jiuwenswarm_runtime.layout import resolve_layout
    from bench_tools.batch import _closed_book
    layout=resolve_layout(BASE)
    if not (out/'plan.json').exists():
        original=json.loads((ROOT/'local/mettlebench/sampling.json').read_text())
        rows=original['population'];assert len(rows)==82 and len({r['task_id'] for r in rows})==82
        rng=random.Random(20261118);rng.shuffle(rows)
        canary=next(r for r in rows if r['task_id']=='programbench_pandoc_reference_handoff')
        rows=[canary]+[r for r in rows if r is not canary]
        for n,row in enumerate(rows):row['strategy_order']=[('continue','restart','rewind')[(n+i)%3] for i in range(3)]
        save(out/'plan.json',{'seed':20261118,'tasks':rows,'arms':246,'budget':{'uncached_tokens':200000,'seconds':1800,'submissions':8,'max_rewinds':6},
                             'design':'independent pristine starts; cyclic arm ordering; two concurrent arms; Pandoc canary first','source_lock':original['source_lock']})
        shutil.copy2(layout.runtime/'feedback-overlay.json',out/'runtime-overlay.json')
        save(out/'image.json',json.loads(docker('image','inspect',IMAGE).stdout))
    rows=json.loads((out/'plan.json').read_text())['tasks'];results=[];failures=[]
    state_path=ROOT/'local/mettlebench/full-latest.json'
    def status(state,current=None):
        # Recount retained results on resume; the loop has not necessarily visited
        # later completed tasks yet. Corrupt/incomplete records remain visible.
        retained=[];invalid=[]
        for path in out.glob('*/*/summary.json'):
            try:retained.append(json.loads(path.read_text()))
            except (ValueError,OSError):invalid.append(str(path))
        effective={(r['task_id'],r['strategy']):r for r in retained}
        if args0.completed_from:
            for path in args0.completed_from.glob('*/*/summary.json'):
                record=json.loads(path.read_text())
                if record.get('state') in ('RESOLVED','UNRESOLVED','BUDGET_EXHAUSTED','REWIND_BUDGET_EXHAUSTED'):
                    effective[(record['task_id'],record['strategy'])]={**record,'external_evidence':str(path)}
        retained=list(effective.values())
        try:free_gib=round(shutil.disk_usage(ROOT).free/1024**3,2)
        except OSError:free_gib=None
        value={'output_root':str(out),'state':state,'task_count':82,'planned_arms':246,'completed_arms':len(results),
               'official_passed':sum(bool(r.get('resolved')) for r in retained),'current_task':current,'infrastructure_tasks':failures,
               'updated':time.time(),'free_gib':free_gib,'invalid_summaries':invalid}
        value['completed_arms']=len(retained)
        save(state_path,value);save(out/'queue-status.json',value);save(out/'summary.json',retained)
    status('STARTING');handle=None
    args=argparse.Namespace(repo=str(ROOT/'vendor/MettleBench'),output_root=str(out),isolated=True,
                            env_file=os.environ['JWS_MODEL_ENV_FILE'])
    try:
        handle=_closed_book.setup_batch_egress(Path(args.env_file),out)
        for index,row in enumerate(rows):
            done=[];pending=[]
            for strategy in row['strategy_order']:
                p=out/row['task_id']/strategy/'summary.json'
                external=args0.completed_from/row['task_id']/strategy/'summary.json' if args0.completed_from else None
                if external and external.exists():
                    record=json.loads(external.read_text())
                    if record.get('state') in ('RESOLVED','UNRESOLVED','BUDGET_EXHAUSTED','REWIND_BUDGET_EXHAUSTED'):
                        done.append({**record,'external_evidence':str(external)})
                        save(out/'external-completions'/row['task_id']/(strategy+'.json'),{'summary':str(external),'state':record['state']})
                        continue
                if p.exists():
                    try:record=json.loads(p.read_text())
                    except (ValueError,OSError):
                        status('PAUSED_INVALID_SUMMARY',row['task_id'])
                        raise RuntimeError('Invalid summary preserved; classify and archive before retry: '+str(p))
                    classification=p.parent/'classification.json'
                    if classification.exists():
                        value=json.loads(classification.read_text())
                        if value.get('state')=='BUDGET_EXHAUSTED' and record.get('error')=='TimeoutError':
                            record={**record,'state':'BUDGET_EXHAUSTED','original_state':record['state'],'original_error':record['error'],
                                'classification_evidence':str(classification)}
                            record.pop('error',None)
                    done.append(record)
                elif (p.parent/'run.json').exists():
                    status('PAUSED_INCOMPLETE',row['task_id']);raise RuntimeError('Interrupted arm requires explicit infrastructure replacement: '+str(p.parent))
                else:pending.append(strategy)
            results.extend(done)
            if not pending:
                if index==0 and any(r.get('error') or (r['state']=='FAILED' and not r.get('terminal')) for r in done):
                    status('PAUSED_CANARY',row['task_id']);return
                continue
            if shutil.disk_usage(ROOT).free<3*1024**3:
                status('PAUSED_DISK',row['task_id']);return
            status('PREPARING',row['task_id'])
            try:
                ws=fetch_workspace(row);row['initial_tree_sha256']=tree_hash(ws);save(out/'inputs'/f"{row['task_id']}.json",row)
                preflight_root=out/('preflight-state-fix' if args0.state_fix else 'preflight')
                prior=preflight_root/row['task_id']/'result.json'
                if prior.exists():report=json.loads(prior.read_text())
                else:feedback,report=evaluate(args.repo,row['task_id'],ws,preflight_root/row['task_id'])
                if report is None or report.get('status')!='fail' or report.get('metrics',{}).get('active_error',0):raise RuntimeError('Initial-state verifier invalid')
            except Exception as exc:
                failures.append({'task':row['task_id'],'phase':'preflight','error_type':type(exc).__name__})
                save(out/'preflight-errors.json',failures)
                if index==0:status('PAUSED_CANARY',row['task_id']);return
                continue
            status('RUNNING',row['task_id'])
            with concurrent.futures.ThreadPoolExecutor(max_workers=args0.workers) as pool:
                futures=[pool.submit(episode,layout,args,row,s,handle.endpoint_url,handle.network) for s in pending]
                for future in concurrent.futures.as_completed(futures):
                    result=future.result();results.append(result)
                    status('RUNNING',row['task_id'])
                    try:compact(out/row['task_id']/result['strategy'])
                    except OSError as exc:
                        save(out/row['task_id']/result['strategy']/'archive-error.json',{'error_type':type(exc).__name__})
                        failures.append({'task':row['task_id'],'strategy':result['strategy'],'phase':'archive','error_type':type(exc).__name__})
                        status('RUNNING',row['task_id'])
            if index==0 and any(r.get('error') or (r['state']=='FAILED' and not r.get('terminal')) for r in results):
                status('PAUSED_CANARY',row['task_id']);return
            # Evaluator expanded copies are disposable, including preflight.
            p=preflight_root/row['task_id']/'workspace'
            if p.is_dir() and p.resolve().is_relative_to(out.resolve()):
                try:shutil.rmtree(p)
                except OSError as exc:
                    failures.append({'task':row['task_id'],'phase':'preflight_cleanup','error_type':type(exc).__name__})
                    save(out/'cleanup-errors.json',failures)
        status('COMPLETED_WITH_INFRA_ERRORS' if failures else 'COMPLETED')
    except BaseException:
        status('INTERRUPTED');raise
    finally:
        if handle is not None:_closed_book.teardown_batch_egress(handle)

if __name__=='__main__':main()
