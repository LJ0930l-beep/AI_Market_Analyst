"""Reproduce the old pytest collection in a disposable, credential-free source copy.

No test is skipped by this harness. Failed network access stays a test outcome.
Only loopback sockets bound by this process are reachable. This is a guard for
trusted repository tests, not a security sandbox for adversarial/native code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import weakref


def install_guard():
    sandbox = Path(os.environ["V36_OFFLINE_ROOT"]).resolve()
    bound = weakref.WeakSet()
    original_bind = socket.socket.bind

    def bind(sock, address):
        original_bind(sock, address)
        bound.add(sock)

    socket.socket.bind = bind

    def inside(value):
        if isinstance(value, int) or value is None:
            return True
        return Path(os.fsdecode(value)).resolve().is_relative_to(sandbox)

    def audit(event, args):
        if event == "socket.bind":
            address = args[1]
            if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1") and address[1] == 0):
                raise PermissionError("V36_OFFLINE_BIND_DENIED")
        elif event == "socket.connect":
            address = args[1]
            allowed = False
            if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
                for listener in tuple(bound):
                    try:
                        if listener.getsockname()[:2] == address[:2]:
                            allowed = True
                    except OSError:
                        pass
            if not allowed:
                raise PermissionError("V36_OFFLINE_CONNECT_DENIED")
        elif event in ("socket.getaddrinfo", "socket.gethostbyname"):
            if args[0] not in ("localhost", "127.0.0.1", "::1", None):
                raise PermissionError("V36_OFFLINE_DNS_DENIED")
        elif event in ("socket.sendto", "socket.sendmsg", "os.system", "os.startfile", "os.posix_spawn"):
            raise PermissionError("V36_OFFLINE_EXTERNAL_IO_DENIED")
        elif event == "subprocess.Popen":
            # The old import-order tests use only this interpreter and inherit sitecustomize.
            executable, command, cwd, env = args
            if (Path(executable).resolve() != Path(sys.executable).resolve()
                    or env is not None or (cwd is not None and not inside(cwd))):
                raise PermissionError("V36_OFFLINE_SUBPROCESS_DENIED")
            if not isinstance(command, (str, list, tuple)) or (isinstance(command, str) and " -I " in command):
                raise PermissionError("V36_OFFLINE_SUBPROCESS_DENIED")
        elif event == "open":
            path, mode, flags = args
            if isinstance(path, int):
                return
            name = Path(os.fsdecode(path)).name
            if name == ".env" or name.startswith(".env."):
                raise PermissionError("V36_OFFLINE_ENV_FILE_DENIED")
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and not inside(path):
                raise PermissionError("V36_OFFLINE_WRITE_DENIED")
        elif event == "sqlite3.connect":
            if args[0] != ":memory:" and not inside(args[0]):
                raise PermissionError("V36_OFFLINE_DATABASE_DENIED")
        elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"):
            if not inside(args[0]):
                raise PermissionError("V36_OFFLINE_WRITE_DENIED")
        elif event in ("os.rename", "os.link", "os.symlink"):
            if not inside(args[0]) or not inside(args[1]):
                raise PermissionError("V36_OFFLINE_WRITE_DENIED")

    sys.addaudithook(audit)
    sys._v36_offline_guard = True


def run(report, *, self_tests=False):
    repository = Path(__file__).resolve().parents[3]
    # baseline / v36-pa-decision-quality / evidence / repository
    report = report.resolve()
    allowed = repository / "evidence/v36-pa-decision-quality/baseline"
    if not report.is_relative_to(allowed) or report.exists():
        raise ValueError("A new report inside the baseline evidence directory is required")
    record_path = report.with_suffix(".run.json")
    if record_path.exists():
        raise ValueError("Run metadata already exists")
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=repository).decode().split("\0")
    source_hashes = {}
    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".v36-offline-", dir=allowed) as temporary:
        sandbox = Path(temporary).resolve()
        if not sandbox.is_relative_to(allowed.resolve()):
            raise ValueError("Invalid sandbox root")
        checkout = sandbox / "repo"
        checkout.mkdir()
        extra = ["scripts/v36_test_evidence.py", "tests/v36/test_test_evidence.py"]
        # Copy current tracked source, including working changes, never credentials or local datasets.
        for name in sorted(set(tracked + extra)):
            if not name or Path(name).name.startswith(".env"):
                continue
            source = repository / name
            if not source.is_file():
                raise ValueError(f"Missing tracked source: {name}")
            if not source.resolve().is_relative_to(repository.resolve()):
                raise ValueError(f"Tracked path escapes repository: {name}")
            target = checkout / name
            if not target.resolve().is_relative_to(checkout.resolve()):
                raise ValueError(f"Invalid destination path: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            content = source.read_bytes()
            target.write_bytes(content)
            source_hashes[name] = hashlib.sha256(content).hexdigest()
        guard_dir = sandbox / "guard"
        guard_dir.mkdir()
        shutil.copyfile(__file__, guard_dir / "v36_offline_guard.py")
        (guard_dir / "sitecustomize.py").write_text(
            "from v36_offline_guard import install_guard\ninstall_guard()\n", encoding="utf-8")
        temp_dir = sandbox / "tmp"
        temp_dir.mkdir()
        profile = sandbox / "profile"
        profile.mkdir()
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC"}}
        env.update(TEMP=str(temp_dir), TMP=str(temp_dir), USERPROFILE=str(profile),
                   APPDATA=str(profile), LOCALAPPDATA=str(profile),
                   PYTHONPATH=os.pathsep.join((str(guard_dir), str(checkout))),
                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", V36_OFFLINE_ROOT=str(sandbox))
        args = ["-m", "pytest", "-q", "-ra", "--tb=short", "-p", "scripts.v36_test_evidence"]
        args += ["--log-file", str(temp_dir / "pytest.log")]
        args += ["tests/v36/test_test_evidence.py"] if self_tests else ["--ignore=tests/v36"]
        inner_report = "evidence/v36-pa-decision-quality/baseline/pytest.json"
        args += [f"--v36-report={inner_report}"]
        # sitecustomize failures are otherwise only printed by Python; explicitly fail closed.
        bootstrap = "import sys; assert getattr(sys, '_v36_offline_guard', False); import pytest; sys.exit(pytest.main(sys.argv[1:]))"
        result = subprocess.run([sys.executable, "-c", bootstrap, *args[2:]], cwd=checkout,
                                env=env, capture_output=True, text=True, encoding="utf-8")
        produced = checkout / inner_report
        data = json.loads(produced.read_text(encoding="utf-8")) if produced.exists() else None
        if data is not None:
            with report.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        record = {
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip(),
            "command": ["python", *args], "process_exit_code": result.returncode,
            "recorded_exit_code": data["exit_code"] if data else None,
            "counts": data["counts"] if data else None,
            "source_file_count": len(source_hashes),
            "source_manifest_sha256": hashlib.sha256(
                json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "guard_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            "isolation": "tracked working-source copy; scrubbed environment; guard in Python children; owned loopback only",
            "third_party_pytest_autoload": False,
        }
        with record_path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({key: record[key] for key in ("process_exit_code", "recorded_exit_code", "counts")}))
        if data is None:
            if self_tests:
                print(result.stdout[-3000:])
                print(result.stderr[-3000:])
            else:
                print("No pytest evidence was produced; stdout/stderr are recorded only by SHA256.")
        return result.returncode if data and data["exit_code"] == result.returncode else 3


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--self-tests", action="store_true")
    options = parser.parse_args()
    sys.exit(run(options.report, self_tests=options.self_tests))
