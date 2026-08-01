---
name: development/serverapp-areas
mode: development
version: 1
description: Implement ServerApp changes inside the Areas pattern — one area owns a domain surface.
trigger: a plan step targets the ServerApp repo
---

# ServerApp Areas

## When to use
Use this playbook for any development step that touches ServerApp. ServerApp
is organized into Areas (NestJS modules that own a domain surface end-to-end).
A change that scatters across areas is a change that will regress.

## Steps
1. Identify the Area that owns the surface (e.g. `encounters` owns encounter
   CRUD + events). Put the change there; do not reach across Areas.
2. Repositories are Drizzle-based and per-Area. Derive tenant filters from
   the authenticated principal — never pass a raw tenantId from the caller.
3. Write the audit log in the SAME transaction as the mutation
   (`audit-log-in-the-same-transaction`).
4. Order every list query with an explicit ORDER BY tie-breaker (id) so
   pagination is deterministic.
5. After the step, run the repo profile's test_cmds and emit a `test_run`
   StepEvent with the real exit code. The backend derives evidence from
   that event — never claim done from prose.

## Anti-patterns
- A service that imports another Area's repository directly (cross-Area
  coupling). Use the Area's published service interface instead.
- A mutation whose audit log is written in a follow-up transaction.
