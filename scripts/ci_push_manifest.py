"""Review the complete unpublished Git range. Never pushes or triggers workflows."""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from ci_support import credential_values, redact, write_json
from dotenv import dotenv_values

REPOSITORY = Path(__file__).resolve().parents[1]
MAX_BLOB = 32 * 1024 * 1024
MAX_ARCHIVE_TOTAL = 64 * 1024 * 1024
PATTERNS = [rb"sk-[A-Za-z0-9_-]{24,}", rb"gh[pousr]_[A-Za-z0-9]{30,}",
            rb"-----BEGIN (?:OPENSSH|RSA|EC|DSA|ENCRYPTED) PRIVATE KEY-----"]


def inspect_bytes(data: bytes, name: str, known: list[bytes], *, depth: int = 0) -> list[dict[str, str]]:
    reasons = []
    if any(secret and secret in data for secret in known):
        reasons.append("configured_credential_value")
    if any(re.search(pattern, data) for pattern in PATTERNS):
        reasons.append("credential_or_private_key_pattern")
    if Path(name).name == ".env":
        reasons.append("tracked_dotenv")
    findings = [{"path": name, "reason": reason} for reason in reasons]
    if name.endswith((".tar.gz", ".tgz")):
        if depth >= 2:
            return findings + [{"path": name, "reason": "archive_depth_not_scanned"}]
        total = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for index, member in enumerate(archive):
                    if not member.isfile():
                        continue
                    total += member.size
                    if member.size > MAX_BLOB or total > MAX_ARCHIVE_TOTAL or index >= 2000:
                        findings.append({"path": name, "reason": "archive_quota_not_scanned"})
                        break
                    handle = archive.extractfile(member)
                    if handle is not None:
                        findings.extend(inspect_bytes(handle.read(MAX_BLOB + 1),
                                                       name + ":" + member.name, known, depth=depth + 1))
                    if len(findings) >= 100:
                        break
        except (tarfile.TarError, OSError):
            findings.append({"path": name, "reason": "archive_unreadable"})
    return findings


def manifest(base: str, branch: str) -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-c", "core.quotePath=false", *args], cwd=REPOSITORY)

    subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"], cwd=REPOSITORY, check=True)
    head = git("rev-parse", "HEAD").decode().strip()
    values = dotenv_values(REPOSITORY / ".env")
    known = [v.encode() for v in credential_values(values)]
    commits = []
    findings: list[dict[str, str]] = []
    for row in git("log", "--reverse", "--format=%H %s", base + "..HEAD").decode().splitlines():
        revision, subject = row.split(" ", 1)
        issues = inspect_bytes(git("show", "-s", "--format=%B", revision), "commit:" + revision, known)
        findings.extend(issues)
        commits.append({"sha": revision, "subject": "[credential review required]" if issues else subject})
    objects = {}
    for line in git("rev-list", "--objects", base + "..HEAD").decode().splitlines():
        object_id, _, name = line.partition(" ")
        objects[object_id] = name
    blob_count, blob_bytes = 0, 0
    with subprocess.Popen(["git", "cat-file", "--batch"], cwd=REPOSITORY,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        assert process.stdin and process.stdout
        for object_id, name in objects.items():
            process.stdin.write((object_id + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            size = int(header[2])
            if size > MAX_BLOB:
                remaining = size
                while remaining:
                    remaining -= len(process.stdout.read(min(65536, remaining)))
                findings.append({"path": name, "reason": "large_object_not_scanned"})
                process.stdout.read(1)
                continue
            payload = process.stdout.read(size)
            process.stdout.read(1)
            if header[1] == "blob":
                blob_count += 1
                blob_bytes += size
                findings.extend(inspect_bytes(payload, name, known))
        process.stdin.close()
    changes = git("diff", "--name-status", "--no-renames", "-z", base, "HEAD").decode().split("\0")
    files = [{"status": changes[i], "path": changes[i + 1]} for i in range(0, len(changes) - 1, 2)]
    return {"schema_version": 1, "remote_authorized": False,
            "repository": "https://github.com/nicedream18/CodeAgent", "visibility": "public",
            "remote": "origin", "target_branch": branch, "base": base, "head": head,
            "commits": commits, "changed_files": files, "commit_count": len(commits),
            "changed_file_count": len(files), "unpublished_objects": len(objects),
            "scanned_blobs": blob_count, "scanned_blob_bytes": blob_bytes,
            "credential_findings": findings, "credential_scan_passed": not findings,
            "scan_scope": "All unpublished blobs and commit messages, known project credential values, common key/private-key patterns, bounded nested tar archives. Not a general DLP guarantee.",
            "history_included": ["Prior 20 unpublished commits at Goal 3 start", "Goal 1/2 and historical experiment archives, traces and absolute local paths", "All new Goal 3 commits listed here"],
            "push_command": ["git", "push", "origin", f"{head}:refs/heads/{branch}"],
            "workflow": ".github/workflows/check.yml", "trigger": "push (automatic; do not duplicate with dispatch)",
            "manual_inputs": {}, "artifacts": {"jobs": ["check", "docker", "wheel"],
                "contents": ["report.json", "summary.md", "bounded sanitized logs", "minimal pytest receipts", "Docker image/build and wheel/install metadata"],
                "retention_days": 14, "max_bytes_per_job": 16 * 1024 * 1024,
                "wheel_binary_uploaded": False, "container_image_published": False},
            "no_remote_write_performed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = manifest(args.base, args.branch)
    write_json(args.output / "remote-review.json", value)
    text = ["# Proposed remote operation — authorization required", "",
            f"Repository: {value['repository']} ({value['visibility']})", f"Branch: {args.branch}",
            f"Range: {args.base}..{value['head']}",
            f"Commits: {value['commit_count']}; changed paths: {value['changed_file_count']}",
            f"Credential scan passed: {value['credential_scan_passed']}", "",
            "Includes all unpublished history and historical experiment/trace archives.",
            "Push automatically starts all three jobs; artifacts are retained for 14 days.", "",
            "## Commits", "", *[c['sha'] + " " + c['subject'] for c in value['commits']], "",
            "## Changed paths", "", *[f['status'] + " " + f['path'] for f in value['changed_files']]]
    (args.output / "REMOTE_REVIEW.md").write_text(redact("\n".join(text) + "\n", []))
    print(json.dumps({"commits": value['commit_count'], "files": value['changed_file_count'],
                      "scan_passed": value['credential_scan_passed'],
                      "findings": len(value['credential_findings']), "remote_write": False}))
