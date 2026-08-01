/**
 * Core service — the thing everything else builds on.
 */
export class Service {
  run(x: number): number {
    return x + 1;
  }
}

export function helper(a: number, b: number): number {
  return a + b;
}

export interface IThing {
  id: string;
}
