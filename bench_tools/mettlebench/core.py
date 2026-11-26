"""Full-tree capture and original-verifier bridge, isolated from SWE patch scoring."""
import hashlib,json,os,shutil,stat,subprocess,tarfile
from pathlib import Path
IMAGE=os.environ.get('JWS_METTLE_IMAGE','jws-mettle:20261123-resume-fix')

def git_trust_environment(workspace):
    """Trust only repositories physically inside this disposable evaluation copy."""
    root=Path(workspace).resolve();repos=[]
    for base,dirs,files in os.walk(root,followlinks=False):
        if '.git' not in dirs and '.git' not in files:continue
        parent=Path(base)
        if not (parent/'.git').is_symlink():
            repos.append('/testbed'+('/'+parent.relative_to(root).as_posix() if parent!=root else ''))
        dirs[:]=[d for d in dirs if d!='.git']
    args=['-e',f'GIT_CONFIG_COUNT={len(repos)}']
    for i,repo in enumerate(sorted(repos)):
        args+=['-e',f'GIT_CONFIG_KEY_{i}=safe.directory','-e',f'GIT_CONFIG_VALUE_{i}={repo}']
    return args

def capture_scratch(container,dest):
    """Archive recovery inputs outside the delivery tree; never submit scratch."""
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    archive=dest/'scratch.tar';pending=dest/'scratch.tar.pending'
    with pending.open('wb') as f:
        subprocess.run(['docker','cp',container+':/bench-scratch/.','-'],stdout=f,check=True)
    with tarfile.open(pending) as tf:
        members=tf.getmembers()
        if any(Path(m.name).is_absolute() or '..' in Path(m.name).parts for m in members):
            raise ValueError('Unsafe scratch archive member')
    pending.replace(archive)
    h=hashlib.sha256()
    with archive.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    manifest={'version':1,'container_path':'/bench-scratch','archive':'scratch.tar',
              'sha256':h.hexdigest(),'members':len(members),'submitted_to_evaluator':False,
              'scope':'task scratch only; not a complete container image or process snapshot'}
    (dest/'scratch-manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest
def docker(*args,timeout=300,check=True):
    p=subprocess.run(['docker',*map(str,args)],capture_output=True,text=True,timeout=timeout)
    if check and p.returncode:raise RuntimeError(p.stderr[-2500:])
    return p
def tree_hash(root):
    root=Path(root);h=hashlib.sha256()
    for base,dirs,files in os.walk(root,followlinks=False):
        dirs.sort()
        for name in sorted(dirs+files):
            p=Path(base)/name;mode=p.lstat().st_mode
            h.update(json.dumps([str(p.relative_to(root)),stat.S_IMODE(mode),stat.S_IFMT(mode)]).encode())
            if p.is_symlink():h.update(os.readlink(p).encode())
            elif p.is_file():
                with p.open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def capture(container,dest):
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    archive=dest/'workspace.tar'
    with archive.open('wb') as f:subprocess.run(['docker','cp',container+':/testbed/.','-'],stdout=f,check=True)
    workspace=dest/'workspace';workspace.mkdir()
    with tarfile.open(archive) as tf:tf.extractall(workspace,filter='tar')
    return workspace,tree_hash(workspace)
def restore(container,source):
    # Exact named disposable directory. Never operate on the host workspace.
    docker('exec',container,'/usr/local/bin/python','-c',
        "from pathlib import Path; import shutil; p=Path('/testbed'); assert p.resolve()==p; shutil.rmtree(p); p.mkdir()")
    docker('cp',str(Path(source))+ '/.',container+':/testbed')
def normalize(report):
    if not isinstance(report,dict) or report.get('status') not in ('pass','fail'):
        return {'status':'infra_failure','resolved':False,'failure_id':None,'message':'Official verifier produced no valid verdict.'}
    if report['status']=='pass' and report.get('pass_rate')==1:
        return {'status':'passed','resolved':True,'failure_id':None,'message':'All official acceptance requirements passed.'}
    first=report.get('first_failure') or {}
    feedback=first.get('feedback')
    if not isinstance(feedback,str) or not feedback:
        return {'status':'infra_failure','resolved':False,'failure_id':None,'message':'Official fail report lacked first_failure.feedback.'}
    # All detailed checklist results remain host-side. Only this field is exposed.
    fid=str(first.get('id') or first.get('criterion_id') or hashlib.sha256(feedback.encode()).hexdigest())
    diagnostic=str(first.get('log_excerpt') or '').strip()
    return {'status':'product_failure','resolved':False,'failure_id':fid,
            'message':feedback+('\nFirst failed criterion diagnostic:\n'+diagnostic if diagnostic else ''),
            'diagnostic':diagnostic}
def evaluate(repo,task_id,workspace,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    evaluation_workspace=output/'workspace'
    shutil.copytree(workspace,evaluation_workspace,symlinks=True)
    result='/results/result.json'
    p=docker('run','--rm','--network','none','--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
        *git_trust_environment(evaluation_workspace),
        '-v',str(Path(repo)/'tasks')+':/verifiers:ro','-v',str(evaluation_workspace)+':/testbed:rw',
        '-v',str(output)+':/results:rw','-w','/testbed',IMAGE,'/usr/local/bin/python',
        '/verifiers/'+task_id+'/verifier/verifier.py','--workspace','/testbed','--output',result,timeout=600,check=False)
    (output/'process.log').write_text(p.stdout+'\n'+p.stderr)
    report=json.loads((output/'result.json').read_text()) if (output/'result.json').exists() else None
    return normalize(report),report
