"""Standalone host runner for feedback Continue / Restart / Rewind episodes."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

from .evaluator_bridge import evaluate
from .feedback import rejected_paths
from bench_tools.jiuwenswarm_runtime.ledger import Ledger,atomic
from .recovery_prompt import RECOVERY_GUIDANCE

ROOT=Path(__file__).resolve().parents[2]
from bench_tools.jiuwenswarm_runtime.host import setup, resolve_layout, _ai_values
from ._test_environment import record_test_environment,describe_test_environment
from ._git_history import prune_git_future_history
from .git_patch import snapshot_git_workspace,capture_git_patch
from .run_case import start_agent_container,preflight_container,load_case


def apply_budget_profile(args):
    if args.paper_budget:
        # Bundled BenchOptions requires finite positive numbers. These sentinels
        # disable practical resource caps; five identical failures still stop.
        args.max_submissions=sys.maxsize
        args.token_limit=sys.maxsize
        args.max_rewinds=sys.maxsize
        args.timeout=10**12
    return args


def docker(*args,check=True,timeout=300):
    result=subprocess.run(['docker',*map(str,args)],capture_output=True,text=True,timeout=timeout)
    if check and result.returncode:raise RuntimeError('Docker operation failed: '+result.stderr[-1500:])
    return result


def prompt(problem,environment):
    return ('You are an expert software engineer repairing a repository-level task.\n'+problem+
        '\n\nWorkspace: /testbed is the delivery Git repository. Read and repair project source here. '
        'Existing repository tests are read-only and may be executed. Never create temporary tests, '
        'reproductions, reports, caches or logs anywhere under /testbed. Write all scratch scripts '
        'under $JWS_SCRATCH_DIR (outside /testbed); use the project interpreter and explicit import paths. '
        'Set PYTHONPATH=/testbed:/testbed/tests when required for external scripts. '
        'Do not change official tests or install dependencies. Do not git commit. Do not ask the user questions. '
        'Do not leave background commands modifying the repository.\n'+describe_test_environment(environment)+
        '\n\nUse submit_patch to request official acceptance. Self-tests alone do not establish task completion. '
        'When feedback rejects a submission, investigate the actual failure and shared implementation; '
        'do not merely repeat the same patch. Continue in place, or consider backtrack tools if available. '
        'Final answers are automatically submitted too. Only official acceptance can finish successfully. '
        'The feedback identifies the first observable failure, not the entire hidden test suite.\n'
        'The failing test named in official feedback may not exist in this checkout: hidden acceptance '
        'tests are applied only by the evaluator. Do not hunt for the original test file or try to '
        'reproduce the official failure exactly; build your own equivalent scenario from the assertion '
        'text. Being unable to reproduce the official failure locally is expected and says nothing about '
        'your fix - only submit_patch and the official evaluator can decide. Never make local '
        'reproduction of the official failure a precondition for submitting.\n'+RECOVERY_GUIDANCE)


def run_episode(args,layout,case_id,strategy):
    episode=f'fb-{int(time.time())}-{uuid.uuid4().hex[:7]}-{strategy}'
    container='jws-'+episode+'-'+case_id
    out=Path(args.output_root)/case_id/strategy/episode;out.mkdir(parents=True)
    seed=getattr(args,'paired_seed',None)
    namespace=seed['namespace'] if seed else episode
    common_history=seed['history'] if seed else []
    shared=layout.shared/'feedback'/namespace
    for sub in ('requests','responses','private'):(shared/sub).mkdir(parents=True,exist_ok=True)
    shared.chmod(0o755);(shared/'responses').chmod(0o755);(shared/'private').chmod(0o700)
    ledger=Ledger(out,max_submissions=args.max_submissions,repeated_limit=getattr(args,'repeated_limit',5),submission_offset=len(common_history))
    base_url,model,key=_ai_values(SimpleNamespace(env_file=Path(args.env_file),base_url_env='JWS_BASE_URL',
        model_name_env='JWS_MODEL_NAME',api_key_env='API_KEY'))
    # Existing shim files can carry a previous WSL address. Only its host is adjusted.
    if ':18999' in base_url:
        from urllib.parse import urlsplit,urlunsplit
        u=urlsplit(base_url);ip=subprocess.check_output(['hostname','-I'],text=True).split()[0]
        base_url=urlunsplit((u.scheme,ip+':18999',u.path,u.query,u.fragment))
    # Closed-book (anti-cheat): the container talks only to the egress forwarder,
    # which permits the model endpoint and blocks everything else.
    egress_endpoint=getattr(args,'egress_endpoint_url',None)
    if egress_endpoint:base_url=egress_endpoint
    private=shared/'private/model.json';atomic(private,{'base_url':base_url,'model_name':model,'api_key':key});private.chmod(0o600)
    started=time.time();deadline=started+args.timeout
    process=None;stream=None;begun=False
    summary={'episode':episode,'case':case_id,'strategy':strategy,'state':'FAILED','output':str(out),
             'container':container,'model':model,'started':started,'source_package':'173855',
             'feedback_host_root':str(shared),'namespace':namespace,'paired':getattr(args,'paired',False),
             'common_submissions':len(common_history)}
    atomic(out/'run.json',summary)
    try:
        image='swebench/sweb.eval.x86_64.'+case_id.replace('__','_1776_',1)+':latest'
        legacy_backend=getattr(args,'backend_root',None)
        start_agent_container(layout,image=image,container_name=container,
                              backend_root=Path(legacy_backend) if legacy_backend else None,
                              jiuwen_port=28092,validation_gate=False,
                              network=getattr(args,'egress_network',None))
        begun=True;preflight_container(container)
        environment=record_test_environment(container,out)
        prune=prune_git_future_history(container)
        if prune.error:raise RuntimeError(prune.error)
        baseline=snapshot_git_workspace(container,'/testbed',timeout=300)
        if baseline.error:raise RuntimeError(baseline.error)
        task=load_case(Path(args.dataset),case_id)
        effective=prompt(task['problem_statement'],environment)
        if seed:
            if baseline.tree!=seed['baseline']:raise ValueError('Paired baseline differs')
            effective=seed['prompt']
            docker('exec',container,'mkdir','-p','/bench-runtime','/bench-scratch')
            docker('cp',seed['patch_file'],container+':/bench-runtime/paired-seed.patch')
            docker('exec','-w','/testbed',container,'git','apply','/bench-runtime/paired-seed.patch')
            docker('exec',container,'rm','/bench-runtime/paired-seed.patch')
            restored=capture_git_patch(container,'/testbed',baseline_tree=baseline.tree,timeout=120)
            if restored.error:raise RuntimeError(restored.error)
            sha=hashlib.sha256(restored.patch.encode()).hexdigest()
            if sha!=seed['feedback']['patch_sha256']:raise ValueError('Paired restored patch differs')
            atomic(out/'paired-start.json',{'patch_sha256':sha,'seed_context_sha256':seed['context_sha256'],
                'baseline':baseline.tree,'history':common_history,'repair_token_limit':args.token_limit,
                'additional_submission_limit':args.max_submissions})
            if strategy=='restart':
                docker('exec','-w','/testbed',container,'git','read-tree','--reset','-u',baseline.tree)
                docker('exec','-w','/testbed',container,'git','clean','-fd')
            else:
                for name in ('traces','snapshot-store'):
                    # Resume reuses the same shared branch: a previous attempt's
                    # traces may already be present here.
                    shutil.copytree(Path(seed['bundle'])/name,shared/'recovery'/name,
                                    symlinks=True,dirs_exist_ok=True)
                scratch=Path(seed['bundle'])/'scratch'
                if scratch.exists():docker('cp',scratch,container+':/bench-scratch/'+namespace)
        (out/'effective-prompt.txt').write_text(effective,encoding='utf-8')
        settings={'episode':namespace,'strategy':strategy,'baseline':baseline.tree,'started':started,'deadline':deadline,
                  'token_limit':args.token_limit,'max_rewinds':args.max_rewinds,'prompt':effective,
                  'paired':getattr(args,'paired',False),'paired_seed':seed}
        atomic(shared/'settings.json',settings);atomic(shared/'public-ledger.json',common_history)
        docker('exec',container,'mkdir','-p','/feedback-code','/bench-runtime','/bench-scratch')
        docker('cp',ROOT/'bench_tools/jiuwenswarm_runtime',container+':/feedback-code/feedback_runner')
        docker('cp',Path(__file__).with_name('feedback.py'),container+':/feedback-code/feedback_runner/feedback.py')
        stream=(out/'agent-process.log').open('w')
        process=subprocess.Popen(['docker','exec','-e','PYTHONPATH=/feedback-code','-e','JWS_EVAL_FEEDBACK=1',
            '-e','JWS_PAIRED='+str(int(settings['paired'])),
            '-e','JWS_FEEDBACK_STRATEGY='+strategy,'-e','JWS_FEEDBACK_ROOT=/shared/feedback/'+namespace,
            container,'/opt/jws_runtime/bin/python','-B','/feedback-code/feedback_runner/agent_entry.py',
            '/shared/feedback/'+namespace+'/settings.json'],stdout=stream,stderr=subprocess.STDOUT)
        print('START',case_id,strategy,episode,flush=True)
        while process.poll() is None:
            if time.time()>deadline+20:summary['state']='BUDGET_EXHAUSTED';break
            for request_path in sorted((shared/'requests').glob('*.json')):
                request=json.loads(request_path.read_text());request_id=request['request_id']
                if request_id!=request_path.stem:raise ValueError('Request filename mismatch')
                old=ledger.existing(request_id)
                if old:
                    if not (shared/'responses'/request_path.name).exists():atomic(shared/'responses'/request_path.name,old['feedback'])
                    continue
                number=len(ledger.entries)+1
                target=out/'submissions'/f'{number:03d}';target.mkdir(parents=True)
                capture=capture_git_patch(container,'/testbed',baseline_tree=baseline.tree,timeout=120)
                if capture.error:raise RuntimeError(capture.error)
                patch=capture.patch;patch_path=target/'patch.diff';patch_path.write_text(patch)
                (target/'git-status.txt').write_text(capture.status)
                sha=hashlib.sha256(patch.encode()).hexdigest()
                cache_key=ledger.cache_key(case_id,baseline.tree,patch,{'evaluator':'original','split':'test'})
                cached=ledger.cached(cache_key);invalid=rejected_paths(patch)
                if invalid:
                    feedback={'status':'invalid_patch','resolved':False,'failure_id':'delivery_pollution',
                        'message':'Drafts, logs or official test edits are present in /testbed. Move scratch work outside the repository and restore official tests.',
                        'paths':invalid}
                elif not patch.strip():
                    feedback={'status':'invalid_patch','resolved':False,'failure_id':'empty_patch',
                              'message':'No task repair changes were submitted. Inspect and repair the source before submitting.'}
                elif cached:feedback=cached['feedback'].copy()
                else:
                    feedback=None
                    for retry in range(2):
                        remaining=int(deadline-time.time())
                        if remaining<30:break
                        feedback=evaluate(patch_path,case_id,target/f'eval-{retry}',python=sys.executable,
                            script=ROOT/'bench_tools/swebench_runner/evaluate_patch.py',run_id=episode+f'-s{number}-r{retry}',
                            timeout=min(args.eval_timeout,max(1,remaining-10)))
                        if feedback['status']!='infra_failure':break
                    if feedback is None:feedback={'status':'infra_failure','resolved':False,'failure_id':None,'message':'Episode deadline reached before evaluation.'}
                after=capture_git_patch(container,'/testbed',baseline_tree=baseline.tree,timeout=120)
                if after.error or after.patch!=patch:
                    feedback={'status':'invalid_patch','resolved':False,'failure_id':'workspace_changed_during_eval',
                              'message':'Workspace changed during acceptance. Stop background writers and submit a stable patch.'}
                # Never inherit stale counters/terminal flags from cached feedback.
                feedback={k:v for k,v in feedback.items() if k not in {'terminal','submission_id','patch_sha256','same_failure_count','remaining_submissions'}}
                entry=ledger.record(request_id,cache_key,sha,feedback,cache_hit=bool(cached))
                atomic(target/'feedback.json',entry['feedback'])
                atomic(shared/'public-ledger.json',common_history+[e['feedback'] for e in ledger.entries])
                atomic(shared/'responses'/request_path.name,entry['feedback'])
                print('FEEDBACK',case_id,strategy,number,feedback['status'],feedback.get('failure_id'),flush=True)
                if entry['feedback'].get('resolved'):
                    shutil.copy2(patch_path,out/'patch.diff')
            time.sleep(.3)
        if process.poll() is not None:process.wait()
        if (shared/'episode-result.json').exists():
            docker('exec',container,'chmod','a+r','/shared/feedback/'+namespace+'/episode-result.json',check=False)
            summary.update(json.loads((shared/'episode-result.json').read_text()))
        summary['submissions']=len(ledger.entries)
        summary['physical_evaluations']=len(list(out.glob('submissions/*/eval-*')))
        summary['resolved']=bool(ledger.entries and ledger.entries[-1]['feedback'].get('resolved'))
        if not (out/'patch.diff').exists() and ledger.entries:
            shutil.copy2(out/'submissions'/f'{len(ledger.entries):03d}'/'patch.diff',out/'patch.diff')
        if strategy=='seed' and summary['state']=='SEED_READY':
            value=json.loads((shared/'paired-seed.json').read_text())
            current=capture_git_patch(container,'/testbed',baseline_tree=baseline.tree,timeout=120)
            if current.error or hashlib.sha256(current.patch.encode()).hexdigest()!=value['feedback']['patch_sha256']:
                raise ValueError('Seed changed after failed submission')
            bundle=out/'seed-bundle';bundle.mkdir()
            docker('exec',container,'chmod','-R','a+rX','/shared/feedback/'+namespace+'/recovery')
            for name in ('traces','snapshot-store'):
                shutil.copytree(shared/'recovery'/name,bundle/name,symlinks=True,dirs_exist_ok=True)
            docker('cp',container+':/bench-scratch/'+namespace,bundle/'scratch')
            value.update(bundle=str(bundle),patch_file=str(out/'patch.diff'),baseline=baseline.tree,
                prompt=effective,history=[e['feedback'] for e in ledger.entries])
            atomic(out/'paired-seed.json',value);summary['seed_manifest']=str(out/'paired-seed.json')
    except Exception as error:
        import traceback
        (out/'host-error.log').write_text(traceback.format_exc())
        summary['error']=type(error).__name__
    finally:
        # Fence this exact container before collecting its output.
        if process is not None and process.poll() is None:
            if begun:docker('stop','-t','2',container,check=False,timeout=20)
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        if stream:stream.close()
        for path in (shared/'private').glob('*'):path.unlink(missing_ok=True)
        for name in ('agent-feedback-events.jsonl','episode-progress.json','episode-result.json','chunks.jsonl','manager-error.log','paired-first-input.json'):
            if (shared/name).exists():
                if begun:docker('exec',container,'chmod','a+r','/shared/feedback/'+namespace+'/'+name,check=False)
                try:shutil.copy2(shared/name,out/name)
                except OSError as error:summary['archive_error']=type(error).__name__
        if (shared/'recovery').exists():
            if begun:docker('exec',container,'chmod','-R','a+rX','/shared/feedback/'+namespace+'/recovery',check=False)
            try:
                shutil.copytree(shared/'recovery',out/'recovery',dirs_exist_ok=True,symlinks=True,
                    ignore=shutil.ignore_patterns('snapshot-store','config','application_plugins','runtime-logs','logs','*.db','*.db-*'))
            except OSError as error:summary['archive_error']=type(error).__name__
        if begun:docker('rm','-f',container,check=False)
        summary['finished']=time.time();atomic(out/'summary.json',summary)
        print('END',case_id,strategy,summary['state'],summary.get('resolved',False),flush=True)
    return summary


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',required=True);p.add_argument('--runtime-source',required=True)
    p.add_argument('--env-file',required=True);p.add_argument('--output-root',required=True)
    p.add_argument('--cases-file',required=True);p.add_argument('--select',required=True)
    p.add_argument('--dataset',required=True)
    p.add_argument('--backend-root',help='Optional legacy BenchRunner driver mounts')
    p.add_argument('--strategies',default='continue,restart,rewind')
    p.add_argument('--workers',type=int,default=1);p.add_argument('--max-submissions',type=int,default=10)
    p.add_argument('--token-limit',type=int,default=500000);p.add_argument('--timeout',type=int,default=3600)
    p.add_argument('--eval-timeout',type=int,default=900);p.add_argument('--max-rewinds',type=int,default=10)
    p.add_argument('--paper-budget',action='store_true',
                   help='Use effectively unlimited episode resources; stop on success or five identical failures.')
    args=apply_budget_profile(p.parse_args())
    allowed=set(Path(args.cases_file).read_text().splitlines());selected=args.select.split(',')
    if not set(selected)<=allowed:raise ValueError('Selected case outside requested resume list')
    strategies=args.strategies.split(',')
    if not set(strategies)<={'continue','restart','rewind'}:raise ValueError('Unknown strategy')
    for name in ('workers','max_submissions','token_limit','timeout','eval_timeout','max_rewinds'):
        if getattr(args,name)<=0:raise ValueError('Budget must be positive: '+name)
    Path(args.output_root).mkdir(parents=True,exist_ok=True)
    layout=setup(args)
    shutil.copytree(Path(__file__).parent,Path(args.output_root)/'implementation-source',
                    ignore=shutil.ignore_patterns('__pycache__'),symlinks=True,dirs_exist_ok=True)
    shutil.copy2(layout.runtime/'feedback-overlay.json',Path(args.output_root)/'runtime-overlay.json')
    atomic(Path(args.output_root)/'experiment.json',{**vars(args),'baseline_commit':'f62bc489',
        'budget_profile':'paper_termination' if args.paper_budget else 'bounded',
        'pilot':True,'independent_repetitions':1,'feedback':'first_observed_official_failure','scratch':'/bench-scratch',
        'validation_gate':False,'evaluator_unchanged':True})
    results=[]
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        tasks={executor.submit(run_episode,args,layout,case,strategy):(case,strategy)
               for case in selected for strategy in strategies}
        for future in as_completed(tasks):
            try:result=future.result()
            except Exception as error:
                case,strategy=tasks[future]
                result={'case':case,'strategy':strategy,'state':'HOST_FAILURE','resolved':False,
                        'error':type(error).__name__}
            results.append(result);atomic(Path(args.output_root)/'summary.json',results)

if __name__=='__main__':main()
