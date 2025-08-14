import argparse
import json
import sys

from ansibench import __version__, engine, inventory


def cmd_hosts(args):
    hosts = inventory.select(inventory.load(args.inventory), args.pattern)
    width = max(len(h.name) for h in hosts)
    for h in hosts:
        target = f"{h.user}@{h.address}" if h.user else h.address
        groups = ",".join(h.groups) or "-"
        print(f"{h.name:<{width}}  {target:<30}  {groups}")
    return 0


def source_from_args(args):
    if args.repo and args.path:
        raise SystemExit("error: --repo and --path don't go together")
    if args.repo:
        return {"repo": args.repo, "ref": args.ref}
    if args.path:
        return {"path": args.path}
    return None


def cmd_exec(args):
    hosts = inventory.select(inventory.load(args.inventory), args.pattern)
    job = {
        "name": args.name or "exec",
        "builds": args.build or [],
    }
    results = engine.run(
        job, args.inventory, ",".join(h.name for h in hosts),
        source=source_from_args(args), results_root=args.results, verbose=args.verbose,
    )
    for h in hosts:
        info = json.loads((results / h.name / "build.json").read_text())
        sha = (info["git_sha"] or "-")[:10]
        ok = "ok" if all(b["exit_code"] == 0 for b in info["builds"]) else "FAILED"
        print(f"{h.name}: sha {sha}, build {ok}")
    print(f"results: {results}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="ansibench")
    p.add_argument("--version", action="version", version=f"ansibench {__version__}")
    sub = p.add_subparsers(dest="cmd")

    hp = sub.add_parser("hosts", help="list hosts from an inventory")
    hp.add_argument("pattern", nargs="?", help="host or group name (default: all)")
    hp.add_argument("-i", "--inventory", default="inventory.ini")
    hp.set_defaults(func=cmd_hosts)

    ep = sub.add_parser("exec", help="clone/copy code to a host, build it")
    ep.add_argument("pattern", help="host or group name")
    ep.add_argument("-i", "--inventory", default="inventory.ini")
    ep.add_argument("--repo", help="git url to clone on the target")
    ep.add_argument("--ref", default="", help="branch, tag or sha (default: remote HEAD)")
    ep.add_argument("--path", help="local dir to copy over instead of cloning")
    ep.add_argument("--build", action="append", help="build command, can repeat")
    ep.add_argument("--name", help="name for the results dir")
    ep.add_argument("--results", default="results", help="where results go (default: ./results)")
    ep.add_argument("-v", "--verbose", action="store_true", help="show ansible output")
    ep.set_defaults(func=cmd_exec)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except (inventory.InventoryError, engine.RunError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
