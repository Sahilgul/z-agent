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
  default: "kimi-k2.6",
  models: [
    { alias: "kimi-k2.6", label: "Kimi K2.6", price_in_per_mtok: 0.95, price_out_per_mtok: 4.0, cache_read_per_mtok: 0.16, reasoning_efforts: ["low", "medium", "high"], supports_thinking_off: true, vision: true },
    { alias: "kimi-k3", label: "Kimi K3", price_in_per_mtok: 3.3, price_out_per_mtok: 16.5, cache_read_per_mtok: 0.33, reasoning_efforts: ["low", "medium", "high", "max"], supports_thinking_off: true, vision: true },
    { alias: "k27code-foundry", label: "kimi k2.7 code", price_in_per_mtok: 2.0, price_out_per_mtok: 8.0, cache_read_per_mtok: null, reasoning_efforts: ["low", "high"], supports_thinking_off: false, vision: false },
    { alias: "legacy-foundry", label: "legacy no-effort", price_in_per_mtok: 1.0, price_out_per_mtok: 2.0, cache_read_per_mtok: null, reasoning_efforts: [], supports_thinking_off: true, vision: false },
    { alias: "glm-5.2", label: "GLM 5.2", price_in_per_mtok: 1.54, price_out_per_mtok: 4.84, cache_read_per_mtok: 0.15, reasoning_efforts: ["low", "medium", "high"], supports_thinking_off: true, vision: false },
    { alias: "deepseek-v4-flash", label: "DeepSeek V4 Flash", price_in_per_mtok: 0.19, price_out_per_mtok: 0.51, cache_read_per_mtok: 0.028, reasoning_efforts: ["minimal", "low", "medium", "high"], supports_thinking_off: true, vision: false },
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
    await waitFor(() => expect(screen.getByText("model: Kimi K2.6")).toBeTruthy());
  });

  it("lists the fleet with prices when opened", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker([]);
    await waitFor(() => expect(screen.getByText("model: Kimi K2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: Kimi K2.6"));
    expect(screen.getByTestId("model-option-glm-5.2")).toBeTruthy();
    expect(screen.getByText("$1.54/$4.84")).toBeTruthy();
  });

  it("badges vision-capable models only", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker([]);
    await waitFor(() => expect(screen.getByText("model: Kimi K2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: Kimi K2.6"));
    // Both Kimi deployments read images natively (probed 2026-08-08); the
    // text-only fleet gets no badge.
    expect(screen.getAllByText("vision")).toHaveLength(2);
  });

  it("multi mode toggles models in and out", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    const onChange = renderPicker(["kimi-k2.6"]);
    await waitFor(() => expect(screen.getByText("model: Kimi K2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: Kimi K2.6"));
    fireEvent.click(screen.getByTestId("model-option-glm-5.2"));
    expect(onChange).toHaveBeenCalledWith(["kimi-k2.6", "glm-5.2"]);
  });

  it("multi mode summarizes several selections", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["kimi-k2.6", "glm-5.2"]);
    await waitFor(() => expect(screen.getByText("2 models")).toBeTruthy());
  });

  it("single mode replaces the selection and closes", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    const onChange = renderPicker(["kimi-k2.6"], vi.fn(), false);
    await waitFor(() => expect(screen.getByText("model: Kimi K2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: Kimi K2.6"));
    fireEvent.click(screen.getByTestId("model-option-deepseek-v4-flash"));
    expect(onChange).toHaveBeenCalledWith(["deepseek-v4-flash"]);
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
    renderPicker(["glm-5.2"]);
    await waitFor(() => expect(screen.getByText("model: GLM 5.2")).toBeTruthy());
    fireEvent.click(screen.getByText("model: GLM 5.2"));
    // Active row: auto/off + the model's efforts (glm: low/medium/high,
    // no minimal — only the DeepSeek deployments take it).
    const row = screen.getByTestId("reasoning-row-glm-5.2");
    expect(row.textContent).toContain("auto");
    expect(row.textContent).toContain("off");
    expect(row.textContent).toContain("high");
    expect(row.textContent).toContain("medium");
    expect(row.textContent).not.toContain("minimal");
    // Inactive rows carry no reasoning control.
    expect(screen.queryByTestId("reasoning-row-kimi-k2.6")).toBeNull();
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

  it("kimi offers low/medium/high (probed direct on Foundry)", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["kimi-k2.6"]);
    await waitFor(() => expect(screen.getByText("model: Kimi K2.6")).toBeTruthy());
    fireEvent.click(screen.getByText("model: Kimi K2.6"));
    const row = screen.getByTestId("reasoning-row-kimi-k2.6");
    expect(row.textContent).toContain("low");
    expect(row.textContent).toContain("medium");
    expect(row.textContent).toContain("high");
  });

  it("Kimi K3 offers max and off (probed: none works despite first-party docs)", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["kimi-k3"]);
    await waitFor(() => expect(screen.getByText("model: Kimi K3")).toBeTruthy());
    fireEvent.click(screen.getByText("model: Kimi K3"));
    const row = screen.getByTestId("reasoning-row-kimi-k3");
    expect(row.textContent).toContain("auto");
    expect(row.textContent).toContain("off");
    expect(row.textContent).toContain("max");
  });

  it("an always-thinking model hides the 'off' pill", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    renderPicker(["k27code-foundry"]);
    await waitFor(() => expect(screen.getByText("model: kimi k2.7 code")).toBeTruthy());
    fireEvent.click(screen.getByText("model: kimi k2.7 code"));
    const row = screen.getByTestId("reasoning-row-k27code-foundry");
    expect(row.textContent).toContain("auto");
    expect(row.textContent).toContain("low");
    expect(row.textContent).toContain("high");
    expect(screen.queryByTestId("reasoning-k27code-foundry-off")).toBeNull();
  });

  it("picking an effort reports it; auto clears back to the default", async () => {
    apiMock.get.mockResolvedValue(FLEET);
    const onReasoningChange = vi.fn();
    renderPicker(["glm-5.2"], vi.fn(), true, { "glm-5.2": "medium" }, onReasoningChange);
    await waitFor(() => expect(screen.getByText("model: GLM 5.2")).toBeTruthy());
    fireEvent.click(screen.getByText("model: GLM 5.2"));
    fireEvent.click(screen.getByTestId("reasoning-glm-5.2-high"));
    expect(onReasoningChange).toHaveBeenCalledWith("glm-5.2", "high");
    fireEvent.click(screen.getByTestId("reasoning-glm-5.2-auto"));
    expect(onReasoningChange).toHaveBeenCalledWith("glm-5.2", null);
  });
});
