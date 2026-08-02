import { QueryClient } from "@tanstack/react-query";

/** Shared instance: the run socket lives outside React (stores/run.ts) and has
 *  to invalidate queries when the backend pushes, so the client can't be
 *  created inside main.tsx's render tree. */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
