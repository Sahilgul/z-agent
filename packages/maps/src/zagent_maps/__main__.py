"""Dispatcher for ``python -m zagent_maps <generate|lint> ...``."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m zagent_maps <generate|lint> ...", file=sys.stderr)
        return 2
    sub = argv[0]
    rest = argv[1:]
    if sub == "generate":
        from zagent_maps.generate import main as gen_main
        return gen_main(rest)
    if sub == "lint":
        from zagent_maps.lint import main as lint_main
        return lint_main(rest)
    print(f"unknown subcommand: {sub!r} (expected 'generate' or 'lint')", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
