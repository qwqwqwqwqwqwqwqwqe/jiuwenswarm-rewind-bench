# Experiment results

[中文](experiments.md) · [Home](../README.en.md)

## SWE-bench exploration

Early Luna6 trials exercised the feedback and rewind plumbing. On three runnable DS1017 tasks, Continue and Restart both passed 3/3, while Rewind passed 2/3 and made zero completed rewinds. These tasks frequently allowed local repair. The sample, model and implementation versions limit the comparison.

## Independent MettleBench runs

The common set contains 76 tasks, after excluding Ripgrep and Pandoc for abnormal runs from the 78 with summaries. Four other tasks stopped during dependency preflight. These are single-run results with replacement runs for affected tasks and mixed versions.

| Strategy | Passed | Uncached input plus output tokens | Total tokens including cached input |
|---|---:|---:|---:|
| Continue | 71/76 | 4,395,201 | 114,025,153 |
| Restart | 73/76 | 4,857,127 | 111,117,351 |
| Rewind | 72/76 | 3,784,712 | 99,947,272 |

The Rewind arm used about 22% fewer uncached-plus-output tokens than Restart overall. Many tasks completed without requesting a rewind, and some repeated failures stayed on the current path. Strategy-level costs include those runs and cache effects.

## Matched failed-Continue endpoints

A task enters only after a fresh Continue run naturally hits five consecutive failures on the same criterion. The small cohort was deliberately selected from prior unsuccessful tasks, so it describes recovery cases rather than estimating a benchmark-wide effect.

Ctags and Gowsdl solved during screening. Bat and Age yielded paired endpoints with identical context, workspace and scratch at the start. Continue failed the same cache evidence criterion five more times on Bat. Rewind returned to the point before removing the original cache input and passed its first new submission. The two arms used 41,473 and 331,807 uncached-input-plus-output tokens, respectively. Both arms failed to finish Age. Nine subsequent screening runs passed, so the expansion produced no additional eligible pairs.

Repeated Restart and Rewind runs on the two frozen endpoints produced the following results.

| Task | Restart | Rewind |
|---|---:|---:|
| Bat | 2/2 | 2/2 |
| Age | 2/2 | 0/2 |

Bat demonstrates that checkpoint restoration can recover an otherwise lost prerequisite. Age shows that restoring an earlier state can still leave substantial downstream acceptance work. Estimating average benefit requires a fixed configuration, a predefined sampling plan and more independent failed endpoints.
