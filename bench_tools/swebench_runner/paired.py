"""One common failed attempt, then three isolated repair interventions."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import replace
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from .controller import run_episode,setup
from bench_tools.jiuwenswarm_runtime.ledger import atomic

# An arm whose latest episode ended in one of these states carries clean,
# collector-ready evidence; anything else (FAILED/HOST_FAILURE) is retried on
# resume only when its logs carry an infrastructure signature - program defects
# (replay mismatch, protocol errors) must not be burned twice.
CLEAN_ARM_STATES={'RESOLVED','UNRESOLVED','BUDGET_EXHAUSTED','SEED_READY','REWIND_BUDGET_EXHAUSTED'}
AUTO_RERUN_LIMIT=1
INFRA_MARKERS=('async stream error','unexpected end of json input','apierror',
    'connection error','readtimeout','apitimeout','deadline while waiting','infra_failure')

def infra_death(episode):
    """True when the episode's logs show a backend/transport failure signature."""
    tails=''
    for log in Path(episode).glob('recovery/attempt-*/process.log'):
        try:tails+=log.read_text(errors='replace')[-20000:]
        except OSError:pass
    for name in ('host-error.log','manager-error.log'):
        path=Path(episode)/name
        if path.exists():
            try:tails+=path.read_text(errors='replace')[-4000:]
            except OSError:pass
    return any(marker in tails.lower() for marker in INFRA_MARKERS)

def latest_dead_episode(out,case,strategy):
    """Newest dead episode directory when the arm has NO clean summary at all."""
    episodes=sorted((out/case/strategy).glob('fb-*'),reverse=True)
    for path in episodes:
        if (path/'summary.json').exists():
            try:state=json.loads((path/'summary.json').read_text()).get('state')
            except (ValueError,OSError):state=None
            if state in CLEAN_ARM_STATES:return None
    for path in episodes:
        if (path/'summary.json').exists() or (path/'run.json').exists():
            return path
    return None

def completed_arm_summary(out,case,strategy):
    """The clean summary of this arm's finished episode, if any."""
    for path in sorted((out/case/strategy).glob('fb-*/summary.json')):
        try:summary=json.loads(path.read_text())
        except (ValueError,OSError):continue
        if summary.get('state') in CLEAN_ARM_STATES:return summary
    return None

def seed_manifest_if_frozen(out,case):
    """Reuse a previously frozen seed instead of re-running the seed episode."""
    common_path=out/'common-summary.json'
    if not common_path.exists():return None
    try:common=json.loads(common_path.read_text())
    except (ValueError,OSError):return None
    if common.get('state')!='SEED_READY':return None
    manifest=common.get('seed_manifest')
    if not manifest or not Path(manifest).exists():return None
    return json.loads(Path(manifest).read_text())

def resume_evidence(out):
    """True when this output root already carries progress from an earlier
    invocation. Must be read BEFORE this run writes anything: the seed phase
    writes common-summary.json, so a check placed after it always reports a
    resume (v10: first-run arm retries were consumed immediately, and a killed
    rewind arm was then skipped forever)."""
    out=Path(out)
    return any((out/name).exists() for name in ('common-summary.json','summary.json','rerun-attempts.json'))


def run(args):
    args.paired=True
    layout=setup(args);out=Path(args.output_root);out.mkdir(parents=True,exist_ok=True)
    # Snapshot resume state before the seed phase can create common-summary.json.
    resumed_initial=resume_evidence(out)
    if not (out/'implementation-source').exists():
        shutil.copytree(Path(__file__).parent,out/'implementation-source',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(layout.runtime/'feedback-overlay.json',out/'runtime-overlay.json')
    # Shared branches are namespaced by the batch directory so a re-attempted
    # batch never collides with a killed predecessor's leftover shared state
    # (v10 attempt 1 died exactly on that mkdir).
    batch_root=out.parent.name+'-'+out.name
    seed=getattr(args,'frozen_seed',None) or seed_manifest_if_frozen(out,args.case)
    if seed is None:
        atomic(out/'experiment.json',{**vars(args),'design':'shared-failure-paired',
            'repair_budget_excludes_seed':True,'restart_resets_workspace_and_context':True,
            'bootstrap_counted_as_rewind':False,'evaluator_unchanged':True,'repetitions':1})
        seed_args=SimpleNamespace(**{**vars(args),'token_limit':300000,'timeout':3600})
        seed_layout=replace(layout,shared=layout.shared/batch_root/'seed');seed_layout.shared.mkdir(parents=True,exist_ok=True)
        common=run_episode(seed_args,seed_layout,args.case,'seed')
        atomic(out/'common-summary.json',common)
        if common['state']!='SEED_READY' or 'seed_manifest' not in common:
            raise RuntimeError('No usable failed seed was frozen: '+common['state'])
        seed=json.loads(Path(common['seed_manifest']).read_text())
    arm_args=SimpleNamespace(**{**vars(args),'paired_seed':seed})
    results=[];rerun={}
    attempts_path=out/'rerun-attempts.json'
    # A dead arm's automatic retry is budgeted on resumes only; the snapshot
    # taken at entry keeps a first run from counting its own arms.
    resumed=resumed_initial
    if attempts_path.exists():
        try:rerun=json.loads(attempts_path.read_text())
        except (ValueError,OSError):rerun={}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        tasks=[]
        for strategy in ('continue','restart','rewind'):
            done=completed_arm_summary(out,args.case,strategy)
            if done is not None:
                results.append(done);atomic(out/'summary.json',results);continue
            if rerun.get(strategy,0)>=AUTO_RERUN_LIMIT:
                continue  # failed after a resume retry: preserve for manual analysis
            dead=latest_dead_episode(out,args.case,strategy)
            if dead is not None and not infra_death(dead):
                continue  # program defect, not infrastructure: never auto-burn twice
            if resumed:
                rerun[strategy]=rerun.get(strategy,0)+1
                atomic(attempts_path,rerun)
            branch=replace(layout,shared=layout.shared/batch_root/strategy);branch.shared.mkdir(parents=True,exist_ok=True)
            tasks.append(executor.submit(run_episode,arm_args,branch,args.case,strategy))
        for future in as_completed(tasks):
            results.append(future.result());atomic(out/'summary.json',results)
    return results
