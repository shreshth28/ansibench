import argparse
import sys

from ansibench import __version__, inventory


def cmd_hosts(args):
    hosts = inventory.select(inventory.load(args.inventory), args.pattern)
    width = max(len(h.name) for h in hosts)
    for h in hosts:
        target = f"{h.user}@{h.address}" if h.user else h.address
        groups = ",".join(h.groups) or "-"
        print(f"{h.name:<{width}}  {target:<30}  {groups}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="ansibench")
    p.add_argument("--version", action="version", version=f"ansibench {__version__}")
    sub = p.add_subparsers(dest="cmd")

    hp = sub.add_parser("hosts", help="list hosts from an inventory")
    hp.add_argument("pattern", nargs="?", help="host or group name (default: all)")
    hp.add_argument("-i", "--inventory", default="inventory.ini")
    hp.set_defaults(func=cmd_hosts)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except inventory.InventoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
