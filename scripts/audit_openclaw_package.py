#!/usr/bin/env python3
"""Compare the deployed Claw asset graph with the signed OpenClaw npm artifact.

The audit is read-only. It downloads public npm metadata/tarball content,
verifies the registry integrity field, compares deployed asset digests with the
package, and reports whether missing lazy chunks are absent upstream or only on
the live host. It never authenticates to Claw or invokes an agent.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import pathlib
import tarfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

USER_AGENT = "idol-openclaw-package-audit/1"
DEFAULT_PACKAGE = "openclaw"
DEFAULT_VERSION = "2026.8.1-beta.3"
DEFAULT_TARGETS = (
    "mcp-servers-CtWfZH8M.js",
    "mcp-app-security-BhWBPx_4.js",
)
MAX_TARBALL_BYTES = 256 * 1024 * 1024
MAX_ASSET_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class AssetRef:
    kind: str
    url: str


class AssetParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.assets: list[AssetRef] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {key.lower(): value for key, value in attrs}
        if tag.lower() == "script" and values.get("src"):
            self.assets.append(
                AssetRef("script", urllib.parse.urljoin(self.base_url, values["src"] or ""))
            )
            return
        if tag.lower() != "link" or not values.get("href"):
            return
        rel = {part.lower() for part in (values.get("rel") or "").split()}
        if "stylesheet" in rel or "modulepreload" in rel:
            kind = "style" if "stylesheet" in rel else "script"
            self.assets.append(
                AssetRef(kind, urllib.parse.urljoin(self.base_url, values["href"] or ""))
            )


def fetch(url: str, *, accept: str, max_bytes: int) -> tuple[bytes, str, int]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"response exceeds byte limit {max_bytes}: {url}")
        return body, response.geturl(), response.status


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def verify_integrity(body: bytes, integrity: str) -> dict[str, Any]:
    algorithms: dict[str, Any] = {
        "sha512": hashlib.sha512,
        "sha384": hashlib.sha384,
        "sha256": hashlib.sha256,
        "sha1": hashlib.sha1,
    }
    checks: list[dict[str, Any]] = []
    for token in integrity.split():
        if "-" not in token:
            continue
        algorithm, expected = token.split("-", 1)
        factory = algorithms.get(algorithm.lower())
        if factory is None:
            checks.append({"algorithm": algorithm, "supported": False, "match": False})
            continue
        actual = base64.b64encode(factory(body).digest()).decode("ascii")
        checks.append(
            {
                "algorithm": algorithm.lower(),
                "supported": True,
                "match": actual == expected,
            }
        )
    verified = any(item["supported"] and item["match"] for item in checks)
    return {"verified": verified, "checks": checks}


def safe_member_name(name: str) -> str:
    normalized = pathlib.PurePosixPath(name)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"unsafe tar member: {name}")
    return str(normalized)


def analyze_tarball(body: bytes, targets: tuple[str, ...]) -> dict[str, Any]:
    assets: dict[str, dict[str, Any]] = {}
    exact_targets: dict[str, list[str]] = {target: [] for target in targets}
    references: dict[str, list[str]] = {target: [] for target in targets}
    candidates: dict[str, list[str]] = {target: [] for target in targets}

    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = safe_member_name(member.name)
            if not member.isfile():
                continue
            basename = pathlib.PurePosixPath(name).name
            lower = basename.lower()
            if not lower.endswith((".js", ".css", ".mjs")):
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            content = stream.read(MAX_ASSET_BYTES + 1)
            if len(content) > MAX_ASSET_BYTES:
                raise ValueError(f"asset exceeds byte limit: {name}")
            record = {
                "path": name,
                "basename": basename,
                "bytes": len(content),
                "sha256": sha256(content),
            }
            assets[name] = record
            for target in targets:
                if basename == target:
                    exact_targets[target].append(name)
                stem = target.rsplit("-", 1)[0].lower()
                if stem and stem in lower:
                    candidates[target].append(name)
                if target.encode("utf-8") in content:
                    references[target].append(name)

    by_basename: dict[str, list[dict[str, Any]]] = {}
    by_digest: dict[str, list[dict[str, Any]]] = {}
    for record in assets.values():
        by_basename.setdefault(record["basename"], []).append(record)
        by_digest.setdefault(record["sha256"], []).append(record)

    return {
        "asset_count": len(assets),
        "assets": assets,
        "by_basename": by_basename,
        "by_digest": by_digest,
        "targets": {
            target: {
                "exact": sorted(exact_targets[target]),
                "references": sorted(references[target]),
                "candidates": sorted(set(candidates[target])),
            }
            for target in targets
        },
    }


def fetch_live_assets(url: str) -> dict[str, Any]:
    root, final_url, status = fetch(
        url,
        accept="text/html,*/*;q=0.5",
        max_bytes=8 * 1024 * 1024,
    )
    parser = AssetParser(final_url)
    parser.feed(root.decode("utf-8", "replace"))
    parser.close()
    seen: set[str] = set()
    assets: list[dict[str, Any]] = []
    for ref in parser.assets:
        if ref.url in seen:
            continue
        seen.add(ref.url)
        content, asset_final, asset_status = fetch(
            ref.url,
            accept="application/javascript,text/javascript,text/css,*/*;q=0.5",
            max_bytes=MAX_ASSET_BYTES,
        )
        assets.append(
            {
                "kind": ref.kind,
                "url": asset_final,
                "path": urllib.parse.urlsplit(asset_final).path,
                "basename": pathlib.PurePosixPath(
                    urllib.parse.urlsplit(asset_final).path
                ).name,
                "status": asset_status,
                "bytes": len(content),
                "sha256": sha256(content),
            }
        )
    return {
        "root_status": status,
        "root_url": final_url,
        "root_sha256": sha256(root),
        "assets": assets,
    }


def classify(
    package: dict[str, Any],
    live: dict[str, Any],
    targets: tuple[str, ...],
) -> dict[str, Any]:
    by_basename = package["by_basename"]
    by_digest = package["by_digest"]
    comparisons: list[dict[str, Any]] = []
    all_live_exact = True
    for asset in live["assets"]:
        name_matches = by_basename.get(asset["basename"], [])
        digest_matches = by_digest.get(asset["sha256"], [])
        exact = any(item["sha256"] == asset["sha256"] for item in name_matches)
        all_live_exact = all_live_exact and exact
        comparisons.append(
            {
                **asset,
                "package_same_name": [item["path"] for item in name_matches],
                "package_same_digest": [item["path"] for item in digest_matches],
                "exact_package_asset": exact,
            }
        )

    target_state = package["targets"]
    all_targets_present = all(target_state[target]["exact"] for target in targets)
    all_targets_referenced = all(target_state[target]["references"] for target in targets)

    if all_live_exact and all_targets_present:
        verdict = "live-install-or-cache-incomplete"
    elif all_live_exact and all_targets_referenced and not all_targets_present:
        verdict = "published-package-incoherent"
    elif not all_live_exact:
        verdict = "live-package-version-or-generation-mismatch"
    else:
        verdict = "insufficient-package-linkage-evidence"

    return {
        "verdict": verdict,
        "all_live_assets_exact_package_matches": all_live_exact,
        "all_targets_present_in_package": all_targets_present,
        "all_targets_referenced_in_package": all_targets_referenced,
        "live_assets": comparisons,
    }


def audit(
    package_name: str,
    version: str,
    live_url: str,
    targets: tuple[str, ...],
) -> dict[str, Any]:
    encoded = urllib.parse.quote(package_name, safe="@")
    metadata_url = f"https://registry.npmjs.org/{encoded}/{urllib.parse.quote(version, safe='')}"
    metadata_body, _, metadata_status = fetch(
        metadata_url,
        accept="application/json",
        max_bytes=8 * 1024 * 1024,
    )
    metadata = json.loads(metadata_body.decode("utf-8"))
    if metadata.get("name") != package_name or metadata.get("version") != version:
        raise ValueError("registry metadata identity mismatch")
    dist = metadata.get("dist") or {}
    tarball_url = str(dist.get("tarball") or "")
    integrity = str(dist.get("integrity") or "")
    if not tarball_url or not integrity:
        raise ValueError("registry metadata lacks tarball or integrity")

    tarball, final_tarball_url, tarball_status = fetch(
        tarball_url,
        accept="application/octet-stream",
        max_bytes=MAX_TARBALL_BYTES,
    )
    integrity_result = verify_integrity(tarball, integrity)
    if not integrity_result["verified"]:
        raise ValueError("tarball integrity verification failed")

    package = analyze_tarball(tarball, targets)
    live = fetch_live_assets(live_url)
    comparison = classify(package, live, targets)

    return {
        "schema": "idol.openclaw.package-audit.v1",
        "package": {
            "name": package_name,
            "version": version,
            "metadata_status": metadata_status,
            "tarball_status": tarball_status,
            "tarball_host": urllib.parse.urlsplit(final_tarball_url).hostname,
            "tarball_bytes": len(tarball),
            "tarball_sha256": sha256(tarball),
            "integrity": integrity_result,
            "asset_count": package["asset_count"],
            "targets": package["targets"],
        },
        "live": {
            "root_status": live["root_status"],
            "root_url": live["root_url"],
            "root_sha256": live["root_sha256"],
            "asset_count": len(live["assets"]),
        },
        "comparison": comparison,
        "safety": {
            "authenticated": False,
            "gateway_invoked": False,
            "agent_invoked": False,
            "package_files_extracted_to_disk": False,
            "asset_bodies_reported": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--live-url", default="https://claw.idol.id/")
    parser.add_argument("--target", action="append", dest="targets")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    targets = tuple(args.targets or DEFAULT_TARGETS)
    try:
        report = audit(args.package, args.version, args.live_url, targets)
        rendered = json.dumps(report, indent=2, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    except Exception as exc:
        error = {
            "schema": "idol.openclaw.package-audit.v1",
            "error": f"{type(exc).__name__}: {exc}",
        }
        rendered = json.dumps(error, indent=2, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
