#!/usr/bin/env python3
"""List, add or remove kube-handrail change freezes.

The freezes are one SSM String parameter, a JSON list of windows (mcp/CONTRACT.md, Change
freezes). Every write is validated with the same code the Lambdas use, so a value they would
refuse is never stored. Requests use your own AWS credentials (AWS_PROFILE, AWS_REGION, ...).

  freeze.py list
  freeze.py add --namespace NS [--namespace NS ...] --until 2026-12-27T00:00:00Z
                --reason TEXT [--start 2026-12-23T00:00:00Z] [--action scale ...]
  freeze.py remove INDEX [INDEX ...]
  freeze.py remove --expired
  freeze.py remove --all

--parameter defaults to FREEZES_PARAM, then to the freezes_parameter Terraform output. Times
are RFC 3339 in UTC; --start defaults to now. A window includes its start and excludes its
until. --namespace '*' freezes every namespace. INDEX is the number `list` shows.

SSM has no conditional write: two operators editing at the same moment can lose one change.
Run `list` after a change to see what is stored.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "mcp" / "common"))

from handrail_common.freezes import (  # noqa: E402
    ACTIONS,
    Freeze,
    FreezeConfigError,
    dump_freezes,
    format_time,
    parse_entry,
    parse_freezes,
)


def parameter_name(explicit: str | None, env: dict[str, str]) -> str:
    if explicit:
        return explicit
    if env.get("FREEZES_PARAM"):
        return env["FREEZES_PARAM"]
    try:
        out = subprocess.run(
            [
                "terraform",
                f"-chdir={REPO_ROOT / 'terraform'}",
                "output",
                "-raw",
                "freezes_parameter",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        out = ""
    if not out.startswith("/"):
        sys.exit("cannot find the parameter; pass --parameter or set FREEZES_PARAM")
    return out


# A standard SSM String parameter holds at most 4096 bytes.
MAX_VALUE_BYTES = 4096


def read(ssm: Any, name: str) -> list[Freeze]:
    try:
        raw = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        sys.exit(f"parameter {name} not found; check --parameter or FREEZES_PARAM")
    try:
        return parse_freezes(raw)
    except FreezeConfigError as exc:
        sys.exit(
            f"the stored freezes are not valid ({exc}); every write is refused until "
            "they are fixed. `remove --all` resets them to []."
        )


def write(ssm: Any, name: str, freezes: list[Freeze]) -> None:
    value = dump_freezes(freezes)
    # Re-parsed, as the Lambdas will: refuse before storing anything they would refuse.
    parse_freezes(value)
    if len(value.encode()) > MAX_VALUE_BYTES:
        raise FreezeConfigError(
            f"the freezes would take {len(value.encode())} bytes, over the parameter's "
            f"{MAX_VALUE_BYTES}; remove expired freezes or shorten reasons"
        )
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


def state(freeze: Freeze, now: int) -> str:
    if freeze.active(now):
        return "active"
    return "upcoming" if now < freeze.start else "expired"


def describe(index: int, freeze: Freeze, now: int) -> str:
    entry = freeze.to_json()
    actions = ",".join(entry.get("actions", ["all"]))
    return (
        f"{index}  {state(freeze, now):8}  {entry['start']} -> {entry['until']}  "
        f"namespaces={','.join(freeze.namespaces)}  actions={actions}  reason={freeze.reason}"
    )


def cmd_list(ssm: Any, name: str, now: int) -> None:
    freezes = read(ssm, name)
    if not freezes:
        print("no freezes")
    for i, freeze in enumerate(freezes):
        print(describe(i, freeze, now))


def cmd_add(ssm: Any, name: str, args: argparse.Namespace, now: int) -> None:
    entry: dict[str, Any] = {
        "namespaces": args.namespace,
        "start": args.start or format_time(now),
        "until": args.until,
        "reason": args.reason,
    }
    if args.action:
        entry["actions"] = args.action
    try:
        freeze = parse_entry(entry)
    except FreezeConfigError as exc:
        sys.exit(f"invalid freeze: {exc}")
    if freeze.until <= now:
        sys.exit("until is in the past: the freeze would never be active")
    freezes = [*read(ssm, name), freeze]
    try:
        write(ssm, name, freezes)
    except FreezeConfigError as exc:
        sys.exit(f"cannot store the freezes: {exc}")
    print("added:", describe(len(freezes) - 1, freeze, now))


def cmd_remove(ssm: Any, name: str, args: argparse.Namespace, now: int) -> None:
    if args.all:
        ssm.put_parameter(Name=name, Value="[]", Type="String", Overwrite=True)
        print("removed every freeze")
        return
    freezes = read(ssm, name)
    if args.expired:
        drop = {i for i, f in enumerate(freezes) if f.until <= now}
    else:
        drop = set(args.index)
        unknown = sorted(i for i in drop if not 0 <= i < len(freezes))
        if unknown:
            sys.exit(f"no freeze with index {', '.join(map(str, unknown))}; see `list`")
    try:
        write(ssm, name, [f for i, f in enumerate(freezes) if i not in drop])
    except FreezeConfigError as exc:
        sys.exit(f"cannot store the freezes: {exc}")
    for i in sorted(drop):
        print("removed:", describe(i, freezes[i], now))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--parameter", help="the freezes SSM parameter name")
    p.add_argument("--region", help="AWS Region (default: your AWS configuration)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every freeze with its index and state")
    add = sub.add_parser("add", help="add a freeze")
    add.add_argument("--namespace", action="append", required=True, help="repeatable; '*' for all")
    add.add_argument("--start", help="RFC 3339 UTC, default now")
    add.add_argument("--until", required=True, help="RFC 3339 UTC, excluded")
    add.add_argument("--reason", required=True, help="shown to agents and in refusals")
    add.add_argument("--action", action="append", choices=sorted(ACTIONS), help="default: all")
    remove = sub.add_parser("remove", help="remove freezes")
    remove.add_argument("index", nargs="*", type=int, help="indexes from `list`")
    remove.add_argument("--expired", action="store_true", help="drop windows that have ended")
    remove.add_argument("--all", action="store_true", help="reset to []")
    return p


def main(argv: list[str] | None = None, ssm: Any = None, now: int | None = None) -> None:
    args = parser().parse_args(argv)
    if args.command == "remove" and sum((bool(args.index), args.expired, args.all)) != 1:
        sys.exit("remove: give indexes, --expired or --all (one of them)")
    name = parameter_name(args.parameter, dict(os.environ))
    if ssm is None:
        import boto3

        ssm = boto3.client("ssm", region_name=args.region)
    now = int(time.time()) if now is None else now
    if args.command == "list":
        cmd_list(ssm, name, now)
    elif args.command == "add":
        cmd_add(ssm, name, args, now)
    else:
        cmd_remove(ssm, name, args, now)


if __name__ == "__main__":
    main()
