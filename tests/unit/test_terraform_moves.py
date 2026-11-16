"""The interceptor and verifier resources lost their count when legacy mode and the verify
switch were removed. Every one of them must have a moved block from its old [0] address,
or an existing deployment would destroy and recreate it (a new interceptor ARN is drift)."""

import re
from pathlib import Path

TERRAFORM = Path(__file__).resolve().parents[2] / "terraform"
FILES = ("interceptor.tf", "verify.tf")
RESOURCE = re.compile(r'^resource "([a-z0-9_]+)" "([a-z0-9_]+)" \{\n(.*?)^\}', re.M | re.S)
MOVED = re.compile(r"^moved \{\n\s*from = ([^\n]+)\n\s*to\s*= ([^\n]+)\n\}", re.M)


def resources(text: str) -> dict[str, str]:
    return {f"{kind}.{name}": body for kind, name, body in RESOURCE.findall(text)}


def moves(text: str) -> dict[str, str]:
    return {source.strip(): target.strip() for source, target in MOVED.findall(text)}


def test_every_resource_without_count_has_a_move_from_its_old_address():
    for name in FILES:
        text = (TERRAFORM / name).read_text()
        single = {
            a for a, body in resources(text).items() if not re.search(r"^\s+count\s", body, re.M)
        }
        expected = {f"{address}[0]": address for address in single}
        assert moves(text) == expected, name


def test_the_verifier_alarms_keep_their_count():
    text = (TERRAFORM / "verify.tf").read_text()
    alarms = {
        a: b for a, b in resources(text).items() if a.startswith("aws_cloudwatch_metric_alarm.")
    }
    assert len(alarms) == 4
    assert all("count = local.verifier_alarm_count" in body for body in alarms.values())


def test_nothing_indexes_the_moved_resources():
    moved = set()
    for name in FILES:
        moved |= set(moves((TERRAFORM / name).read_text()).values())
    for path in sorted(TERRAFORM.glob("*.tf")):
        text = path.read_text()
        for line in text.splitlines():
            if line.strip().startswith("from ="):
                continue
            for address in moved:
                assert not re.search(rf"\b{re.escape(address)}(\[|\[\*\])", line), (path.name, line)


def test_the_removed_switches_are_only_deprecated_variables():
    for path in sorted(TERRAFORM.glob("*.tf")):
        text = path.read_text()
        for switch in ("caller_interceptor_enabled", "verify_enabled"):
            uses = re.findall(rf"var\.{switch}\b", text)
            if path.name == "main.tf":
                # The deprecation check only.
                assert len(uses) == 1, (path.name, switch)
            else:
                assert uses == [], (path.name, switch)
