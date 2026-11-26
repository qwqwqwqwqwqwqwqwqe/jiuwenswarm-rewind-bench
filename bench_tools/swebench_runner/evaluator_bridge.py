"""Host-only evaluator subprocess; official tests never enter the agent workspace."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
from .feedback import normalize


def evaluate(patch: Path, case_id: str, output: Path, *, python: str, script: Path, run_id: str, timeout: int):
    output.mkdir(parents=True,exist_ok=True)
    command=[python,str(script),'--patch',str(patch),'--instance-id',case_id,'--output-dir',str(output),
             '--run-id',run_id,'--max-workers','1','--timeout',str(timeout)]
    with (output/'bridge.log').open('w') as log:
        try:result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=timeout+180)
        except subprocess.TimeoutExpired:
            return {'status':'infra_failure','resolved':False,'failure_id':None,'message':'Evaluator timeout; host diagnostics retained.'}
    report=None;test_output=''
    for path in sorted(output.rglob('report.json')):
        data=json.loads(path.read_text())
        if case_id in data:
            report=data
            if (path.parent/'test_output.txt').exists():test_output=(path.parent/'test_output.txt').read_text(errors='replace')
            break
    if not test_output:test_output=(output/'bridge.log').read_text(errors='replace')
    return normalize(report,test_output,case_id=case_id,exit_code=result.returncode)
