import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { ModelPicker } from "../features/sessions/ModelPicker";

const apiMock = vi.hoisted(() => ({
  get: vi.fn<(path: string) => Promise<unknown>>(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../lib/api")>();
  return { ...orig, api: { get: (path: string) => apiMock.get(path) } };
});

const FLEET = {
  default: "kimi-foundry",
  models: [
    { alias: "kimi-foundry", label: "kimi k2.6", price_in_per_mtok: 0.95, price_out_per_mtok: 4.0, cache_read_per_mtok: 0.16, reasoning_efforts: ["low", "high", "max"], supports_thinking_off: true },
    { alias: "kimi3-foundry", label: "kimi k3", price_in_per_mtok: 3.0, price_out_per_mtok: 15.0, cache_read_per_mtok: null, reasoning_efforts: ["low", "high", "max"], supports_thinking_off: false },
    { alias: "legacy-foundry", label: "legacy no-effort", price_in_per_mtok: 1.0, price_out_per_mtok: 2.0, cache_read_per_mtok: null, reasoning_efforts: [], supports_thinking_off: true },
    { alias: "glm-foundry", label: "glm 5.2", price_in_per_mtok: 1.54, price_out_per_mtok: 4.84, cache_read_per_mtok: 0.15, reasoning_efforts: ["high", "max"], supports_thinking_off: true },
    { alias: "deepseek-flash-foundry", label: "deepseek v4 flash", price_in_per_mtok: 0.19, price_out_per_mtok: 0.51, cache_read_per_mtok: 0.028, reasoning_efforts: ["low", "high", "max"], supports_thinking_off: true },
  ],
};

function renderPicker(
  selected: string[],
  onChange = vi.fn(),
  multi = true,
  reasoning: Record<string, string> = {},
  onReasoningChange = vi.fn(),
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ModelPicker
        selected={selected}
        onChange={onChange}
        reasoning={reasoning}
        onReasoningChange={onReasoningChange}
        multi={multi}
      />
    </QueryClientProvider>,
  );
  return onChange;
}

describe("ModelPicker", () => {
  it("shows the deployment default when nothing is selected", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker([]);
    await waitFor(() => expect(screen.getByText("model: kimi k2.6")).toBeTruthy());
  });

  it("lists the fleet with prices when opened", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker([]);
    await waitFor(() => expect(screen.getByText("model: kimi k2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: kimi k2.6"));
    expect(screen.getByTestId("model-option-glm-foundry")).toBeTruthy();
    expect(screen.getByText("$1.54/$4.84")).toBeTruthy();
  });

  it("multi mode toggles models in and out", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    const onChange = renderPicker(["kimi-foundry"]);
    await waitFor(() => expect(screen.getByText("model: kimi k2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: kimi k2.6"));
    fireEvent.click(screen.getByTestId("model-option-glm-foundry"));
    expect(onChange).toHaveBeenCalledWith(["kimi-foundry", "glm-foundry"]);
  });

  it("multi mode summarizes several selections", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["kimi-foundry", "glm-foundry"]);
    await waitFor(() => expect(screen.getByText("2 models")).toBeTruthy());
  });

  it("single mode replaces the selection and closes", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    const onChange = renderPicker(["kimi-foundry"], vi.fn(), false);
    await waitFor(() => expect(screen.getByText("model: kimi k2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: kimi k2.6"));
    fireEvent.click(screen.getByTestId("model-option-deepseek-flash-foundry"));
    expect(onChange).toHaveBeenCalledWith(["deepseek-flash-foundry"]);
  });

  it("renders nothing when the fleet fetch fails", async () => {
    apiMock.get.mockRejectedValue(new Error("down"));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(
      <QueryClientProvider client={qc}>
        <ModelPicker selected={[]} onChange={vi.fn()} reasoning={{}} onReasoningChange={vi.fn()} multi />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(apiMock.get).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector("[data-testid='model-picker']")).toBeNull());
  });

  it("shows reasoning pills only on active rows, per-model options", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["glm-foundry"]);
    await waitFor(() => expect(screen.getByText("model: glm 5.2")).toBeTruthy());
    fireEvent.click(screen.getByText("model: glm 5.2"));
    // Active row: auto/off + the model's two efforts.
    const row = screen.getByTestId("reasoning-row-glm-foundry");
    expect(row.textContent).toContain("auto");
    expect(row.textContent).toContain("off");
    expect(row.textContent).toContain("high");
    expect(row.textContent).toContain("max");
    expect(row.textContent).not.toContain("low");
    // Inactive rows carry no reasoning control.
    expect(screen.queryByTestId("reasoning-row-kimi-foundry")).toBeNull();
  });

  it("a model with no efforts offers the thinking toggle only", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["legacy-foundry"]);
    await waitFor(() => expect(screen.getByText("model: legacy no-effort")).toBeTruthy());
    fireEvent.click(screen.getByText("model: legacy no-effort"));
    const row = screen.getByTestId("reasoning-row-legacy-foundry");
    expect(row.textContent).toContain("auto");
    expect(row.textContent).toContain("off");
    expect(row.textContent).not.toContain("high");
  });

  it("kimi offers the full effort range (live-probed on Foundry)", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["kimi-foundry"]);
    await waitFor(() => expect(screen.getByText("model: kimi k2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: kimi k2.6"));
    const row = screen.getByTestId("reasoning-row-kimi-foundry");
    expect(row.textContent).toContain("low");
    expect(row.textContent).toContain("high");
    expect(row.textContent).toContain("max");
  });

  it("kimi k3 always thinks — no 'off' pill", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["kimi3-foundry"]);
    await waitFor(() => expect(screen.getByText("model: kimi k3")).toBeTruthy());
    fireEvent.click(screen.getByText("model: kimi k3"));
    const row = screen.getByTestId("reasoning-row-kimi3-foundry");
    expect(row.textContent).toContain("auto");
    expect(row.textContent).toContain("low");
    expect(row.textContent).toContain("max");
    expect(screen.queryByTestId("reasoning-kimi3-foundry-off")).toBeNull();
  });

  it("picking an effort reports it; auto clears back to the default", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    const onReasoningChange = vi.fn();
    renderPicker(["glm-foundry"], vi.fn(), true, { "glm-foundry": "max" }, onReasoningChange);
    await waitFor(() => expect(screen.getByText("model: glm 5.2")).toBeTruthy());
    fireEvent.click(screen.getByText("model: glm 5.2"));
    fireEvent.click(screen.getByTestId("reasoning-glm-foundry-high"));
    expect(onReasoningChange).toHaveBeenCalledWith("glm-foundry", "high");
    fireEvent.click(screen.getByTestId("reasoning-glm-foundry-auto"));
    expect(onReasoningChange).toHaveBeenCalledWith("glm-foundry", null);
  });
});
