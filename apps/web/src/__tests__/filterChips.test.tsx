import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FilterChips } from "../components/ui/filter-chips";

const MODES = [
  { value: "ask", label: "ask" },
  { value: "plan", label: "plan" },
  { value: "development", label: "development" },
];

describe("FilterChips disabledValues", () => {
  it("disables development when passed in disabledValues and suppresses its click", () => {
    const onChange = vi.fn();
    render(
      <FilterChips
        options={MODES}
        value="ask"
        onChange={onChange}
        disabledValues={new Set(["development"])}
      />,
    );
    const dev = screen.getByText("development");
    expect(dev).toBeDisabled();
    dev.click();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not disable any chip when disabledValues is undefined", () => {
    render(<FilterChips options={MODES} value="ask" onChange={() => {}} />);
    expect(screen.getByText("development")).not.toBeDisabled();
    expect(screen.getByText("plan")).not.toBeDisabled();
  });
});
