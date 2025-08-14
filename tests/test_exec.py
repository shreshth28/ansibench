import json
import subprocess

import pytest

from ansibench.cli import main

SSH = ["ssh", "-i", "testenv/.keys/id_ed25519", "-p", "2222", "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "bench@127.0.0.1"]


def on_target(cmd):
    return subprocess.run(SSH + [cmd], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="session")
def repo(target):
    """small git repo living on the target, so cloning doesn't need the internet"""
    on_target(
        "set -e; rm -rf /tmp/proj /tmp/proj.git; mkdir /tmp/proj; cd /tmp/proj; git init -q -b main;"
        "git config user.email t@t; git config user.name t;"
        "printf 'hi:\\n\\techo built > out.txt\\n' > Makefile; git add .; git commit -qm one;"
        "git tag v1; echo '# two' >> Makefile; git commit -qam two;"
        "git clone -q --bare /tmp/proj /tmp/proj.git"
    )
    shas = on_target("cd /tmp/proj && git rev-parse v1 HEAD").split()
    return {"url": "file:///tmp/proj.git", "v1": shas[0], "head": shas[1]}


def build_info(results_dir):
    return json.loads((results_dir / "target" / "build.json").read_text())


def latest(tmp_path):
    (d,) = list(tmp_path.iterdir())
    return d


def test_exec_clones_head_and_builds(target, repo, tmp_path, capsys):
    rc = main(["exec", "target", "-i", str(target), "--repo", repo["url"],
               "--build", "make hi", "--results", str(tmp_path)])
    assert rc == 0
    info = build_info(latest(tmp_path))
    assert info["git_sha"] == repo["head"]
    assert info["builds"][0]["exit_code"] == 0
    assert "build ok" in capsys.readouterr().out


def test_exec_clones_at_ref(target, repo, tmp_path):
    rc = main(["exec", "target", "-i", str(target), "--repo", repo["url"], "--ref", "v1",
               "--results", str(tmp_path)])
    assert rc == 0
    assert build_info(latest(tmp_path))["git_sha"] == repo["v1"]


def test_exec_copies_local_path(target, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "build.sh").write_text("echo local-build-ran\n")
    out = tmp_path / "res"
    assert main(["exec", "target", "-i", str(target), "--path", str(src),
                 "--build", "bash build.sh", "--results", str(out)]) == 0
    d = latest(out)
    assert build_info(d)["git_sha"] is None
    assert "local-build-ran" in (d / "target" / "build0.log").read_text()


def test_exec_build_failure(target, tmp_path, capsys):
    rc = main(["exec", "target", "-i", str(target), "--build", "exit 3", "--results", str(tmp_path)])
    assert rc == 2
    assert "ansible-playbook failed" in capsys.readouterr().err
    # results still come back so you can look at the log
    assert build_info(latest(tmp_path))["builds"][0]["exit_code"] == 3
