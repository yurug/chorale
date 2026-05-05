"""Allow `python -m chorale ...` as well as the `chorale` console script."""
from chorale.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
