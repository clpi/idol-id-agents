#!/usr/bin/env python3
"""Probe the public Claw control UI boot surface without gateway credentials.

The probe records exact HTTP/asset evidence while refusing to print asset
contents or runtime secrets. It does not authenticate, open the gateway
WebSocket, invoke an agent, or mutate the deployment.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

UTC = dt.timezone.utc
USER_AGENT = "idol-claw-surface-probe/1"
FALLBACK_MARKERS = (
    "Control UI did not start",
    "app bundle did not start",
)
SECRET_KEY_PARTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "cookie",
)


@dataclass(frozen=True)
class AssetRef:
    kind: str
    url: str


@dataclass
class Response:
    requested_url: str
    final_url: str
    status: int | None
    content_type: str
    headers: dict[str, str]
    body: bytes
    error: str | None = None


class BootAssetParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.assets: list[AssetRef] = []

    def handle_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs}
        if tag.lower() == "script" and values.get("src"):
            self.assets.append(
                AssetRef("script", urllib.parse.urljoin(self.base_url, values["src"] or ""))
            )
            return
        if tag.lower() != "link" or not values.get("href"):
            return
        rel = {part.lower() for part in (values.get("rel") or "").split()}
        target = urllib.parse.urljoin(self.base_url, values["href"] or "")
        if "stylesheet" in rel:
            self.assets.append(AssetRef("style", target))
        elif "modulepreload" in rel:
            self.assets.append(AssetRef("script", target))
        elif "preload" in rel and (values.get("as") or "").lower() in {"script", "style"}:
            self.assets.append(AssetRef((values.get("as") or "").lower(), target))


def normalized_content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def fetch(url: str, timeout: float, max_bytes: int = 8 * 1024 * 1024) -> Response:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,text/css,application/javascript,*/*;q=0.5",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as result:
            body = result.read(max_bytes + 1)
            if len(body) > max_bytes:
                return Response(
                    requested_url=url,
                    final_url=result.geturl(),
                    status=result.status,
                    content_type=normalized_content_type(result.headers.get("Content-Type")),
                    headers={key.lower(): value for key, value in result.headers.items()},
                    body=b"",
                    error=f"body-exceeds-limit:{max_bytes}",
                )
            return Response(
                requested_url=url,
                final_url=result.geturl(),
                status=result.status,
                content_type=normalized_content_type(result.headers.get("Content-Type")),
                headers={key.lower(): value for key, value in result.headers.items()},
                body=body,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read(max_bytes + 1)
        return Response(
            requested_url=url,
            final_url=exc.geturl(),
            status=exc.code,
            content_type=normalized_content_type(exc.headers.get("Content-Type")),
            headers={key.lower(): value for key, value in exc.headers.items()},
            body=body[:max_bytes],
            error=None if len(body) <= max_bytes else f"body-exceeds-limit:{max_bytes}",
        )
    except Exception as exc:  # network/TLS/DNS evidence must remain explicit
        return Response(
            requested_url=url,
            final_url=url,
            status=None,
            content_type="",
            headers={},
            body=b"",
            error=f"{type(exc).__name__}:{exc}",
        )


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def path_of(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, "")) or "/"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            result[str(key)] = (
                "<redacted>"
                if any(part in lowered for part in SECRET_KEY_PARTS)
                else redact(item)
            )
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def asset_expected_type(kind: str, content_type: str) -> bool:
    if kind == "style":
        return content_type == "text/css"
    return content_type in {
        "application/ecmascript",
        "application/javascript",
        "application/x-javascript",
        "text/ecmascript",
        "text/javascript",
    }


def probe(url: str, timeout: float = 15.0) -> dict[str, Any]:
    requested = url if url.endswith("/") else url + "/"
    root = fetch(requested, timeout)
    errors: list[str] = []
    root_text = root.body.decode("utf-8", "replace")

    if root.status != 200:
        errors.append(f"root-status:{root.status}")
    if root.error:
        errors.append(f"root-fetch:{root.error}")
    if root.status == 200 and root.content_type != "text/html":
        errors.append(f"root-content-type:{root.content_type or 'missing'}")

    fallback = any(marker.lower() in root_text.lower() for marker in FALLBACK_MARKERS)
    if fallback:
        errors.append("fallback-shell-present")

    parser = BootAssetParser(root.final_url)
    if root.status == 200:
        parser.feed(root_text)
        parser.close()

    unique: list[AssetRef] = []
    seen: set[tuple[str, str]] = set()
    for item in parser.assets:
        marker = (item.kind, item.url)
        if marker not in seen:
            seen.add(marker)
            unique.append(item)

    if not any(item.kind == "script" for item in unique):
        errors.append("no-external-script")

    assets: list[dict[str, Any]] = []
    for item in unique:
        response = fetch(item.url, timeout)
        error = response.error
        if response.status != 200 and error is None:
            error = f"status:{response.status}"
        if response.status == 200 and error is None and not asset_expected_type(
            item.kind, response.content_type
        ):
            error = f"unexpected-content-type:{response.content_type or 'missing'}"
        assets.append(
            {
                "kind": item.kind,
                "path": path_of(response.final_url),
                "requested_path": path_of(item.url),
                "status": response.status,
                "content_type": response.content_type,
                "bytes": len(response.body),
                "sha256": digest(response.body) if response.body else None,
                "cache_control": response.headers.get("cache-control"),
                "etag": response.headers.get("etag"),
                "ok": error is None,
                "error": error,
            }
        )

    failed_assets = sum(1 for item in assets if not item["ok"])
    if failed_assets:
        errors.append(f"asset-failures:{failed_assets}")

    config_url = urllib.parse.urljoin(root.final_url, "/control-ui-config.json")
    config_response = fetch(config_url, timeout, max_bytes=1024 * 1024)
    config: dict[str, Any] = {
        "path": path_of(config_response.final_url),
        "status": config_response.status,
        "content_type": config_response.content_type,
        "bytes": len(config_response.body),
        "sha256": digest(config_response.body) if config_response.body else None,
        "protected": config_response.status in {401, 403},
        "error": config_response.error,
    }
    if config_response.status == 200:
        try:
            config["json"] = redact(json.loads(config_response.body.decode("utf-8")))
        except Exception as exc:
            config["error"] = f"invalid-json:{type(exc).__name__}:{exc}"
            errors.append("runtime-config-invalid")
    elif config_response.status not in {401, 403}:
        errors.append(f"runtime-config-status:{config_response.status}")
    if config_response.error:
        errors.append(f"runtime-config-fetch:{config_response.error}")

    report = {
        "schema": "idol.claw.surface.v1",
        "observed_at": dt.datetime.now(UTC).isoformat(),
        "requested_url": requested,
        "final_url": root.final_url,
        "root": {
            "status": root.status,
            "content_type": root.content_type,
            "bytes": len(root.body),
            "sha256": digest(root.body) if root.body else None,
            "cache_control": root.headers.get("cache-control"),
            "etag": root.headers.get("etag"),
            "fallback_shell": fallback,
            "error": root.error,
        },
        "assets": assets,
        "config": config,
        "summary": {
            "asset_count": len(assets),
            "script_assets": sum(1 for item in assets if item["kind"] == "script"),
            "style_assets": sum(1 for item in assets if item["kind"] == "style"),
            "failed_assets": failed_assets,
        },
        "errors": errors,
        "ok": not errors,
        "safety": {
            "authenticated": False,
            "gateway_invoked": False,
            "agent_invoked": False,
            "asset_bodies_reported": False,
            "runtime_config_redacted": True,
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="https://claw.idol.id/")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    report = probe(args.url, timeout=args.timeout)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
