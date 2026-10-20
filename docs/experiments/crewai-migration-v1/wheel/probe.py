import importlib.metadata, json, os, socket, sys
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
from mokioclaw.orchestration.runtime import single_step_workflow
flow = single_step_workflow("probe", lambda _: {"final_answer": "offline-flow-ok"})
assert flow.backend == "crewai"
assert flow.invoke({})["final_answer"] == "offline-flow-ok"
value = {"crewai_flow_executed": True, 'ok': True, 'installed_from': str(package), 'prefix': sys.prefix,
 'python': sys.version, 'site_packages_verified': True, 'network_disabled': True,
 'sample_tokens': len(tokens), 'static_assets_verified': True,
 'credential_environment_empty': True,
 'packages': {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}}
Path(sys.argv[1]).write_text(json.dumps(value, indent=2) + '\n')
