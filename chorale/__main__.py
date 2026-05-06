"""Entry point for `python -m chorale'.

Exists so `python -m chorale ARGS' works alongside the installed
`chorale' console script (registered in `pyproject.toml''s
`[project.scripts]'). Both call into `chorale.cli.main' and exit with
its return value -- there's no other code worth running here.
"""
from chorale.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
