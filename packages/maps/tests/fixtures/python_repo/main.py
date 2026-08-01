"""Application entrypoint — wires the package together."""

from python_repo.pkg.api import build


def main() -> int:
    """Run the app."""
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
