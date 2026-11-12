"""Research-only, one-file JSON transport; no graph or model integration.

The observer never imports candidate code. Docker is a privileged external
dependency: this transport is not a claim of a hardened general sandbox.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

MAX_OUTPUT = 16_384
MAX_SOURCE = 32_768
IMAGE_PATTERN = re.compile(r"python@sha256:[0-9a-f]{64}\Z")
PYTEST_CONFIG_BYTES = b"[pytest]\naddopts =\n"


def decode_response(data: bytes) -> int:
    """Validate data, independently of the candidate's claimed exit status."""
    def unique_pairs(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("Duplicate JSON key.")
            values[key] = value
        return values

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Invalid JSON response.") from error
    if (not isinstance(value, dict) or set(value) != {"total"}
            or type(value["total"]) is not int):
        raise ValueError("Response must contain exactly one integer total.")
    return value["total"]


@dataclass(frozen=True)
class CandidateResult:
    status: str
    source_hash: str
    stdout: bytes
    stderr: bytes
    exit_code: int | None
    cleanup_complete: bool
    elapsed_seconds: float
    snapshot: str
    container_name: str
    isolation_config: dict


@dataclass(frozen=True)
class ObserverSetup:
    observer_root: Path
    generation_root: Path
    receipt_path: Path
    control_path: Path
    lease_path: Path


ORACLE_SOURCE = '''import dataclasses
import hashlib
import json
from pathlib import Path
import time
import uuid
from control_runtime import decode_response, run_candidate

def test_sum_json_contract():
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    control = json.loads(Path(config["control_path"]).read_text(encoding="utf-8"))
    deadline = control["deadline"]
    source = Path(config["source_path"]).read_bytes()
    records = []
    correct = True
    for values, expected in (([], 0), ([1, 2, 3], 6), ([-2, 5, 7], 10)):
        remaining = deadline - time.monotonic() - 2.0
        if remaining < 2.0:
            correct = False
            break
        result = run_candidate(source, config["image"], values,
                               timeout_seconds=min(20, remaining),
                               lease_path=Path(config["lease_path"]))
        record = dataclasses.asdict(result)
        record["stdout"] = result.stdout.decode("utf-8", errors="replace")
        record["stderr"] = result.stderr.decode("utf-8", errors="replace")
        records.append(record)
        try:
            correct = (result.status == "completed" and result.cleanup_complete
                       and decode_response(result.stdout) == expected and correct)
        except ValueError:
            correct = False
        if not result.cleanup_complete:
            break
    receipt = {"receipt_id": uuid.uuid4().hex,
               "source_hash": hashlib.sha256(source).hexdigest(),
               "image": config["image"], "executions": records,
               "cleanup_complete": bool(records) and all(r["cleanup_complete"] for r in records),
               "status": "completed" if len(records) == 3 and all(r["status"] == "completed" for r in records) else "execution_error"}
    Path(config["receipt_path"]).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    assert correct and len(records) == 3, "Candidate failed the fixed JSON contract"
'''


def oracle_source_for(task_id: str = "json-sum") -> str:
    """Select fixed fixture bytes on the host; copied runtime stays stdlib-only."""
    from agent.evaluation.pilot_tasks import get_pilot_task
    task = get_pilot_task(task_id)
    return ORACLE_SOURCE.replace("(([], 0), ([1, 2, 3], 6), ([-2, 5, 7], 10))", task.case_literal)


def build_observer(output_root: str | Path, image: str, *,
                   generation_root: str | Path | None = None,
                   task_id: str = "json-sum") -> ObserverSetup:
    """Build a fixed trusted pytest observer, never copying oracle into G."""
    if not IMAGE_PATTERN.fullmatch(image):
        raise ValueError("A fixed official Python digest is required.")
    oracle = oracle_source_for(task_id)
    root = Path(output_root).resolve()
    observer = root / "O"
    generation = Path(generation_root).resolve() if generation_root else root / "G"
    receipt = root / "receipt.json"
    control = root / "control.json"
    lease = root / "leases.json"
    observer.mkdir(parents=True, exist_ok=False)
    generation.mkdir(parents=True, exist_ok=True)
    if observer == generation or observer in generation.parents or generation in observer.parents:
        raise ValueError("Observer and generation roots must be separate.")
    (observer / "control_runtime.py").write_bytes(Path(__file__).read_bytes())
    (observer / "test_oracle.py").write_bytes(oracle.encode("utf-8"))
    (observer / "pytest.ini").write_bytes(PYTEST_CONFIG_BYTES)
    (observer / "config.json").write_bytes(json.dumps({
        "source_path": str(generation / "main.py"), "image": image, "task_id": task_id,
        "receipt_path": str(receipt), "control_path": str(control), "lease_path": str(lease),
    }, sort_keys=True).encode("utf-8"))
    control.write_text(json.dumps({"deadline": time.monotonic() + 90}), encoding="utf-8")
    lease.write_text("[]", encoding="utf-8")
    return ObserverSetup(observer, generation, receipt, control, lease)


def _command(argv: list[str], seconds: float) -> subprocess.CompletedProcess:
    if seconds <= 0:
        raise subprocess.TimeoutExpired(argv, 0)
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=seconds, check=False, shell=False)


def _register_lease(path: Path, name: str, snapshot: Path, source_hash: str) -> None:
    if path.stat().st_size > 16_384:
        raise ValueError("Lease inventory exceeds its bound.")
    leases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(leases, list) or len(leases) >= 3:
        raise ValueError("Lease inventory is invalid or full.")
    leases.append({"container_name": name, "snapshot": str(snapshot), "source_hash": source_hash})
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(leases), encoding="utf-8")
    temporary.replace(path)


def cleanup_leases(path: Path, *, timeout_seconds: float = 5) -> bool:
    """Independent parent backstop for an observer killed before its finally."""
    deadline = time.monotonic() + timeout_seconds
    try:
        if path.stat().st_size > 16_384:
            return False
        leases = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(leases, list) or len(leases) > 3:
            return False
        for lease in leases:
            name = lease["container_name"]
            snapshot = Path(lease["snapshot"])
            if (not re.fullmatch(r"swe-pilot-[a-f0-9]{32}", name)
                    or snapshot.parent.resolve() != Path(tempfile.gettempdir()).resolve()
                    or not snapshot.name.startswith("swe-pilot-")
                    or snapshot.is_symlink() or snapshot.is_junction()):
                return False
            _command(["docker", "rm", "--force", name], deadline - time.monotonic())
            absent = _command(["docker", "container", "ls", "--all", "--filter",
                               f"name=^/{name}$", "--format", "{{.ID}}"], deadline - time.monotonic())
            if absent.returncode or absent.stdout.strip():
                return False
            if snapshot.exists():
                shutil.rmtree(snapshot)
        return True
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return False


def run_candidate(source: bytes, image: str, values: list[int], *,
                  timeout_seconds: float = 20.0, lease_path: Path | None = None) -> CandidateResult:
    """Run one frozen main.py; deadline includes snapshot and Docker cleanup.

    Cleanup receives a reserved portion of the deadline. If Docker cannot prove
    removal, retain the owned snapshot and return cleanup_error for inspection.
    No automatic retry or host fallback is permitted.
    """
    if not isinstance(source, bytes) or len(source) > MAX_SOURCE:
        raise ValueError("Source must be bounded bytes.")
    source.decode("utf-8")
    if not IMAGE_PATTERN.fullmatch(image):
        raise ValueError("An official Python image repository digest is required.")
    if (not math.isfinite(timeout_seconds) or timeout_seconds < 2
            or timeout_seconds > 120):
        raise ValueError("Timeout must be finite and between 2 and 120 seconds.")
    if (not isinstance(values, list) or len(values) > 32
            or any(type(value) is not int or abs(value) > 10**9 for value in values)):
        raise ValueError("Input must be a bounded list of integers.")
    started = time.monotonic()
    deadline = started + timeout_seconds
    execution_deadline = deadline - min(5.0, timeout_seconds / 2)
    digest = hashlib.sha256(source).hexdigest()
    root = Path(tempfile.mkdtemp(prefix="swe-pilot-"))
    name = "swe-pilot-" + uuid.uuid4().hex
    target = root / "main.py"
    stdout, stderr = bytearray(), bytearray()
    overflow = threading.Event()
    capture_error = threading.Event()
    readers = []
    process = None
    status, exit_code, cleaned = "execution_error", None, False
    isolation = {}
    try:
        target.write_bytes(source)
        if lease_path is not None:
            _register_lease(lease_path, name, root, digest)
        argv = ["docker", "run", "--pull", "never", "--name", name,
                "--network", "none", "--read-only", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "16", "--memory", "64m", "--memory-swap", "64m",
                "--cpus", "0.5", "--ulimit", "nofile=64:64", "--log-driver", "none",
                "--mount", f"type=bind,source={root},target=/candidate,readonly",
                "--workdir", "/candidate", "-i", image,
                "python", "-I", "-S", "-B", "/candidate/main.py"]
        if time.monotonic() >= execution_deadline:
            raise TimeoutError("Preparation exhausted the deadline.")
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False)

        def capture(stream, output):
            try:
                while chunk := stream.read(4096):
                    remaining = MAX_OUTPUT - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
                        break
            except OSError:
                capture_error.set()
            finally:
                stream.close()

        for stream, output in ((process.stdout, stdout), (process.stderr, stderr)):
            thread = threading.Thread(target=capture, args=(stream, output), daemon=True)
            thread.start()
            readers.append(thread)
        process.stdin.write(json.dumps({"values": values}).encode("utf-8"))
        process.stdin.close()
        while process.poll() is None:
            if overflow.is_set():
                status = "output_limit"
                break
            if time.monotonic() >= execution_deadline:
                status = "timeout"
                break
            time.sleep(0.01)
        else:
            exit_code = process.returncode
            status = "completed" if exit_code == 0 else "execution_error"
    except (OSError, subprocess.SubprocessError, ValueError, TimeoutError) as error:
        status = "timeout" if isinstance(error, TimeoutError) else "execution_error"
        stderr.extend(str(error).encode("utf-8")[:MAX_OUTPUT - len(stderr)])
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=max(0.01, min(1, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                status = "cleanup_error"
        try:
            inspected = _command(["docker", "inspect", name], deadline - time.monotonic())
            details = json.loads(inspected.stdout)[0] if inspected.returncode == 0 else {}
            host = details.get("HostConfig", {})
            config = details.get("Config", {})
            isolation = {key: host.get(key) for key in (
                "NetworkMode", "ReadonlyRootfs", "PidsLimit", "Memory", "MemorySwap",
                "NanoCpus", "CapDrop", "SecurityOpt")}
            isolation.update(user=config.get("User"), image_id=details.get("Image"))
            if not (host.get("NetworkMode") == "none" and host.get("ReadonlyRootfs") is True
                    and host.get("PidsLimit") == 16 and host.get("Memory") == 67_108_864
                    and host.get("MemorySwap") == 67_108_864 and host.get("NanoCpus") == 500_000_000
                    and "ALL" in host.get("CapDrop", [])
                    and "no-new-privileges" in host.get("SecurityOpt", [])
                    and config.get("User") == "65534:65534"):
                status = "isolation_error"
        except (OSError, subprocess.SubprocessError, ValueError, IndexError, TypeError):
            status = "isolation_error"
        try:
            removal = _command(["docker", "rm", "--force", name], deadline - time.monotonic())
            cleaned = removal.returncode == 0 and name.encode() in removal.stdout
            if cleaned:
                absent = _command(["docker", "container", "ls", "--all", "--filter",
                                   f"name=^/{name}$", "--format", "{{.ID}}"], deadline - time.monotonic())
                cleaned = absent.returncode == 0 and not absent.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            cleaned = False
        for thread in readers:
            thread.join(timeout=max(0, min(0.5, deadline - time.monotonic())))
        cleaned = cleaned and all(not thread.is_alive() for thread in readers)
        if overflow.is_set() and status != "isolation_error":
            status = "output_limit"
        if capture_error.is_set():
            status = "capture_error"
        try:
            snapshot_matches = not target.is_symlink() and target.read_bytes() == source
        except OSError:
            snapshot_matches = False
        if not snapshot_matches:
            status = "snapshot_changed"
        if cleaned:
            try:
                shutil.rmtree(root)
            except OSError:
                cleaned = False
        if not cleaned:
            status = "cleanup_error"
    return CandidateResult(status, digest, bytes(stdout), bytes(stderr), exit_code,
                           cleaned, time.monotonic() - started, str(root), name, isolation)
