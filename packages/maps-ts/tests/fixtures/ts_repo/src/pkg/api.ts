/**
 * API surface — builds a Service.
 */
import { Service, helper } from "./core";

export function build(): Service {
  helper(1, 2);
  return new Service();
}
