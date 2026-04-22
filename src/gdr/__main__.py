"""Entry point for `python -m gdr`."""

from __future__ import annotations

from gdr.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
