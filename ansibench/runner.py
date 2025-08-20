"""Gets copied to the target and run there with the system python3.
Stdlib only, keep it that way.

    python3 runner.py build job.json
    python3 runner.py bench job.json
"""
import json
import os
import re
import shlex
import shutil
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


def grab_metrics(stdout_path, metrics):
    """last match of each regex in stdout -> float (None if it never showed up)"""
    if not metrics:
        return {}
    with open(stdout_path, errors="replace") as f:
        text = f.read()
    out = {}
    for name, rx in metrics.items():
        found = re.findall(rx, text)
        val = None
        if found:
            last = found[-1]
            last = last[0] if isinstance(last, tuple) else last
            try:
                val = float(last)
            except ValueError:
                pass
        out[name] = val
    return out


def perf_wrap(cmd, events, out_file):
    # perf's own overhead ends up in wall/user/sys too, it's small but it's there
    return f"perf stat -x, -o {shlex.quote(out_file)} -e {shlex.quote(','.join(events))} -- bash -c {shlex.quote(cmd)}"


def parse_perf(path, events):
    """perf stat -x, output: value,unit,event,... one per line, # lines are comments"""
    vals = {e: None for e in events}
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        return vals
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        value, event = parts[0], parts[2]
        name = next((e for e in events if event == e or event.startswith(e + ":")), None)
        if name is None:
            continue
        try:
            vals[name] = float(value)
        except ValueError:
            vals[name] = None  # <not counted> / <not supported>
    return vals


def note(out, msg):
    path = os.path.join(out, "notes.json")
    notes = json.load(open(path)) if os.path.exists(path) else []
    if msg not in notes:
        notes.append(msg)
    with open(path, "w") as f:
        json.dump(notes, f, indent=2)


def cmd_bench(job, workdir):
    src = os.path.join(workdir, "src")
    out = os.path.join(workdir, "out")
    raw = os.path.join(out, "raw")
    os.makedirs(raw, exist_ok=True)

    repeat = job.get("repeat", 1)
    warmup = job.get("warmup", 0)
    timeout = job.get("timeout")
    perf_events = job.get("perf_stat") or []
    if perf_events and not shutil.which("perf"):
        note(out, "perf_stat skipped: perf isn't installed on this host")
        perf_events = []

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
            cmd = r["cmd"]
            perf_out = os.path.join(raw, f"{tag}.rep{rep}.perf")
            if perf_events:
                cmd = perf_wrap(cmd, perf_events, perf_out)
            rec.update(timed(cmd, src, stdout, stderr, timeout))
            rec["metrics"] = grab_metrics(stdout, job.get("metrics"))
            if perf_events:
                rec["perf"] = parse_perf(perf_out, perf_events)
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
