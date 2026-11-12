"""Opt-in real Docker + external pytest controls; no model calls."""

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.evaluation.pilot_control import build_observer, cleanup_leases, decode_response, run_candidate

CONTROLS = {
    "original": b'import json,sys\nv=json.load(sys.stdin)["values"]\nprint(json.dumps({"total":sum(v[:-1])}))\n',
    "gold": b'import json,sys\nv=json.load(sys.stdin)["values"]\nprint(json.dumps({"total":sum(v)}))\n',
    "wrong": b'print("{\\"total\\":999}")\n',
    "empty": b'',
    "printed_pass": b'print("PASS")\n',
}

BOUNDARY_PROBE = b'''import json,os,pathlib,socket
assert os.getuid() == 65534
assert sorted(p.name for p in pathlib.Path('/candidate').iterdir()) == ['main.py']
for path in ('/candidate/main.py', '/root-write-probe'):
    try:
        with open(path,'ab') as target: target.write(b'forbidden')
    except OSError: pass
    else: raise AssertionError('write was allowed')
for path in ('/oracle','/var/run/docker.sock','/host','/workspace'):
    assert not pathlib.Path(path).exists(), path
assert 'SWE_PILOT_HOST_SECRET' not in os.environ
try:
    socket.create_connection(('1.1.1.1',443),timeout=0.3).close()
except OSError: pass
else: raise AssertionError('external network was reachable')
print(json.dumps({'total':6}))
'''
PROBES = {
    "boundary": (BOUNDARY_PROBE, "completed", 20),
    "timeout_descendant": (b'import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-I","-S","-c","import time; time.sleep(300)"])\ntime.sleep(300)\n', "timeout", 8),
    "output_limit": (b'import sys\nwhile True: sys.stdout.write("x"*4096); sys.stdout.flush()\n', "output_limit", 8),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", required=True)
    parser.add_argument("--image", required=True, help="python@sha256:<official digest>")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory")
    parser.add_argument("--probes", action="store_true", help="Also run fixed boundary/timeout/output controls")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    outcomes = []
    for name, source in CONTROLS.items():
        setup = build_observer(args.output / name, args.image)
        (setup.generation_root / "main.py").write_bytes(source)
        report = args.output.resolve() / name / "report.xml"
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                   PYTEST_ADDOPTS="", PYTEST_PLUGINS="")
        env.pop("PYTHONPATH", None)
        command = [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q",
                   "--tb=no", "--assert=plain", "--noconftest", "--confcutdir", str(setup.observer_root),
                   "-c", str(setup.observer_root / "pytest.ini"), "--rootdir", str(setup.observer_root),
                   str(setup.observer_root / "test_oracle.py"), "--junitxml", str(report)]
        setup.control_path.write_text(json.dumps({"deadline": time.monotonic() + 90}), encoding="utf-8")
        try:
            completed = subprocess.run(command, cwd=setup.observer_root, env=env,
                                       capture_output=True, timeout=95, check=False)
        except subprocess.TimeoutExpired as error:
            completed = subprocess.CompletedProcess(command, 124, error.stdout or b"", error.stderr or b"Observer timeout")
        finally:
            parent_cleanup = cleanup_leases(setup.lease_path)
        (args.output / name / "pytest.stdout.txt").write_bytes(completed.stdout)
        (args.output / name / "pytest.stderr.txt").write_bytes(completed.stderr)
        receipt = json.loads(setup.receipt_path.read_text(encoding="utf-8")) if setup.receipt_path.exists() else None
        cases = ET.parse(report).findall(".//testcase") if report.exists() else []
        functional_pass = (completed.returncode == 0 and len(cases) == 1
                           and all(c.find("failure") is None and c.find("error") is None for c in cases))
        valid = bool(parent_cleanup and receipt and receipt["cleanup_complete"] and receipt["status"] == "completed"
                     and receipt["source_hash"] == hashlib.sha256(source).hexdigest()
                     and len(cases) == 1 and cases[0].find("error") is None
                     and completed.returncode in (0, 1))
        row = {"control": name, "exit_code": completed.returncode,
               "functional_pass": functional_pass, "transport_valid": valid,
               "receipt_id": receipt["receipt_id"] if receipt else None,
               "expected_pass": name == "gold"}
        outcomes.append(row)
        print(json.dumps(row), flush=True)
        if not valid:
            break
    probes = []
    if args.probes and len(outcomes) == 5 and all(r["transport_valid"] for r in outcomes):
        os.environ["SWE_PILOT_HOST_SECRET"] = "author-controlled-canary-never-forward"
        for name, (source, expected, timeout) in PROBES.items():
            result = run_candidate(source, args.image, [], timeout_seconds=timeout)
            record = asdict(result)
            record["stdout"] = result.stdout.decode("utf-8", errors="replace")
            record["stderr"] = result.stderr.decode("utf-8", errors="replace")
            record["control"] = name
            record["passed"] = result.status == expected and result.cleanup_complete
            if name == "boundary" and record["passed"]:
                record["passed"] = decode_response(result.stdout) == 6
            probes.append(record)
            (args.output / (name + ".json")).write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(json.dumps({"probe": name, "status": result.status, "passed": record["passed"]}), flush=True)
            if not record["passed"]:
                break
    result = {"kind": "ENV_CONTROL_NOT_MODEL_RESULT", "image": args.image,
              "elapsed_seconds": time.monotonic() - started, "controls": outcomes,
              "probes": probes,
              "passed": len(outcomes) == 5 and all(r["transport_valid"] and r["functional_pass"] == r["expected_pass"] for r in outcomes)
                        and (not args.probes or len(probes) == 3 and all(p["passed"] for p in probes))}
    (args.output / "RESULT.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
