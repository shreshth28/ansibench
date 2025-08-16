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


def results_of(results_dir):
    return json.loads((results_dir / "target" / "results.json").read_text())


def test_exec_times_the_command(target, tmp_path):
    burn = "python3 -c 'import time\nt=time.time()\nwhile time.time()-t<0.5: pass\nprint(\"done\")'"
    assert main(["exec", "target", "-i", str(target), "--results", str(tmp_path), "--", burn]) == 0
    d = latest(tmp_path)
    (r,) = results_of(d)
    assert r["exit_code"] == 0
    assert r["wall_s"] >= 0.5
    assert r["user_s"] > 0.3  # it's a busy loop, should be almost all user time
    assert (d / "target" / r["stdout"]).read_text().strip() == "done"


def test_exec_maxrss(target, tmp_path):
    hog = "python3 -c 'b = bytearray(200 * 1024 * 1024); b[::4096] = b\"x\" * len(b[::4096])'"
    assert main(["exec", "target", "-i", str(target), "--results", str(tmp_path), "--", hog]) == 0
    (r,) = results_of(latest(tmp_path))
    assert r["maxrss_kb"] > 200 * 1024


def test_exec_nonzero_exit_is_recorded_not_fatal(target, tmp_path, capsys):
    assert main(["exec", "target", "-i", str(target), "--results", str(tmp_path),
                 "--", "echo oops >&2; exit 7"]) == 0
    d = latest(tmp_path)
    (r,) = results_of(d)
    assert r["exit_code"] == 7
    assert (d / "target" / r["stderr"]).read_text().strip() == "oops"
    assert "exit 7" in capsys.readouterr().out


def test_exec_multi_word_command(target, tmp_path):
    assert main(["exec", "target", "-i", str(target), "--results", str(tmp_path),
                 "--", "echo", "two words", "x"]) == 0
    d = latest(tmp_path)
    (r,) = results_of(d)
    assert (d / "target" / r["stdout"]).read_text() == "two words x\n"


def test_exec_repeat_warmup_and_csv(target, tmp_path):
    assert main(["exec", "target", "-i", str(target), "--results", str(tmp_path),
                 "-n", "4", "-w", "1", "--", "true"]) == 0
    d = latest(tmp_path)
    assert len(results_of(d)) == 4
    lines = (d / "runs.csv").read_text().splitlines()
    assert lines[0].startswith("host,variant,rep,wall_s")
    assert len(lines) == 5 and lines[1].startswith("target,default,0,")


def test_exec_timeout(target, tmp_path, capsys):
    assert main(["exec", "target", "-i", str(target), "--results", str(tmp_path),
                 "-t", "1", "--", "sleep 60"]) == 0
    (r,) = results_of(latest(tmp_path))
    assert r["timed_out"] and r["wall_s"] < 5
    assert "TIMEOUT" in capsys.readouterr().out
