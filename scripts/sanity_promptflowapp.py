"""Sanity check: run the Python generator against a real repo checkout.

Does NOT write .collegium/ into the repo — writes map.json/map.md to a temp
dir and prints module count, top-5 hot files, and crash count.

Point SANITY_REPO at any local checkout (default: the golden tree layout).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

from collegium_maps import generate, to_dict, render_markdown

REPO = Path(os.environ.get("SANITY_REPO", "./golden/repos/PromptFlowApp"))


def main():
    if not REPO.exists():
        print(f"ERROR: repo not found: {REPO}", file=sys.stderr)
        return 2
    repo_map, report = generate(str(REPO))
    tmp = Path(tempfile.mkdtemp(prefix="collegium_sanity_py_"))
    out = tmp / "PromptFlowApp_map.json"
    out.write_text(json.dumps(to_dict(repo_map), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp / "PromptFlowApp_map.md").write_text(render_markdown(repo_map), encoding="utf-8")

    print("=== PromptFlowApp (Python generator) ===")
    print(f"repo: {REPO}")
    print(f"output dir: {tmp}")
    print(f"files_scanned: {report.files_scanned}")
    print(f"files_skipped: {report.files_skipped}")
    print(f"crash_count: {report.crash_count}")
    print(f"crashes (first 10): {report.crashes[:10]}")
    print(f"modules_count: {report.modules_count}")
    print(f"symbols_count: {report.symbols_count}")
    print(f"edges_count: {report.edges_count}")
    print("hot_files_top5:")
    for h in report.hot_files_top:
        print(f"  - {h.file} ({h.importers} importers)")
    print("entrypoints:")
    for ep in repo_map.entrypoints:
        print(f"  - {ep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
