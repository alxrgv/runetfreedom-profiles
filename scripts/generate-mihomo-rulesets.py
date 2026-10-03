#!/usr/bin/env python3
"""Orchestrate upstream conversion and check source/export/decoded MRS parity."""
import argparse
import base64
from collections import Counter
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def selection(config, kind):
    pattern = r"[a-z0-9][a-z0-9_-]*(?:@[a-z0-9][a-z0-9_-]*)?" if kind == "geosite" else r"[a-z0-9][a-z0-9_-]*"
    names = config[kind]
    if not isinstance(names, list) or not all(isinstance(name, str) and re.fullmatch(pattern, name) for name in names):
        raise ValueError(f"Invalid {kind} category list")
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate {kind} category")
    return names


def categories(value):
    """Accept comma-separated names or a JSON array, including an empty selection."""
    if value.lstrip().startswith("["):
        try:
            names = json.loads(value)
        except json.JSONDecodeError as error:
            raise argparse.ArgumentTypeError("Invalid JSON category array") from error
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise argparse.ArgumentTypeError("Category array must contain strings")
        return names
    return [name.strip() for name in value.split(",")] if value.strip() else []


def command(*args):
    result = subprocess.run([str(arg) for arg in args], text=True, encoding="utf-8", capture_output=True)
    output = result.stdout + result.stderr
    if result.returncode:
        raise ValueError(f"{args[0]} failed ({result.returncode}):\n{output}")
    return output


def lines(path):
    if not path.is_file():
        raise ValueError(f"Missing converter output: {path}")
    return path.read_text(encoding="utf-8").splitlines()


def site_expectations(entry):
    types = {"Plain": "DOMAIN-KEYWORD", "Regex": "DOMAIN-REGEX", "Domain": "DOMAIN-SUFFIX", "Full": "DOMAIN"}
    stat_names = {"Plain": "keyword", "Regex": "regexp", "Domain": "domain", "Full": "full"}
    rules, domains, stats = [], [], dict.fromkeys(stat_names.values(), 0)
    for item in entry["domain"]:
        kind, value = item.get("type", "Plain"), item.get("value", "")
        if kind not in types or not value or any(char in value for char in (",", "\n", "\r")):
            raise ValueError(f"Unrepresentable geosite selector: {item}")
        if kind in ("Domain", "Full"):
            if any(char in value for char in ("*", "+", "/")) or value.startswith(".") or value.endswith("."):
                raise ValueError(f"Domain has incompatible MRS semantics: {value}")
            domains.append(("+." if kind == "Domain" else "") + value)
        rules.append(types[kind] + "," + value)
        stats[stat_names[kind]] += 1
    if not rules:
        raise ValueError("Empty geosite category")
    return rules, domains, stats


def canonical_domains(values):
    # MRS can deduplicate entries covered by suffixes; compare the matching union.
    values = {value.lower() for value in values}
    suffixes = {value[2:] for value in values if value.startswith("+.")}
    result = set()
    for value in values:
        suffix = value.startswith("+.")
        domain = value[2:] if suffix else value
        labels = domain.split(".")
        if any(".".join(labels[index:]) in suffixes for index in range(1, len(labels))):
            continue
        if not suffix and domain in suffixes:
            continue
        result.add(value)
    return result


def canonical_networks(values):
    networks = [ipaddress.ip_network(value, strict=False) for value in values]
    return {str(network) for version in (4, 6) for network in
            ipaddress.collapse_addresses(n for n in networks if n.version == version)}


def ip_expectations(entry):
    if entry.get("reverseMatch", False):
        raise ValueError("Inverse GeoIP categories are unsupported")
    values = []
    for item in entry["cidr"]:
        address = ipaddress.ip_address(base64.b64decode(item["ip"], validate=True))
        values.append(str(ipaddress.ip_network((address, item.get("prefix", 0)), strict=False)))
    if not values:
        raise ValueError("Empty GeoIP category")
    return values


def size_warnings(files, previous):
    old_files, warnings = previous, []
    for name, info in files.items():
        old_size = old_files.get(name, {}).get("size", 0)
        if old_size and info["size"] >= old_size * 5:
            warnings.append(f"{name} grew from {old_size} to {info['size']} bytes (at least 5x)")
    old_total = sum(info["size"] for info in old_files.values())
    total = sum(info["size"] for info in files.values())
    if old_total and total >= old_total * 3:
        warnings.append(f"Total grew from {old_total} to {total} bytes (at least 3x)")
    return warnings


def generate(args):
    config = {"geosite": args.geosite, "geoip": args.geoip}
    for kind in ("geosite", "geoip"):
        selection(config, kind)
    if not any(config.values()):
        raise ValueError("Select at least one geosite or geoip category")
    previous = read_json(args.output / "mihomo.lock.json") if (args.output / "mihomo.lock.json").exists() else {}
    lock = {}
    version = command(args.mihomo, "-v")
    if args.mihomo_version not in version.split():
        raise ValueError(f"Mihomo version does not match pinned tools: {version}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mihomo-build-", dir=args.output.parent) as directory:
        work = Path(directory).resolve()
        # Keep the Go audit's file interface; this temporary input is never published.
        audit_config = work / "selection.json"
        audit_config.write_text(json.dumps(config), encoding="utf-8")
        audit = json.loads(command(args.audit, audit_config, args.geosite_dat.resolve(), args.geoip_dat.resolve()))
        converted, staged = work / "converted", work / "output"
        staged.mkdir()
        providers, local_providers, validation_rules = {}, {}, []

        def add_provider(identifier, relative, behavior, format):
            providers[identifier] = {"type": "http", "behavior": behavior, "format": format,
                "url": args.base_url.rstrip("/") + "/" + relative, "path": "./rulesets/" + relative, "interval": 21600}
            local_providers[identifier] = {"type": "file", "behavior": behavior, "format": format,
                "path": str(staged / relative)}
            validation_rules.append(f"RULE-SET,{identifier},DIRECT" + (",no-resolve" if behavior == "ipcidr" else ""))

        for kind, source in (("geosite", args.geosite_dat), ("geoip", args.geoip_dat)):
            if not config[kind]:
                continue
            # Upstream converts all categories. Only selected outputs are published.
            log = command(args.converter, kind, "-f", source.resolve(), "-o", converted / kind)
            (work / f"{kind}-conversion.log").write_text(log, encoding="utf-8")
            for name in config[kind]:
                source_entry, source_dir = audit[kind][name], converted / kind
                relative, mrs = f"{kind}/{name}.mrs", source_dir / f"{name}.mrs"
                if kind == "geosite":
                    expected, domains, _ = site_expectations(source_entry)
                    if Counter(lines(source_dir / "classical" / f"{name}.list")) != Counter(expected):
                        raise ValueError(f"{name}: source/classical selector parity failed")
                    if Counter(lines(source_dir / f"{name}.list")) != Counter(domains):
                        raise ValueError(f"{name}: source/domain text parity failed")
                    extras = sorted(set(rule for rule in expected if rule.startswith(("DOMAIN-KEYWORD,", "DOMAIN-REGEX,"))))
                    behavior = "domain"
                    # Keep the provider stable when upstream adds or removes extra selectors.
                    extra_relative = f"classical/{name}-extra.list"
                    extra_path = staged / extra_relative
                    extra_path.parent.mkdir(parents=True, exist_ok=True)
                    extra_text = "\n".join(extras) + "\n" if extras else "# No keyword or regex rules in this category.\n"
                    extra_path.write_text(extra_text, encoding="utf-8", newline="\n")
                    add_provider(f"geosite-{name}-extra", extra_relative, "classical", "text")
                    if not domains:
                        print(f"geosite:{name}: {len(extras)} extra rules, parity=passed")
                        continue
                else:
                    domains = ip_expectations(source_entry)
                    if Counter(str(ipaddress.ip_network(value, strict=False)) for value in lines(source_dir / f"{name}.list")) != Counter(domains):
                        raise ValueError(f"{name}: source/IP text parity failed")
                    behavior = "ipcidr"
                if not mrs.is_file() or not mrs.stat().st_size:
                    raise ValueError(f"Missing or empty selected MRS: {mrs}")
                decoded = work / "decoded.list"
                command(args.mihomo, "convert-ruleset", behavior, "mrs", mrs, decoded)
                decoded_values = lines(decoded)
                canonical = canonical_domains if kind == "geosite" else canonical_networks
                if canonical(domains) != canonical(decoded_values):
                    raise ValueError(f"{name}: decoded MRS semantic parity failed")
                print(f"{kind}:{name}: {len(decoded_values)} MRS rules, parity=passed")
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(mrs, target)
                add_provider(f"{kind}-{name}", relative, behavior, "mrs")
        # JSON is valid YAML and quotes all values without a YAML dependency.
        (staged / "rule-providers.yaml").write_text(json.dumps({"rule-providers": providers}, indent=2) + "\n", encoding="utf-8", newline="\n")
        validation_config = work / "validation.yaml"
        validation_config.write_text(json.dumps({"mode": "rule", "dns": {"enable": False},
            "rule-providers": local_providers, "rules": validation_rules + ["MATCH,DIRECT"]}), encoding="utf-8")
        validation_log = command(args.mihomo, "-t", "-d", work, "-f", validation_config)
        if re.search(r"level=(warning|error|fatal)|invalid |parse classical rule", validation_log, re.IGNORECASE):
            raise ValueError(f"Mihomo reported rejected rules: {validation_log}")
        print(validation_log.strip())
        for path in sorted(staged.rglob("*")):
            if path.is_file():
                relative = path.relative_to(staged)
                if relative.parent.name == "classical":
                    identifier = f"geosite:{relative.stem.removesuffix('-extra')}:extra"
                else:
                    identifier = f"{relative.parent.name}:{relative.stem}" if len(relative.parts) == 2 else relative.name
                lock[identifier] = {"size": path.stat().st_size, "sha256": sha256(path)}
        for warning in size_warnings(lock, previous):
            print(f"::warning::{warning}")
        (staged / "mihomo.lock.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")
        # Replace generated files only after every check succeeds.
        args.output.mkdir(parents=True, exist_ok=True)
        desired = set()
        for path in staged.rglob("*"):
            if path.is_file():
                relative_path = path.relative_to(staged)
                desired.add(relative_path.as_posix())
                target = args.output / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        for kind in ("geosite", "geoip", "classical"):
            for stale in (args.output / kind).glob("*"):
                if stale.is_file() and stale.suffix in (".yaml", ".mrs", ".list") and stale.relative_to(args.output).as_posix() not in desired:
                    stale.unlink()
        # Remove the retired metadata file when upgrading an existing output directory.
        (args.output / "manifest.json").unlink(missing_ok=True)
    summary = ["| File | KiB |", "| --- | ---: |"]
    for name, info in lock.items():
        summary.append(f"| {name} | {info['size'] / 1024:.2f} |")
    total = sum(info["size"] for info in lock.values())
    summary.append(f"| **TOTAL** | **{total / 1024:.2f}** |\n\nTotal: {total / 1048576:.3f} MiB")
    print("\n".join(summary))
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(summary) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("geosite", "geoip"):
        parser.add_argument("--" + name, type=categories, default="", metavar="CATEGORIES",
                            help="Comma-separated names or a JSON array; omit or pass [] to select none")
    parser.add_argument("--mihomo-version", required=True)
    for name in ("geosite-dat", "geoip-dat", "converter", "audit", "mihomo"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("MIHOMO"))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    for name in ("converter", "audit", "mihomo"):
        setattr(args, name, getattr(args, name).resolve())
    generate(args)


if __name__ == "__main__":
    main()
