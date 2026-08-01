---
name: development/drizzle-transactions
mode: development
version: 1
description: Keep every multi-write mutation inside one Drizzle transaction so partial writes can never land.
trigger: a plan step writes more than one row in ServerApp
---

# Drizzle transactions

## When to use
Use this playbook for any ServerApp step that writes to more than one table
(a mutation + its audit log, a parent + children, a side-effect row). A
partial write is a data-integrity bug.

## Steps
1. Open a Drizzle transaction (`db.transaction(async (tx) => ...)`) and do
   every write in the same `tx`.
2. The audit log row goes in the SAME transaction as the mutation it
   records — if the mutation rolls back, the audit must roll back too.
3. Derive tenant filters from the authenticated principal inside the
   transaction; never read a tenantId off the request body.
4. Commit once, at the end. If any write fails, the whole transaction
   rolls back and the run records the failure as a `test_run` event.

## Anti-patterns
- Two writes in separate transactions where the second can fail after the
  first commits (orphan row).
- An audit log written after the commit — it can diverge from the
  mutation it claims to record.
