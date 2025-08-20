import argparse
import json
import shlex
import sys
from pathlib import Path

from ansibench import __version__, engine, inventory, spec as specmod


def cmd_hosts(args):
    hosts = inventory.select(inventory.load(args.inventory), args.pattern)
    width = max(len(h.name) for h in hosts)
    for h in hosts:
        target = f"{h.user}@{h.address}" if h.user else h.address
        groups = ",".join(h.groups) or "-"
        print(f"{h.name:<{width}}  {target:<30}  {groups}")
    return 0


def command_from_args(words):
    if not words:
        return None
    # one arg = treat it as a shell string, so `-- "make run | tee log"` works
    return words[0] if len(words) == 1 else shlex.join(words)


def spec_from_exec_args(args):
    if args.repo and args.path:
        raise specmod.SpecError("--repo and --path don't go together")
    source = None
    if args.repo:
        source = {"repo": args.repo, "ref": args.ref}
    elif args.path:
        source = {"path": str(Path(args.path).resolve())}
    cmd = command_from_args(args.command)
    if not cmd:
        raise specmod.SpecError("nothing to run, put the command after --")
    return {
        "name": args.name or "exec",
        "hosts": args.pattern,
        "source": source,
        "build": args.build or [],
        "variants": {"default": {"run": cmd, "build": []}},
        "matrix": {},
        "metrics": {},
        "repeat": args.repeat,
        "warmup": args.warmup,
        "timeout": args.timeout,
    }


def bench(spec, inventory_path, pattern, args):
    hosts = inventory.select(inventory.load(inventory_path), pattern)
    job = specmod.to_job(spec)
    results = engine.run(
        job, inventory_path, ",".join(h.name for h in hosts),
        source=spec["source"], results_root=args.results, verbose=args.verbose,
        save={"spec.yml": specmod.dump(spec)},
    )
    for h in hosts:
        info = json.loads((results / h.name / "build.json").read_text())
        sha = (info["git_sha"] or "-")[:10]
        ok = "ok" if all(b["exit_code"] == 0 for b in info["builds"]) else "FAILED"
        print(f"{h.name}: sha {sha}, build {ok}")
    print()
    print((results / "summary.md").read_text().split("\n", 2)[2].rstrip())
    print()
    print(f"results: {results}")
    return 0


def cmd_exec(args):
    spec = spec_from_exec_args(args)
    if args.save_spec:
        Path(args.save_spec).write_text(specmod.dump(spec))
        print(f"wrote {args.save_spec}")
    return bench(spec, args.inventory, args.pattern, args)


def cmd_run(args):
    spec = specmod.load(args.spec)
    pattern = args.limit or spec["hosts"]
    if not pattern:
        raise specmod.SpecError("no hosts: set `hosts:` in the spec or pass --limit")
    return bench(spec, args.inventory, pattern, args)


def add_common(p):
    p.add_argument("-i", "--inventory", default="inventory.ini")
    p.add_argument("--results", default="results", help="where results go (default: ./results)")
    p.add_argument("-v", "--verbose", action="store_true", help="show ansible output")


def build_parser():
    p = argparse.ArgumentParser(prog="ansibench")
    p.add_argument("--version", action="version", version=f"ansibench {__version__}")
    sub = p.add_subparsers(dest="cmd")

    hp = sub.add_parser("hosts", help="list hosts from an inventory")
    hp.add_argument("pattern", nargs="?", help="host or group name (default: all)")
    hp.add_argument("-i", "--inventory", default="inventory.ini")
    hp.set_defaults(func=cmd_hosts)

    ep = sub.add_parser("exec", help="quick bench: get code onto a host, build, time a command",
                        usage="ansibench exec HOST [options] -- COMMAND ...")
    ep.add_argument("pattern", help="host or group name")
    ep.add_argument("--repo", help="git url to clone on the target")
    ep.add_argument("--ref", default="", help="branch, tag or sha (default: remote HEAD)")
    ep.add_argument("--path", help="local dir to copy over instead of cloning")
    ep.add_argument("--build", action="append", help="build command, can repeat")
    ep.add_argument("-n", "--repeat", type=int, default=1, help="measured runs (default 1)")
    ep.add_argument("-w", "--warmup", type=int, default=0, help="unmeasured runs first (default 0)")
    ep.add_argument("-t", "--timeout", type=float, help="kill a run after this many seconds")
    ep.add_argument("--name", help="name for the results dir")
    ep.add_argument("--save-spec", metavar="FILE", help="also write this as a spec file")
    add_common(ep)
    ep.set_defaults(func=cmd_exec)

    rp = sub.add_parser("run", help="run a spec file")
    rp.add_argument("spec", help="path to a spec .yml")
    rp.add_argument("-l", "--limit", help="host or group (overrides hosts: in the spec)")
    add_common(rp)
    rp.set_defaults(func=cmd_run)

    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # everything after -- is the command to bench. split it off by hand,
    # argparse.REMAINDER eats options that come before it
    command = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    args.command = command
    if not args.cmd:
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except (inventory.InventoryError, engine.RunError, specmod.SpecError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
