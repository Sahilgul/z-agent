---
name: plan/fleet-scoping
mode: plan
version: 1
description: Scope every plan from the fleet graph's blast radius — never assume a change is local.
trigger: task touches a repo with a non-empty blast radius
---

# Fleet-scoping

## When to use
Use this playbook for any task that touches a repo the fleet graph flags as
having downstream dependents. The blast radius is the set of services a
change can ripple into; a plan that ignores it will under-scope and the
development lane will miss a required cross-repo step.

## Steps
1. Read the fleet graph's `blast_radius_for(target_repo)` — this is the
   Layer 0 hydration the planner already injected.
2. For every repo in the blast radius, add a Plan step that names the
   integration point (file:symbol) and a success_criterion the evidence
   nodes can verify (a test, a type check, or a contract assertion).
3. Cite every file/symbol claim with a `file:line` reference you verified
   by read-only grep on the mounted golden repo. Flag anything you could
   NOT verify — the critic lane will re-check the unverified ones first.
4. Never assume a change is local. If the blast radius is empty, say so
   explicitly in the Plan's `risks` field.

## Anti-patterns
- Drafting steps only for the target repo when the graph says three repos
  are in the blast radius.
- Citing a file from memory instead of grep — the citation lint will flag
  it and the critic will reject the plan.
