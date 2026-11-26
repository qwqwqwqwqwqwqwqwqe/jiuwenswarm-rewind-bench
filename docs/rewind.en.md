# Rewind mechanics

[中文](rewind.md) · [Home](../README.en.md)

1. At model decision boundaries, associate the model request, response, tool observations and workspace snapshot with an execution record.
2. The Rewind arm exposes `backtrack_candidates` and `backtrack_commit`. The model can inspect candidates and select a point before its mistaken decision. It supplies a short note about the failure and a corrected strategy.
3. Fence the old attempt and restore the selected workspace. Recorded outputs rebuild the retained context without rerunning side-effecting tools. The next live call starts at the selected decision.
4. Inject observed verifier feedback and operational memory. Record which hypothesis failed, which state to preserve, and how to verify the next approach.

The restoration scope covers the task workspace and model context. Workspace snapshots preserve Git metadata required to deliver the task. External services and running processes are outside that scope. Temporary artifacts and drafts live outside the delivery tree. SWE-bench submits a Git patch; MettleBench submits the workspace.

Matched recovery clones the workspace, context and `/bench-scratch` from the same failed Continue endpoint. Scratch may contain backups available to the in-place agent. Both arms verify the cloned state before the first live request and reset their failure counters. The common-prefix bootstrap is recorded separately from model-triggered rewind.

Audit a rewind by correlating model tool calls, tool responses, recovery events and the new branch. Count candidate queries, attempts to commit, rejected commits and actual restores separately. Assess whether the restored state removes the original blocker and whether the final verification passes. Bat and Code Image illustrate these distinctions. [Experimental results](experiments.en.md)
