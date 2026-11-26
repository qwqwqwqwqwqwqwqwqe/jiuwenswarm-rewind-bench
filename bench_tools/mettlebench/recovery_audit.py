"""Evidence-based recovery classification; never infer a hidden model intention."""
import json,gzip
from pathlib import Path

LABELS={'no_failure':'无官方失败','failed_continue':'失败但原地修','query_failed':'查询失败',
 'query_without_commit':'查询后未提交','commit_failed':'已请求提交但未完成恢复',
 'recovered':'实际完成回退','no_feedback':'没有官方反馈，原因不确定','incomplete':'尚未结束','environment_unavailable':'评测环境缺少命令'}

def analyze(episode):
    episode=Path(episode);summary=episode/'summary.json'
    if not summary.exists():return {'reason':'incomplete','label':LABELS['incomplete']}
    result=json.loads(summary.read_text());seen=set();queries=[];commits=set();tool_counts={}
    for p in sorted((episode/'ipc/recovery/traces').glob('*.jsonl')):
        if '.backtrack_' in p.name:continue
        with p.open() as stream:
            for line in stream:
                try:r=json.loads(line)
                except ValueError:continue
                if r.get('kind')=='llm':
                    for call in (r.get('output') or {}).get('tool_calls') or []:
                        if call.get('function',{}).get('name')=='backtrack_commit':commits.add(call.get('id'))
                if r.get('kind')!='tool':continue
                inp=r.get('input') or {};ident=inp.get('id') or r.get('record_uid')
                if ident in seen:continue
                seen.add(ident);name=inp.get('name');tool_counts[name]=tool_counts.get(name,0)+1
                if name=='backtrack_commit':commits.add(ident)
                if name=='backtrack_candidates':
                    value=(r.get('output') or {}).get('value') or {}
                    ok=value.get('ok') is True
                    failure=value.get('failure') or {}
                    queries.append({'record':r.get('record_uid'),'success':ok,
                        'error':failure.get('message') if not ok else None,'trace':str(p)})
    feedback=[];environment=[]
    for p in sorted((episode/'submissions').glob('*/feedback.json')):
        v=json.loads(p.read_text());feedback.append(v)
        report=p.parent/'eval/result.json'
        if report.exists():
            f=json.loads(report.read_text()).get('first_failure') or {}
            excerpt=str(f.get('log_excerpt',''))
            if 'not found' in excerpt or 'No module named' in excerpt:
                environment.append({'submission':p.parent.name,'criterion':f.get('id'),'log_excerpt':excerpt})
    failures=[f for f in feedback if f.get('status')=='product_failure']
    stage=None
    for p in (episode/'ipc/manager-error.log',episode/'ipc/manager-error.log.gz'):
        if p.exists():
            text=gzip.open(p,'rt').read() if p.suffix=='.gz' else p.read_text()
            if 'Traceback' in text and ('prepare_recovery' in text or 'restore_commit' in text):stage='restore_failed'
    successes=sum(q['success'] for q in queries);actual=result.get('rewinds',0)
    if actual:reason='recovered'
    elif queries and not successes:reason='query_failed'
    elif commits:reason='commit_failed'
    elif successes:reason='query_without_commit'
    elif queries:reason='query_failed'
    elif any(e['log_excerpt'].startswith('sphinx-build not found:') for e in environment):reason='environment_unavailable'
    elif failures:reason='failed_continue'
    elif feedback:reason='no_failure'
    else:reason='no_feedback'
    return {'audit_schema_version':4,'task':result['task_id'],'strategy':result['strategy'],'state':result['state'],
        'reason':reason,'label':LABELS[reason],'classification_basis':'observable tool and official feedback records; not model self-report',
        'actual_rewinds':actual,'commit_requests':len(commits),'query_count':len(queries),
        'commit_failure_stage':stage or ('commit_not_completed' if commits and not actual else None),
        'query_successes':successes,'queries':queries,'official_failures':len(failures),
        'failure_ids':[f.get('failure_id') for f in failures],'tool_counts':tool_counts,
        'environment_findings':environment,'summary':str(summary)}

def write(episode):
    value=analyze(episode);p=Path(episode)/'recovery-audit.json'
    tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(p)
    return value
