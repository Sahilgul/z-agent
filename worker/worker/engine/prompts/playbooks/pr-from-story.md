# Story to PR playbook (T5)

Use when a thread's task is a user story that must land as a pull request.

1. Restate the story as acceptance criteria with `update_tasks` (one item per
   criterion, scope + acceptance filled).
2. Explore before editing: `code_search`/`file_glob` the affected areas; read
   every file you will touch.
3. Implement the smallest change satisfying each criterion, one criterion at
   a time; keep the task list's in_progress pointer honest.
4. Verify with the repo's own checks (tests, typecheck, lint). A criterion is
   not done until its check ran green THIS session.
5. Take a `git_snapshot` and review the diff for scope creep or unrelated
   changes before any git mutation.
6. Git mutations go through the shell with approval: branch `zagent/...`,
   focused commits, never push to main/master/development directly.
7. Open the PR with the story's acceptance criteria in the body and the
   verification evidence listed. Report the PR link to the thread.
