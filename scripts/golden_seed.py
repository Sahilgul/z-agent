"""Golden seed script (plan §9 Phase 0: "~50 lines, run 10x").

Per repo: clone into golden -> checkout origin/<integrationBranch> -> register
(already done by seed_repos.py) -> profile stub. Golden is FETCH-ONLY from this
moment on; workspaces stamp self-contained clones from it (plan §3).

Run (env: FETCH_PAT, ZAGENT_ADO_ORG/PROJECT, ZAGENT_GOLDEN_DIR):
  python golden_seed.py [--only ServerApp]
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fleet-config"))
from loader import load_repos  # noqa: E402

GOLDEN = Path(os.environ.get("ZAGENT_GOLDEN_DIR", "./golden/repos"))
ORG = os.environ.get("ZAGENT_ADO_ORG") or os.environ.get("ADO_ORG", "")
PROJECT = os.environ.get("ZAGENT_ADO_PROJECT") or os.environ.get("ADO_PROJECT", "")
PAT = os.environ.get("FETCH_PAT", "")

HELPER = Path(__file__).resolve().parent / "git-credential-zagent"


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "ZAGENT_CREDENTIAL_SCOPE": "fetch"})


def seed_repo(name: str, branch: str) -> None:
    dest = GOLDEN / name
    remote_url = f"https://dev.azure.com/{ORG}/{PROJECT}/_git/{name}"
    auth = base64.b64encode(f":{PAT}".encode()).decode()

    if dest.exists():
        print(f"[golden] {name}: exists, fetching")
        git("-C", str(dest), "fetch", "--quiet", "origin")
    else:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        print(f"[golden] {name}: cloning {remote_url}")
        git("clone", "--quiet", remote_url, str(dest))

    # Credential mechanics: helper + extraHeader from env — PAT never in the URL/config.
    git("-C", str(dest), "config", "credential.helper", "")
    git("-C", str(dest), "config", "credential.helper", f"!python3 {HELPER}")
    git("-C", str(dest), "config", "http.extraHeader", f"Authorization: Basic {auth}")

    # Branch discipline (sacred): golden tracks origin/<integrationBranch>.
    git("-C", str(dest), "checkout", "--quiet", "-B", branch, f"origin/{branch}")
    head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    print(f"[golden] {name}: ready @ origin/{branch} ({head[:8]})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument("--config-dir", default=str(Path(__file__).resolve().parent.parent / "fleet-config"))
    args = parser.parse_args()
    if not PAT:
        raise SystemExit("FETCH_PAT is required (Code:Read, golden fetcher account)")
    for spec in load_repos(Path(args.config_dir)):
        if args.only and spec.name != args.only:
            continue
        seed_repo(spec.name, spec.integration_branch)


if __name__ == "__main__":
    main()
