"""Deterministic, limited evaluator observations. Never change the verdict."""
from __future__ import annotations
import re


def excerpt(block: str) -> str:
    lines=block.strip().splitlines()
    if len(lines)>36:lines=lines[:8]+['[middle traceback frames omitted]']+lines[-24:]
    text='\n'.join(lines)
    if len(text)>3500:text=text[:1000]+'\n[middle omitted]\n'+text[-2400:]
    return text


def normalize(report: dict | None, output: str, *, case_id: str, exit_code: int) -> dict:
    output=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',output)
    value = (report or {}).get(case_id)
    if not isinstance(value, dict) or type(value.get('resolved')) is not bool:
        # A patch application error is an invalid submission, not infrastructure.
        if re.search(r'patch (?:failed|does not apply)|PATCH_APPLY_FAIL', output, re.I):
            return {'status':'invalid_patch','resolved':False,'failure_id':'patch_application',
                    'message':'The submitted patch could not be applied to the task baseline.'}
        return {'status':'infra_failure','resolved':False,'failure_id':None,
                'message':'Evaluator did not produce a valid instance verdict. Host diagnostics retained.'}
    if value['resolved']:
        return {'status':'passed','resolved':True,'failure_id':None,'message':'All official acceptance requirements passed.'}
    # Use traceback sections, not the ambiguous interleaved progress lines.
    failure = re.search(r'^(FAIL|ERROR): (.+)$', output, re.M)
    if failure:
        end = re.search(r'^={10,}|^-{10,}\s*\n(?:Ran|FAIL:|ERROR:)', output[failure.end():], re.M)
        stop = failure.end()+end.start() if end else len(output)
        return {'status':'product_failure','resolved':False,'failure_id':failure.group(2).strip(),
                'message':'Official acceptance failed; first observed failure follows.',
                'log_excerpt':excerpt(output[failure.start():stop])}
    # SymPy's native runner and pytest use underscore-delimited failure titles,
    # not unittest's FAIL:/ERROR: labels. Preserve the terminal exception too.
    failure=re.search(r'^_{3,}\s+([^\n]*?test_[^\n]*?)\s+_{3,}\s*$',output,re.M)
    if failure:
        title=failure.group(1).strip()
        end=re.search(r'^_{3,}|^=+|^\s*tests finished:|^>>>>>',output[failure.end():],re.M)
        stop=failure.end()+end.start() if end else len(output)
        identifier=re.search(r'(?:::|:)(test_[^\s]+)$',title)
        return {'status':'product_failure','resolved':False,
                'failure_id':identifier.group(1) if identifier else title,
                'message':'Official acceptance failed; first native test failure follows.',
                'log_excerpt':excerpt(output[failure.start():stop])}
    failure=re.search(r'^FAILED\s+(\S+)\s+-\s+(.+)$',output,re.M)
    if failure:
        return {'status':'product_failure','resolved':False,'failure_id':failure.group(1),
                'message':'Official acceptance failed.', 'log_excerpt':failure.group(0)[:3500]}
    status=value.get('tests_status',{})
    failures=[test for group in status.values() if isinstance(group,dict) for test in group.get('failure',[])]
    return {'status':'product_failure','resolved':False,
            'failure_id': sorted(failures)[0] if failures else 'unresolved_without_traceback',
            'message':'Official report is unresolved. No unambiguous FAIL/ERROR traceback was available.',
            'feedback_uncertain':True}


def rejected_paths(patch: str) -> list[str]:
    """Reject identifiable draft/test pollution; do not silently filter a patch."""
    paths=re.findall(r'^\+\+\+ b/(.+)$',patch,re.M)
    bad=[]
    for path in paths:
        parts=path.lower().split('/')
        base=parts[-1]
        if (any(p in {'tests','test','__pycache__','.pytest_cache','.jiuwenswarm'} for p in parts)
            or base.startswith(('repro','probe','scratch','test_'))
            or base in {'deployment_service.log','coverage.xml','.coverage','feedback.json'}
            or base.endswith(('.log','.pyc'))):bad.append(path)
    return bad
