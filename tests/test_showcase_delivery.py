"""Showcase readers must remain read-only and reject unverifiable claims."""

import hashlib
import importlib.util
import io
import json
import socket
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checks = load("check_showcase")
reader = load("inspect_showcase_case")


def test_real_case_inspection_never_launches_process_or_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Archive inspection must be read-only")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    files = [ROOT / path / name for path in reader.CASES.values()
             for name in ("result.json", "trace.tar.gz")]
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
    success = reader.inspect_case(ROOT, "d01")
    failure = reader.inspect_case(ROOT, "verifier-budget")
    assert success["execution_status"] == "finished" and success["task_solved"]
    assert failure["execution_status"] == "failed" and not failure["task_solved"]
    assert failure["artifact_passed"] and failure["tests_run"] == 13
    assert failure["last_streams"][-1]["node"] == "verifier"
    assert any(item["stop_reason"] == "token_budget_exhausted"
               for item in failure["last_milestones"])
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]


@pytest.mark.parametrize("names", [[], ["a", "b"]])
def test_missing_or_ambiguous_trace_is_rejected(tmp_path, names):
    path = tmp_path / "case.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name in names:
            member = tarfile.TarInfo(f".mokioclaw/traces/{name}/events.jsonl")
            data = b'{"seq": 1}\n'
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="exactly one"):
        reader.read_trace(path)


def test_oversized_trace_is_rejected_before_json_read(tmp_path, monkeypatch):
    path = tmp_path / "case.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(".mokioclaw/traces/a/events.jsonl")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"oops"))
    monkeypatch.setattr(reader, "MAX_TRACE_BYTES", 3)
    with pytest.raises(ValueError, match="bounded"):
        reader.read_trace(path)


def test_mismatched_terminal_cannot_be_presented_as_verified(tmp_path, monkeypatch):
    directory = tmp_path / reader.CASES["d01"]
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps({"run_id": "r", "execution_status": "finished"}))
    monkeypatch.setattr(reader, "read_trace", lambda _: ("trace", [
        {"type": "run_end", "run_id": "r", "payload": {"status": "failed"}},
    ]))
    with pytest.raises(ValueError, match="status mismatch"):
        reader.inspect_case(tmp_path, "d01")


def test_fact_checker_rejects_changed_evidence_and_boolean_number_confusion(tmp_path):
    folder = tmp_path / checks.SHOWCASE
    folder.mkdir(parents=True)
    evidence = tmp_path / "receipt.json"
    evidence.write_text('{"solved": true}')
    fact = {"id": "F", "claim": "claim", "version": "v", "scope": "s", "limitation": "l",
            "evidence": [{"path": "receipt.json", "pointer": "/solved", "expected": True,
                          "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]}

    def save():
        (folder / "facts.json").write_text(json.dumps({"facts": [fact]}))
        (folder / "FACTS.md").write_text(checks.render_facts([fact]))

    save()
    assert not checks.check_facts(tmp_path)[1]
    fact["evidence"][0]["expected"] = 1
    save()
    assert "differs from claim" in checks.check_facts(tmp_path)[1][0]
    evidence.write_text('{"solved": false}')
    assert "hash changed" in checks.check_facts(tmp_path)[1][0]


def test_local_links_check_missing_lines_and_escape_without_fetching_urls(tmp_path):
    document = tmp_path / "page.md"
    (tmp_path / "code.py").write_text("pass\n")
    document.write_text(
        "[ok](code.py#L1) [bad](code.py#L2) [missing](absent.md) "
        "[escape](../outside) [url](https://example.invalid/unavailable)"
    )
    count, errors = checks.check_links(tmp_path, [document])
    assert count == 4 and len(errors) == 3


def test_json_pointer_supports_escaped_keys_and_array_indices():
    assert checks.pointer_value({"a/b": [{"~": None}]}, "/a~1b/0/~0") is None
    with pytest.raises(ValueError):
        checks.pointer_value({}, "missing-slash")
