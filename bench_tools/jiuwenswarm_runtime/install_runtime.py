"""Build a checked experimental overlay directly from the 173855 wheel."""
from __future__ import annotations
import ast
import hashlib
import io
import os
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[2]
EXPECTED='0361783cef566cdff2fa867f46a553e639731498c0e22466d346e9a6c4c70cbd'

def replace(text,old,new):
    if text.count(old)!=1:raise ValueError('Source anchor changed: '+old[:100])
    return text.replace(old,new,1)

def install(runtime):
    archive=Path(os.environ.get('JWS_REWIND_ARCHIVE', ROOT/'local/jiuwenswarm-rewind-20261112-173855.zip'))
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=EXPECTED:raise ValueError('173855 package hash mismatch')
    source=zipfile.ZipFile(archive)
    wheel=zipfile.ZipFile(io.BytesIO(source.read(next(n for n in source.namelist() if '/wheels/workswarm-' in n))))
    sp=next(Path(runtime).glob('lib/python*/site-packages')).resolve()
    names=['jiuwenswarm/rewind/attempt.py','jiuwenswarm/rewind/boundaries.py','jiuwenswarm/rewind/worker.py',
           'jiuwenswarm/rewind/chat.py','jiuwenswarm/server/runtime/agent_adapter/interface_deep.py']
    manifest={'source_zip_sha256':EXPECTED,'overlay_version':'eval-feedback-v8.4-probe-and-submission-gate',
        'recovery_prompt_sha256':hashlib.sha256(Path(__file__).with_name('recovery_prompt.py').read_bytes()).hexdigest(),
        'files':{}}
    # Remove inherited modifications to package-owned rewind sources in the
    # private runtime clone. No file in the source runtime is written.
    for name in wheel.namelist():
        if name.startswith('jiuwenswarm/rewind/') and name.endswith('.py'):
            target=sp/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(wheel.read(name).decode().replace('\r',''),encoding='utf-8')
            manifest['files'][name]=hashlib.sha256(target.read_bytes()).hexdigest()
    for name in names:
        text=wheel.read(name).decode().replace('\r','')
        if name.endswith('attempt.py'):
            text=replace(text,'    tools = recovery_tools(boundaries) if job["mode"] == "on" else ()',
                '    feedback_enabled = os.environ.get("JWS_EVAL_FEEDBACK") == "1"\n'
                '    if feedback_enabled:\n        boundaries.code_recovery = False\n'
                '        if os.environ.get("JWS_PAIRED") == "1":\n'
                '            boundaries.rewind_enabled = os.environ["JWS_FEEDBACK_STRATEGY"] == "rewind"\n'
                '    tools = recovery_tools(boundaries) if job["mode"] == "on" else ()\n'
                '    if feedback_enabled:\n'
                '        from jiuwenswarm.rewind.eval_feedback import tools as submission_tools\n'
                '        tools += submission_tools(boundaries, job)')
            text=replace(text,'    with recorder(job, manifest) as session:',
                '    if os.environ.get("JWS_EVAL_FEEDBACK") == "1":\n'
                '        instructions = "Recovery is optional. Official evaluator feedback is authoritative evidence of task failure. Consider repairing in place or rewinding an earlier wrong decision. Preserve feedback and changed strategy in rewind memory." if job["mode"] == "on" else ""\n'
                '    if os.environ.get("JWS_PAIRED") == "1":\n        instructions = ""\n'
                '    with recorder(job, manifest) as session:')
        elif name.endswith('interface_deep.py'):
            anchor='        install_tool_allowlist(manager, (*WORKSPACE_TOOLS, *(tool.card.name for tool in host_tools)))'
            text=replace(text,anchor,'        from jiuwenswarm.rewind.eval_feedback import install_rail\n'
                '        await install_rail(self._instance, host_tools)\n'
                '        if __import__("os").environ.get("JWS_METTLE_ISOLATED") == "1":\n'
                '            from mettle_runner.isolation import TODO_TOOLS\n'
                '            WORKSPACE_TOOLS = (*WORKSPACE_TOOLS, *TODO_TOOLS)\n'+anchor)
        elif name.endswith('worker.py'):
            text=replace(text,'    code_recovery = "options" in job',
                '    code_recovery = "options" in job and os.environ.get("JWS_EVAL_FEEDBACK") != "1"')
        elif name.endswith('chat.py'):
            # A resume reusing the same shared branch finds the previous
            # invocation's attempt-1 directory already present (real 15127
            # rerun died exactly here); mkdir must tolerate it.
            text=replace(text,'    attempt.mkdir(parents=True)\n',
                             '    attempt.mkdir(parents=True, exist_ok=True)\n')
        else:
            text=replace(text,'        request = {**request, **self.model_parameters}',
                '        if os.environ.get("JWS_PAIRED") == "1":\n'
                '            from feedback_runner.paired_support import paired_request\n'
                '            request = paired_request(self, request)\n'
                '        request = {**request, **self.model_parameters}')
            # The context compressor can rebuild history in a way that keeps an
            # assistant message but drops its tool responses (real v8.1 crash:
            # HTTP 400 "tool_call_ids did not have response messages"). Repair
            # the sequence before the request is assembled.
            text=replace(text,'        messages = plain(request["messages"])',
                '        messages = plain(request["messages"])\n'
                '        from feedback_runner.runtime_messages import repair_orphan_tool_calls\n'
                '        messages = repair_orphan_tool_calls(messages)')
            # Position anchors are meaningless once the compressor rebuilds the
            # history; append keeps the marker at a pairing-safe position.
            text=replace(text,'                if metadata.get(NOTICE_KEY):\n'
                '                    position = metadata[NOTICE_POSITION_KEY]\n'
                '                    if not 0 <= position <= len(messages):\n'
                '                        raise ValueError("Recovery notification history anchor was removed")\n'
                '                    messages.insert(position, attachment)\n'
                '                else:\n'
                '                    messages.append(attachment)',
                '                if metadata.get(NOTICE_KEY):\n'
                '                    messages.append(attachment)\n'
                '                else:\n'
                '                    messages.append(attachment)')
            # Replay-segment records are copied common history, not decisions of
            # this arm; three real runs mis-selected them and burned the whole
            # repair budget rebuilding known ground. Drop them from candidates;
            # the regenerated target request right after the prefix stays
            # selectable as the full-restart boundary.
            text=replace(text,'        candidates = backtrack_candidates(reason=reason)\n'
                '        candidates = self.session.workspace_manager.denormalize_paths(candidates)\n'
                '        self._candidate_ids = {item["record_uid"] for item in candidates}\n'
                '        return candidates',
                '        candidates = backtrack_candidates(reason=reason)\n'
                '        from feedback_runner.candidate_preview import denormalize_previews\n'
                '        candidates = denormalize_previews(candidates, self.session.workspace_manager)\n'
                '        if os.environ.get("JWS_EVAL_FEEDBACK") == "1" and self._prefix_records:\n'
                '            from feedback_runner.runtime_messages import filter_replay_candidates, replay_prefix_llm_uids\n'
                '            from replay.storage import load_raw_records, run_path\n'
                '            fork_records = load_raw_records(run_path(Path(self.session.log_dir),\n'
                '                getattr(self.session, "fork_run", None) or getattr(self.session, "run_id", None)))\n'
                '            candidates = filter_replay_candidates(candidates,\n'
                '                replay_prefix_llm_uids(fork_records))\n'
                '        self._candidate_ids = {item["record_uid"] for item in candidates}\n'
                '        return candidates')
            anchor='            effective = record_kwargs if overridden is None else self.session.workspace_manager.denormalize_paths(overridden)'
            text=replace(text,anchor,anchor+'\n'
                '            if os.environ.get("JWS_PAIRED") == "1":\n'
                '                from feedback_runner.paired_support import observe_live\n'
                '                observe_live(self, effective)')
            text=replace(text,'"jiuwen_request": encode({','"jiuwen_request": json.dumps(encode({')
            text=replace(text,'}), "jiuwen_operation": operation}',
                         '}), ensure_ascii=False, sort_keys=True), "jiuwen_operation": operation}')
            text=replace(text,'actual = decode(effective["jiuwen_request"])',
                'encoded_request = effective["jiuwen_request"]\n'
                '            actual = decode(json.loads(encoded_request) if isinstance(encoded_request, str) else encoded_request)')
            text=replace(text,'                result = await invoke()\n                return {"version": 1, "ok": True, "value": encode(result)}',
                '                if os.environ.get("JWS_EVAL_FEEDBACK") == "1":\n'
                '                    from jiuwenswarm.rewind.eval_feedback import guard\n'
                '                    guard(tool_call)\n'
                '                result = await invoke()\n                return {"version": 1, "ok": True, "value": encode(result)}')
        ast.parse(text)
        target=sp/name;target.write_text(text,encoding='utf-8')
        manifest['files'][name]=hashlib.sha256(target.read_bytes()).hexdigest()
    for source_path,name in [(Path(__file__).with_name('model_retry.py'),'model_retry.py'),
                             (Path(__file__).with_name('runtime_tools.py'),'eval_feedback.py')]:
        target=sp/'jiuwenswarm/rewind'/name
        shutil.copyfile(source_path,target)
        manifest['files'][str(target.relative_to(sp))]=hashlib.sha256(target.read_bytes()).hexdigest()
    recorder=zipfile.ZipFile(io.BytesIO(source.read(next(n for n in source.namelist() if '/wheels/replay_agent_recorder-' in n))))
    name='replay/path_normalization.py'
    text=recorder.read(name).decode().replace('\r','')
    # The normalizer already treats ':' as a POSIX path-list delimiter. The
    # inverse parser must do the same after the replay://workspace/ prefix;
    # otherwise adjacent normalized PYTHONPATH entries merge into an invalid ID.
    text=replace(text,'        and not _starts_escaped_terminator(text, end)\n',
        '        and not (end >= start + len(LOGICAL_WORKSPACE_PREFIX) and text[end] == ":")\n'
        '        and not _starts_escaped_terminator(text, end)\n')
    text=replace(text,'            result.append(_denormalize_uri(uri, by_id))',
        '            # Source excerpts can contain an uninterpolated URI template.\n'
        '            if uri == LOGICAL_WORKSPACE_PREFIX and re.match(r"\\{[A-Za-z_][A-Za-z_0-9]*\\}", text[end:]):\n'
        '                result.append(uri)\n'
        '            else:\n'
        '                result.append(_denormalize_uri(uri, by_id))')
    ast.parse(text);(sp/name).write_text(text,encoding='utf-8')
    manifest['files'][name]=hashlib.sha256((sp/name).read_bytes()).hexdigest()
    # MettleBench may include the root task repository's Git history as data.
    # The upstream default intentionally only captures nested repositories.
    # Opt in for this benchmark; normal SWE behavior stays unchanged.
    name='replay/git_workspace.py'
    text=recorder.read(name).decode().replace('\r','')
    text=replace(text,'if dirname == ".git" and current_path != resolved_root:',
        'if dirname == ".git" and (current_path != resolved_root or os.environ.get("JWS_BENCHMARK") == "mettlebench"):')
    text=replace(text,'if filename != ".git" or current_path == resolved_root:',
        'if filename != ".git" or (current_path == resolved_root and os.environ.get("JWS_BENCHMARK") != "mettlebench"):')
    ast.parse(text);(sp/name).write_text(text,encoding='utf-8')
    manifest['files'][name]=hashlib.sha256((sp/name).read_bytes()).hexdigest()
    manifest['mettle_root_git_capture']=True
    (Path(runtime)/'feedback-overlay.json').write_text(json.dumps(manifest,indent=2))
    print('Installed',runtime,manifest['overlay_version'],flush=True)

if __name__=='__main__':install(sys.argv[1])
