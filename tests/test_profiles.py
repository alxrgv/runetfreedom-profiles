"""Exercise the preserved Bash profile generator against a verified test artifact."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


BASH = os.environ.get("PROFILE_TEST_BASH") or ("bash" if os.name == "posix" else None)


def helper_code(workflow):
    body = workflow.split("<<'BASH_HELPERS'\n", 1)[1]
    return textwrap.dedent(re.split(r"\n[ \t]*BASH_HELPERS\n", body, maxsplit=1)[0])


@unittest.skipUnless(BASH and shutil.which("jq"), "Requires Bash and jq (available in CI)")
class ProfileCompatibilityTests(unittest.TestCase):
    def test_timestamp_flags_and_deeplink_roundtrip(self):
        root = Path(__file__).parents[1]
        test_root = root / ".build"
        test_root.mkdir(exist_ok=True)
        workflow = (root / ".github/workflows/update-geodata.yml").read_text()
        helpers = helper_code(workflow)
        profile_step = workflow.split("        id: profiles\n", 1)[1].split("        run: |\n", 1)[1].split("\n      -", 1)[0]
        profile_code = textwrap.dedent(profile_step)
        def shell_path(path):
            return path.relative_to(root).as_posix() if os.name == "nt" else path.as_posix()
        with tempfile.TemporaryDirectory(dir=test_root) as directory:
            work = Path(directory)
            artifact = work / "geodata"
            artifact.mkdir()
            profile_script = work / "profiles.sh"
            profile_script.write_text("set -Eeuo pipefail\n" + helpers + "\n" + profile_code, newline="\n")
            lock = {}
            for kind in ("geosite", "geoip"):
                data = (kind + "-fixture").encode()
                (artifact / f"{kind}.dat").write_bytes(data)
                lock[kind] = {"sha256": hashlib.sha256(data).hexdigest(), "url": f"https://example.org/release/{kind}.dat"}
            (artifact / "geodata.lock.json").write_text(json.dumps(lock))
            metadata = {"lock": lock, "geodata_changed": False, "release_assets_changed": False}
            env = dict(os.environ, TEMPLATE_PATH=shell_path(root / "config/routing.template.json"),
                       GEODATA_DIRECTORY=shell_path(artifact), FORCE_REFRESH="false",
                       LAST_UPDATED_OFFSET_SECONDS="0", GITHUB_OUTPUT=shell_path(work / "outputs"))
            for client in ("HAPP", "INCY"):
                env.update({client + "_ENABLED": "true", client + "_PROFILE_NAME": "runetfreedom",
                            client + "_DEEPLINK_SCHEME": client.lower(), client + "_DEEPLINK_ACTION": "onadd",
                            client + "_JSON_PATH": shell_path(work / client / "JSONSUB.JSON"),
                            client + "_DEEPLINK_PATH": shell_path(work / client / "JSONSUB.DEEPLINK")})
                (work / client).mkdir()
                (work / client / "JSONSUB.JSON").write_text(json.dumps({"LastUpdated": "42"}))
            for changed, release_changed, forced in ((False, False, False), (True, False, False), (False, True, False), (False, False, True)):
                metadata.update(geodata_changed=changed, release_assets_changed=release_changed)
                (artifact / "metadata.json").write_text(json.dumps(metadata))
                env["FORCE_REFRESH"] = str(forced).lower()
                before = json.loads(Path(env["HAPP_JSON_PATH"]).read_text())["LastUpdated"]
                result = subprocess.run([BASH, shell_path(profile_script)], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for client in ("HAPP", "INCY"):
                    output = json.loads(Path(env[client + "_JSON_PATH"]).read_text())
                    payload = Path(env[client + "_DEEPLINK_PATH"]).read_text().strip().split("/")[-1]
                    self.assertEqual(json.loads(base64.b64decode(payload)), output)
                    self.assertEqual(output["Geoipurl"], lock["geoip"]["url"])
                    self.assertEqual(output["Geositeurl"], lock["geosite"]["url"])
                    if changed or release_changed or forced:
                        self.assertGreater(int(output["LastUpdated"]), int(before))
                    else:
                        self.assertEqual(output["LastUpdated"], before)
                    template = json.loads(Path(env["TEMPLATE_PATH"]).read_text())
                    for key in set(template) - {"Name", "LastUpdated", "Geoipurl", "Geositeurl"}:
                        self.assertEqual(output[key], template[key])


@unittest.skipUnless(BASH, "Requires Bash (available in CI)")
class PartialPublicationTests(unittest.TestCase):
    def test_successful_outputs_publish_without_failed_build_artifacts(self):
        root = Path(__file__).parents[1]
        test_root = root / ".build"
        test_root.mkdir(exist_ok=True)
        workflow = (root / ".github/workflows/update-geodata.yml").read_text()
        helpers = helper_code(workflow)
        publication = textwrap.dedent(workflow.split("      - name: Publish successful outputs\n", 1)[1]
                                     .split("        run: |\n", 1)[1])

        for xray, mihomo in ((True, False), (False, True), (True, True)):
            with self.subTest(xray=xray, mihomo=mihomo), tempfile.TemporaryDirectory(dir=test_root) as directory:
                base = Path(directory)
                remote, work = base / "remote.git", base / "checkout"
                work.mkdir()

                def git(*args, cwd=work):
                    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return result.stdout.strip()

                def write(relative, data):
                    path = work / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)

                git("init", "--bare", str(remote), cwd=base)
                git("init", "-b", "main")
                git("config", "user.name", "Test")
                git("config", "user.email", "test@example.org")
                for filename in ("verify-geodata.py", "verify-mihomo-lock.py"):
                    write("scripts/" + filename, (root / "scripts" / filename).read_bytes())
                write(".gitignore", b".build/\n")
                write("HAPP/JSONSUB.JSON", b"old-happ")
                write("INCY/JSONSUB.JSON", b"old-incy")
                write("MIHOMO/geosite/stale.mrs", b"old-mihomo")
                write("MIHOMO/manifest.json", b"{}")
                write("geodata.lock.json", b'{"release_tag":"old"}\n')
                git("add", ".")
                git("commit", "-m", "Initial fixture")
                git("remote", "add", "origin", str(remote))
                git("push", "origin", "main")
                original = git("rev-parse", "HEAD")

                lock = {kind: {"sha256": hashlib.sha256(kind.encode()).hexdigest()} for kind in ("geosite", "geoip")}
                new_lock = json.dumps(lock).encode()
                write(".build/geodata/geodata.lock.json", new_lock)
                write(".build/geodata/metadata.json", json.dumps({"lock": lock}).encode())
                for kind in lock:
                    write(f".build/geodata/{kind}.dat", kind.encode())
                if xray:
                    for profile in ("HAPP", "INCY"):
                        write(f".build/artifacts/profiles/{profile}/JSONSUB.JSON", b"new-profile")
                if mihomo:
                    rule = b"new-mihomo"
                    write(".build/artifacts/MIHOMO/geosite/new.mrs", rule)
                    output_lock = {"geosite:new": {"size": len(rule), "sha256": hashlib.sha256(rule).hexdigest()}}
                    write(".build/artifacts/MIHOMO/mihomo.lock.json", json.dumps(output_lock).encode())

                # Run the workflow's publication code against a real local Git remote.
                python = Path(sys.executable).as_posix()
                script = f'python() {{ "{python}" "$@"; }}\n' + helpers + "\n" + publication
                env = dict(os.environ, GITHUB_REF_NAME="main", GITHUB_SHA=original,
                    GITHUB_ACTOR="test", GITHUB_ACTOR_ID="1", GEODATA_DIRECTORY=".build/geodata",
                    COMMIT_MESSAGE_PREFIX="chore: test", LAST_UPDATED="123",
                    XRAY_RESULT="success" if xray else "failure", MIHOMO_RESULT="success" if mihomo else "failure")
                result = subprocess.run([BASH, "-c", script], cwd=work, env=env,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((work / "HAPP/JSONSUB.JSON").read_bytes(), b"new-profile" if xray else b"old-happ")
                self.assertEqual((work / "INCY/JSONSUB.JSON").read_bytes(), b"new-profile" if xray else b"old-incy")
                self.assertEqual((work / "geodata.lock.json").read_bytes(), new_lock if xray else b'{"release_tag":"old"}\n')
                self.assertEqual((work / "MIHOMO/geosite/stale.mrs").exists(), not mihomo)
                self.assertEqual((work / "MIHOMO/geosite/new.mrs").exists(), mihomo)
                self.assertEqual((work / "MIHOMO/manifest.json").exists(), not mihomo)
                self.assertEqual(git("rev-parse", "HEAD"), git("rev-parse", "main", cwd=remote))
                self.assertNotEqual(git("rev-parse", "HEAD"), original)


if __name__ == "__main__":
    unittest.main()
