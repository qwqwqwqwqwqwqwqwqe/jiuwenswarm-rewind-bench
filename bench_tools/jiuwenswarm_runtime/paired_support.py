"""Exact-prefix bootstrap for paired interventions; never modifies the evaluator."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

RECOVERY_NAMES={'backtrack_candidates','backtrack_commit'}

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def unpack(value):
    from jiuwenswarm.rewind.codec import decode
    return decode(json.loads(value) if isinstance(value,str) else value)

def paired_request(boundaries,request):
    """Keep original schemas at every strict replay boundary, including mixed forks."""
    request=dict(request)
    if boundaries._prefix_records and not boundaries._fork_live:
        record=boundaries._prefix_records[boundaries._prefix_position]
        if record['kind']!='llm':raise ValueError('Paired replay expected a model boundary')
        original=boundaries.session.workspace_manager.denormalize_paths(record['input'])
        request['tools']=unpack(original['jiuwen_request']).get('tools',[])
    elif os.environ['JWS_FEEDBACK_STRATEGY']!='rewind':
        request['tools']=[t for t in request.get('tools',[]) if tool_name(t) not in RECOVERY_NAMES]
    return request

def tool_name(tool):
    if isinstance(tool,dict):return tool.get('name',tool.get('function',{}).get('name'))
    return getattr(tool,'name',None)

def add_recovery_tools(value):
    from jiuwenswarm.rewind.codec import encode
    from jiuwenswarm.rewind.native import recovery_tools
    from openjiuwen.core.foundation.tool.schema import ToolInfo
    request=unpack(value['jiuwen_request'])
    ordinary=[t for t in request.get('tools',[]) if tool_name(t) not in RECOVERY_NAMES]
    native=[ToolInfo(name=t.card.name,description=t.card.description,parameters=t.card.input_params)
            for t in recovery_tools(SimpleNamespace(code_recovery=False))]
    index=next((i for i,t in enumerate(ordinary) if tool_name(t)=='submit_patch'),len(ordinary))
    request['tools']=ordinary[:index]+native+ordinary[index:]
    value['jiuwen_request']=json.dumps(encode(request),ensure_ascii=False,sort_keys=True)
    return value

def prepare_bootstrap(settings,state):
    from replay.backtrack import BacktrackRequested,prepare_backtrack_replay
    seed=settings['paired_seed'];fork=settings['episode']+'-a1'
    request=BacktrackRequested(record_uid=seed['record_uid'],memory_summary='',reason='paired bootstrap')
    recovery=prepare_backtrack_replay(request,current_run_id=seed['run_id'],log_dir=state/'traces',fork_run=fork,
        memory_policy='none',context_policy='rollback',workspace_policy='rollback')
    if settings['strategy']=='rewind':augment_recovery(recovery)
    return {k:str(v) if isinstance(v,Path) else v for k,v in recovery.items()}

def augment_recovery(recovery):
    path=Path(recovery['override_input_file']);value=json.loads(path.read_text())
    path.write_text(json.dumps(add_recovery_tools(value),ensure_ascii=False,indent=2))

def observe_live(boundaries,effective):
    root=Path(os.environ['JWS_FEEDBACK_ROOT'])
    settings=json.loads((root/'settings.json').read_text())
    if not settings.get('paired_seed') or (root/'paired-first-input.json').exists():return
    from jiuwenswarm.rewind.eval_feedback import emit
    normalized=boundaries.session.workspace_manager.normalize_paths(effective)
    actual=digest(normalized['messages']);seed=settings['paired_seed']
    equal=actual==seed['context_sha256']
    if settings['strategy']!='restart' and not equal:raise ValueError('Paired initial context differs from seed')
    if settings.get('paper_recovery'):
        from mettle_runner.recovery_experiment import verify_and_steer
        verify_and_steer(root,settings,effective)
    tools=[tool_name(t) for t in unpack(effective['jiuwen_request']).get('tools',[])]
    audit={'context_sha256':actual,'seed_context_sha256':seed['context_sha256'],'context_equal':equal,
        'intentional_context_reset':settings['strategy']=='restart','tools':tools,'bootstrap_is_rewind':False}
    (root/'paired-first-input.json').write_text(json.dumps(audit,indent=2));(root/'paired-first-input.json').chmod(0o644)
    emit(root,'paired_first_repair_input',**audit)

def freeze_seed(root,job,feedback):
    from replay.storage import load_raw_records,run_path
    records=load_raw_records(run_path(Path(job['state_dir'])/'traces',job['run_id']))
    last=next(r for r in reversed(records) if r.get('kind')=='llm')
    if feedback['patch_sha256'] not in json.dumps(last['input']['messages']):
        raise ValueError('Seed checkpoint does not contain official failure feedback')
    value={'namespace':job['task_id'],'run_id':job['run_id'],'record_uid':last['record_uid'],
        'context_sha256':digest(last['input']['messages']),'request':job['request'],
        'logical_timestamp':job['logical_timestamp'],'feedback':copy.deepcopy(feedback)}
    path=root/'paired-seed.json';path.write_text(json.dumps(value,ensure_ascii=False,indent=2));path.chmod(0o644)
    return value
