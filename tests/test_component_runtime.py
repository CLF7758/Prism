from prism.component_runtime import invocation


def test_bundled_python_wins_over_missing_developer_home(tmp_path):
    runtime = tmp_path/'capture/runtime'
    runtime.mkdir(parents=True)
    (runtime/'pyvenv.cfg').write_text('home = Z:/missing/developer/python\n', encoding='utf-8')
    python = tmp_path/'python-base/python.exe'
    python.parent.mkdir()
    python.touch()
    program, args = invocation(runtime, tmp_path/'worker.py', ['hello'])
    assert program == str(python)
    assert args[-1] == 'hello'
    assert args[:2] == ['-I', '-S']
