/** CodeView contracts: the highlighter must NEVER receive an undefined or
 *  unregistered language (refractor throws "Expected `string` for
 *  `aliasOrLanguage`" and the error boundary ate the whole session screen in
 *  encounter), and shell blocks get the terminal chrome instead of flat
 *  white text. */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CodeView } from "../components/CodeView";
import { Markdown } from "../components/Markdown";

describe("CodeView — language safety", () => {
  it("undefined language renders a plain pre, never crashes", () => {
    render(<CodeView code="The application is running on port 3000" lang={undefined} />);
    const pre = screen.getByTestId("code-plain");
    expect(pre.textContent).toContain("running on port 3000");
  });

  it("unregistered language falls back to plain instead of throwing", () => {
    render(<CodeView code="some cobol" lang="cobol" />);
    expect(screen.getByTestId("code-plain").textContent).toContain("some cobol");
  });

  it("empty-string language is treated as no language", () => {
    render(<CodeView code="x" lang="" />);
    expect(screen.getByTestId("code-plain")).toBeInTheDocument();
  });
});

describe("CodeView — terminal treatment for shell", () => {
  it("bash renders inside the terminal frame", () => {
    render(<CodeView code="npm run dev" lang="bash" />);
    expect(screen.getByTestId("terminal-frame")).toBeInTheDocument();
    expect(screen.queryByTestId("code-plain")).not.toBeInTheDocument();
  });

  it("fence aliases (sh, shell, zsh) land on the bash grammar", () => {
    for (const alias of ["sh", "shell", "zsh"]) {
      const { unmount } = render(<CodeView code="ls -la" lang={alias} />);
      expect(screen.getByTestId("terminal-frame")).toBeInTheDocument();
      unmount();
    }
  });
});

describe("Markdown fences — no language, no crash", () => {
  it("a bare fence renders plain (the encounter regression)", () => {
    render(<Markdown>{"before\n\n```\nplain agent output\n```\n\nafter"}</Markdown>);
    expect(screen.getByTestId("code-plain").textContent).toContain("plain agent output");
  });

  it("a bash fence gets the terminal frame through markdown", () => {
    render(<Markdown>{"```bash\nnpm run dev\n```"}</Markdown>);
    expect(screen.getByTestId("terminal-frame")).toBeInTheDocument();
  });
});
