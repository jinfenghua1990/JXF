"""Unified offline checks. Each suite runs in a fresh process and temporary DB."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=("all", "selfcheck", "regression"), default="all")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    groups = {"selfcheck": "tests.test_selfcheck", "regression": "tests.test_regression"}
    chosen = groups if args.group == "all" else {args.group: groups[args.group]}
    results = []
    with tempfile.TemporaryDirectory(prefix="claims-check-run-") as tmp:
        for name, module in chosen.items():
            env = dict(os.environ)
            env.update(DATABASE_URL="sqlite:///" + (Path(tmp) / (name + ".sqlite")).as_posix(),
                       UPLOAD_DIR=str(Path(tmp) / (name + "-uploads")),
                       APP_SECRET="isolated-check-run-secret-not-for-production", DISABLE_AUTH="false",
                       PYTHONPATH=str(root), PYTHONIOENCODING="utf-8")
            for key in ("WDT_SID", "WDT_APPKEY", "WDT_APPSECRET", "WDT_QIMEN_APPKEY", "WDT_QIMEN_APPSECRET", "WDT_QIMEN_TARGET_APPKEY"):
                env[key] = ""
            print(f"Running {module} (temporary database, offline fixtures)", flush=True)
            result = subprocess.run([sys.executable, "-m", module], cwd=root, env=env, check=False)
            results.append({"suite": name, "passed": result.returncode == 0, "exit_code": result.returncode})
    passed = all(row["passed"] for row in results)
    print(json.dumps({"all_passed": passed, "suites": results}, ensure_ascii=False), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
