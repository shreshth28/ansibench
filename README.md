# ansibench

Benchmark stuff on remote machines with Ansible. Point it at a box, tell it what to clone, how to build and what to run, and it does the boring parts: repeats, warmups, timeouts, pinning, putting the CPU in a known state, recording what the machine looked like, and giving you a table at the end.

I got the idea from benchmarking at ARM, where Ansible turned out to be a really good way to run the same benchmark the same way on a bunch of different machines. This is a smaller, general version of that for my own projects, e.g. comparing a plain CPU version of something against a SIMD or GPU version.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -e .
```

Targets just need SSH and `python3`. Machine tuning also needs root (passwordless sudo, or pass `-K`).

Make an inventory (see `inventory.example.ini`):

```ini
[gpu]
gpu-box ansible_host=10.0.0.12 ansible_user=me
```

```
ansibench hosts -i inventory.ini
```

## Quick: `exec`

Clone a repo on the box, build it, time a command:

```
ansibench exec gpu-box --repo https://github.com/me/thing --ref v1.2 \
    --build make -n 10 -w 2 -- ./bin/thing 2048
```

Here's what it prints (from the test container, trimmed to the table):

```
| host | variant | n | median | min | stdev | cv | maxrss |  |
|---|---|---|---|---|---|---|---|---|
| target | default | 10 | 655.22ms | 640.72ms | 12.52ms | 1.9% | 12.5MB |  |
```

Timing happens on the target (wall, user/sys, peak RSS via `wait4`), so SSH isn't in the numbers. `--path DIR` copies a local dir over instead of cloning. Other flags: `-t` timeout, `--perf cycles --perf instructions`, `--pin 2,3`, `--governor performance`, `--no-turbo`, `--no-smt`, `--drop-caches`. Add `--save-spec bench.yml` to turn it into a spec file.

## Spec files: `run`

For comparing versions of something over a few inputs:

```yaml
name: sorter
hosts: gpu
source:
  repo: https://github.com/me/sorter
  ref: main
variants:
  baseline: {build: make baseline, run: './bin/baseline {{ size }}'}
  simd:     {build: make simd,     run: './bin/simd {{ size }}'}
  gpu:      {build: make gpu,      run: './bin/gpu {{ size }}'}
matrix:
  size: [1000000, 10000000, 100000000]
metrics:
  throughput: 'items_per_s=([0-9.]+)'     # pulled out of stdout
perf_stat: [cycles, instructions]
repeat: 10
warmup: 2
timeout: 120
machine:
  cpu_governor: performance
  turbo: off
  smt: off
  pin_cores: [2, 3]
  drop_caches: true
  hugepages: 512
  gpu:
    persistence_mode: on
    lock_clocks_mhz: 1500
```

```
ansibench run sorter.yml -i inventory.ini
```

Quote anything with `{{ }}` in it, YAML gets confused otherwise.

## Machine settings

Everything under `machine:` gets recorded before the run and put back after, in an Ansible `always` block, so it gets restored even if the benchmark crashes. If a box doesn't support something (no cpufreq in a VM, no `nvidia-smi`, an SMT control the kernel won't let you touch), that setting is skipped and listed in the summary instead of failing the run.

One gap: if you Ctrl-C ansibench itself, Ansible might not get to the restore step. If that happens, run it again or fix the box by hand.

## Results

Each run gets a folder in `results/`:

```
results/2025-08-24T14-30-00_sorter/
  spec.yml        exactly what ran
  runs.csv        one row per run: timings, params, metrics, perf counters
  summary.md      the table, plus noise warnings and notes
  gpu-box/
    sysinfo.json  cpu, kernel, governor/turbo/smt, compilers, gpu + driver
    build.json    git sha + build exit codes
    results.json
    raw/          stdout/stderr of every run
```

Rows with more than 5% variation between runs get flagged as noisy.

## Tests

```
make test-target   # ubuntu container with sshd on localhost:2222
make test
```

The tests run real playbooks against the container over SSH. Docker can't change CPU frequency, so those parts are tested against a fake `/sys` tree (`ANSIBENCH_SYSROOT`) and a fake `nvidia-smi`. If Docker isn't running, the container tests skip.
