"""
People exports quality sweep — the same checks the server runs by itself
(app/data/people_quality.py), printed on demand.

    .venv/bin/python scripts/people_quality_sweep.py           # all checks
    .venv/bin/python scripts/people_quality_sweep.py --examples 10

Exit code 1 when a MUST-BE-ZERO check finds anything, so it can gate a deploy.
LIST checks are for a person to read (they can be genuine: e.l.f. and Amazon pay
several executives identically). Read-only: SELECTs only.

Needs the app environment (.env, STG DB over VPN), like .claude/dev/run_local.sh.
See docs/superpowers/specs/2026-11-13-people-exports-data-review-design.md.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))


def load_env():
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    os.environ.setdefault("APP_ENV", "LOCAL")
    for key in ("HOST", "PORT", "NAME", "USER", "PASSWORD"):
        if os.environ.get(f"STG_DB_{key}"):
            os.environ.setdefault(f"DB_{key}", os.environ[f"STG_DB_{key}"])
    os.environ.setdefault("ENABLE_SSL", "true")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--examples", type=int, default=5)
    args = parser.parse_args()
    load_env()
    import logging
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger("streamlit").setLevel(logging.ERROR)
    from data.people_quality import load_inputs, run_checks

    failed = False
    for c in run_checks(*load_inputs()):
        items = c["items"]
        flag = ("FAIL" if items else "ok  ") if c["must_be_zero"] else "LIST"
        failed |= c["must_be_zero"] and bool(items)
        print(f"[{flag}] {len(items):5d}  {c['name']}")
        for row in items[:args.examples]:
            print(f"               {row}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
