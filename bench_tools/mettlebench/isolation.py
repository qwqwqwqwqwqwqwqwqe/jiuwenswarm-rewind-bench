"""Mettle tool boundary: unprivileged shell; native file tools stay in public roots."""
import json
import os
from pathlib import Path

TOOL_UID = 10001
TODO_TOOLS = ('todo_create', 'todo_list', 'todo_get', 'todo_modify')


def prepare_public_tree():
    # The recorder restores files as root, so refresh ownership before each live
    # shell invocation. Never follow a task-created symlink out of the workspace.
    roots = [Path('/testbed'), Path(os.environ['JWS_SCRATCH_DIR'])]
    home=roots[1];home.mkdir(parents=True,exist_ok=True)
    if not (home/'.gitconfig').exists():
        (home/'.gitconfig').write_text('[user]\nname = Benchmark Agent\nemail = benchmark@example.invalid\n')
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        for base, dirs, files in os.walk(root, followlinks=False):
            os.chown(base, TOOL_UID, TOOL_UID, follow_symlinks=False)
            for name in dirs + files:
                os.chown(Path(base) / name, TOOL_UID, TOOL_UID, follow_symlinks=False)


def shell_identity():
    prepare_public_tree()
    return {'user': TOOL_UID, 'group': TOOL_UID, 'extra_groups': []}


def guard_files(call):
    raw = call.arguments
    args = json.loads(raw) if isinstance(raw, str) else raw
    name = call.name.rsplit('.', 1)[-1]
    if name not in {'read_file', 'write_file', 'edit_file', 'glob', 'grep', 'list_files'}:
        return
    roots = [Path('/testbed'), Path(os.environ['JWS_SCRATCH_DIR']).resolve()]
    # The agent's own notebook is public; its sibling recorder/IPC paths are not.
    roots.append(Path(os.environ['JWS_FEEDBACK_ROOT']) / 'recovery/active-data/agent/workspace/memory')
    values = [v for k, v in args.items() if k in {'file_path', 'path', 'directory', 'cwd', 'root', 'search_path'} and isinstance(v, str)]
    if name == 'glob' and isinstance(args.get('pattern'), str):
        values.append(args['pattern'])
    for value in values:
        value = os.path.expanduser(os.path.expandvars(value.replace('replay://workspace/workspace', '/testbed')))
        if '://' in value:raise PermissionError('Unsupported file URI')
        path = Path(value)
        if not path.is_absolute():
            path = Path('/testbed') / path
        path = path.resolve()
        if not any(path == root or path.is_relative_to(root) for root in roots):
            raise PermissionError('File tools may access the task workspace, current scratch directory and agent notebook only.')
    if name in {'glob','grep','list_files'}:
        # Recursive native readers must not follow an in-tree link into the
        # recorder; shell inspection remains available under the tool UID.
        for root in roots[:2]:
            for base, dirs, files in os.walk(root,followlinks=False):
                for entry in dirs+files:
                    p=Path(base)/entry
                    if p.is_symlink() and not any(p.resolve().is_relative_to(r) for r in roots):
                        raise PermissionError('Use bash for trees containing links outside the public roots.')
