from __future__ import annotations

import importlib.util
import io
import pathlib
import sys
import tarfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_openclaw_package", ROOT / "scripts" / "audit_openclaw_package.py"
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)

TARGETS = ("mcp-servers-a.js", "mcp-app-security-b.js")


def tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def live_asset(name: str, body: bytes) -> dict:
    return {
        "kind": "script",
        "url": f"https://example.invalid/assets/{name}",
        "path": f"/assets/{name}",
        "basename": name,
        "status": 200,
        "bytes": len(body),
        "sha256": audit.sha256(body),
    }


class PackageAuditTests(unittest.TestCase):
    def test_integrity_verifies_supported_registry_digest(self) -> None:
        body = b"package"
        import base64
        import hashlib

        value = "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()
        self.assertTrue(audit.verify_integrity(body, value)["verified"])
        self.assertFalse(audit.verify_integrity(body + b"x", value)["verified"])

    def test_exact_live_bundle_and_present_chunks_means_install_or_cache_fault(self) -> None:
        parent = b"import('./mcp-servers-a.js');import('./mcp-app-security-b.js')"
        package = audit.analyze_tarball(
            tarball(
                {
                    "package/dist/control-ui/assets/app.js": parent,
                    "package/dist/control-ui/assets/mcp-servers-a.js": b"servers",
                    "package/dist/control-ui/assets/mcp-app-security-b.js": b"security",
                }
            ),
            TARGETS,
        )
        result = audit.classify(
            package,
            {"assets": [live_asset("app.js", parent)]},
            TARGETS,
        )
        self.assertEqual(result["verdict"], "live-install-or-cache-incomplete")

    def test_referenced_but_unpublished_chunks_means_package_fault(self) -> None:
        parent = b"import('./mcp-servers-a.js');import('./mcp-app-security-b.js')"
        package = audit.analyze_tarball(
            tarball({"package/dist/control-ui/assets/app.js": parent}), TARGETS
        )
        result = audit.classify(
            package,
            {"assets": [live_asset("app.js", parent)]},
            TARGETS,
        )
        self.assertEqual(result["verdict"], "published-package-incoherent")

    def test_live_bundle_not_in_package_means_version_or_generation_mismatch(self) -> None:
        package_body = b"console.log('package')"
        live_body = b"console.log('live')"
        package = audit.analyze_tarball(
            tarball({"package/dist/control-ui/assets/app.js": package_body}), TARGETS
        )
        result = audit.classify(
            package,
            {"assets": [live_asset("app.js", live_body)]},
            TARGETS,
        )
        self.assertEqual(
            result["verdict"], "live-package-version-or-generation-mismatch"
        )

    def test_unsafe_tar_member_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe tar member"):
            audit.analyze_tarball(tarball({"../escape.js": b"x"}), TARGETS)


if __name__ == "__main__":
    unittest.main()
