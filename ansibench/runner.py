"""Gets copied to the target and run there with the system python3.
Stdlib only, keep it that way.

    python3 runner.py build job.json
"""
import json
import os
import subprocess
import sys


def sh(cmd, cwd, log):
    with open(log, "w") as f:
        return subprocess.run(["bash", "-c", cmd], cwd=cwd, stdout=f, stderr=subprocess.STDOUT).returncode


def git_sha(src):
    p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def cmd_build(job, workdir):
    src = os.path.join(workdir, "src")
    out = os.path.join(workdir, "out")
    os.makedirs(out, exist_ok=True)

    builds = []
    for i, cmd in enumerate(job.get("builds", [])):
        log = os.path.join(out, f"build{i}.log")
        rc = sh(cmd, src, log)
        builds.append({"cmd": cmd, "exit_code": rc, "log": os.path.basename(log)})
        if rc != 0:
            break

    info = {"git_sha": git_sha(src), "builds": builds}
    with open(os.path.join(out, "build.json"), "w") as f:
        json.dump(info, f, indent=2)
    return 0 if all(b["exit_code"] == 0 for b in builds) else 1


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    action, job_path = argv[1], argv[2]
    with open(job_path) as f:
        job = json.load(f)
    workdir = os.path.dirname(os.path.abspath(job_path))
    if action == "build":
        return cmd_build(job, workdir)
    print(f"unknown action {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
