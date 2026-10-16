"""Network-enabled preparation only. Model and test execution remain offline."""

import json
import subprocess

from mokioclaw.evaluation.prepare import ASSETS, cache_root

root = cache_root() / "image-builds"
root.mkdir(parents=True, exist_ok=True)
receipts = {}
bases = json.loads((ASSETS / "image-bases.json").read_text())
for _version, profiles in [("3.11", ["flask20", "flask23"]), ("3.9", ["requests210"])]:
    tag = bases[profiles[0]]
    subprocess.run(["docker", "pull", tag], check=True)
    image = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
    pinned = image["RepoDigests"][0]
    for profile in profiles:
        directory = root / profile
        directory.mkdir(exist_ok=True)
        (directory / "requirements.txt").write_bytes(
            (ASSETS / ("requirements-" + profile + ".txt")).read_bytes()
        )
        dockerfile = (
            "FROM "
            + pinned
            + "\nCOPY requirements.txt /opt/requirements.txt\nRUN pip install --no-cache-dir --no-deps -r /opt/requirements.txt\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
        )
        (directory / "Dockerfile").write_text(dockerfile)
        name = "mokioclaw-" + profile + ":internship-v1"
        subprocess.run(["docker", "build", "-t", name, str(directory)], check=True)
        receipts[profile] = {
            "base": pinned,
            "image": json.loads(subprocess.check_output(["docker", "image", "inspect", name]))[0][
                "Id"
            ],
            "dockerfile": dockerfile,
        }
(root / "images.json").write_text(json.dumps(receipts, indent=2) + "\n")
