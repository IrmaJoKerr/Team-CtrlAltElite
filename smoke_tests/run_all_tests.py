#!/usr/bin/env python3
"""Run all smoke tests and produce a concise report.

This runner executes the existing test scripts under `smoke_tests/`, captures
their stdout/stderr into `smoke_tests/outputs/`, and writes a summary to
`smoke_tests/report.txt`.
"""
import subprocess
import time
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH = ROOT / "report.txt"

TESTS = [
    ("smoke_test_vertex.py", "Smoke: Vertex unit test (mocked)"),
    ("stress_endpoints.py", "Stress: endpoints stress test (mocked)")
]

results = []

for script, desc in TESTS:
    script_path = ROOT / script
    out_file = OUT_DIR / (script + ".log")
    start = time.time()
    print(f"Running: {desc} -> {script}")
    try:
        proc = subprocess.run(
            ["python3", str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=600,
        )
        duration = time.time() - start
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(proc.stdout)
        passed = proc.returncode == 0
        sample = "\n".join(proc.stdout.splitlines()[-20:])
        results.append({"script": script, "desc": desc, "passed": passed, "returncode": proc.returncode, "duration": duration, "log": str(out_file), "sample_tail": sample})
        print(f"Completed {script}: returncode={proc.returncode} time={duration:.2f}s")
    except subprocess.TimeoutExpired as e:
        duration = time.time() - start
        msg = f"Test timed out after {duration:.1f}s"
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(msg + "\n")
        results.append({"script": script, "desc": desc, "passed": False, "returncode": -1, "duration": duration, "log": str(out_file), "sample_tail": msg})
        print(msg)

# Write consolidated report
with open(REPORT_PATH, "w", encoding="utf-8") as rfh:
    rfh.write("Test run report\n")
    rfh.write("=================\n\n")
    for res in results:
        status = "PASS" if res["passed"] else "FAIL"
        rfh.write(f"{res['script']}: {res['desc']}\n")
        rfh.write(f"  Status: {status}\n")
        rfh.write(f"  Return code: {res['returncode']}\n")
        rfh.write(f"  Duration: {res['duration']:.2f}s\n")
        rfh.write(f"  Log: {res['log']}\n")
        rfh.write("  Sample tail:\n")
        for line in (res['sample_tail'] or '').splitlines():
            rfh.write("    " + line + "\n")
        rfh.write("\n")

print("\nAll tests complete. Report written to:", REPORT_PATH)
print("Logs are available under:", OUT_DIR)
