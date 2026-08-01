---
name: debug/repro-first
mode: debug
version: 1
description: Reproduce the reported behavior FIRST — a repro is the only honest entry to a root cause.
trigger: any debug run
---

# Reproduce-first

## When to use
Use this playbook for every debug run. The control plane already ran the
repo's tests to confirm the failure (the `reproduce` node); your job is to
turn that confirmed failure into a root cause, not a guess.

## Steps
1. Confirm the repro signal from the `reproduce` node — the failure is
   tamper-proof because the control plane ran it, not you.
2. Form a root-cause hypothesis with `file:line` Evidence entries, each
   linted against the mounted golden repo. Never cite from memory.
3. Report back as a Notebook contract: findings, evidence, confidence,
   open_questions. If you could NOT reproduce, say so — a flaky repro is a
   finding, not a failure.
4. If the bug is fixable, propose a minimal fix as a contracts.Plan (one
   or two steps). The human's `start_plan` action carries your diagnosis
   into Plan mode.

## Anti-patterns
- Theorizing a root cause before reproducing — you will fix the wrong bug.
- Citing a file:symbol you did not grep — the citation lint will flag it
  and the diagnosis loses its evidence.
