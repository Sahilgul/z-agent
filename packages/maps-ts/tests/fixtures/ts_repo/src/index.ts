/**
 * App entrypoint — wires the modules together.
 */
import { build } from "./pkg/api";

export function main(): number {
  return build();
}
