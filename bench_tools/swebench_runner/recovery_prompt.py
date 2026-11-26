"""Generic feedback/recovery guidance shared by all experiment arms."""

RECOVERY_GUIDANCE = """
Recovery decisions after official rejection:
Distinguish observed acceptance failures from your hypotheses. Before making the
next repair, briefly state the rejected hypothesis, any new evidence, and whether
you will continue in place or evaluate a rewind. A local self-test passing does
not override failed official acceptance.

When backtrack tools are available, call backtrack_candidates to evaluate recovery if:
1. Two consecutive official submissions fail the same acceptance item and local
   changes have produced no verifiable progress toward its expected behavior.
2. You are undoing and then reintroducing substantially the same repair approach.
3. New evidence contradicts an earlier key assumption on which later edits depend.
The same failing test alone is not enough: compare its observed behavior and cause.
If evidence shows a small isolated correction will suffice, continue and explain
the new hypothesis and distinguishing check. Candidate lookup does not commit a rewind.

Concrete positive and negative examples (illustrations, not facts about this task):
POSITIVE 1: You decide a request reaches handler A, edit A, and submit. Official
acceptance fails. A routing check then shows the request actually reaches B.
Decision: query backtrack_candidates; the assumption behind the edits is disproved.
If committing, select the returned invocation that decided to repair A, before
its edits. Memory: observed route is B; repairing A did not address acceptance;
next verify B's behavior and inspect its implementation, rather than editing A again.

POSITIVE 2: You assume flag X controls an unwanted output. Submission 1 changes X
but official output is still wrong. Submission 2 changes X again with the same
observed failure. Inspection now shows component Y constructs the output without X.
Decision: query candidates and consider committing to the invocation that chose X
as the repair location. Preserve both failures and the evidence about Y in memory;
regenerate the decision and verify the actual output path before making new edits.

POSITIVE 3: You add approach A and acceptance fails; remove it and try B, which
also fails; now propose restoring A without new evidence explaining its failure.
Decision: do not repeat A blindly. Query candidates before choosing A, compare
restoring that decision with retaining independently verified work, and commit if
the dependency chain is invalid. Memory lists the failures of A and B and a distinct
next check; it must not present either failed approach as already proven correct.

NEGATIVE 1: Official feedback identifies one missing boundary condition. A direct
check locates an isolated branch; existing evidence still supports the larger repair.
Decision: continue in place, correct that branch and check the boundary and nearby
behavior. A failed submission alone does not justify discarding valid prior work.

NEGATIVE 2: A self-test command reports pytest is unavailable, while startup probing
lists a native test runner. Decision: use that runner with the project interpreter,
without installing dependencies. This does not disprove the source repair and is
not by itself a reason to rewind it.

NEGATIVE 3: The acceptance service times out without a test verdict, or an API
request has a transient error. Decision: treat this as infrastructure uncertainty,
use the allowed retry path, and obtain actual evidence before changing the repair
hypothesis. Do not invent a product failure or rewind solely because of the timeout.

If choosing recovery, select an actual returned record_uid for the LLM invocation
whose wrong decision should be replaced, including the invocation that proposed
the bad tool call. Prefer the nearest decision point that still preserves your
independently verified work and file knowledge; rewinding to the first record
discards all of it, so that choice needs explicit evidence that the original framing
itself (not a later decision) was disproved. If the same wrong judgment appears
more than once in the trajectory - it often does, because an inherited assumption
gets re-affirmed after the fork - target its MOST RECENT occurrence, not its first
appearance: the recent copy has the fullest context and discards the least
verified work. Records that were only replayed shared history are not listed as
candidates; the regenerated request that begins your own live work is the
full-restart boundary. Call backtrack_commit; naming a
checkpoint in prose is not a rewind.

In memory_summary, write these labeled sections so each is checkable. Keep the
whole note under about 3,500 characters; density beats length.
- observed_fact: the official failure_id plus the key assertion or log line
  VERBATIM (never a paraphrase such as "missing attribute" for an assertion about
  a rendered string; quote what the test actually looked for).
- hypothesis: your current best explanation, explicitly labeled as a hypothesis,
  with the single check that would DISCONFIRM it.
- do_not_repeat: the precise abandoned approach and its boundary, not a blanket
  ban (e.g. "do not resubmit patches that only alter X's empty branch", not
  "avoid clean()").
- verify_first: one to three checks executable in THIS repository right now
  (exact commands or inspections) with the expected outcome for and against the
  hypothesis. If the official fixture is not present locally, name the concrete
  substitute probe (a scratch script under $JWS_SCRATCH_DIR, the closest native
  test) instead of an impossible gate. An official failure that cannot be
  reproduced locally is expected (hidden acceptance fixtures differ); it is not
  evidence about your fix and must not delay submission.
- next_plan: numbered steps, each with the evidence that marks it done, ending
  with a submit_patch step.
- success_criteria: what official acceptance must report for you to stop.

After recovery, before any new source edit: restate verify_first and next_plan
as a short checklist, execute the checks in order, and mark each item done or
failed with the observed evidence. If a listed check cannot be performed as
written, record the substitute you used and continue the plan; do not stall on
an unverifiable requirement and do not keep re-reading files you have already
read - if a second read of the same file yields nothing new, produce new
information instead (a scratch probe, a targeted native test) or submit the
best-supported patch. Never resubmit an unchanged or cosmetic variant of a
rejected patch. Do not deliberately recreate the abandoned failure.
If backtrack tools are unavailable, use the same evidence analysis to repair in
place; do not invent tool calls or claim that a rewind happened.
"""
