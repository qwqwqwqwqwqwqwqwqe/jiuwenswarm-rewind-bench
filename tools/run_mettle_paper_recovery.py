"""Small B.4-style recovery pilot: natural failed Continue, then matched C/RW."""
import argparse,concurrent.futures,fcntl,json,os,shutil,sys,time,uuid,ast
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ.setdefault('JWS_METTLE_IMAGE','jws-mettle:20261123-resume-fix')
from bench_tools.mettlebench import runner
from bench_tools.mettlebench.core import docker,evaluate,IMAGE
from bench_tools.mettlebench.recovery_experiment import finalize_endpoint,sha
from bench_tools.jiuwenswarm_runtime.layout import resolve_layout
from bench_tools.jiuwenswarm_runtime.ledger import atomic
from bench_tools.batch import _closed_book
from run_mettle_full import fetch_workspace

BASE=Path(os.environ.get('JWS_METTLE_ROOT', ROOT/'local/jws_mettle_paper_recovery')).resolve()
STATUS=ROOT/'local/mettlebench/paper-recovery-latest.json'
TASKS=['programbench_ctags_golden_handoff','programbench_bat_cache_handoff',
       'programbench_age_crypto_fixture_handoff','programbench_gowsdl_regen_handoff']
EXPANSION_TASKS={
    'terminal_db_wal_recovery_handoff':'Historical Continue stopped after five rejected submissions; recovery of original state.',
    'terminal_code_image_handoff':'Original images retired during publishing; historical Continue passed on submission three.',
    'swe_matplotlib_readonly_mask_handoff':'Golden artifacts overwritten; local scratch backup enabled repair.',
    'terminal_cython_numpy_route_handoff':'Build changed vendor source; local copy enabled repair.',
    'projecteval_budget_rerun_double_lockin':'Duplicate posting record was locally repairable after rejection.',
    'projecteval_expense_nonidempotent_posting_lockin':'Duplicate batch accounting was locally repairable after rejection.',
    'programbench_trdsql_data_handoff':'Rewind stalled on validation schema; historical Continue passed on submission three.',
    'swe_pydicom_explicit_vr_handoff':'Repeated conversion failure followed by an in-place repair.',
    'programbench_gomplate_template_handoff':'Historical Continue needed two submissions; multi-step handoff comparison.',
}

def patch(text,old,new):
    if text.count(old)!=1:raise ValueError('Patch anchor mismatch: '+old[:100])
    return text.replace(old,new,1)

def install():
    from bench_tools.jiuwenswarm_runtime.install_runtime import install as overlay
    runtime=BASE/'runtime';runtime.parent.mkdir(parents=True,exist_ok=True)
    if runtime.exists():raise ValueError('Dedicated runtime exists; do not overwrite a running experiment')
    shutil.copytree(os.environ['JWS_RUNTIME_SOURCE'],runtime,symlinks=True)
    overlay(runtime)
    sp=next(runtime.glob('lib/python*/site-packages'))
    p=sp/'jiuwenswarm/rewind/boundaries.py';s=p.read_text()
    s=patch(s,'if os.environ.get("JWS_EVAL_FEEDBACK") == "1" and self._prefix_records:',
        'if os.environ.get("JWS_EVAL_FEEDBACK") == "1" and self._prefix_records and os.environ.get("JWS_PAPER_RECOVERY") != "1":')
    s=patch(s,'candidates = backtrack_candidates(reason=reason)',
        'candidates = backtrack_candidates(reason=reason, limit=80)')
    s=patch(s,'request = {**request, **self.model_parameters}',
        'request = {**request, **self.model_parameters, "temperature": 0.0}')
    ast.parse(s);p.write_text(s)
    p=sp/'replay/backtrack.py';s=p.read_text()
    # Lexical bans conflate "cannot repair the damaged branch" with "task
    # impossible". The paper pilot keeps nonempty/schema/duplicate validation
    # but does not veto operational memory merely for a feasibility keyword.
    s=patch(s,'phrase = find_feasibility_verdict(memory_summary)',
        'phrase = None  # paper recovery: do not veto operational memory by keyword')
    ast.parse(s);p.write_text(s)
    from bench_tools.mettlebench.recovery_experiment import sha
    atomic(runtime/'paper-recovery-overlay.json',{'version':2,'temperature':0,'candidate_limit':80,
        'historical_prefix_candidates':True,'lexical_memory_veto':False,
        'historical_stagnation_terminal_suppressed':True,
        'files':{str(p.relative_to(sp)):sha(p) for p in [sp/'replay/backtrack.py',
            sp/'jiuwenswarm/rewind/boundaries.py',sp/'jiuwenswarm/rewind/eval_feedback.py']}})
    print('INSTALLED',runtime,flush=True)

def repeat_strategies(repetition, task_index):
    """Balance serial arm order across repeats and tasks."""
    return ('restart','rewind') if (repetition + task_index) % 2 else ('rewind','restart')

def run_endpoint_arms(out,args,row,handle,seedroot,strategies,repetition,results,status,expected_origin):
    seed=json.loads((seedroot/'endpoint/manifest.json').read_text())['seed']
    args.recovery_trial={'seed':seed,'source':str(seedroot)}
    args.output_root=str(out)
    task=row['task_id']
    for strategy in strategies:
        status('RUNNING_RESTART' if repetition is None else 'RUNNING_REPEATS',task,strategy,repetition)
        arm=out/task/strategy
        saved=arm/'summary.json'
        result=json.loads(saved.read_text()) if saved.is_file() else runner.episode(
            resolve_layout(BASE),args,row,strategy,handle.endpoint_url,handle.network)
        results.append({'task':task,'arm':strategy,'repetition':repetition,'summary':result})
        if result['state'] not in {'RESOLVED','UNRESOLVED','BUDGET_EXHAUSTED'}:
            raise RuntimeError(f'{task} {strategy} runtime failed; preserved attempt for audit')
        if strategy=='rewind':
            context=json.loads((arm/'ipc/paired-first-input.json').read_text())
            environment=json.loads((arm/'ipc/paired-environment-audit.json').read_text())
            if not context['context_equal'] or environment!=expected_origin:
                raise ValueError(f'{task}: repeated Rewind differs from the original failed endpoint')
        else:
            origin=json.loads((arm/'ipc/restart-origin.json').read_text())
            if origin['prior_failures_sha256']!=sha(seedroot/'endpoint/failures.json'):
                raise ValueError(f'{task}: repeated Restart has different failure feedback')
        status('RUNNING_RESTART' if repetition is None else 'RUNNING_REPEATS',task,strategy,repetition)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--install',action='store_true');ap.add_argument('--resume',type=Path)
    cohort=ap.add_mutually_exclusive_group()
    cohort.add_argument('--expansion',action='store_true',help='Run the second, nine-task recovery cohort')
    cohort.add_argument('--restart-from',type=Path,help='Add Restart arms for the validated pairs in this batch')
    cohort.add_argument('--repeat-from',type=Path,help='Repeat Restart and Rewind twice for each validated frozen endpoint')
    args0=ap.parse_args()
    if args0.install:install();return
    tasks=list(EXPANSION_TASKS) if args0.expansion else TASKS
    status_path=STATUS.with_name('paper-recovery-expansion-latest.json') if args0.expansion else STATUS
    lock=(BASE/'queue.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    prefix='recovery_expansion_' if args0.expansion else 'recovery_'
    selected_source=args0.restart_from or args0.repeat_from
    restart_source=selected_source.resolve(strict=True) if selected_source else None
    repetitions=2 if args0.repeat_from else 1
    if restart_source:
        original=json.loads((restart_source/'experiment.json').read_text())
        tasks=[]
        for task in original['tasks']:
            validation=restart_source/task/'pair-validation.json'
            if validation.is_file() and json.loads(validation.read_text()).get('valid') is True:
                tasks.append(task)
        if not tasks:raise ValueError('No validated recovery pairs in source batch')
        status_path=STATUS.with_name('paper-recovery-restart-latest.json')
        prefix='recovery_restart_'
        if args0.repeat_from:
            status_path=STATUS.with_name('paper-recovery-repeats-latest.json')
            prefix='recovery_repeats_'
    out=args0.resume or BASE/'results'/(prefix+time.strftime('%Y%m%d_%H%M%S'))
    if args0.resume:
        previous=json.loads((out/'experiment.json').read_text())
        if (previous['tasks']!=tasks or previous.get('restart_source')!=(str(restart_source) if restart_source else None)
                or previous['repetitions']!=repetitions):
            raise ValueError('Resume tasks and restart source must match the original cohort')
    out.mkdir(parents=True,exist_ok=bool(args0.resume))
    source=out/('source-resume-'+uuid.uuid4().hex[:8] if args0.resume else 'source')
    if not source.exists():
        (source/'bench_tools').mkdir(parents=True)
        for part in ('jiuwenswarm_runtime','mettlebench'):
            shutil.copytree(ROOT/'bench_tools'/part,source/'bench_tools'/part,ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(__file__,source/'launch-source.py' if args0.resume else out/'launch-source.py')
        shutil.copy2(BASE/'runtime/paper-recovery-overlay.json',source/'runtime-overlay.json' if args0.resume else out/'runtime-overlay.json')
    runner.ROOT=source
    image=json.loads(docker('image','inspect',IMAGE).stdout)
    if restart_source and image[0]['Id']!=json.loads((restart_source/'image.json').read_text())[0]['Id']:
        raise ValueError('Restart image differs from the original paired experiment')
    atomic(out/'image.json',image)
    protocol='Restart from pristine task with frozen failed Continue experience' if restart_source else 'paper B.4 natural failed Continue -> Continue/Rewind'
    if args0.repeat_from:protocol='Repeated Restart and Rewind comparisons on validated frozen Continue endpoints'
    atomic(out/'experiment.json',{'protocol':protocol,
        'restart_source':str(restart_source) if restart_source else None,
        'tasks':tasks,'model':'DeepSeek-V4.1-Flash-1017','harness':'JiuwenSwarm','temperature':0,
        'selection_reasons':EXPANSION_TASKS if args0.expansion else {},
        'repetitions':repetitions,'workers':1 if restart_source else 2,'seed_eligibility':'five consecutive identical official failures',
        'budgets':'paper profile: no practical token/time/submission/rewind cap; finite compatibility sentinels',
        'same_failure_limit':5,'scratch':('Restart fresh per attempt; Rewind exact shared endpoint' if args0.repeat_from else
            'fresh per attempt' if restart_source else 'exact shared endpoint; retained outside rewind boundary'),
        'bootstrap_is_recovery':False,'selection':'purposive pilot, not random 50-trial replication'})
    rows=json.loads((ROOT/'local/mettlebench/sampling.json').read_text())['population']
    args=argparse.Namespace(repo=str(ROOT/'vendor/MettleBench'),output_root=str(out),isolated=True,
        env_file=os.environ['JWS_MODEL_ENV_FILE'],recovery_trial={})
    results=[]
    def status(state,task=None,arm=None,repetition=None):
        atomic(status_path,{'state':state,'output_root':str(out),'pid':os.getpid(),'tasks':tasks,
            'current_task':task,'current_arm':arm,'current_repetition':repetition,
            'planned_arms':len(tasks)*repetitions*(2 if args0.repeat_from else 1) if restart_source else None,
            'results':results,'source':str(source),'updated':time.time()})
    status('PREFLIGHT')
    for task in tasks:
        p=out/'preflight'/task
        if not (p/'result.json').exists():
            row=next(r for r in rows if r['task_id']==task)
            feedback,report=evaluate(args.repo,task,fetch_workspace(row),p)
            if not report or report.get('status')!='fail' or report.get('metrics',{}).get('active_error',0):
                raise RuntimeError('Preflight failed: '+task)
    handle=_closed_book.setup_batch_egress(Path(args.env_file),out)
    try:
        for task_index,task in enumerate(tasks):
            if shutil.disk_usage(ROOT).free<4*1024**3:raise RuntimeError('Disk headroom below 4 GiB; censored, not model failure')
            row=next(r for r in rows if r['task_id']==task);seedroot=out/task/'seed'
            if restart_source:
                seedroot=restart_source/task/'seed'
                expected=json.loads((restart_source/task/'pair-validation.json').read_text())['environment']
                for repetition in range(1,repetitions+1):
                    if shutil.disk_usage(ROOT).free<4*1024**3:
                        raise RuntimeError('Disk headroom below 4 GiB; censored, not model failure')
                    strategies=repeat_strategies(repetition,task_index) if args0.repeat_from else ('restart',)
                    target=out/f'repeat-{repetition:02d}' if args0.repeat_from else out
                    run_endpoint_arms(target,args,row,handle,seedroot,strategies,
                        repetition if args0.repeat_from else None,results,status,expected)
                continue
            status('GENERATING_FAILED_CONTINUE',task)
            if (seedroot/'summary.json').exists():seed_result=json.loads((seedroot/'summary.json').read_text())
            else:
                args.recovery_trial={'phase':'seed'}
                seed_result=runner.episode(resolve_layout(BASE),args,row,'seed',handle.endpoint_url,handle.network)
            if seed_result['state'] in {'SEED_READY','SEED_CAPTURE_FAILED'} and not (seedroot/'endpoint/manifest.json').exists():
                entries=json.loads((seedroot/'ledger.json').read_text())
                final=seedroot/'submissions'/f'{len(entries):03d}'
                for name,reference in [('workspace.tar',final/'workspace.tar'),
                                       ('scratch.tar',final/'recovery-state/scratch.tar')]:
                    if sha(seedroot/'endpoint'/name)!=sha(reference):
                        raise ValueError(f'Interrupted {task} {name} differs from the frozen final submission')
                finalize_endpoint(seedroot,seedroot/'ipc',entries)
                atomic(seedroot/'endpoint/archive-repair.json',{
                    'matched_final_submission_archives':True,'submission':len(entries),
                    'previous_state':seed_result['state']})
                seed_result={**seed_result,'state':'SEED_READY'}
            if seed_result['state']=='SEED_CAPTURE_FAILED' and (seedroot/'endpoint/manifest.json').is_file():
                seed_result={**seed_result,'state':'SEED_READY'}
            if seed_result['state']!='SEED_READY':
                results.append({'task':task,'eligible':False,'seed_state':seed_result['state'],
                    'reason':'Continue solved naturally' if seed_result.get('resolved') else 'No valid stagnated endpoint'})
                if seed_result['state']=='FAILED':raise RuntimeError('Seed runtime failed: '+task)
                continue
            manifest=json.loads((seedroot/'endpoint/manifest.json').read_text())
            args.recovery_trial={'seed':manifest['seed'],'source':str(seedroot)}
            status('RECOVERING_MATCHED_ENDPOINT',task)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures={}
                for strategy in ['continue','rewind']:
                    arm=out/task/strategy
                    p=arm/'summary.json'
                    if p.exists():
                        previous=json.loads(p.read_text())
                        if (not (arm/'ipc/paired-first-input.json').exists()
                                and previous.get('submissions')==0
                                and previous.get('usage',{}).get('api_calls')==0
                                and previous.get('terminal')==manifest['seed']['feedback']):
                            # Preserve a replay-only exit for audit before retrying.
                            archive=out/'replay-only-exits'/task
                            archive.mkdir(parents=True,exist_ok=True)
                            arm.rename(archive/f'{strategy}-{uuid.uuid4().hex[:8]}')
                        else:
                            results.append({'task':task,'arm':strategy,'summary':previous})
                            continue
                    futures[pool.submit(runner.episode,resolve_layout(BASE),args,row,strategy,handle.endpoint_url,handle.network)]=strategy
                for f in concurrent.futures.as_completed(futures):
                    result=f.result();results.append({'task':task,'arm':futures[f],'summary':result})
                    status('RECOVERING_MATCHED_ENDPOINT',task)
                    if result['state']=='FAILED':raise RuntimeError('Recovery runtime failed; preserve evidence and stop')
            audits=[json.loads((out/task/s/'ipc/paired-environment-audit.json').read_text()) for s in ['continue','rewind']]
            if audits[0]!=audits[1]:raise ValueError('Paired environments/instructions differ')
            atomic(out/task/'pair-validation.json',{'valid':True,'environment':audits[0]})
        status('COMPLETED')
    except BaseException as e:
        status('STOPPED_ERROR');raise
    finally:_closed_book.teardown_batch_egress(handle)

if __name__=='__main__':main()
