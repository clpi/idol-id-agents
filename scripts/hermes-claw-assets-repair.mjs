import { readFileSync } from "node:fs";
import process from "node:process";
import { chromium } from "playwright-core";

const hermesUrl = process.env.HERMES_URL ?? "https://hermes.idol.id";
const hermesPassword = process.env.HERMES_PASSWORD?.trim();
const chromeBin = process.env.CHROME_BIN?.trim();
const repair = readFileSync(new URL("./repair-claw-control-ui.sh", import.meta.url), "utf8");

if (!hermesPassword || !chromeBin) {
  throw new Error("required encrypted Hermes runtime inputs are absent");
}

function encodeCommand(script) {
  const payload = Buffer.from(script, "utf8").toString("base64");
  const runner = [
    "import base64,subprocess,sys",
    `script=base64.b64decode('${payload}').decode('utf-8')`,
    "run=subprocess.run(['bash','-lc',script],text=True)",
    "sys.exit(run.returncode)",
  ].join(";");
  return `python3 -c \"${runner}\"`;
}

async function openTerminal() {
  const browser = await chromium.launch({
    executablePath: chromeBin,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  try {
    await page.goto(hermesUrl, { waitUntil: "domcontentloaded", timeout: 45_000 });
    await page.waitForTimeout(1_000);
    const password = page.locator('input[type="password"]');
    if (await password.count() > 0 && await password.first().isVisible()) {
      await password.first().fill(hermesPassword);
      const signIn = page.getByRole("button", { name: "Sign in", exact: true });
      if (await signIn.count() > 0 && await signIn.first().isVisible()) await signIn.first().click();
      else await password.first().press("Enter");
      await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 45_000 }).catch(() => {});
      await page.waitForTimeout(1_500);
    }
    if (new URL(page.url()).pathname.startsWith("/login")) throw new Error("Hermes login failed");
    await page.waitForFunction(() => typeof api === "function" && typeof S === "object", null, { timeout: 30_000 });
    const state = await page.evaluate(async () => {
      if (!S?.session?.session_id || !S?.session?.workspace) {
        if (typeof newSession !== "function") throw new Error("newSession is unavailable");
        await newSession(false, { awaitWorkspaceLoad: true, worktree: false });
      }
      const deadline = Date.now() + 30_000;
      while (Date.now() < deadline && (!S?.session?.session_id || !S?.session?.workspace)) {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      return { sessionId: S?.session?.session_id ?? null, workspace: Boolean(S?.session?.workspace) };
    });
    if (!state.sessionId || !state.workspace) throw new Error("Hermes session lacks a terminal workspace");
    await page.evaluate(async ({ sessionId }) => {
      await api("/api/terminal/start", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, rows: 40, cols: 160, restart: false }),
      });
    }, { sessionId: state.sessionId });
    return { browser, context, page, sessionId: state.sessionId };
  } catch (error) {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
    throw error;
  }
}

async function main() {
  const report = {
    schema: "idol.hermes.claw-assets-repair.v1",
    submitted: false,
    waitedMs: 0,
    semanticOutcomeClaimed: false,
  };
  let hermes;
  try {
    hermes = await openTerminal();
    const command = encodeCommand(repair);
    await hermes.page.evaluate(async ({ sessionId, data }) => {
      await api("/api/terminal/input", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, data }),
      });
    }, { sessionId: hermes.sessionId, data: `${command}\r` });
    report.submitted = true;
    const waitMs = 150_000;
    await hermes.page.waitForTimeout(waitMs);
    report.waitedMs = waitMs;
    report.nextEvidence = "independent browser and signed-package probes";
  } catch (error) {
    report.error = { name: error?.name, message: String(error?.message ?? error).slice(0, 500) };
    process.exitCode = 1;
  } finally {
    if (hermes) {
      await hermes.page.evaluate(async ({ sessionId }) => {
        await api("/api/terminal/close", {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId }),
        });
      }, { sessionId: hermes.sessionId }).catch(() => {});
      await hermes.context.close().catch(() => {});
      await hermes.browser.close().catch(() => {});
    }
    console.log(JSON.stringify(report));
  }
}

await main();
