/** Query key factory — one registry so invalidation stays consistent. */
export const qk = {
  me: ["me"] as const,
  tickets: ["hydration", "my-tickets"] as const,
  approvals: ["approvals"] as const,
  knowledge: (scope?: string) => ["knowledge", scope ?? "all"] as const,
  knowledgeDrafts: ["knowledge", "drafts"] as const,
  ideas: ["ideas"] as const,
  ideaThread: (id: string) => ["ideas", id] as const,
  proposals: (includeDecided: boolean) => ["proposals", { decided: includeDecided }] as const,
  repos: ["repos"] as const,
  repoBranches: (url: string) => ["repos", "branches", url] as const,
  team: ["team", "users"] as const,
  costStats: (days: number) => ["stats", "cost", days] as const,
  deliveries: ["deliveries"] as const,
  plan: (runId: string) => ["runs", runId, "plan"] as const,
  pr: (runId: string) => ["runs", runId, "pr"] as const,
};
