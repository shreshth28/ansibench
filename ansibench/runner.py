"""Gets copied to the target and run there with the system python3.
Stdlib only, keep it that way.

    python3 runner.py build job.json
    python3 runner.py bench job.json
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time


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


def maxrss_kb(ru):
    # linux reports KB, macOS bytes
    return ru.ru_maxrss // 1024 if sys.platform == "darwin" else ru.ru_maxrss


def kill_group(p, fired):
    fired.set()
    try:
        # whole process group, otherwise `bash -c "a | b"` leaves b running
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def timed(cmd, cwd, stdout_path, stderr_path, timeout=None):
    fired = threading.Event()
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        t0 = time.perf_counter()
        p = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, stdout=out, stderr=err, start_new_session=True)
        timer = threading.Timer(timeout, kill_group, (p, fired)) if timeout else None
        if timer:
            timer.start()
        # wait4 instead of p.wait() so we get rusage for just this child
        _, status, ru = os.wait4(p.pid, 0)
        wall = time.perf_counter() - t0
        if timer:
            timer.cancel()
    p.returncode = os.waitstatus_to_exitcode(status)
    return {
        "wall_s": round(wall, 6),
        "user_s": round(ru.ru_utime, 6),
        "sys_s": round(ru.ru_stime, 6),
        "maxrss_kb": maxrss_kb(ru),
        "exit_code": p.returncode,
        "timed_out": fired.is_set(),
    }


def cmd_bench(job, workdir):
    src = os.path.join(workdir, "src")
    out = os.path.join(workdir, "out")
    raw = os.path.join(out, "raw")
    os.makedirs(raw, exist_ok=True)

    repeat = job.get("repeat", 1)
    warmup = job.get("warmup", 0)
    timeout = job.get("timeout")

    records = []
    for i, r in enumerate(job.get("runs", [])):
        tag = f"{i:03d}_{r.get('variant', 'default')}"
        for w in range(warmup):
            # warmups aren't recorded, output kept around in case they blow up
            timed(r["cmd"], src, os.path.join(raw, f"{tag}.warmup{w}.stdout"),
                  os.path.join(raw, f"{tag}.warmup{w}.stderr"), timeout)
        for rep in range(repeat):
            stdout = os.path.join(raw, f"{tag}.rep{rep}.stdout")
            stderr = os.path.join(raw, f"{tag}.rep{rep}.stderr")
            rec = {"variant": r.get("variant", "default"), "params": r.get("params", {}),
                   "rep": rep, "cmd": r["cmd"]}
            rec.update(timed(r["cmd"], src, stdout, stderr, timeout))
            rec["stdout"] = os.path.relpath(stdout, out)
            rec["stderr"] = os.path.relpath(stderr, out)
            records.append(rec)

    with open(os.path.join(out, "results.json"), "w") as f:
        json.dump(records, f, indent=2)
    return 0


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
    if action == "bench":
        return cmd_bench(job, workdir)
    print(f"unknown action {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
