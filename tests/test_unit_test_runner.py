import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from automation import prepare_config, publish_report
from automation import run_unit_test_agent as runner


def git(repository, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(repository), *arguments], text=True
    ).strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "config.json").write_text('{"trusted": true}')
    (tmp_path / "source.py").write_text("value = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Base")
    return tmp_path


def test_configuration_is_read_from_base_not_pr(repository, tmp_path):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "config.json").write_text('{"trusted": false}')
    git(repository, "commit", "-qam", "Change")
    head = git(repository, "rev-parse", "HEAD")
    output = tmp_path / "trusted.json"
    prepare_config.prepare(repository, base, head, "config.json", output)
    assert json.loads(output.read_text()) == {"trusted": True}
    with pytest.raises(ValueError, match="changed during checkout"):
        prepare_config.prepare(repository, base, base, "config.json", output)


@pytest.mark.parametrize("path", ["../config.json", "/config.json", "a\\b", "a\nb"])
def test_configuration_path_rejects_unsafe_values(repository, path, tmp_path):
    sha = git(repository, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        prepare_config.prepare(repository, sha, sha, path, tmp_path / "out")


def test_snapshot_only_extracts_tracked_regular_files(repository, tmp_path):
    (repository / "untracked").write_text("never mount me")
    destination = tmp_path / "snapshot"
    runner.snapshot(repository, git(repository, "rev-parse", "HEAD"), destination)
    assert (destination / "source.py").read_text() == "value = 1\n"
    assert not (destination / "untracked").exists()
    (repository / "link").symlink_to("/etc/passwd")
    git(repository, "add", "link")
    git(repository, "commit", "-qm", "Link")
    with pytest.raises(ValueError, match="links"):
        runner.snapshot(
            repository, git(repository, "rev-parse", "HEAD"), tmp_path / "unsafe"
        )


def test_snapshot_preserves_exact_blobs_and_executable_modes(repository, tmp_path):
    (repository / ".gitattributes").write_text(
        "source.py export-ignore\nscript.sh export-subst\n"
    )
    script = repository / "script.sh"
    script.write_text("#!/bin/sh\n# $Format:%H$\n")
    script.chmod(0o755)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Archive attributes")
    destination = tmp_path / "snapshot"
    runner.snapshot(repository, git(repository, "rev-parse", "HEAD"), destination)
    assert (destination / "source.py").exists()
    assert (destination / "script.sh").read_bytes() == script.read_bytes()
    assert (destination / "script.sh").stat().st_mode & 0o111


def test_overlay_only_adds_new_test_files(tmp_path):
    generated = tmp_path / "generated"
    destination = tmp_path / "source"
    test = generated / "project/tests/test_new.py"
    test.parent.mkdir(parents=True)
    test.write_text("def test_ok(): assert True\n")
    config = {"project_directory": "project", "tests_directory": "tests"}
    assert runner.overlay(generated, destination, config) == 1
    with pytest.raises(ValueError, match="overwrite"):
        runner.overlay(generated, destination, config)
    test.unlink()
    (generated / "project/production.py").write_text("bad = True")
    with pytest.raises(ValueError, match="allowed test"):
        runner.overlay(generated, destination, config)


def test_overlay_rejects_links(tmp_path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="links"):
        runner.overlay(
            generated,
            tmp_path / "source",
            {"project_directory": ".", "tests_directory": "tests"},
        )


def test_bounded_logs_and_timeout():
    code, output = runner.bounded_command(
        [sys.executable, "-c", "print('x' * 200000)"], timeout=5
    )
    assert code == 0
    assert len(output) == runner.LOG_LIMIT
    code, _ = runner.bounded_command(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1
    )
    assert code == 124


def test_sandbox_command_never_forwards_host_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("UNIT_TEST_AGENT_API_KEY", "fake-test-value")
    captured = []
    monkeypatch.setattr(
        runner,
        "bounded_command",
        lambda command, timeout: (captured.extend(command) or 0, "ok"),
    )
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: None)
    runner.sandbox(
        tmp_path,
        {"project_directory": ".", "test_command": "pytest"},
        "unit-test-agent:local",
    )
    command = " ".join(captured)
    assert "--network none" in command
    assert "--read-only" in command
    assert "--log-driver none" in command
    assert "--cap-drop ALL" in command
    assert "target=/workspace,readonly" in command
    assert "fake-test-value" not in command
    assert "UNIT_TEST_AGENT_API_KEY" not in command
    assert "docker.sock" not in command


@pytest.mark.parametrize(
    "baseline_code,validation_codes,expected",
    [
        (1, [], "baseline_failed"),
        (0, [0], "passed"),
        (5, [0], "passed"),
        (0, [1, 0], "passed"),
        (0, [1, 1], "tests_failed"),
    ],
)
def test_orchestration_limits_repairs_and_publishes_only_passing_patch(
    tmp_path, monkeypatch, baseline_code, validation_codes, expected
):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "project_directory": ".",
                "tests_directory": "tests",
                "test_command": "python -m pytest",
                "max_attempts": 2,
            }
        )
    )
    args = argparse.Namespace(
        repository=tmp_path,
        config=config,
        base="a" * 40,
        head="b" * 40,
        output=tmp_path / "results",
        image="unit-test-agent:local",
    )
    monkeypatch.setenv("UNIT_TEST_AGENT_API_KEY", "fake-test-value")
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "read_config", lambda path: json.loads(path.read_text())
    )
    monkeypatch.setattr(runner, "snapshot", lambda *args: None)
    codes = iter([baseline_code, *validation_codes])
    monkeypatch.setattr(runner, "sandbox", lambda *args, **kwargs: (next(codes), "log"))
    repairs = []

    def generate(arguments, attempt, key, feedback=None, previous=None):
        assert key == "fake-test-value"
        assert "UNIT_TEST_AGENT_API_KEY" not in os.environ
        repairs.append((feedback, previous))
        tests = attempt / "generated/tests"
        tests.mkdir(parents=True)
        (tests / "test_new.py").write_text("def test_ok(): assert True\n")
        (attempt / "proposed-tests.patch").write_text("candidate patch")
        (attempt / "report.json").write_text('{"status": "generated"}')

    monkeypatch.setattr(runner, "generate", generate)
    code = runner.execute(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["status"] == expected
    assert code == (0 if expected == "passed" else 1)
    assert (args.output / "proposed-tests.patch").exists() == (expected == "passed")
    assert len(repairs) == len(validation_codes)
    if len(repairs) == 2:
        assert repairs[1][0].name == "attempt-1.log"
        assert repairs[1][1].name == "attempt-1"


def test_publication_does_not_render_model_content():
    body = publish_report.summary(
        {
            "status": "<script>bad</script>",
            "attempts": "\nmalicious",
            "generated_files": 100000,
            "model_text": "secret",
        },
        "https://github.com/example/repository/actions/runs/1",
    )
    assert "<script>" not in body
    assert "malicious" not in body
    assert "secret" not in body
    assert "Intentos: 0." in body


def test_real_docker_sandbox_has_no_credentials_network_or_write_access():
    if os.environ.get("TEST_UNIT_AGENT_DOCKER") != "1":
        pytest.skip("Set TEST_UNIT_AGENT_DOCKER=1 after building the trusted image")
    with tempfile.TemporaryDirectory(prefix="unit-agent-integration-") as temp:
        source = Path(temp)
        source.chmod(0o755)
        (source / "test_isolation.py").write_text(
            "import os, pathlib, socket\n"
            "import pytest\n"
            "def test_isolation():\n"
            "    assert os.getuid() == 65534\n"
            "    assert 'UNIT_TEST_AGENT_API_KEY' not in os.environ\n"
            "    assert 'GH_TOKEN' not in os.environ\n"
            "    assert not pathlib.Path('/var/run/docker.sock').exists()\n"
            "    with pytest.raises(OSError):\n"
            "        pathlib.Path('/workspace/modified').write_text('no')\n"
            "    with pytest.raises(OSError):\n"
            "        socket.create_connection(('1.1.1.1', 443), timeout=1)\n"
        )
        code, log = runner.sandbox(
            source,
            {
                "project_directory": ".",
                "test_command": "python -m pytest /workspace -q",
            },
            "unit-test-agent:local",
        )
        assert code == 0, log
