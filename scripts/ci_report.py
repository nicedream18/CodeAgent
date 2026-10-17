"""Run the same local/remote checks, retaining bounded sanitized evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from ci_support import child_environment, metadata, run_command, save_report, sha

REPOSITORY = Path(__file__).resolve().parents[1]
PLANNED = {
    "check": ["uv-version", "make-check", "commit-whitespace"],
    "docker": ["uv-version", "frozen-install", "export-execution-lock", "engine",
               "build-execution-image", "real-docker-tests"],
    "wheel": ["uv-version", "frozen-install", "export-runtime-lock", "build-wheel",
              "fresh-environment", "install-locked-dependencies", "install-wheel",
              "dependency-consistency", "cli-entry", "module-entry", "installed-offline-probe"],
}
PROBE = '''import importlib.metadata, json, os, socket, sys
from pathlib import Path
assert not any(os.getenv(k) for k in ('API_KEY', 'OPENAI_API_KEY', 'MODEL', 'BASE_URL'))
def denied(*args, **kwargs):
    raise AssertionError('Network access forbidden in the installed-wheel probe')
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
import mokioclaw
from mokioclaw.providers.token_estimate import encoding
from mokioclaw.core.state import RuntimeState
from mokioclaw.tools.file_tools import read_file
package = Path(mokioclaw.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve()), package
assert 'site-packages' in package.parts, package
assert not package.is_relative_to(Path(sys.argv[2]).resolve()), package
tokens = encoding().encode_ordinary('离线安装检查 hello')
assert tokens
assets = package.parent / 'evaluation/assets/baseline.tar.gz'
assert assets.is_file() and assets.stat().st_size > 0
workspace = Path.cwd() / 'workspace'
workspace.mkdir()
(workspace / 'sample.txt').write_text('安装验证成功', encoding='utf-8')
read = read_file(RuntimeState(workspace=workspace), 'sample.txt')
assert read['ok'] and '安装验证成功' in str(read)
value = {'ok': True, 'installed_from': str(package), 'prefix': sys.prefix,
 'python': sys.version, 'site_packages_verified': True, 'network_disabled': True,
 'sample_tokens': len(tokens), 'static_assets_verified': True,
 'credential_environment_empty': True,
 'packages': {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}}
Path(sys.argv[1]).write_text(json.dumps(value, indent=2) + '\\n')
'''


class Checks:
    def __init__(self, job: str, output: Path):
        if output.exists() and any(output.iterdir()):
            raise ValueError("Report directory must be new; existing evidence is never overwritten")
        output.mkdir(parents=True, exist_ok=True)
        self.output = output
        self.report: dict[str, Any] = {"schema_version": 1, "job": job, "status": "running",
                                       "metadata": metadata(REPOSITORY), "steps": [],
                                       "planned_steps": PLANNED[job]}
        self.report["metadata"]["uv"] = "unavailable"
        self.environment: dict[str, str] = {}
        save_report(output, self.report)

    def step(self, name: str, command: list[str], *, cwd: Path = REPOSITORY,
             timeout: float = 900) -> None:
        result = run_command(command, cwd=cwd, environment=self.environment,
                             log=self.output / (name + ".log"), timeout=timeout)
        self.report["steps"].append({"name": name, **result})
        save_report(self.output, self.report)
        if result["status"] != "passed":
            raise RuntimeError("Check failed: " + name)

    def pytest_receipt(self, path: Path) -> None:
        value = json.loads(path.read_text())
        if (value["session_token"] != self.environment["CI_REPORT_TOKEN"]
                or value["exit_code"] != 0 or not value["counts"]["passed"]):
            raise ValueError("Missing, stale or unsuccessful pytest receipt")
        self.report["pytest"] = value


def wheel(checks: Checks, work: Path) -> None:
    checks.step("export-runtime-lock", ["uv", "export", "--frozen", "--no-dev", "--no-emit-project",
                                       "--no-header", "--output-file", str(work / "runtime.txt")])
    checks.report["runtime_requirements_sha256"] = sha(work / "runtime.txt")
    checks.report["build_constraints_sha256"] = sha(REPOSITORY / "scripts/build-constraints.txt")
    checks.step("build-wheel", ["uv", "build", "--wheel", "--build-constraints",
                                "scripts/build-constraints.txt", "--out-dir", str(work / "wheel")])
    wheels = list((work / "wheel").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Exactly one freshly built wheel is required")
    artifact = wheels[0]
    checks.report["wheel"] = {"file": artifact.name, "sha256": sha(artifact),
                               "bytes": artifact.stat().st_size, "uploaded": False}
    checks.step("fresh-environment", ["uv", "venv", "--python", sys.executable, str(work / "venv")],
                cwd=work)
    python = work / "venv/bin/python"
    checks.step("install-locked-dependencies", ["uv", "pip", "install", "--python", str(python),
                                               "--require-hashes", "-r", str(work / "runtime.txt")],
                cwd=work)
    checks.step("install-wheel", ["uv", "pip", "install", "--python", str(python), "--no-deps",
                                  str(artifact)], cwd=work)
    checks.step("dependency-consistency", ["uv", "pip", "check", "--python", str(python)], cwd=work)
    # No editable path, PYTHONPATH, user config or provider credential survives.
    checks.environment.pop("PYTHONPATH", None)
    checks.environment.pop("PYTEST_PLUGINS", None)
    checks.environment.pop("VIRTUAL_ENV", None)
    checks.step("cli-entry", [str(work / "venv/bin/mokioclaw"), "--help"], cwd=work, timeout=60)
    checks.step("module-entry", [str(python), "-I", "-m", "mokioclaw", "--help"], cwd=work, timeout=60)
    (work / "probe.py").write_text(PROBE)
    checks.step("installed-offline-probe", [str(python), "-I", str(work / "probe.py"),
                                           str(work / "probe.json"), str(REPOSITORY)],
                cwd=work, timeout=60)
    checks.report["installed_probe"] = json.loads((work / "probe.json").read_text())


def docker(checks: Checks, work: Path) -> None:
    tag = "mokioclaw-ci:" + uuid4().hex[:16]
    export = work / "execution.txt"
    checks.step("export-execution-lock", ["uv", "export", "--frozen", "--no-emit-project",
                                         "--no-hashes", "--output-file", str(export)])

    def pins(path: Path) -> list[str]:
        return sorted(line.strip() for line in path.read_text().splitlines()
                      if line.strip() and not line.strip().startswith("#"))

    requirements = REPOSITORY / "docker/execution/requirements.txt"
    if pins(export) != pins(requirements):
        raise ValueError("Docker requirements differ from frozen uv.lock")
    checks.report["docker_build"] = {
        "dockerfile_sha256": sha(REPOSITORY / "docker/execution/Dockerfile"),
        "requirements_sha256": sha(requirements), "tag": tag, "published": False}
    checks.step("engine", ["docker", "info", "--format", "{{.ServerVersion}} {{.OSType}}/{{.Architecture}}"],
                timeout=30)
    checks.step("build-execution-image", ["docker", "build", "--iidfile", str(work / "image-id"),
                                         "-t", tag, "docker/execution"], timeout=900)
    image = (work / "image-id").read_text().strip()
    if not image.startswith("sha256:"):
        raise ValueError("Build produced no immutable image ID")
    checks.report["docker_build"]["image_id"] = image
    checks.environment["MOKIO_DOCKER_IMAGE"] = image
    checks.environment["MOKIO_DOCKER_REPORT"] = str(checks.output / "docker-detail")
    checks.step("real-docker-tests", ["make", "check-docker"], timeout=180)
    detail = json.loads((checks.output / "docker-detail/result.json").read_text())
    if not detail["ok"] or detail["image_digest"] != image:
        raise ValueError("Docker integration evidence does not match the freshly built image")
    checks.report["docker"] = detail


def run(job: str, output: Path) -> bool:
    checks = Checks(job, output.resolve())
    try:
        with TemporaryDirectory(prefix="mokio-ci-") as temporary:
            work = Path(temporary)
            (work / "home").mkdir()
            checks.environment = child_environment(work / "home", repository=REPOSITORY)
            checks.environment.update(CI_REPORT_TOKEN=uuid4().hex,
                                      CI_PYTEST_REPORT=str(checks.output / "pytest-summary.json"),
                                      PYTEST_PLUGINS="ci_pytest_plugin")
            checks.step("uv-version", ["uv", "--version"], timeout=15)
            checks.report["metadata"]["uv"] = checks.report["steps"][-1]["tail"].strip()
            if job == "check":
                checks.step("make-check", ["make", "check"], timeout=900)
                checks.pytest_receipt(checks.output / "pytest-summary.json")
                checks.step("commit-whitespace", ["git", "show", "--format=", "--check"], timeout=30)
            else:
                checks.step("frozen-install", ["uv", "sync", "--frozen"], timeout=600)
                if job == "wheel":
                    wheel(checks, work)
                else:
                    docker(checks, work)
            checks.report["status"] = "passed"
    except Exception as exc:
        checks.report["status"] = "failed"
        # No exception string: a transport or index error can contain credentials.
        checks.report["error"] = {"type": type(exc).__name__,
                                   "message": "See sanitized step logs and explicit exit codes"}
    finally:
        save_report(checks.output, checks.report)
    return bool(checks.report["status"] == "passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", choices=["check", "docker", "wheel"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not shutil.which("uv"):
        raise SystemExit("uv is required")
    success = run(args.job, args.output)
    print(json.dumps({"job": args.job, "status": "passed" if success else "failed",
                      "report": str(args.output / "report.json")}))
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
