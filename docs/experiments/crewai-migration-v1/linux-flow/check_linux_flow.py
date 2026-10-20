"""Exercise the migrated Flow on the newly built Linux image, offline."""
import json
import shutil
import sys
import zipfile
from pathlib import Path

root = Path('/private/tmp/mokio-crewai-YJSObx')
repo = Path('/Users/nicedream/xm/MokioAgent')
sys.path.insert(0, str(repo / 'src'))
from mokioclaw.core.execution_backend import DockerBackend, ExecutionRequest, docker_cli

work = root / 'linux-flow-workspace'
assert not work.exists()
work.mkdir()
with zipfile.ZipFile(root / 'wheel/mokioclaw-0.2.0-py3-none-any.whl') as archive:
    for name in archive.namelist():
        if name.startswith('mokioclaw/') and not name.endswith('/'):
            target = work / 'src' / name
            assert target.resolve().is_relative_to(work)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
(work / 'tests').mkdir()
shutil.copyfile(repo / 'tests/test_crewai_orchestration.py', work / 'tests/test_crewai_orchestration.py')
image = (root / 'crewai-image-id').read_text().strip()
owner = 'crewai-migration-linux-flow'
result = DockerBackend(image).run(ExecutionRequest(
    workspace=work,
    command='PYTHONPATH=src PYTHON_DOTENV_DISABLED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_crewai_orchestration.py',
    timeout_seconds=120,
    max_output_chars=12000,
    owner=owner,
)).as_dict()
result['remaining_owned_containers'] = docker_cli(['ps', '-aq', '--filter', 'label=mokioclaw.owner=' + owner]).stdout.strip().split()
(root / 'linux-flow-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'ok': result['ok'], 'stdout': result['stdout'], 'stderr': result['stderr'], 'cleanup_confirmed': not result['remaining_owned_containers']}))
assert result['ok'] and '11 passed' in result['stdout'] and not result['remaining_owned_containers']
