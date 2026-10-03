import base64
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


converter = module("converter", "generate-mihomo-rulesets.py")
geodata = module("geodata", "verify-geodata.py")
lock_check = module("lock", "verify-mihomo-lock.py")


class ParityTests(unittest.TestCase):
    def test_all_selector_types_are_accounted_for(self):
        entry = {"domain": [{"type": kind, "value": value} for kind, value in (
            ("Full", "exact.example"), ("Domain", "suffix.example"),
            ("Plain", "keyword"), ("Regex", r"^regex\.example$")
        )]}
        rules, domains, stats = converter.site_expectations(entry)
        self.assertEqual(stats, {"full": 1, "domain": 1, "keyword": 1, "regexp": 1})
        self.assertEqual(domains, ["exact.example", "+.suffix.example"])
        self.assertEqual(len(rules), sum(stats.values()))
        self.assertEqual([rule for rule in rules if rule.startswith(("DOMAIN-KEYWORD,", "DOMAIN-REGEX,"))],
                         ["DOMAIN-KEYWORD,keyword", r"DOMAIN-REGEX,^regex\.example$"])

    def test_exact_and_suffix_semantics_remain_distinct(self):
        canonical = converter.canonical_domains
        self.assertNotEqual(canonical(["example.com"]), canonical(["+.example.com"]))
        self.assertEqual(canonical(["+.example.com", "example.com", "www.example.com", "+.api.example.com"]), {"+.example.com"})
        self.assertEqual(canonical(["+.example.com", "notexample.com"]), {"+.example.com", "notexample.com"})
        self.assertNotEqual(canonical(["+.example.com"]), canonical(["+.other.com"]))

    def test_unrepresentable_and_unknown_selectors_fail(self):
        for kind, value in (("Unknown", "example.org"), ("Regex", "a{1,3}"),
                            ("Full", "*.example.org"), ("Domain", "example.org."), ("Full", "")):
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                converter.site_expectations({"domain": [{"type": kind, "value": value}]})

    def test_attribute_category_and_unsafe_names(self):
        self.assertEqual(converter.selection({"geosite": ["steam@cn"]}, "geosite"), ["steam@cn"])
        for names in (["../outside"], ["steam", "steam"], ["steam@!cn"], ["STEAM"]):
            with self.assertRaises(ValueError):
                converter.selection({"geosite": names}, "geosite")

    def test_category_arguments_accept_csv_and_json_arrays(self):
        for value in ('reddit, steam@cn', '["reddit", "steam@cn"]'):
            self.assertEqual(converter.selection({"geosite": converter.categories(value)}, "geosite"),
                             ["reddit", "steam@cn"])
        for value in ("", " ", "[]"):
            self.assertEqual(converter.categories(value), [])
        for value in ('["reddit",', '[1]', '[null]'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                converter.categories(value)
        for value in ("reddit,", "reddit,,steam", "reddit, reddit", "../outside"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                converter.selection({"geosite": converter.categories(value)}, "geosite")

    def test_network_merging_retains_ipv4_ipv6_coverage(self):
        self.assertEqual(converter.canonical_networks(["10.0.0.0/9", "10.128.0.0/9", "fc00::/7"]), {"10.0.0.0/8", "fc00::/7"})
        entry = {"cidr": [{"ip": base64.b64encode(bytes([10, 1, 2, 3])).decode(), "prefix": 8}]}
        self.assertEqual(converter.ip_expectations(entry), ["10.0.0.0/8"])
        with self.assertRaises(ValueError):
            converter.ip_expectations(dict(entry, reverseMatch=True))

    def test_regression_warnings(self):
        old = {"a.mrs": {"size": 100}}
        self.assertEqual(len(converter.size_warnings({"a.mrs": {"size": 500}}, old)), 2)
        self.assertEqual(converter.size_warnings({"a.mrs": {"size": 150}}, old), [])


class ArtifactTests(unittest.TestCase):
    def test_extra_provider_survives_upstream_selector_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dat"
            source.write_bytes(b"fixture")
            args = SimpleNamespace(geosite=["example"], geoip=[], mihomo_version="v1.19.17",
                geosite_dat=source, geoip_dat=source, converter=root / "converter", audit=root / "audit",
                mihomo=root / "mihomo-core", output=root / "output", base_url="https://example.org/MIHOMO", summary=None)
            entry = {"domain": []}

            def run_tool(*values):
                if values[0] == args.audit:
                    self.assertEqual(converter.read_json(Path(values[1])), {"geosite": ["example"], "geoip": []})
                    return json.dumps({"geosite": {"example": entry}, "geoip": {}})
                if values[0] == args.converter:
                    if values[1] == "geosite":
                        target = Path(values[-1])
                        (target / "classical").mkdir(parents=True)
                        rules, domains, _ = converter.site_expectations(entry)
                        (target / "classical/example.list").write_text("\n".join(rules) + "\n")
                        (target / "example.list").write_text("\n".join(domains) + "\n")
                        (target / "example.mrs").write_bytes(b"fixture-mrs")
                    return ""
                if values[1] == "-v":
                    return "Mihomo v1.19.17"
                if values[1] == "convert-ruleset":
                    Path(values[-1]).write_text("exact.example\n")
                return ""

            # Exercise real staging, publication, stale-file cleanup and lock writing.
            full = {"type": "Full", "value": "exact.example"}
            keyword = {"type": "Plain", "value": "new-keyword"}
            regex = {"type": "Regex", "value": r"^new\.example$"}
            stable_provider = None
            args.output.mkdir()
            (args.output / "manifest.json").write_text("{}")
            for selectors, expected in (([full], []), ([full, keyword, regex],
                    ["DOMAIN-KEYWORD,new-keyword", r"DOMAIN-REGEX,^new\.example$"]), ([full], [])):
                entry["domain"] = selectors
                with patch.object(converter, "command", side_effect=run_tool), redirect_stdout(io.StringIO()):
                    converter.generate(args)
                extra = args.output / "classical/example-extra.list"
                self.assertEqual([line for line in converter.lines(extra) if not line.startswith("#")], expected)
                self.assertGreater(extra.stat().st_size, 0)
                providers = converter.read_json(args.output / "rule-providers.yaml")["rule-providers"]
                provider = providers["geosite-example-extra"]
                self.assertEqual((provider["behavior"], provider["format"]), ("classical", "text"))
                if stable_provider is not None:
                    self.assertEqual(provider, stable_provider)
                stable_provider = provider
                lock = converter.read_json(args.output / "mihomo.lock.json")
                self.assertEqual(set(lock), {"geosite:example", "geosite:example:extra", "rule-providers.yaml"})
                self.assertTrue(all(set(value) == {"size", "sha256"} for value in lock.values()))
                self.assertFalse((args.output / "manifest.json").exists())
                lock_check.verify(args.output)

    def test_empty_or_corrupted_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = {kind: {"sha256": converter.sha256(Path(__file__))} for kind in ("geosite", "geoip")}
            (root / "geodata.lock.json").write_text(json.dumps(lock))
            (root / "metadata.json").write_text(json.dumps({"lock": lock}))
            for kind in lock:
                (root / f"{kind}.dat").write_bytes(Path(__file__).read_bytes())
            geodata.verify(root)
            (root / "geoip.dat").write_bytes(b"<html>error</html>")
            with self.assertRaises(ValueError):
                geodata.verify(root)
            (root / "geoip.dat").write_bytes(b"")
            with self.assertRaises(ValueError):
                geodata.verify(root)

    def test_lock_detects_tampering_unlisted_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "MIHOMO"
            output.mkdir()
            (output / "geosite").mkdir()
            rule = output / "geosite/test.mrs"
            rule.write_bytes(b"fixture")
            lock = {"geosite:test": {"size": 7, "sha256": converter.sha256(rule)}}
            (output / "mihomo.lock.json").write_text(json.dumps(lock))
            lock_check.verify(output)
            (output / "unexpected.list").write_text("ignored")
            with self.assertRaises(ValueError):
                lock_check.verify(output)
            (output / "unexpected.list").unlink()
            rule.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                lock_check.verify(output)
            rule.unlink()
            with self.assertRaises(ValueError):
                lock_check.verify(output)

    def test_lock_identifiers_cannot_escape_output_directory(self):
        for identifier in ("geosite:../outside", "../../outside", "geosite:test/extra:extra", "unknown:test",
                           "geoip:private:extra", "geosite:test:unknown", "geosite:test:extra:extra"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                lock_check.artifact_path(identifier)

    def test_extra_lock_keys_preserve_origin_without_category_collisions(self):
        self.assertEqual(lock_check.artifact_path("geosite:reddit:extra"), "classical/reddit-extra.list")
        self.assertEqual(lock_check.artifact_path("geosite:reddit-extra"), "geosite/reddit-extra.mrs")
        self.assertEqual(lock_check.artifact_path("geosite:reddit-extra:extra"), "classical/reddit-extra-extra.list")
        self.assertEqual(lock_check.artifact_path("geosite:steam@cn:extra"), "classical/steam@cn-extra.list")


if __name__ == "__main__":
    unittest.main()
