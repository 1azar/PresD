"""Backward-compatible CLI entry point.

Prefer: python -m template_model
"""

from template_model.main import main


if __name__ == "__main__":
    raise SystemExit(main())
