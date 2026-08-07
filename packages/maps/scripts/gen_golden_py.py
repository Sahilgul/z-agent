"""Generate the golden map.json for the Python fixture repo.

Run once to (re)generate the committed golden fixture. Pinned generated_at so
the golden stays byte-stable across runs.
"""
import json
from pathlib import Path

from collegium_maps import generate, to_dict

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "python_repo"
EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "python_repo_map.json"
EXPECTED.parent.mkdir(parents=True, exist_ok=True)

repo_map, report = generate(str(FIXTURE), generated_at="2026-01-01T00:00:00Z")
EXPECTED.write_text(
    json.dumps(to_dict(repo_map), indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
print("wrote", EXPECTED)
print("report:", json.dumps(report.as_dict(), indent=2))
