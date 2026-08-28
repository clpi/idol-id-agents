#!/usr/bin/env python3
"""Observe the unauthenticated Claw Control UI in a real browser.

This probe never supplies gateway credentials or invokes an agent. It records
visible fallback state, JavaScript exceptions, console errors, failed requests,
and a screenshot while redacting likely credential material.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import pathlib
import re
import urllib.parse
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

UTC = dt.timezone.utc
FALLBACK_MARKERS = (
    "Control UI did not start",
    "app bundle did not start",
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|password|secret|authorization|api[_-]?key)\b\s*[:=]\s*([^\s,;&]+)"
)
BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def scrub(value: object, limit: int = 4000) -> str:
    text = str(value)
    text = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = BEARER.sub("Bearer <redacted>", text)
    return text[:limit]


def path_of(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, "")) or "/"


def body_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


async def inspect_page(page: Page) -> dict[str, Any]:
    return await page.evaluate(
        """(markers) => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              Number(style.opacity || '1') !== 0 && box.width > 0 && box.height > 0;
          };
          const fallback = [];
          for (const element of document.querySelectorAll('body *')) {
            const own = (element.innerText || '').trim();
            if (!own || !visible(element)) continue;
            if (markers.some((marker) => own.includes(marker))) {
              fallback.push({
                tag: element.tagName.toLowerCase(),
                id: element.id || null,
                className: typeof element.className === 'string' ? element.className : null,
                text: own.slice(0, 240),
              });
            }
          }
          const customTags = [...new Set(
            [...document.querySelectorAll('*')]
              .map((element) => element.tagName.toLowerCase())
              .filter((tag) => tag.includes('-'))
          )].sort();
          const registered = customTags.filter((tag) => Boolean(customElements.get(tag)));
          const body = document.body?.innerText || '';
          return {
            title: document.title,
            readyState: document.readyState,
            bodyLength: body.length,
            bodyText: body,
            fallback,
            customTags,
            registered,
            rootChildren: [...(document.body?.children || [])].map((element) => ({
              tag: element.tagName.toLowerCase(),
              id: element.id || null,
              className: typeof element.className === 'string' ? element.className : null,
            })),
          };
        }""",
        list(FALLBACK_MARKERS),
    )


async def probe(
    url: str,
    *,
    timeout_ms: int,
    settle_ms: int,
    screenshot: pathlib.Path | None,
) -> dict[str, Any]:
    consoles: list[dict[str, Any]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, Any]] = []
    error_responses: list[dict[str, Any]] = []
    navigation_error: str | None = None
    navigation_status: int | None = None
    final_url = url
    inspected: dict[str, Any] = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = await browser.new_context(
            ignore_https_errors=False,
            service_workers="block",
            user_agent="idol-claw-browser-probe/1",
            viewport={"width": 1440, "height": 1000},
        )
        page = await context.new_page()

        def on_console(message: Any) -> None:
            if message.type in {"error", "warning"}:
                consoles.append(
                    {
                        "type": message.type,
                        "text": scrub(message.text),
                        "location": {
                            "path": path_of(message.location.get("url", "")),
                            "line": message.location.get("lineNumber"),
                            "column": message.location.get("columnNumber"),
                        },
                    }
                )

        def on_page_error(error: Any) -> None:
            page_errors.append(scrub(error))

        def on_request_failed(request: Any) -> None:
            failed_requests.append(
                {
                    "method": request.method,
                    "path": path_of(request.url),
                    "resource": request.resource_type,
                    "failure": scrub(request.failure or "unknown"),
                }
            )

        def on_response(response: Any) -> None:
            if response.status >= 400:
                error_responses.append(
                    {
                        "path": path_of(response.url),
                        "status": response.status,
                        "resource": response.request.resource_type,
                    }
                )

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            navigation_status = response.status if response else None
            await page.wait_for_timeout(settle_ms)
            final_url = page.url
            inspected = await inspect_page(page)
            body_text = inspected.pop("bodyText", "")
            inspected["bodySha256"] = body_digest(body_text)
            if screenshot:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot), full_page=True)
        except PlaywrightError as exc:
            navigation_error = scrub(exc)
            final_url = page.url
            if screenshot:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await page.screenshot(path=str(screenshot), full_page=True)
                except PlaywrightError:
                    pass
        finally:
            await context.close()
            await browser.close()

    visible_fallback = bool(inspected.get("fallback"))
    errors: list[str] = []
    if navigation_status != 200:
        errors.append(f"navigation-status:{navigation_status}")
    if navigation_error:
        errors.append("navigation-error")
    if visible_fallback:
        errors.append("fallback-shell-visible")
    if page_errors:
        errors.append(f"page-errors:{len(page_errors)}")
    if failed_requests:
        errors.append(f"failed-requests:{len(failed_requests)}")

    # A protected runtime-config response is expected before authentication and
    # must not itself fail the public boot shell.
    unexpected_responses = [
        item
        for item in error_responses
        if not (
            item["path"] == "/control-ui-config.json"
            and item["status"] in {401, 403}
        )
    ]
    if unexpected_responses:
        errors.append(f"unexpected-http-errors:{len(unexpected_responses)}")

    return {
        "schema": "idol.claw.browser.v1",
        "observed_at": dt.datetime.now(UTC).isoformat(),
        "requested_url": url,
        "final_url": final_url,
        "navigation": {
            "status": navigation_status,
            "error": navigation_error,
        },
        "document": inspected,
        "console": consoles,
        "page_errors": page_errors,
        "failed_requests": failed_requests,
        "error_responses": error_responses,
        "unexpected_error_responses": unexpected_responses,
        "errors": errors,
        "ok": not errors,
        "safety": {
            "authenticated": False,
            "gateway_invoked": False,
            "agent_invoked": False,
            "browser_storage_seeded": False,
            "messages_redacted": True,
        },
    }


async def async_main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="https://claw.idol.id/")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--settle-ms", type=int, default=8000)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--screenshot", type=pathlib.Path)
    args = parser.parse_args()
    report = await probe(
        args.url,
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        screenshot=args.screenshot,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
