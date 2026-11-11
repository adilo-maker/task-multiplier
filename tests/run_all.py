# -*- coding: utf-8 -*-
"""Run every repository test with the current Python interpreter."""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = (
    "test_transform.py",
    "test_plugin_api.py",
    "check_ui.py",
)


def main():
    for name in TESTS:
        path = os.path.join(HERE, name)
        print("=== %s ===" % name, flush=True)
        result = subprocess.run([sys.executable, path])
        if result.returncode != 0:
            print("FAILED: %s (exit code %d)" % (name, result.returncode))
            return result.returncode
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
