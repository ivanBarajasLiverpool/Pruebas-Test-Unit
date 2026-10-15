"""Trusted orchestration: model access on the host, target code only in Docker."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath

LOG_LIMIT = 64000
SOURCE_LIMIT = 100 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


def read_config(path):
    # -I isolates the entrypoint; only the pinned agent directory is importable.
    sys.path.insert(0, str(ROOT))
    from unit_test_agent.core import AgentError, load_config

    try:
        return load_config(path)
    except AgentError as error:
        raise ValueError(error.code) from None


def checked_path(value):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("Unsafe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise ValueError("Unsafe relative path")
    return path


def snapshot(repository, head, destination):
    """Copy exact Git blobs, without links or archive export-attribute changes."""
    listing = subprocess.check_output(
        ["git", "-C", str(repository), "ls-tree", "-rlz", "--full-tree", head],
        timeout=30,
    )
    entries = [entry for entry in listing.split(b"\0") if entry]
    if len(listing) > 4_000_000 or len(entries) > 25000:
        raise ValueError("Repository contains too many files")
    files = []
    total = 0
    for entry in entries:
        metadata, name = entry.split(b"\t", 1)
        mode, kind, oid, size = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("Repository links and special files are unsupported")
        size = int(size)
        path = checked_path(name.decode("utf-8"))
        total += size
        if total > SOURCE_LIMIT:
            raise ValueError("Repository exceeds the sandbox size limit")
        files.append((path, oid, size, mode))
    process = subprocess.Popen(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        for path, oid, size, mode in files:
            process.stdin.write(oid + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            if header != [oid, b"blob", str(size).encode()]:
                raise ValueError("Invalid Git object")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            remaining = size
            with target.open("xb") as output:
                while remaining:
                    chunk = process.stdout.read(min(remaining, 65536))
                    if not chunk:
                        raise ValueError("Incomplete Git object")
                    output.write(chunk)
                    remaining -= len(chunk)
            if process.stdout.read(1) != b"\n":
                raise ValueError("Invalid Git object boundary")
            target.chmod(0o755 if mode == b"100755" else 0o644)
        process.stdin.close()
        if process.wait(timeout=30):
            raise ValueError("Git snapshot failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()


def overlay(generated, destination, config):
    allowed = checked_path(config["project_directory"]) / checked_path(
        config["tests_directory"]
    )
    count = 0
    size = 0
    for source in generated.rglob("*"):
        if source.is_symlink():
            raise ValueError("Generated links are forbidden")
        if not source.is_file():
            continue
        path = source.relative_to(generated)
        relative = PurePosixPath(path.as_posix())
        if not relative.is_relative_to(allowed) or not re.fullmatch(
            r"test_[A-Za-z0-9_.-]+\.py", relative.name
        ):
            raise ValueError("Generated file is not an allowed test")
        size += source.stat().st_size
        count += 1
        if size > config.get("max_output_bytes", 32000) or count > config.get(
            "max_files", 20
        ):
            raise ValueError("Generated tests exceed sandbox limits")
        target = destination / path
        if target.exists():
            raise ValueError("Cannot overwrite existing repository files")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644)
    return count


def bounded_command(command, timeout):
    """Drain output continuously without allowing untrusted logs to fill disk."""
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    tail = bytearray()

    def drain():
        while chunk := process.stdout.read(4096):
            tail.extend(chunk)
            if len(tail) > LOG_LIMIT:
                del tail[:-LOG_LIMIT]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        code = 124
    finally:
        reader.join(timeout=5)
        process.stdout.close()
    return code, bytes(tail).decode("utf-8", errors="replace")


def sandbox(snapshot_path, config, image, timeout=300, command=None):
    name = f"unit-test-agent-{uuid.uuid4().hex}"
    project = checked_path(config["project_directory"])
    script = (
        "set -eu; export HOME=/tmp; "
        "python -m venv --system-site-packages /tmp/venv; "
        'export PATH="/tmp/venv/bin:$PATH"; '
        + (command if command is not None else config["test_command"])
    )
    docker = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--log-driver",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "128",
        "--memory",
        "1g",
        "--cpus",
        "2",
        "--user",
        "65534:65534",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--mount",
        f"type=bind,source={snapshot_path},target=/workspace,readonly",
        "--workdir",
        f"/workspace/{project.as_posix()}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTEST_ADDOPTS=-p no:cacheprovider",
        image,
        "/bin/sh",
        "-c",
        script,
    ]
    try:
        return bounded_command(docker, timeout)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def generate(args, attempt, key, feedback=None, previous=None):
    command = [
        sys.executable,
        "-m",
        "unit_test_agent",
        "generate",
        "--repository",
        str(args.repository),
        "--config",
        str(args.config),
        "--base",
        args.base,
        "--head",
        args.head,
        "--output",
        str(attempt),
    ]
    if feedback is not None:
        command.extend(["--feedback", str(feedback), "--previous", str(previous)])
    # Do not forward the Actions token or arbitrary runner environment.
    environment = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if name in os.environ
    }
    environment["UNIT_TEST_AGENT_API_KEY"] = key
    subprocess.run(command, cwd=ROOT, env=environment, check=True, timeout=180)


def execute(args):
    for revision in (args.base, args.head):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Expected full commit SHAs")
    config = read_config(args.config)
    # This pilot deliberately supports one prebuilt runtime.
    if config.get("python_version", "3.11") != "3.11":
        raise ValueError("The sandbox image supports Python 3.11 only")
    attempts = config.get("max_attempts", 2)
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")
    key = os.environ.pop("UNIT_TEST_AGENT_API_KEY", "")
    if not key:
        raise ValueError("Configure the UNIT_TEST_AGENT_API_KEY secret")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"status": "error", "attempts": 0, "generated_files": 0}
    try:
        with tempfile.TemporaryDirectory(prefix="unit-agent-") as temp:
            source = Path(temp) / "source"
            source.mkdir(mode=0o755)
            # tempfile's 0700 parent would otherwise hide files from uid 65534.
            Path(temp).chmod(0o755)
            snapshot(args.repository, args.head, source)
            test_directory = (
                source
                / checked_path(config["project_directory"])
                / checked_path(config["tests_directory"])
            )
            test_directory.mkdir(parents=True, exist_ok=True)
            baseline_command = (config.get("install_command") or "true") + (
                "; " + config["test_command"]
            )
            code, log = sandbox(source, config, args.image, command=baseline_command)
            (args.output / "baseline.log").write_text(log)
            # pytest uses exit code 5 when a new project has no tests yet.
            if code not in (0, 5):
                report["status"] = "baseline_failed"
                return 1
            feedback = previous = None
            for number in range(1, attempts + 1):
                attempt = args.output / f"attempt-{number}"
                report["attempts"] = number
                generate(args, attempt, key, feedback, previous)
                generated = attempt / "generated"
                candidate = json.loads((attempt / "report.json").read_text())
                if candidate["status"] != "generated":
                    report["status"] = candidate["status"]
                    return 0
                trial = Path(temp) / f"trial-{number}"
                shutil.copytree(source, trial)
                count = overlay(generated, trial, config)
                if not count:
                    raise ValueError("Model returned no test files")
                code, log = sandbox(trial, config, args.image, command=baseline_command)
                feedback = args.output / f"attempt-{number}.log"
                feedback.write_text(log)
                previous = attempt
                report["generated_files"] = count
                if code == 0:
                    report["status"] = "passed"
                    shutil.copyfile(
                        attempt / "proposed-tests.patch",
                        args.output / "proposed-tests.patch",
                    )
                    return 0
            report["status"] = "tests_failed"
            return 1
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="unit-test-agent:local")
    args = parser.parse_args()
    try:
        return execute(args)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError):
        print(
            "Agent failed; inspect the bounded artifacts or trusted configuration.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
