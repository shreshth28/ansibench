import argparse
import sys

from ansibench import __version__


def build_parser():
    p = argparse.ArgumentParser(prog="ansibench")
    p.add_argument("--version", action="version", version=f"ansibench {__version__}")
    p.add_subparsers(dest="cmd")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
