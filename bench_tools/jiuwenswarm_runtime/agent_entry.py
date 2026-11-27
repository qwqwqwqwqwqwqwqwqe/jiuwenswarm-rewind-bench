"""In-container episode manager using the bundled Runtime attempt/IPC protocol."""
from __future__ import annotations
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace


def classify_attempt_error(error,deadline,now=None):
    if isinstance(error,TimeoutError) and (time.time() if now is None else now)>=deadline:
        return {'state':'BUDGET_EXHAUSTED','budget_cause':'deadline'}
    return {'state':'FAILED','error':type(error).__name__}


def retire_scratch(scratch,episode_id):
    """Old literal draft paths must not silently expose abandoned suffix state."""
    if not scratch.exists():return
    archive=Path('/bench-runtime')/episode_id/'retired-scratch'/scratch.name
    archive.parent.mkdir(parents=True,exist_ok=True)
    scratch.rename(archive)


async def episode(settings):
    root=Path(os.environ['JWS_FEEDBACK_ROOT'])
    state=root/'recovery';state.mkdir(exist_ok=True)
    private=root/'private/model.json'
    credentials=json.loads(private.read_text())
    import yaml
    import jiuwenswarm
    from jiuwenswarm.rewind.bench_config import SCHEMA,BenchOptions,single_agent_config,wire_value
    from jiuwenswarm.rewind.storage import atomic_json,UsageJournal
    from jiuwenswarm.rewind.chat import messages
    from jiuwenswarm.rewind.worker import prepare_recovery
    from jiuwenswarm.common.schema.agent import AgentRequest,ReqMethod
    config=yaml.safe_load((Path(jiuwenswarm.__file__).parent/'resources/config.yaml').read_text())
    for entry in config['models']['defaults']:
        entry['model_client_config'].update(api_base=credentials['base_url'],api_key=credentials['api_key'],
            model_name=credentials['model_name'],client_provider='OpenAI',supports_vision=False)
        entry['model_client_config']['api_mode']='chat_completions'
    config=single_agent_config(config)
    config_file=root/'private/config.json';atomic_json(config_file,config);config_file.chmod(0o600)
    # Imports above do not instantiate a model. All child processes use private config snapshots.
    strategy=settings['strategy'];deadline=settings['deadline']
    keep_single_scratch=settings.get('paper_recovery') and strategy!='restart'
    workspace=Path(settings.get('workspace','/testbed')).resolve()
    paired=settings.get('paired',False);seed=settings.get('paired_seed')
    bootstrap=bool(seed and strategy!='restart')
    options=BenchOptions(mode='on' if strategy=='rewind' or paired else 'record-only',token_limit=settings['token_limit'],
                         remaining_seconds=max(1,deadline-time.time()),max_rewinds=settings['max_rewinds']+int(bootstrap),
                         model_max_retries=6,model_retry_initial_delay_seconds=3,model_retry_max_delay_seconds=30)
    query=settings['prompt']
    recovery=None;attempt=0;rewinds=0;restarts=0
    if seed:
        history=json.loads((root/'public-ledger.json').read_text())
        if bootstrap:
            from feedback_runner.paired_support import prepare_bootstrap
            recovery=prepare_bootstrap(settings,state);attempt=1
        else:query+='\n\nExperiences from previous rejected submissions:\n'+json.dumps(history,ensure_ascii=False)
    report={'state':'FAILED','strategy':strategy,'attempts':[],'rewinds':0,'restarts':0}
    try:
        while time.time()<deadline:
            scratch=Path('/bench-scratch')/settings['episode']/f'attempt-{attempt-int(bootstrap)}'
            if keep_single_scratch:
                scratch=Path('/bench-scratch')/settings['episode']/'attempt-0'
            scratch.mkdir(parents=True,exist_ok=bool(keep_single_scratch or (bootstrap and attempt==1)))
            os.environ.update(JWS_SCRATCH_DIR=str(scratch),TMPDIR=str(scratch),
                              PYTHONDONTWRITEBYTECODE='1',PYTEST_ADDOPTS='-p no:cacheprovider')
            request=AgentRequest(request_id=settings['episode'],session_id=settings['episode'],channel_id='tui',
                req_method=ReqMethod.CHAT_SEND,params={'query':query,'mode':'code.normal',
                'workspace_dir':str(workspace),'cwd':str(workspace),'project_dir':str(workspace),
                'trusted_dirs':[str(workspace),'/bench-scratch'],'supports_user_interaction':False,
                'supports_permission_approval':False})
            job={'schema':SCHEMA,'task_id':settings['episode'],'attempt':attempt,
                 'run_id':settings['episode']+f'-a{attempt}','mode':options.mode,'options':options.to_dict(),
                 'request':wire_value(request),'state_dir':str(state),'attempt_dir':str(state/f'attempt-{attempt}'),
                 'data_dir':str(state/'active-data'),'config_file':str(config_file),'workspace':str(workspace),
                 'logical_timestamp':settings['started'],'deadline':deadline,'max_output_tokens':16384,'recovery':recovery}
            if strategy=='rewind' and settings.get('candidate_policy')=='semantic':
                job['candidate_policy']='semantic'
            if bootstrap:job['request']=seed['request']
            if seed:job['logical_timestamp']=seed['logical_timestamp']
            final=None
            from contextlib import aclosing
            async with aclosing(messages(job)) as source:
                async for packet in source:
                    if packet['kind']=='result':final=packet['value']
                    else:
                        with (root/'chunks.jsonl').open('a') as f:f.write(json.dumps(packet,ensure_ascii=False)+'\n')
            if final is None:raise RuntimeError('Missing attempt result')
            report['attempts'].append(final);atomic_json(state/f'attempt-{attempt}'/'result.json',final)
            if final['state']!='CONTROL':raise RuntimeError('Attempt exited without evaluator terminal control')
            control=final['control'];kind=control['kind'];payload=control['payload']
            if kind=='paired_seed' and strategy=='seed':
                report.update(state='SEED_READY',paired_seed=payload);break
            if kind=='feedback_terminal':
                report.update(state='RESOLVED' if payload.get('resolved') else 'UNRESOLVED',terminal=payload)
                break
            if kind=='budget_exhausted':report.update(state='BUDGET_EXHAUSTED',terminal=payload);break
            if kind=='feedback_restart' and strategy=='restart':
                # Only this disposable task container is restored, never the host repository.
                if settings.get('benchmark')=='mettlebench':
                    # The pristine task is host-owned and never exposed to the agent.
                    import uuid
                    nonce=uuid.uuid4().hex
                    # Publish only after permissions are ready: chmod after
                    # publication left a race where the host saw root:0600.
                    ready=root/('restore-request-'+nonce+'.pending')
                    atomic_json(ready,{'nonce':nonce})
                    ready.chmod(0o644)
                    ready.replace(root/'restore-request.json')
                    while True:
                        response=root/'restore-response.json'
                        if response.exists() and json.loads(response.read_text()).get('nonce')==nonce:break
                        if time.time()>deadline:raise TimeoutError('Host restart restore timeout')
                        await asyncio.sleep(.2)
                else:
                    if not workspace.is_absolute() or not (workspace/'.git').exists():raise ValueError('Invalid disposable workspace')
                    subprocess.run(['git','-C',str(workspace),'read-tree','--reset','-u',settings['baseline']],check=True,capture_output=True)
                    subprocess.run(['git','-C',str(workspace),'clean','-fd'],check=True,capture_output=True)
                history=json.loads((root/'public-ledger.json').read_text())
                query=settings['prompt']+'\n\nExperiences from previous rejected submissions:\n'+json.dumps(history,ensure_ascii=False)
                restarts+=1;recovery=None
                # A fresh context must not inherit a stale submission-gate arm.
                from jiuwenswarm.rewind.eval_feedback import reset_read_counters
                reset_read_counters()
            elif kind=='rewind' and strategy=='rewind':
                if rewinds>=settings['max_rewinds']:report.update(state='REWIND_BUDGET_EXHAUSTED');break
                history=json.loads((root/'public-ledger.json').read_text())
                payload=copy.deepcopy(payload)
                # Keep the same operational memory bound as paper tool schema. Official
                # evidence is no longer appended to the memory item: v6 truncated it
                # away under the old render cap and cumulative items repeated it.
                from feedback_runner.recovery_inject import apply_recovery_injection,cap_notes,evidence_text
                notes=cap_notes(str(payload.get('memory_summary') or '').strip())
                payload['memory_summary']=notes
                prepared=prepare_recovery(SimpleNamespace(kind=kind,payload=payload),job,state)
                recovery=prepared['recovery'];rewinds+=1
                # The restored context no longer holds the abandoned branch's file
                # contents, so repeated-read counters must start over.
                from jiuwenswarm.rewind.eval_feedback import reset_read_counters
                reset_read_counters()
                # Single notebook copy, fresh evidence message, operative plan header.
                path=Path(recovery['override_input_file'])
                override=json.loads(path.read_text(encoding='utf-8'))
                apply_recovery_injection(override,evidence_text(history[-1:]),notes)
                path.write_text(json.dumps(override,ensure_ascii=False),encoding='utf-8')
                if paired:
                    from feedback_runner.paired_support import augment_recovery
                    augment_recovery(recovery)
            else:raise ValueError('Unexpected execution control: '+kind)
            # messages() fenced all old tools before this branch transition.
            if not keep_single_scratch:
                retire_scratch(scratch,settings['episode'])
            attempt+=1
            report.update(rewinds=rewinds,restarts=restarts)
            atomic_json(root/'episode-progress.json',report)
            (root/'episode-progress.json').chmod(0o644)
        else:report['state']='BUDGET_EXHAUSTED'
    except Exception as error:
        import traceback
        (root/'manager-error.log').write_text(traceback.format_exc())
        report.update(classify_attempt_error(error,deadline))
    finally:
        report.update(rewinds=rewinds,restarts=restarts,
            usage=UsageJournal(state/'usage.jsonl',token_limit=options.token_limit,deadline=deadline,
                               accounting='uncached_usage').totals())
        atomic_json(root/'episode-result.json',report)
        (root/'episode-result.json').chmod(0o644)
        config_file.unlink(missing_ok=True);private.unlink(missing_ok=True)
    return report


if __name__=='__main__':
    settings=json.loads(Path(sys.argv[1]).read_text())
    root=Path(os.environ['JWS_FEEDBACK_ROOT'])
    os.environ.update(JIUWENSWARM_DATA_DIR='/bench-runtime/'+settings['episode'],MEMORY_ENGINE='none',
                      OPENJIUWEN_RUNTIME_LOG_DIR='/bench-runtime/'+settings['episode']+'/logs')
    result=asyncio.run(episode(settings))
    print(json.dumps({'state':result['state'],'rewinds':result['rewinds'],'restarts':result['restarts']}),flush=True)
