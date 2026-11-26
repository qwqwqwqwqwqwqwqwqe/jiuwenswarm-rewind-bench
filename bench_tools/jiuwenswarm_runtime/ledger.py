"""Host-owned append-only logical submissions and evaluator cache."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time


def atomic(path: Path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)


class Ledger:
    def __init__(self, directory, *, max_submissions=10, repeated_limit=5, submission_offset=0):
        self.root=Path(directory);self.root.mkdir(parents=True,exist_ok=True)
        self.maximum=max_submissions;self.repeated_limit=repeated_limit
        self.submission_offset=submission_offset
        self.entries=[]
        if (self.root/'ledger.json').exists():self.entries=json.loads((self.root/'ledger.json').read_text())

    def existing(self, request_id):
        return next((e for e in self.entries if e['request_id']==request_id),None)

    def cache_key(self, case_id, baseline, patch, evaluator):
        return hashlib.sha256(json.dumps([case_id,baseline,patch,evaluator],sort_keys=True).encode()).hexdigest()

    def cached(self,key):
        return next((e for e in reversed(self.entries) if e['cache_key']==key
                     and e['feedback']['status'] in {'passed','product_failure','invalid_patch'}),None)

    def record(self, request_id, key, patch_sha, feedback, *, cache_hit=False):
        old=self.existing(request_id)
        if old:return old
        failures=[e for e in self.entries if e['feedback']['status'] in {'product_failure','invalid_patch'}]
        same=0
        if feedback['status'] in {'product_failure','invalid_patch'}:
            same=1
            for entry in reversed(failures):
                if entry['feedback']['failure_id']!=feedback['failure_id']:break
                same+=1
        count=len(self.entries)+1
        feedback={**feedback,'submission_id':count+self.submission_offset,'patch_sha256':patch_sha,
                  'same_failure_count':same,'remaining_submissions':max(0,self.maximum-count)}
        if feedback['status']=='infra_failure': feedback['terminal']='infra_failure'
        elif feedback.get('resolved'):feedback['terminal']='success'
        elif same>=self.repeated_limit:feedback['terminal']='stagnated'
        elif count>=self.maximum:feedback['terminal']='submission_budget'
        entry={'request_id':request_id,'cache_key':key,'timestamp':time.time(),
               'cache_hit':cache_hit,'patch_sha256':patch_sha,'feedback':feedback}
        self.entries.append(entry);atomic(self.root/'ledger.json',self.entries)
        return entry
