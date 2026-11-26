"""Fetch public MettleBench artifacts; no reference solutions are downloaded."""
import argparse,hashlib,io,json,random,tarfile,urllib.request,subprocess,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'vendor/MettleBench'
def fetch(url):
    with urllib.request.urlopen(url,timeout=180) as response:
        data=response.read(209715201)
    if len(data)>209715200:
        raise ValueError('Public artifact exceeds 200 MiB')
    return data
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--task',action='append',help='Download an additional named workspace')
    args=parser.parse_args()
    DEST.mkdir(parents=True,exist_ok=True)
    if not (DEST/'manifest.jsonl').exists():
        cached=ROOT/'tmp/mettle-source.tar.gz'
        blob=cached.read_bytes() if cached.exists() else fetch('https://codeload.github.com/Kelvin-Coffee/MettleBench/tar.gz/refs/heads/main')
        archive=tarfile.open(fileobj=io.BytesIO(blob))
        prefix=archive.getmembers()[0].name.split('/')[0]+'/'
        for member in archive:
            if not member.name.startswith(prefix):continue
            member.name=member.name[len(prefix):]
            if member.name:archive.extract(member,DEST,filter='data')
        (DEST/'source-lock.json').write_text(json.dumps({'branch':'main','archive_sha256':hashlib.sha256(blob).hexdigest()},indent=2))
    rows=[]
    for line in (DEST/'manifest.jsonl').read_text().splitlines():
        row=json.loads(line);task=DEST/row['task_dir'];checks=json.loads((task/'verifier/verifier_checklist.json').read_text())
        if isinstance(checks,dict):
            items=next((v for v in checks.values() if isinstance(v,list)),[])
        else:items=checks
        prompt=(task/'prompt.md').read_text();code=(task/'verifier/verifier.py').read_text()
        rows.append({**row,'criteria_count':len(items),'prompt_chars':len(prompt),
                     'verifier_check_functions':code.count('\ndef check_')})
    rows.sort(key=lambda r:(r['criteria_count'],r['verifier_check_functions'],r['prompt_chars'],r['task_id']))
    rng=random.Random(20261117);selected=[]
    for k,label in enumerate(('lower','middle','upper')):
        group=rows[len(rows)*k//3:len(rows)*(k+1)//3]
        for r in group:r['structural_stratum']=label
        chosen=rng.choice(group);selected.append(chosen)
    plan={'method':'structural proxy, not measured model difficulty','seed':20261117,
          'ranking':['criteria_count','verifier_check_functions','prompt_chars','task_id'],
          'source_lock':json.loads((DEST/'source-lock.json').read_text()),'population':rows,'selected':selected}
    out=ROOT/'local/mettlebench';out.mkdir(parents=True,exist_ok=True)
    (out/'sampling.json').write_text(json.dumps(plan,indent=2))
    downloads=selected if not args.task else [row for row in rows if row['task_id'] in args.task]
    if args.task and set(args.task)!={row['task_id'] for row in downloads}:raise ValueError('Unknown MettleBench task')
    for row in downloads:
        task=DEST/row['task_dir'];name=row['task_id'];target=task/'workspace'
        if not target.exists():
            cached=ROOT/'tmp'/(name+'.tar.gz')
            blob=cached.read_bytes() if cached.exists() else fetch('https://huggingface.co/datasets/KelvinCoffee/MettleBench/resolve/main/workspaces/'+name+'.tar.gz')
            tarfile.open(fileobj=io.BytesIO(blob)).extractall(task,filter='tar')
            row['workspace_tar_sha256']=hashlib.sha256(blob).hexdigest()
        print(json.dumps(row),flush=True)
    (out/'sampling.json').write_text(json.dumps(plan,indent=2))
if __name__=='__main__':main()
