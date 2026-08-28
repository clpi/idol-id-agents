import fs from "node:fs";
import { chromium } from "playwright";

const password = fs.readFileSync(process.env.HERMES_PASSWORD_FILE, "utf8").trim();
const url = process.env.HERMES_URL ?? "https://hermes.idol.id";
const resultFile = process.env.HERMES_RESULT_FILE ?? "hermes-audit.json";
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  ignoreHTTPSErrors: false,
});
const page = await context.newPage();
const consoleErrors = [];
const pageErrors = [];
const network = [];
page.on("console", (message) => {
  if (["error", "warning"].includes(message.type())) consoleErrors.push({ type: message.type(), text: message.text().slice(0, 500) });
});
page.on("pageerror", (error) => pageErrors.push(String(error).slice(0, 500)));
page.on("response", (response) => {
  try {
    const parsed = new URL(response.url());
    if (parsed.origin === new URL(url).origin) network.push({ method: response.request().method(), path: parsed.pathname, status: response.status() });
  } catch {}
});

function uniq(rows) {
  const seen = new Set();
  return rows.filter((row) => {
    const key = JSON.stringify(row);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function snapshot(label) {
  const data = await page.evaluate(() => {
    const clean = (value) => (value ?? "").replace(/\s+/g, " ").trim().slice(0, 250);
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const headings = [...document.querySelectorAll("h1,h2,h3,[role=heading]")].filter(visible).map((el) => clean(el.textContent)).filter(Boolean);
    const buttons = [...document.querySelectorAll("button,[role=button]")].filter(visible).map((el) => ({
      text: clean(el.textContent),
      aria: clean(el.getAttribute("aria-label")),
      title: clean(el.getAttribute("title")),
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
    }));
    const links = [...document.querySelectorAll("a[href]")].filter(visible).map((el) => ({
      text: clean(el.textContent),
      aria: clean(el.getAttribute("aria-label")),
      href: (() => { try { const u = new URL(el.href); return u.origin === location.origin ? `${u.pathname}${u.search}` : u.origin; } catch { return ""; } })(),
    }));
    const inputs = [...document.querySelectorAll("input,textarea,[contenteditable=true]")].filter(visible).map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute("type") || "",
      name: el.getAttribute("name") || "",
      placeholder: clean(el.getAttribute("placeholder")),
      aria: clean(el.getAttribute("aria-label")),
      autocomplete: el.getAttribute("autocomplete") || "",
    }));
    const selects = [...document.querySelectorAll("select")].filter(visible).map((el) => ({
      name: el.name || "",
      aria: clean(el.getAttribute("aria-label")),
      options: [...el.options].slice(0, 100).map((option) => clean(option.textContent)).filter(Boolean),
    }));
    const nav = [...document.querySelectorAll("nav,[role=navigation],aside")].filter(visible).map((el) => clean(el.textContent)).filter(Boolean).map((text) => text.slice(0, 500));
    return { headings, buttons, links, inputs, selects, nav, localStorageKeys: Object.keys(localStorage).sort(), sessionStorageKeys: Object.keys(sessionStorage).sort() };
  });
  return { label, url: page.url(), title: await page.title(), ...data };
}

const report = {
  schema: "idol.hermes.live-audit.v1",
  observedAt: new Date().toISOString(),
  origin: new URL(url).origin,
  outcome: "unknown",
  snapshots: [],
  consoleErrors,
  pageErrors,
  network: [],
};

try {
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.waitForTimeout(2500);
  report.snapshots.push(await snapshot("landing"));

  const passwordInput = page.locator('input[type="password"]').first();
  if (await passwordInput.count()) {
    await passwordInput.fill(password);
    const button = page.getByRole("button", { name: /sign in|log in|login|unlock|continue|submit/i }).first();
    if (await button.count()) await button.click();
    else await passwordInput.press("Enter");
    await page.waitForTimeout(5000);
    await page.waitForLoadState("networkidle", { timeout: 20_000 }).catch(() => {});
  }

  const stillPassword = await page.locator('input[type="password"]').count();
  report.outcome = stillPassword ? "authentication-not-cleared" : "authenticated";
  report.snapshots.push(await snapshot("after-auth"));

  if (!stillPassword) {
    const candidates = [/settings/i, /agents?/i, /models?/i, /providers?/i, /tools?/i, /mcp/i, /sessions?/i, /tasks?/i];
    const visited = new Set();
    for (const pattern of candidates) {
      let locator = page.getByRole("link", { name: pattern }).first();
      if (!(await locator.count())) locator = page.getByRole("button", { name: pattern }).first();
      if (!(await locator.count())) continue;
      const label = (await locator.innerText().catch(() => "")) || String(pattern);
      if (visited.has(label)) continue;
      visited.add(label);
      try {
        await locator.click();
        await page.waitForTimeout(2500);
        await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => {});
        report.snapshots.push(await snapshot(`nav:${label}`));
      } catch (error) {
        report.snapshots.push({ label: `nav:${label}`, error: String(error).slice(0, 400) });
      }
    }
  }
} catch (error) {
  report.outcome = "browser-failed";
  report.error = String(error).slice(0, 1000);
} finally {
  report.network = uniq(network).slice(0, 500);
  report.consoleErrors = uniq(consoleErrors).slice(0, 100);
  report.pageErrors = [...new Set(pageErrors)].slice(0, 100);
  fs.writeFileSync(resultFile, JSON.stringify(report, null, 2));
  await browser.close();
}
