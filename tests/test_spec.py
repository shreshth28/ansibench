import pytest
import yaml

from ansibench import spec
from ansibench.cli import main


def test_minimal():
    s = spec.parse({"name": "x", "run": "./a"})
    assert s["repeat"] == 1 and s["warmup"] == 0 and s["build"] == [] and s["source"] is None
    job = spec.to_job(s)
    assert job["runs"] == [{"variant": "default", "cmd": "./a"}]


def test_build_string_or_list():
    assert spec.parse({"name": "x", "run": "a", "build": "make"})["build"] == ["make"]
    assert spec.parse({"name": "x", "run": "a", "build": ["a", "b"]})["build"] == ["a", "b"]


def test_path_is_relative_to_spec(tmp_path):
    (tmp_path / "code").mkdir()
    f = tmp_path / "b.yml"
    f.write_text("name: x\nrun: a\nsource: {path: code}\n")
    assert spec.load(f)["source"]["path"] == str((tmp_path / "code").resolve())


@pytest.mark.parametrize("data,err", [
    ({"run": "a"}, "needs a name"),
    ({"name": "x"}, "needs a run"),
    ({"name": "x", "run": "a", "nope": 1}, "unknown keys: nope"),
    ({"name": "x", "run": "a", "repeat": 0}, "repeat"),
    ({"name": "x", "run": "a", "timeout": -1}, "timeout"),
    ({"name": "x", "run": "a", "source": {"repo": "r", "path": "p"}}, "exactly one"),
    ({"name": "x", "run": "a", "build": [1]}, "build"),
])
def test_validation(data, err):
    with pytest.raises(spec.SpecError, match=err):
        spec.parse(data)


def test_bad_yaml(tmp_path):
    f = tmp_path / "b.yml"
    f.write_text("name: [\n")
    with pytest.raises(spec.SpecError, match="bad yaml"):
        spec.load(f)


def test_dump_roundtrip():
    s = spec.parse({"name": "x", "run": "a", "build": "make", "repeat": 3,
                    "source": {"repo": "r", "ref": "v1"}})
    assert spec.parse(yaml.safe_load(spec.dump(s))) == s


def test_run_needs_hosts(tmp_path, capsys):
    f = tmp_path / "b.yml"
    f.write_text("name: x\nrun: a\n")
    assert main(["run", str(f)]) == 2
    assert "no hosts" in capsys.readouterr().err


def test_exec_without_command(capsys):
    assert main(["exec", "somehost"]) == 2
    assert "nothing to run" in capsys.readouterr().err


def test_run_spec_on_target(target, tmp_path, capsys):
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "hello.sh").write_text("echo hello from spec\n")
    f = tmp_path / "b.yml"
    f.write_text("name: hello\nhosts: target\nsource: {path: code}\nrun: bash hello.sh\nrepeat: 2\n")
    res = tmp_path / "res"
    assert main(["run", str(f), "-i", str(target), "--results", str(res)]) == 0
    (d,) = list(res.iterdir())
    assert d.name.endswith("_hello")
    assert yaml.safe_load((d / "spec.yml").read_text())["repeat"] == 2
    assert "| target | default | 2 |" in capsys.readouterr().out


def test_exec_save_spec_then_run_it(target, tmp_path):
    f = tmp_path / "saved.yml"
    res = tmp_path / "res"
    assert main(["exec", "target", "-i", str(target), "--results", str(res), "-n", "2",
                 "--save-spec", str(f), "--", "echo", "hi there"]) == 0
    saved = yaml.safe_load(f.read_text())
    assert saved == {"name": "exec", "hosts": "target", "run": "echo 'hi there'", "repeat": 2}
    assert main(["run", str(f), "-i", str(target), "--results", str(res)]) == 0
