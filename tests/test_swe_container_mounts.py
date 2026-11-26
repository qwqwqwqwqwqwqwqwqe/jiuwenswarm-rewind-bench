from pathlib import Path
from types import SimpleNamespace

import pytest

from bench_tools.swebench_runner import run_case


@pytest.mark.parametrize('legacy_backend', [None, Path('/legacy/backend')])
def test_swe_container_mounts_optional_legacy_backend(tmp_path, monkeypatch, legacy_backend):
    commands = []
    monkeypatch.setattr(run_case, 'read_json', lambda _: {'status': 'prepared'})
    monkeypatch.setattr(run_case, 'run_command', lambda command, **_: (
        commands.append(command) or SimpleNamespace(returncode=0)))
    layout = SimpleNamespace(state=tmp_path / 'state', shared=tmp_path / 'shared',
                             runtime=tmp_path / 'runtime')
    run_case.start_agent_container(layout, image='example', container_name='test',
                                   backend_root=legacy_backend, jiuwen_port=28092)

    mounts = [arg for arg in commands[0] if ':/benchrunner-backend/' in arg]
    expected = ['runners', 'llm_forward', 'utils', 'schemas'] if legacy_backend else []
    assert [mount.split(':/benchrunner-backend/')[1].split(':')[0] for mount in mounts] == expected
    assert commands[0][-3:] == ['example', '/bin/sleep', 'infinity']
