"""Installed into the experimental runtime only; no Docker access in agent tools."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import re

_TESTBED_ROOT='/testbed'
_REPLAY_PREFIX='replay://workspace/workspace'
_READ_COUNTERS:dict={}  # resolved real path -> (times read, content sha256)
_READ_BLOCK_THRESHOLD=3
_PROBE_COUNTERS:dict={}  # whitespace-normalized command -> times run unchanged
_PROBE_BLOCK_THRESHOLD=3
SUBMISSION_GATE_TURNS=15
SUBMISSION_GATE_TOOLS=50
SUBMISSION_GATE_STALL_TURNS=5
# The gate is delivered as a recorded tool rejection, never as a rail-injected
# steering message: injected messages enter the request record and become a
# replay obligation that a later rewind cannot reproduce (v8.4 crashed twice
# exactly this way). A rejected tool call replays as its recorded result.
_GATE_STATE={'turns':0,'tools':0,'armed':False,'armed_turns':0,'stalls':0}


def gate_should_arm(turns,tools):
    """Either budget trips the gate: turn-counted agents wait 15 turns, but
    parallel agents (4-5 calls per turn) can burn the whole budget in 13 turns
    (real sphinx-7454 shape), so tool volume counts too."""
    return turns>=SUBMISSION_GATE_TURNS or tools>=SUBMISSION_GATE_TOOLS


def tool_call_name(call):
    """Provider-agnostic tool name extraction.

    Luna responses surface tool calls as objects (``call.function.name``);
    DeepSeek responses along the same runtime path surface them as plain dicts
    (``call['function']['name']``). A getattr-only read silently fails on the
    dict shape, making every submit invisible to the gate and turning the
    stall stop into a false positive (real DeepSeek-V4.1-Flash-1017 seed run).
    """
    function=getattr(call,'function',None)
    if function is None and isinstance(call,dict):
        function=call.get('function')
    if isinstance(function,dict):
        return function.get('name')
    return getattr(function,'name',None)


def gate_enabled():
    """The seed episode must keep its natural failure trajectory: the gate
    belongs to the repair phase, not to the common failed attempt."""
    return os.environ.get('JWS_BENCHMARK')!='mettlebench' and os.environ.get('JWS_FEEDBACK_STRATEGY')!='seed'


def _resolve_read_path(value):
    text=str(value)
    if text.startswith(_REPLAY_PREFIX):
        text=_TESTBED_ROOT+text[len(_REPLAY_PREFIX):]
    path=Path(text)
    if not path.is_absolute():path=Path(_TESTBED_ROOT)/path
    return path.resolve()


def _content_digest(path):
    try:return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:return None


def note_read(path):
    """Count unchanged-content reads of one file; edits reset the counter.

    Returns (count, changed). Unreadable paths never accumulate past 1 so the
    guard cannot lock the agent out of files it cannot stat.
    """
    digest=_content_digest(path)
    if digest is None:return (1,True)
    key=str(path)
    count,previous=_READ_COUNTERS.get(key,(0,''))
    count=count+1 if previous==digest else 1
    _READ_COUNTERS[key]=(count,digest)
    return (count,previous!=digest)


def note_probe(command):
    """Count unchanged reruns of one exact probe command (whitespace-normalized)."""
    key=' '.join(str(command).split())
    count=_PROBE_COUNTERS.get(key,0)+1
    _PROBE_COUNTERS[key]=count
    return count


def reset_read_counters():
    """Call after an actual rewind or restart: the restored context no longer
    contains the file contents the abandoned branch had read, so re-reading is
    new information again. Repeated probe reruns are likewise new information."""
    _READ_COUNTERS.clear()
    _PROBE_COUNTERS.clear()
    _GATE_STATE.update(turns=0,tools=0,armed=False,armed_turns=0,stalls=0)


def should_count_submission_turn(boundaries):
    """The gate must never steer inside a replayed prefix: injected messages
    change the request stream and break strict replay matching (v8.4 crashed
    exactly this way during bootstrap replay)."""
    prefix=getattr(boundaries,'_prefix_records',None)
    live=getattr(boundaries,'_fork_live',False)
    return not prefix or bool(live)


def submission_gate_message(turns):
    return ('You have gone '+str(turns)+' live turns without submitting. Local verification cannot '
        'replace official acceptance: the failing official test may be a hidden acceptance test that '
        'your checkout does not contain, and being unable to reproduce the official failure locally is '
        'expected and says nothing about your fix. Submit your current best-supported patch now with '
        'submit_patch, or state one concrete, removable blocker and clear it in the next turn. Do not '
        'spend more turns on further local verification.')


def guard(call):
    """Early rejection for known draft writes; repeated-read nudge; final patch audit is independent."""
    if os.environ.get('JWS_BENCHMARK')=='mettlebench':
        if os.environ.get('JWS_METTLE_ISOLATED')=='1':
            from mettle_runner.isolation import guard_files
            guard_files(call)
        return
    args=call.arguments
    args=json.loads(args) if isinstance(args,str) else args
    name=call.name.rsplit('.',1)[-1]
    if _GATE_STATE['armed'] and name!='submit_patch':
        # Armed submission gate: EVERY non-submitting call is rejected until the
        # model calls submit_patch (the rail clears the arm after observing it).
        # The rejection is recorded, so a later rewind replays it as-is.
        raise ValueError(submission_gate_message(_GATE_STATE['armed_turns'])
            +' Every further non-submitting tool call will be rejected until you call submit_patch.')
    if name in {'write_file','edit_file'}:
        path=Path(args.get('file_path',args.get('path','')))
        if not path.is_absolute():path=Path(_TESTBED_ROOT)/path
        path=Path(_resolve_read_path(str(path)))
        if path.is_relative_to(Path(_TESTBED_ROOT)):
            from feedback_runner.feedback import rejected_paths
            relative=str(path.relative_to(Path(_TESTBED_ROOT))).replace(os.sep,'/')
            if rejected_paths('+++ b/'+relative+'\n'):
                raise ValueError('Drafts and tests must be written under '+os.environ['JWS_SCRATCH_DIR']+'; existing official tests are read-only.')
            # Accepted source edits make rerunning the same probe new information.
            _PROBE_COUNTERS.clear()
    elif name=='bash':
        count=note_probe(args.get('command',''))
        if count>=_PROBE_BLOCK_THRESHOLD:
            raise ValueError('This exact command was already run '+str(count)+' times with unchanged '
                'code in between; identical reruns produce no new information. Change the probe or the '
                'code first, or submit the current patch for official acceptance when your evidence '
                'supports it.')
    elif name in {'read_file','view_file'}:
        path=_resolve_read_path(args.get('file_path',args.get('path','')))
        if path.is_file():
            count,_changed=note_read(path)
            if count>=_READ_BLOCK_THRESHOLD:
                raise ValueError(
                    'This file was already read '+str(count)+' times with unchanged content; re-reading it '
                    '(including with other offsets or ranges) cannot produce new information. If you expected '
                    'hidden content here, it does not exist in this checkout. Produce new evidence instead: '
                    'write a scratch probe under '+os.environ.get('JWS_SCRATCH_DIR','/bench-scratch')+
                    ', run a targeted native test, or inspect a different file.')


def emit(root, event, **data):
    with (root/'agent-feedback-events.jsonl').open('a') as f:
        f.write(json.dumps({'event':event,'time':time.time(),**data},ensure_ascii=False)+'\n')


def tools(boundaries,job):
    from openjiuwen.core.foundation.tool import Tool,ToolCard,ToolExposure
    from openjiuwen.core.foundation.tool.schema import ToolOutput
    root=Path(os.environ['JWS_FEEDBACK_ROOT'])

    class SubmissionTool(Tool):
        def __init__(self):
            card=ToolCard(name='submit_patch',description=(
                'Submit the current delivery workspace for official acceptance. Waits for evaluator feedback. '
                'A failed acceptance is reliable evidence; repair in place or consider rewind when available. '+
                ('Temporary scratch belongs outside the workspace; required deliverables stay inside. '
                 'Task-specific repository operations are allowed. Submit the entire workspace state.'
                 if os.environ.get('JWS_BENCHMARK')=='mettlebench' else
                 'Temporary tests/scripts belong outside /testbed in the given scratch directory. '
                 'Do not install dependencies. Submission does not require git commit.')),
                input_params={'type':'object','properties':{'summary':{'type':'string'}},'required':[]},parallel_safe=False)
            card.exposure=ToolExposure.DIRECT;card.set_exposure_declared(True)
            super().__init__(card);self.feedback_tool=True;self.last=None;self.job=job;self.boundaries=boundaries
            self.seed_feedback=None;self.seed_index=None

        async def invoke(self,inputs,**kwargs):
            request_id=f"a{job['attempt']}-"+uuid.uuid4().hex
            request={'request_id':request_id,'attempt':job['attempt'],'summary':inputs.get('summary','')[:2000]}
            temporary=root/'requests'/(request_id+'.tmp')
            temporary.write_text(json.dumps(request));temporary.replace(temporary.with_suffix('.json'))
            emit(root,'submission_requested',**request)
            response=root/'responses'/(request_id+'.json')
            while not response.exists():
                if time.time()>=job['deadline']:boundaries.stop_for_budget('Deadline while waiting for evaluation')
                await asyncio.sleep(.25)
            self.last=json.loads(response.read_text())
            if os.environ['JWS_FEEDBACK_STRATEGY']=='seed' and self.last['status']=='product_failure' and (
                    os.environ.get('JWS_PAPER_RECOVERY')!='1' or self.last.get('terminal')=='stagnated'):
                self.seed_feedback=dict(self.last);self.seed_index=boundaries._model_index
            emit(root,'feedback_returned_to_tool',attempt=job['attempt'],feedback=self.last)
            return ToolOutput(success=True,data={'content':json.dumps(self.last,ensure_ascii=False)})

        async def stream(self,inputs,**kwargs):yield await self.invoke(inputs,**kwargs)

    return (SubmissionTool(),)


async def install_rail(agent,host_tools):
    from openjiuwen.core.single_agent.rail.base import AgentRail
    from openjiuwen.core.execution import ExecutionRequest,current_execution_scope
    tool=next((t for t in host_tools if getattr(t,'feedback_tool',False)),None)
    if tool is None:return
    boundaries=tool.boundaries
    root=Path(os.environ['JWS_FEEDBACK_ROOT'])
    class FeedbackRail(AgentRail):
        priority=5
        async def before_model_call(self,ctx):
            # Restart and successful acceptance do not need an extra discarded
            # model request. Continue/Rewind failures remain ordinary inputs.
            if tool.last is None:return
            result=tool.last
            if os.environ.get('JWS_PAPER_RECOVERY')=='1' and tool.seed_feedback is not None:
                return  # Capture the next request including the fifth rejection; execute no proposed tool.
            if result.get('terminal') or os.environ['JWS_FEEDBACK_STRATEGY']=='restart':
                tool.last=None
                kind='feedback_terminal' if result.get('terminal') else 'feedback_restart'
                emit(root,'feedback_control_before_model',attempt=tool.job['attempt'],
                     feedback_id=result['submission_id'],control=kind)
                current_execution_scope().request_control(ExecutionRequest(kind,result))

        async def after_model_call(self,ctx):
            response=getattr(ctx.inputs,'response',None)
            if tool.seed_feedback is not None and boundaries._model_index>tool.seed_index:
                from feedback_runner.paired_support import freeze_seed
                value=freeze_seed(root,tool.job,tool.seed_feedback)
                emit(root,'paired_seed_frozen',**{k:v for k,v in value.items() if k!='request'})
                current_execution_scope().request_control(ExecutionRequest('paired_seed',value));return
            # Always allow the model to observe the last live tool result first.
            if tool.last is not None:
                result=tool.last;tool.last=None
                emit(root,'feedback_seen_by_model',attempt=tool.job['attempt'],feedback_id=result['submission_id'])
            # Submission-discipline gate: local verification cannot stall the
            # episode forever. Delivery is a RECORDED TOOL REJECTION (armed
            # here, raised by guard on every non-submit call until a submit),
            # never a rail-injected message - injected messages enter the
            # request record and become an unreproducible replay obligation.
            if response is not None and getattr(response,'tool_calls',None) \
                    and should_count_submission_turn(tool.boundaries) and gate_enabled():
                names={tool_call_name(call) for call in response.tool_calls}
                if 'submit_patch' in names:
                    _GATE_STATE.update(turns=0,tools=0,armed=False,stalls=0)
                elif _GATE_STATE['armed']:
                    # Armed and still not submitting: each stalled turn burns a
                    # full prompt for nothing (real django-15128 shape: 40 rejections
                    # in a row emptied the remaining budget). Stop instead of grind.
                    _GATE_STATE['stalls']+=1
                    if _GATE_STATE['stalls']>=SUBMISSION_GATE_STALL_TURNS:
                        emit(root,'submission_gate_stall_stop',stalls=_GATE_STATE['stalls'],
                             attempt=tool.job['attempt'])
                        tool.boundaries.stop_for_budget(
                            'Submission gate ignored: '+str(_GATE_STATE['stalls'])
                            +' armed turns produced no submit_patch call; stopping instead of '
                            'burning the remaining budget on rejected calls.')
                        return
                else:
                    _GATE_STATE['turns']+=1
                    _GATE_STATE['tools']+=len(response.tool_calls)
                    if gate_should_arm(_GATE_STATE['turns'],_GATE_STATE['tools']):
                        emit(root,'submission_gate',turns=_GATE_STATE['turns'],
                             tools=_GATE_STATE['tools'],attempt=tool.job['attempt'])
                        _GATE_STATE.update(armed=True,
                            armed_turns=max(_GATE_STATE['turns'],1),turns=0,tools=0)
            if response is None or getattr(response,'tool_calls',None):return
            # Automatic submissions must be recorded too: a later rewind may
            # retain this final-answer/feedback segment in its replayed prefix.
            from types import SimpleNamespace
            automatic={'summary':'Automatic submission of final answer'}
            call=SimpleNamespace(name='submit_patch',id=f'automatic-submit-{boundaries._model_index}',
                                 arguments=json.dumps(automatic))
            output=await boundaries.tool(call,lambda:tool.invoke(automatic))
            result=output.data['content']
            result=json.loads(result)
            paper_stagnation=(os.environ.get('JWS_PAPER_RECOVERY')=='1'
                and result.get('terminal')=='stagnated'
                and (tool.seed_feedback is not None or not should_count_submission_turn(boundaries)))
            if result.get('terminal') and not paper_stagnation:
                current_execution_scope().request_control(ExecutionRequest('feedback_terminal',result));return
            if os.environ['JWS_FEEDBACK_STRATEGY']=='restart':
                current_execution_scope().request_control(ExecutionRequest('feedback_restart',result));return
            if ctx.steering_queue is None:ctx.bind_steering_queue(asyncio.Queue())
            ctx.push_steering('Official evaluator feedback: '+json.dumps(result,ensure_ascii=False)+
                '\nThe task remains unresolved. Continue repairing, or decide whether to rewind if tools are available. '
                'Submit again when ready. Do not repeat an unchanged patch.')
            ctx.request_model_continue()
            # This automatic submission appears as a steering user message.
            tool.last=None
    await agent.register_rail(FeedbackRail())
    emit(root,'feedback_rail_installed',attempt=tool.job['attempt'])
