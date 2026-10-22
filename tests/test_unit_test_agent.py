"""Hermetic tests for the trusted generator, with no provider network calls."""

import json
import os
import shutil
import subprocess
import urllib.error
import uuid
from pathlib import Path

import pytest

from unit_test_agent import AgentError, core, generate, load_config
from unit_test_agent.__main__ import main


@pytest.fixture
def workspace():
    # Keep even ephemeral test data in the checkout, never in system temp dirs.
    root = Path.cwd() / "tests" / (".unit-test-agent-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def git(repository, *args):
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True, capture_output=True,
    ).stdout.decode().strip()


def commit(repository):
    git(repository, "add", ".")
    git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "fixture")
    return git(repository, "rev-parse", "HEAD")


def put(root, path, content):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


@pytest.fixture
def case(workspace):
    repository = workspace / "repository"
    repository.mkdir()
    git(repository, "init", "-q")
    project = repository / "pilot"
    put(project, "Calculadora/calcula.py", "def add(a, b):\n    return a + b\n")
    put(project, "tests/unit/test_existing.py", "def test_existing():\n    assert True\n")
    base = commit(repository)
    put(project, "Calculadora/calcula.py",
        "from .helpers import identity\n\ndef add(a, b):\n    return identity(a + b)\n")
    put(project, "Calculadora/helpers.py", "def identity(value):\n    return value\n")
    head = commit(repository)
    config = {
        "version": 1, "language": "python", "python_version": "3.11",
        "project_directory": "pilot", "tests_directory": "tests/unit",
        "source_paths": ["Calculadora"], "exclude_paths": [],
        "install_command": "python -m pip install -e '.[dev]'",
        "test_command": "python -m pytest tests/unit",
    }
    config_path = workspace / "config.json"
    config_path.write_text(json.dumps(config))
    return {
        "repository": repository, "config_path": config_path,
        "base": base, "head": head, "output": workspace / "output",
        "_project": project, "_config": config, "_workspace": workspace,
    }


def settings(case, **updates):
    config = dict(case["_config"], **updates)
    case["config_path"].write_text(json.dumps(config))
    return config


def arguments(case, **updates):
    return {key: value for key, value in dict(case, **updates).items()
            if not key.startswith("_")}


def response(path="tests/unit/test_generated.py", content=None, ambiguities=None):
    if content is None:
        content = "from Calculadora.calcula import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    return json.dumps({
        "files": [{"path": path, "content": content}],
        "ambiguities": [] if ambiguities is None else ambiguities,
    })


def provider(*_):
    return response()


def fails(code, function, *args, **kwargs):
    with pytest.raises(AgentError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def test_configuration_defaults_and_public_loader(case):
    config = load_config(case["config_path"])
    assert config["model"] == "gpt-4.1-mini"
    assert config["max_attempts"] == 2
    assert config["max_completion_tokens"] == 8000
    assert config["project_directory"] == "pilot"


@pytest.mark.parametrize("updates", [
    {"version": True}, {"version": 2}, {"language": "javascript"},
    {"python_version": "3.12"}, {"unknown": 1}, {"max_attempts": 4},
    {"max_attempts": 0}, {"max_files": True}, {"max_context_bytes": 1_000_001},
    {"max_file_bytes": 200_001}, {"max_output_bytes": 1_000_001},
    {"max_completion_tokens": 32_769}, {"source_paths": []},
    {"source_paths": "Calculadora"}, {"exclude_paths": "../private"},
    {"exclude_paths": ["../private"]}, {"exclude_paths": ["/absolute"]},
    {"install_command": "true\nEVIL=value"}, {"test_command": "pytest\rprintenv"},
    {"model": "model\nheader"}, {"test_command": 1},
])
def test_rejects_malformed_configuration(case, updates):
    settings(case, **updates)
    fails("invalid_config", load_config, case["config_path"])


@pytest.mark.parametrize("path", [
    "../outside", "/absolute", "tests/../escape", "tests//unit", ".git/tests",
    "tests\\unit", "tests/\nunit", "tests/./unit",
])
def test_rejects_configuration_traversal(case, path):
    settings(case, tests_directory=path)
    fails("invalid_path", load_config, case["config_path"])


def test_rejects_duplicate_config_keys(case):
    case["config_path"].write_text('{"version":1,"version":1}')
    fails("invalid_config", load_config, case["config_path"])


def test_rejects_config_inside_repository(case):
    config_path = put(case["repository"], "config.json", json.dumps(case["_config"]))
    fails("untrusted_config_path", generate,
          **arguments(case, config_path=config_path), provider=provider)


def test_generate_new_files_report_and_applicable_patch(case):
    original = git(case["repository"], "status", "--porcelain")
    report = generate(**arguments(case), provider=provider)
    assert report["status"] == "generated"
    assert report["changed_files"] == 2
    assert report["generated_files"] == 1
    assert report["ambiguities"] == 0
    assert all(type(value) in (str, int, bool) for value in report.values())
    assert json.loads((case["output"] / "report.json").read_text()) == report
    path = "pilot/tests/unit/test_generated.py"
    content = (case["output"] / "generated" / path).read_text()
    assert content == json.loads(response())["files"][0]["content"]
    patch = case["output"] / "proposed-tests.patch"
    git(case["repository"], "apply", "--check", str(patch))
    git(case["repository"], "apply", str(patch))
    assert (case["repository"] / path).read_text() == content
    assert original == ""


@pytest.mark.parametrize("content", [
    "def test_x():\n    assert True",
    "def test_x():\n    assert True\n",
    "def test_x():\n\f    assert True\n",
])
def test_patch_handles_last_newline_and_formfeed(case, content):
    generate(**arguments(case), provider=lambda *_: response(content=content))
    patch = case["output"] / "proposed-tests.patch"
    git(case["repository"], "apply", "--check", str(patch))
    git(case["repository"], "apply", str(patch))
    assert (case["_project"] / "tests/unit/test_generated.py").read_bytes() == content.encode()


def test_changed_selection_excludes_tests_non_sources_and_deleted(case):
    put(case["_project"], "tests/unit/test_new.py", "assert True\n")
    put(case["_project"], "unrelated.py", "assert True\n")
    put(case["_project"], "Calculadora/ignored.py", "assert True\n")
    (case["_project"] / "Calculadora/helpers.py").unlink()
    settings(case, exclude_paths=["**/ignored.py"])
    head = commit(case["repository"])
    captured = []

    def capture(messages, _):
        captured.append(json.loads(messages[1]["content"]))
        return response()

    report = generate(**arguments(case, head=head), provider=capture)
    assert report["changed_files"] == 1
    changed = [item["path"] for item in captured[0]["files"] if item["kind"] == "changed_source"]
    assert changed == ["Calculadora/calcula.py"]


def test_no_changed_sources_does_not_call_api_or_create_patch(case):
    put(case["_project"], "tests/unit/test_only.py", "assert True\n")
    head = commit(case["repository"])

    def no_provider(*_):
        pytest.fail("provider must not be called")

    report = generate(
        **arguments(case, base=case["head"], head=head), provider=no_provider,
    )
    assert report["status"] == "no_changes"
    assert report["generated_files"] == report["changed_files"] == 0
    assert sorted(path.name for path in case["output"].iterdir()) == ["report.json"]


def test_conventional_test_names_do_not_trigger_generation(case):
    settings(case, source_paths=["."])
    put(case["_project"], "odd/location/feature_test.py", "assert True\n")
    put(case["_project"], "test/feature.py", "assert True\n")
    head = commit(case["repository"])

    def no_provider(*_):
        pytest.fail("test files are not changed source inputs")

    report = generate(**arguments(
        case, base=case["head"], head=head,
    ), provider=no_provider)
    assert report["status"] == "no_changes"


def test_tests_directory_nested_under_sources_is_automatically_excluded(case):
    settings(
        case, tests_directory="Calculadora/tests",
        test_command="python -m pytest Calculadora/tests -q",
    )
    put(case["_project"], "Calculadora/tests/conftest.py", "fixture_value = 1\n")
    put(case["_project"], "Calculadora/tests/helpers.py", "value = 2\n")
    put(case["_project"], "Calculadora/tests/test_add.py", "assert True\n")
    head = commit(case["repository"])

    def no_provider(*_):
        pytest.fail("configured tests directory must never trigger generation")

    report = generate(**arguments(
        case, base=case["head"], head=head,
    ), provider=no_provider)
    assert report["status"] == "no_changes"
    assert report["changed_files"] == 0


def test_nested_pilot_tests_directory_receives_new_tests(case):
    settings(case, tests_directory="Calculadora/tests")
    report = generate(**arguments(case), provider=lambda *_: response(
        path="Calculadora/tests/test_generated.py",
    ))
    assert report["status"] == "generated"
    assert (
        case["output"] / "generated/pilot/Calculadora/tests/test_generated.py"
    ).is_file()
    git(case["repository"], "apply", "--check", str(case["output"] / "proposed-tests.patch"))


def test_context_contains_local_dependencies_and_existing_tests(case):
    # helpers is unchanged in this diff, so it must be discovered statically.
    put(case["_project"], "Calculadora/calcula.py",
        "from .helpers import identity\n\ndef add(a, b):\n    return identity(b + a)\n")
    head = commit(case["repository"])
    captured = []

    def capture(messages, config):
        assert "UNTRUSTED DATA" in messages[0]["content"]
        assert "install_command" not in messages[1]["content"]
        captured.append(json.loads(messages[1]["content"]))
        return response()

    generate(**arguments(case, base=case["head"], head=head), provider=capture)
    files = {item["path"]: item["kind"] for item in captured[0]["files"]}
    assert files["Calculadora/helpers.py"] == "local_dependency"
    assert files["tests/unit/test_existing.py"] == "existing_test"


def test_context_includes_contract_docs_within_shared_budget(case):
    put(case["_project"], "README.md", "The add function returns the sum of its two arguments.\n")
    put(case["_project"], "docs/calcula.md", "Contract: add(2, 3) returns 5.\n")
    captured = []

    def capture(messages, _):
        assert "Source implementation alone is NOT evidence" in messages[0]["content"]
        assert "corroborated" in messages[0]["content"]
        captured.append(json.loads(messages[1]["content"]))
        return response()

    report = generate(**arguments(case), provider=capture)
    files = {item["path"]: item for item in captured[0]["files"]}
    assert files["README.md"]["kind"] == "documentation"
    assert files["docs/calcula.md"]["kind"] == "documentation"
    assert report["context_bytes"] == sum(
        len(item["content"].encode()) for item in files.values()
    )


def test_documentation_does_not_bypass_file_or_total_limits(case):
    put(case["_project"], "README.md", "contract " * 1000)
    settings(case, max_context_bytes=200, max_file_bytes=80, max_files=3)
    captured = []

    def capture(messages, _):
        captured.append(json.loads(messages[1]["content"]))
        return response(content="assert True\n")

    report = generate(**arguments(case), provider=capture)
    assert report["context_bytes"] <= 200
    assert report["context_files"] <= 3
    assert all(len(item["content"].encode()) <= 80 for item in captured[0]["files"])
    assert report["context_truncated"]


def test_merge_base_selection_excludes_changes_unique_to_updated_base(case):
    common = put(case["_project"], "Calculadora/unchanged_on_head.py", "value = 1\n")
    merge_base = commit(case["repository"])
    common.write_text("value = 2\n")
    updated_base = commit(case["repository"])
    git(case["repository"], "checkout", "--detach", merge_base)
    put(case["_project"], "Calculadora/head_only.py", "value = 3\n")
    head = commit(case["repository"])
    captured = []

    def capture(messages, _):
        captured.append(json.loads(messages[1]["content"]))
        return response()

    report = generate(**arguments(
        case, base=updated_base, head=head,
    ), provider=capture)
    assert report["changed_files"] == 1
    changed = [item["path"] for item in captured[0]["files"] if item["kind"] == "changed_source"]
    assert changed == ["Calculadora/head_only.py"]


def test_target_code_is_not_imported_or_executed(case):
    marker = case["_workspace"] / "executed"
    put(case["_project"], "Calculadora/calcula.py",
        f"open({str(marker)!r}, 'w').write('executed')\n")
    head = commit(case["repository"])
    generate(**arguments(case, head=head), provider=provider)
    assert not marker.exists()


def test_context_and_file_limits_are_enforced(case):
    settings(case, max_context_bytes=30, max_file_bytes=20, max_files=1)
    captured = []

    def capture(messages, _):
        captured.append(json.loads(messages[1]["content"]))
        return response(content="assert True\n")

    report = generate(**arguments(case), provider=capture)
    assert report["context_files"] <= 1
    assert report["context_bytes"] <= 30
    assert report["context_truncated"]
    assert all(len(item["content"].encode()) <= 20 for item in captured[0]["files"])


@pytest.mark.parametrize("path", [
    "../test_escape.py", "/tests/unit/test_escape.py",
    "tests/unit/../../test_escape.py", "tests/unit/.git/test_escape.py",
    "tests/unit\\test_escape.py", "Calculadora/test_escape.py",
    "tests/unit/conftest.py", "tests/unit/not_a_test.py",
    "tests/unit/test_escape.py\nextra",
])
def test_invalid_proposal_paths_are_rejected(case, path):
    with pytest.raises(AgentError):
        generate(**arguments(case), provider=lambda *_: response(path=path))
    assert not case["output"].exists()


def test_never_replaces_existing_test(case):
    existing = case["_project"] / "tests/unit/test_existing.py"
    original = existing.read_bytes()
    fails("existing_file", generate, **arguments(case),
          provider=lambda *_: response(path="tests/unit/test_existing.py"))
    assert existing.read_bytes() == original
    assert not case["output"].exists()


@pytest.mark.parametrize("kind", ["source", "test_parent", "output", "config"])
def test_rejects_symlinks(case, kind):
    if kind == "source":
        source = case["_project"] / "Calculadora/calcula.py"
        source.unlink()
        source.symlink_to(case["_project"] / "Calculadora/helpers.py")
    elif kind == "test_parent":
        destination = case["_workspace"] / "external"
        destination.mkdir()
        shutil.rmtree(case["_project"] / "tests/unit")
        (case["_project"] / "tests/unit").symlink_to(destination, target_is_directory=True)
    elif kind == "output":
        destination = case["_workspace"] / "external"
        destination.mkdir()
        case["output"].symlink_to(destination, target_is_directory=True)
    else:
        destination = case["_workspace"] / "trusted.json"
        case["config_path"].rename(destination)
        case["config_path"].symlink_to(destination)
    fails("symlink_not_allowed", generate, **arguments(case), provider=provider)


def test_rejects_output_inside_target_or_ancestor(case):
    for output in (case["repository"] / "output", case["_workspace"]):
        fails("unsafe_output_path", generate,
              **arguments(case, output=output), provider=provider)


@pytest.mark.parametrize("payload", [
    "not json", "[]", '{"files":[]}', '{"files":[],"ambiguities":"maybe"}',
    '{"files":[],"ambiguities":[],"commands":["printenv"]}',
    '{"files":[],"files":[],"ambiguities":[]}',
    json.dumps({"files": [{"path": "tests/unit/test_bad.py", "content": ""}], "ambiguities": []}),
    json.dumps({"files": [{"path": "tests/unit/test_bad.py", "content": "bad python )"}], "ambiguities": []}),
    json.dumps({"files": [{"path": "tests/unit/test_bad.py", "content": "type Alias = int\n"}], "ambiguities": []}),
])
def test_malformed_proposals_are_rejected_atomically(case, payload):
    with pytest.raises(AgentError):
        generate(**arguments(case), provider=lambda *_: payload)
    assert not case["output"].exists()


def test_duplicate_proposed_files_are_rejected(case):
    payload = json.loads(response())
    payload["files"] *= 2
    fails("invalid_proposal", generate, **arguments(case),
          provider=lambda *_: json.dumps(payload))


def test_output_limits(case):
    settings(case, max_file_bytes=10)
    fails("output_limit", generate, **arguments(case), provider=provider)


def test_total_output_limit(case):
    settings(case, max_output_bytes=10)
    fails("output_limit", generate, **arguments(case), provider=provider)


def test_ambiguities_preserve_explanations_in_report_without_generating_files(case):
    report = generate(**arguments(case), provider=lambda *_: response(
        ambiguities=["<script>untrusted text including a pretend secret</script>"],
    ))
    assert report["status"] == "ambiguous"
    assert report["ambiguities"] == 1
    assert report["generated_files"] == 0
    assert sorted(path.name for path in case["output"].iterdir()) == ["report.json"]
    assert report["ambiguity_explanations"] == [
        "<script>untrusted text including a pretend secret</script>",
    ]
    assert json.loads((case["output"] / "report.json").read_text()) == report


def test_ambiguity_explanations_are_bounded(case):
    payload = json.loads(response())
    payload["files"] = []
    payload["ambiguities"] = ["é" * 1500] * 10
    report = generate(**arguments(case), provider=lambda *_: json.dumps(payload))
    explanations = report["ambiguity_explanations"]
    assert report["ambiguities"] == 10
    assert all(len(item.encode("utf-8")) <= 2000 for item in explanations)
    assert sum(len(item.encode("utf-8")) for item in explanations) <= 10_000


def test_ambiguity_cli_never_prints_model_explanations(case, monkeypatch, capsys):
    explanation = "<script>model text must stay out of logs</script>"
    monkeypatch.setattr(
        core, "request_completion",
        lambda *_: response(ambiguities=[explanation]),
    )
    code = main([
        "generate", "--repository", str(case["repository"]),
        "--config", str(case["config_path"]), "--base", case["base"],
        "--head", case["head"], "--output", str(case["output"]),
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert explanation not in captured.out + captured.err
    assert "ambiguity_explanations" not in captured.out
    assert json.loads(captured.out)["ambiguities"] == 1
    assert json.loads((case["output"] / "report.json").read_text())[
        "ambiguity_explanations"
    ] == [explanation]


def test_empty_proposal_is_ambiguous_not_success(case):
    report = generate(**arguments(case), provider=lambda *_: '{"files":[],"ambiguities":[]}')
    assert report["status"] == "ambiguous"
    assert report["ambiguities"] == 1


def test_retry_includes_bounded_feedback_and_previous_proposal(case):
    generate(**arguments(case), provider=provider)
    feedback = put(case["_workspace"], "feedback.txt", "AssertionError: mismatch\n" * 100)
    settings(case, max_context_bytes=300, max_file_bytes=100)
    captured = []

    def capture(messages, _):
        captured.append(json.loads(messages[1]["content"]))
        return response(content="def test_fixed():\n    assert 2 + 3 == 5\n")

    retry_output = case["_workspace"] / "second"
    report = generate(**arguments(
        case, output=retry_output, feedback=feedback, previous=case["output"],
    ), provider=capture)
    payload = captured[0]
    assert "AssertionError" in payload["test_feedback"]
    assert len(payload["test_feedback"].encode()) <= 100
    assert payload["previous_files"][0]["path"] == "tests/unit/test_generated.py"
    assert report["context_bytes"] <= 300
    assert report["context_truncated"]
    assert report["generated_files"] == 1
    assert not (case["_project"] / "tests/unit/test_generated.py").exists()


def test_refuses_reusing_output_directory(case):
    generate(**arguments(case), provider=provider)
    fails("output_already_exists", generate, **arguments(case), provider=provider)


def test_retry_preserves_source_context_with_single_file_budget(case):
    settings(case, max_files=1)
    feedback = put(case["_workspace"], "feedback.txt", "untrusted feedback")
    captured = []

    def capture(messages, _):
        captured.append(json.loads(messages[1]["content"]))
        return response()

    report = generate(**arguments(case, feedback=feedback), provider=capture)
    assert report["context_files"] == 1
    assert captured[0]["files"][0]["kind"] == "changed_source"
    assert "test_feedback" not in captured[0]


def test_retry_budget_includes_every_context_file(case):
    generate(**arguments(case), provider=provider)
    feedback = put(case["_workspace"], "feedback.txt", "failure")
    settings(case, max_files=2)
    captured = []

    def capture(messages, _):
        captured.append(json.loads(messages[1]["content"]))
        return response()

    report = generate(**arguments(
        case, output=case["_workspace"] / "second",
        feedback=feedback, previous=case["output"],
    ), provider=capture)
    assert report["context_files"] <= 2
    assert captured[0]["files"][0]["kind"] == "changed_source"
    assert not captured[0]["previous_files"]


def test_retry_rejects_feedback_symlink(case):
    target = put(case["_workspace"], "real-feedback.txt", "failure")
    link = case["_workspace"] / "feedback.txt"
    link.symlink_to(target)
    fails("symlink_not_allowed", generate, **arguments(case, feedback=link), provider=provider)


def test_retry_rejects_repository_feedback(case):
    feedback = put(case["repository"], "feedback.txt", "failure")
    fails("untrusted_feedback_path", generate, **arguments(case, feedback=feedback), provider=provider)


def test_dependency_symlink_is_not_read(case):
    put(case["_project"], "Calculadora/calcula.py", "from sensitive import value\n")
    head = commit(case["repository"])
    private = put(case["_workspace"], "private.py", "value = 'private'\n")
    (case["_project"] / "sensitive.py").symlink_to(private)
    fails("symlink_not_allowed", generate, **arguments(case, head=head), provider=provider)


def test_project_root_configuration_produces_root_relative_patch(case):
    settings(case, project_directory=".", source_paths=["pilot/Calculadora"],
             tests_directory="pilot/tests/unit")
    generate(**arguments(case), provider=lambda *_: response(
        path="pilot/tests/unit/test_generated.py",
    ))
    patch = case["output"] / "proposed-tests.patch"
    git(case["repository"], "apply", "--check", str(patch))
    assert (case["output"] / "generated/pilot/tests/unit/test_generated.py").is_file()


def test_invalid_revision_is_not_passed_to_git(case):
    fails("invalid_revision", generate, **arguments(case, base="--output=evil"), provider=provider)


def test_request_uses_fixed_endpoint_limits_and_no_redirects(case, monkeypatch):
    captured = {}
    config = load_config(case["config_path"])

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            captured["limit"] = limit
            return json.dumps({
                "choices": [{"finish_reason": "stop", "message": {"content": response()}}],
            }).encode()

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def build(*handlers):
        assert any(isinstance(handler, core._NoRedirect) for handler in handlers)
        return Opener()

    monkeypatch.setenv("UNIT_TEST_AGENT_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(core.urllib.request, "build_opener", build)
    assert core.request_completion([], config) == response()
    assert captured["request"].full_url == core.API_URL
    assert captured["timeout"] == 60
    body = json.loads(captured["request"].data)
    assert body["model"] == "gpt-4.1-mini"
    assert body["max_completion_tokens"] == config["max_completion_tokens"]
    assert captured["limit"] <= config["max_output_bytes"] * 8 + 65_537


def test_api_error_is_safe_and_does_not_include_secret(case, monkeypatch, capsys):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                core.API_URL, 429, "secret-key and provider response", {}, None,
            )

    monkeypatch.setenv("UNIT_TEST_AGENT_API_KEY", "secret-key")
    monkeypatch.setattr(core.urllib.request, "build_opener", lambda *_: Opener())
    code = main([
        "generate", "--repository", str(case["repository"]),
        "--config", str(case["config_path"]), "--base", case["base"],
        "--head", case["head"], "--output", str(case["output"]),
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "api_http_429" in captured.err
    assert "secret-key" not in captured.err + captured.out
    assert "provider response" not in captured.err + captured.out


def test_missing_key_is_safe(case, monkeypatch):
    monkeypatch.delenv("UNIT_TEST_AGENT_API_KEY", raising=False)
    fails("missing_api_key", core.request_completion, [], load_config(case["config_path"]))


@pytest.mark.parametrize("envelope, code", [
    ([], "invalid_api_response"),
    ({"choices": []}, "invalid_api_response"),
    ({"choices": [None]}, "invalid_api_response"),
    ({"choices": [{"message": {}}]}, "incomplete_api_response"),
    ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
     "incomplete_api_response"),
    ({"choices": [{"finish_reason": "stop", "message": {}}]}, "invalid_api_response"),
])
def test_malformed_provider_envelopes_are_safe(case, monkeypatch, envelope, code):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(envelope).encode()

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setenv("UNIT_TEST_AGENT_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(core.urllib.request, "build_opener", lambda *_: Opener())
    fails(code, core.request_completion, [], load_config(case["config_path"]))


def test_validate_config_cli(case, capsys):
    assert main(["validate-config", "--config", str(case["config_path"])]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "valid"}


def test_module_entrypoint_no_change(case):
    result = subprocess.run([
        os.sys.executable, "-m", "unit_test_agent", "generate",
        "--repository", str(case["repository"]), "--config", str(case["config_path"]),
        "--base", case["head"], "--head", case["head"], "--output", str(case["output"]),
    ], check=True, capture_output=True)
    assert json.loads(result.stdout)["status"] == "no_changes"
