#!/usr/bin/env python3
"""Verify that a downloaded Actions artifact matches its shared lock metadata."""
import hashlib
import json
from pathlib import Path
import sys


def verify(directory):
    metadata = json.loads((directory / "metadata.json").read_text())
    lock = json.loads((directory / "geodata.lock.json").read_text())
    if metadata["lock"] != lock:
        raise ValueError("Artifact lock and metadata disagree")
    for kind in ("geosite", "geoip"):
        data = (directory / f"{kind}.dat").read_bytes()
        if not data or hashlib.sha256(data).hexdigest() != lock[kind]["sha256"]:
            raise ValueError(f"Artifact {kind}.dat checksum mismatch or empty file")
    return metadata


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
