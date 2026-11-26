"""Paper B.4 failed-Continue endpoint cloning, not a forced-rewind intervention."""
import hashlib,json,os,shutil,stat,sys,tarfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from .core import capture,capture_scratch,tree_hash
try:
    from bench_tools.jiuwenswarm_runtime.ledger import atomic
except ModuleNotFoundError:
    from feedback_runner.ledger import atomic

COMMON_NOTICE=('This Continue trajectory terminated after five consecutive official submissions '
    'failed the same acceptance criterion. You are resuming an exact copy of that failed '
    'context and task environment. The recovery trial has fresh failure counters. '
    'Diagnose which assumptions or actions caused the rejected branch, and change the '
    'approach instead of repeating the same repair loop. The following is the common '
    'observed failure record, not new acceptance results:\n')
REWIND_NOTICE=('You additionally have backtrack_candidates and backtrack_commit. You may '
    'return to an earlier decision in the retained failed Continue prefix, restoring its '
    'context and task workspace while carrying operational failure memory. Whether and '
    'where to rewind is your decision. Query candidates before committing. Scratch is '
    'outside the rewind boundary; verify any surviving scratch against the restored state.')

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024**2),b''):h.update(b)
    return h.hexdigest()

def archive_tree_hash(path):
    """Hash the extracted tree's paths, modes, links and bytes without extracting."""
    digest=hashlib.sha256()
    with tarfile.open(path) as archive:
        children=defaultdict(dict)
        for member in archive.getmembers():
            name=PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts:
                raise ValueError(f'Unsafe archive member: {member.name}')
            if name==PurePosixPath('.'):
                continue
            if name.name in children[name.parent]:
                raise ValueError(f'Duplicate archive member: {member.name}')
            children[name.parent][name.name]=member

        def visit(parent):
            directories=[]
            for basename,member in sorted(children[parent].items()):
                name=parent/basename
                if member.isdir():
                    kind=stat.S_IFDIR
                    directories.append(name)
                elif member.issym():kind=stat.S_IFLNK
                elif member.isfile() or member.islnk():kind=stat.S_IFREG
                else:raise ValueError(f'Unsupported archive member: {member.name}')
                mode=0o777 if member.issym() else member.mode & 0o777
                digest.update(json.dumps([str(name),mode,kind]).encode())
                if member.issym():digest.update(os.fsencode(member.linkname))
                elif kind==stat.S_IFREG:
                    with archive.extractfile(member) as stream:
                        for block in iter(lambda:stream.read(1024**2),b''):digest.update(block)
            for directory in directories:visit(directory)

        visit(PurePosixPath('.'))
    return digest.hexdigest()

def prepare_episode(settings,trial,shared):
    settings.update(paper_recovery=True,paired=True,token_limit=sys.maxsize,max_rewinds=sys.maxsize)
    seed=trial.get('seed')
    if not seed:return settings
    source=Path(trial['source']);endpoint=source/'endpoint'
    prior_failures=json.loads((endpoint/'failures.json').read_text())
    if settings['strategy']=='restart':
        if not prior_failures or prior_failures[-1]!=seed['feedback']:
            raise ValueError('Restart experience differs from the frozen failed endpoint')
        notice=('The shared Continue trajectory terminated after five consecutive official submissions '
            'failed the same acceptance criterion. Start over from the pristine task workspace with '
            'fresh failure counters. Identify which assumptions or actions led to the rejection and '
            'change the approach. This is the common observed failure record:\n')
        settings.update(paired=False,prior_failures=prior_failures,restart_from_endpoint=True)
        settings['prompt']+='\n\n'+notice+json.dumps(prior_failures,ensure_ascii=False)
        atomic(shared/'restart-origin.json',{
            'seed_run_id':seed['run_id'],'seed_record_uid':seed['record_uid'],
            'seed_context_sha256':seed['context_sha256'],
            'prior_failures_sha256':sha(endpoint/'failures.json'),
            'workspace':'pristine task workspace','failure_counter_at_start':0,
            'previous_submissions_in_recovery_ledger':0})
        return settings
    for part in ('traces','snapshot-store'):
        shutil.copytree(source/'ipc/recovery'/part,shared/'recovery'/part)
    notebook=source/'ipc/recovery/active-data/agent/workspace/memory'
    if notebook.exists():shutil.copytree(notebook,shared/'recovery/active-data/agent/workspace/memory')
    dest=shared/'endpoint';dest.mkdir()
    for name in ('workspace.tar','scratch.tar','manifest.json','failures.json'):
        shutil.copyfile(endpoint/name,dest/name)
    settings['paired_seed']=seed
    settings['endpoint_manifest']=json.loads((endpoint/'manifest.json').read_text())
    settings['prior_failures']=prior_failures
    return settings

def capture_endpoint(container,out,shared,entries):
    endpoint=out/'endpoint'
    # The attempt has exited and its tool scope has been fenced.
    capture(container,endpoint)
    capture_scratch(container,endpoint)
    return finalize_endpoint(out,shared,entries)

def finalize_endpoint(out,shared,entries):
    """Finalize saved endpoint archives, including an interrupted capture."""
    if not entries or entries[-1]['feedback'].get('terminal')!='stagnated':
        raise ValueError('Only a naturally stagnated Continue is eligible')
    seed=json.loads((shared/'paired-seed.json').read_text())
    endpoint=out/'endpoint'
    feedback=[e['feedback'] for e in entries]
    if seed['feedback']!=feedback[-1] or feedback[-1].get('same_failure_count')!=5:
        raise ValueError('Frozen checkpoint does not match the fifth identical failure')
    scratch_sha=sha(endpoint/'scratch.tar')
    scratch_capture=json.loads((endpoint/'scratch-manifest.json').read_text())
    if scratch_sha!=scratch_capture['sha256']:
        raise ValueError('Scratch archive differs from capture checksum')
    manifest={'workspace_tree_sha256':archive_tree_hash(endpoint/'workspace.tar'),
        'scratch_tree_sha256':archive_tree_hash(endpoint/'scratch.tar'),
        'workspace_archive_sha256':sha(endpoint/'workspace.tar'),
        'scratch_archive_sha256':scratch_sha,
        'context_sha256':seed['context_sha256'],'seed':seed,
        'failure_counter':entries[-1]['feedback']['same_failure_count'],
        'snapshot_scope':'/testbed including internal git; scratch preserved separately',
        'process_scope':'fresh agent process; tool calls fenced; no live task process cloned'}
    atomic(endpoint/'failures.json',feedback)
    atomic(endpoint/'manifest.json',manifest)
    return str(endpoint)

def endpoint_tar_filter(member,destination):
    # Retain tar_filter's path checks and strip special permission bits, but
    # preserve ordinary write bits so both arms see the captured permissions.
    checked=tarfile.tar_filter(member,destination)
    return checked.replace(mode=member.mode & 0o777)

def restore_archive(archive,root):
    root=Path(root)
    if str(root) not in ('/testbed','/bench-scratch'):raise ValueError('Unexpected clone target')
    if root.is_symlink():raise ValueError('Clone root cannot be a symlink')
    cwd=Path.cwd()
    inside=cwd==root or cwd.is_relative_to(root)
    if inside:os.chdir('/')
    root.mkdir(exist_ok=True)
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():shutil.rmtree(child)
        else:child.unlink()
    with tarfile.open(archive) as tf:
        members=tf.getmembers()
        if any(Path(m.name).is_absolute() or '..' in Path(m.name).parts for m in members):
            raise ValueError('Unsafe archive member')
        # The production container has CHOWN but lacks FOWNER. Apply modes
        # while newly created entries still belong to the extracting process.
        def filter_mode_first(member,destination):
            return endpoint_tar_filter(member,destination).replace(uid=None,gid=None,uname=None,gname=None)

        tf.extractall(root,filter=filter_mode_first)
        if os.geteuid()==0:
            for member in reversed(members):
                entry=root/member.name
                if entry!=root and not entry.parent.resolve().is_relative_to(root):
                    raise ValueError(f'Archive member parent escapes clone root: {member.name}')
                os.chown(entry,member.uid,member.gid,follow_symlinks=False)
    if inside:os.chdir(cwd if cwd.exists() else root)

def verify_and_steer(root,settings,effective):
    """Called once before the arm's first live request, after strict prefix replay."""
    endpoint=root/'endpoint';m=settings['endpoint_manifest']
    for name,key in [('workspace','workspace_archive_sha256'),('scratch','scratch_archive_sha256')]:
        if sha(endpoint/(name+'.tar'))!=m[key]:raise ValueError('Endpoint archive changed')
    # Git snapshots omit empty dirs and non-executable mode bits. Restore the
    # exact full endpoint archive for BOTH arms after bootstrap, before live work.
    restore_archive(endpoint/'workspace.tar','/testbed')
    restore_archive(endpoint/'scratch.tar','/bench-scratch')
    actual={'workspace_tree_sha256':tree_hash('/testbed'),'scratch_tree_sha256':tree_hash('/bench-scratch')}
    if any(actual[k]!=m[k] for k in actual):
        atomic(root/'paired-environment-mismatch.json',{
            'actual':actual,'expected':{k:m[k] for k in actual}})
        raise ValueError('Cloned endpoint differs from frozen seed')
    notice=COMMON_NOTICE+json.dumps(settings['prior_failures'],ensure_ascii=False)
    effective['messages'].append({'role':'system','content':notice})
    if settings['strategy']=='rewind':effective['messages'].append({'role':'system','content':REWIND_NOTICE})
    atomic(root/'paired-environment-audit.json',{
        **actual,'equal_to_seed':True,'common_notice_sha256':hashlib.sha256(notice.encode()).hexdigest(),
        'failure_counter_reset':0,'scratch_path':os.environ['JWS_SCRATCH_DIR'],
        'original_absolute_scratch_paths_preserved':True,
        'bootstrap_counted_as_rewind':False})
