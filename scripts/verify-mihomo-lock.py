#!/usr/bin/env python3
"""Verify generated file sizes and hashes against the compact Mihomo lock."""
import hashlib
import json
from pathlib import Path
import re
import sys


def artifact_path(identifier):
    if identifier == "rule-providers.yaml":
        return identifier
    parts = identifier.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid Mihomo lock entry: {identifier}")
    kind, name = parts[:2]
    if kind not in ("geosite", "geoip") or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*(?:@[a-z0-9][a-z0-9_-]*)?", name):
        raise ValueError(f"Invalid Mihomo lock entry: {identifier}")
    if len(parts) == 3:
        if kind != "geosite" or parts[2] != "extra":
            raise ValueError(f"Invalid Mihomo lock entry: {identifier}")
        return f"classical/{name}-extra.list"
    return f"{kind}/{name}.mrs"


def verify(directory):
    lock = json.loads((directory / "mihomo.lock.json").read_text())
    listed = set()
    for identifier, expected in lock.items():
        relative = artifact_path(identifier)
        listed.add(relative)
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Mihomo lock path escapes output directory")
        if not path.is_file():
            raise ValueError(f"Missing Mihomo artifact: {relative}")
        data = path.read_bytes()
        if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError(f"Mihomo artifact mismatch: {relative}")
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    if actual != listed | {"mihomo.lock.json"}:
        raise ValueError("Unlisted or missing Mihomo artifact files")


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
