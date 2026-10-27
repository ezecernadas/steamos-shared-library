"""Entry point for `python -m steamshare <command>`.

Each command lives in its own `cli` module and registers its subparser here,
so adding a command means adding one module and one line below.
"""

import argparse
import sys

from steamshare.cli import doctor, force_proton, install, keep_native, uninstall, watch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steamshare",
        description="Share one NTFS Steam library between Windows and Linux on a dual-boot PC.",
    )
    subcommands = parser.add_subparsers(title="commands", required=True)
    doctor.add_parser(subcommands)
    install.add_parser(subcommands)
    uninstall.add_parser(subcommands)
    force_proton.add_parser(subcommands)
    watch.add_parser(subcommands)
    keep_native.add_parser(subcommands)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    return arguments.run(arguments)


if __name__ == "__main__":
    sys.exit(main())
