import crypto from "node:crypto";
import process from "node:process";
import { chromium } from "playwright-core";
import WebSocket from "ws";

const gatewayUrl = process.env.OPENCLAW_GATEWAY_URL ?? "wss://claw.idol.id";
const gatewayOrigin = process.env.OPENCLAW_GATEWAY_ORIGIN ?? "https://claw.idol.id";
const gatewayToken = process.env.OPENCLAW_SHARED_TOKEN?.trim();
const hermesUrl = process.env.HERMES_URL ?? "https://hermes.idol.id";
const hermesPassword = process.env.HERMES_PASSWORD?.trim();
const chromeBin = process.env.CHROME_BIN?.trim();
const connectTimeoutMs = 30_000;
const requestTimeoutMs = 30_000;
const approvalTimeoutMs = 120_000;

if (!gatewayToken || !hermesPassword || !chromeBin) {
  throw new Error("required encrypted runtime inputs are absent");
}

const secrets = [gatewayToken, hermesPassword];
const sensitiveKey = /(token|password|secret|credential|cookie|authorization|private.?key|bootstrap|signature|csrf)/i;
const contentKey = /(^|_)(message|messages|content|text|prompt|transcript|preview|history|reasoning|summary|response|answer)(_|$)/i;
const pathKey = /(^|_)(path|cwd|dir|root|home|workspace|repo)(_|$)|stateDir|repoRoot/i;

function redactString(value) {
  let out = String(value ?? "");
  for (const secret of secrets) out = out.split(secret).join("[redacted]");
  return out.length > 700 ? `${out.slice(0, 700)}…` : out;
}

function sanitize(value, key = "", depth = 0) {
  if (depth > 7) return "[depth-omitted]";
  if (sensitiveKey.test(key)) return "[redacted]";
  if (contentKey.test(key)) return "[content-omitted]";
  if (pathKey.test(key)) return "[path-omitted]";
  if (value == null || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return redactString(value);
  if (Array.isArray(value)) return value.slice(0, 120).map((item) => sanitize(item, key, depth + 1));
  if (typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([childKey, childValue]) => [childKey, sanitize(childValue, childKey, depth + 1)]));
  }
  return redactString(value);
}

function errorSummary(error) {
  return sanitize({
    name: error?.name,
    code: error?.code,
    message: error?.message ?? String(error),
    details: error?.details,
    retryable: error?.retryable,
    retryAfterMs: error?.retryAfterMs,
  });
}

function createIdentity() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
  const raw = publicKey.export({ format: "der", type: "spki" }).subarray(-32);
  return {
    deviceId: crypto.createHash("sha256").update(raw).digest("hex"),
    publicKey: raw.toString("base64url"),
    privateKeyPem: privateKey.export({ format: "pem", type: "pkcs8" }).toString(),
  };
}

const identity = createIdentity();
const client = {
  id: "openclaw-probe",
  mode: "probe",
  role: "operator",
  scopes: ["operator.read"],
  platform: "linux",
  deviceFamily: "github-actions-hermes-terminal",
};

function deviceProof(challenge) {
  const payload = [
    "v3",
    identity.deviceId,
    client.id,
    client.mode,
    client.role,
    client.scopes.join(","),
    String(challenge.ts),
    gatewayToken,
    challenge.nonce,
    client.platform,
    client.deviceFamily,
  ].join("|");
  return {
    id: identity.deviceId,
    publicKey: identity.publicKey,
    signature: crypto.sign(null, Buffer.from(payload), identity.privateKeyPem).toString("base64url"),
    signedAt: challenge.ts,
    nonce: challenge.nonce,
  };
}

class Gateway {
  constructor() {
    this.ws = null;
    this.pending = new Map();
    this.events = new Map();
    this.hello = null;
  }

  async connect() {
    const ws = new WebSocket(gatewayUrl, {
      origin: gatewayOrigin,
      handshakeTimeout: 20_000,
      perMessageDeflate: false,
      headers: { "User-Agent": "idol-openclaw-hermes-terminal/1" },
    });
    this.ws = ws;
    let challengeResolve;
    let challengeReject;
    const challengePromise = new Promise((resolve, reject) => {
      challengeResolve = resolve;
      challengeReject = reject;
    });
    ws.on("message", (raw) => {
      let frame;
      try { frame = JSON.parse(raw.toString("utf8")); } catch { return; }
      if (frame?.type === "event") {
        this.events.set(frame.event, (this.events.get(frame.event) ?? 0) + 1);
        if (frame.event === "connect.challenge") challengeResolve(frame.payload);
        return;
      }
      if (frame?.type !== "res" || typeof frame.id !== "string") return;
      const waiter = this.pending.get(frame.id);
      if (!waiter) return;
      clearTimeout(waiter.timer);
      this.pending.delete(frame.id);
      if (frame.ok) waiter.resolve(frame.payload);
      else {
        const error = new Error(frame.error?.message ?? "gateway request failed");
        Object.assign(error, frame.error ?? {});
        waiter.reject(error);
      }
    });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("websocket open timeout")), connectTimeoutMs);
      ws.once("open", () => { clearTimeout(timer); resolve(); });
      ws.once("error", (error) => { clearTimeout(timer); reject(error); });
    });
    const challengeTimer = setTimeout(() => challengeReject(new Error("gateway challenge timeout")), connectTimeoutMs);
    const challenge = await challengePromise.finally(() => clearTimeout(challengeTimer));
    if (!challenge || typeof challenge.nonce !== "string" || !Number.isInteger(challenge.ts)) throw new Error("malformed gateway challenge");
    this.hello = await this.request("connect", {
      minProtocol: 4,
      maxProtocol: 4,
      client: {
        id: client.id,
        displayName: "Idol read-only fleet probe",
        version: "1.0.0",
        platform: client.platform,
        deviceFamily: client.deviceFamily,
        mode: client.mode,
        instanceId: crypto.randomUUID(),
      },
      role: client.role,
      scopes: client.scopes,
      caps: ["agent-kind"],
      commands: [],
      permissions: {},
      auth: { token: gatewayToken },
      locale: "en-US",
      userAgent: "idol-openclaw-hermes-terminal/1",
      device: deviceProof(challenge),
    }, connectTimeoutMs);
    return this.hello;
  }

  request(method, params = {}, timeoutMs = requestTimeoutMs) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return Promise.reject(new Error("gateway socket is not open"));
    const id = `idol-${crypto.randomUUID()}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`gateway request timeout: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ type: "req", id, method, params }));
    });
  }

  close() {
    for (const waiter of this.pending.values()) clearTimeout(waiter.timer);
    this.pending.clear();
    try { this.ws?.close(1000, "probe transition"); } catch {}
  }
}

async function createPairingRequest() {
  const gateway = new Gateway();
  try {
    await gateway.connect();
    return { alreadyPaired: true, gateway };
  } catch (error) {
    gateway.close();
    const details = error?.details;
    if (error?.code !== "NOT_PAIRED" || details?.code !== "PAIRING_REQUIRED") throw error;
    if (details.deviceId !== identity.deviceId || details.requestedRole !== "operator") throw new Error("gateway pairing identity or role changed");
    if (!Array.isArray(details.requestedScopes) || !details.requestedScopes.includes("operator.read")) throw new Error("gateway omitted operator.read");
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(details.requestId)) throw new Error("gateway returned malformed request id");
    return {
      alreadyPaired: false,
      requestId: details.requestId,
      deviceId: details.deviceId,
      requestedRole: details.requestedRole,
      requestedScopes: details.requestedScopes,
    };
  }
}

async function openHermesTerminal() {
  const browser = await chromium.launch({
    executablePath: chromeBin,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const diagnostics = {
    loginSubmitted: false,
    loginSucceeded: false,
    sessionExisted: false,
    sessionCreated: false,
    workspacePresent: false,
    terminalStarted: false,
    inputAccepted: false,
  };
  try {
    await page.goto(hermesUrl, { waitUntil: "domcontentloaded", timeout: 45_000 });
    await page.waitForTimeout(1_000);
    const password = page.locator('input[type="password"]');
    if (await password.count() > 0 && await password.first().isVisible()) {
      await password.first().fill(hermesPassword);
      const signIn = page.getByRole("button", { name: "Sign in", exact: true });
      if (await signIn.count() > 0 && await signIn.first().isVisible()) await signIn.first().click();
      else await password.first().press("Enter");
      diagnostics.loginSubmitted = true;
      await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 45_000 }).catch(() => {});
      await page.waitForTimeout(1_500);
    }
    diagnostics.loginSucceeded = !new URL(page.url()).pathname.startsWith("/login");
    if (!diagnostics.loginSucceeded) throw new Error("Hermes login failed");
    await page.waitForFunction(() => typeof window.api === "function" && typeof window.S === "object", null, { timeout: 30_000 });
    const sessionState = await page.evaluate(async () => {
      const before = Boolean(window.S?.session?.session_id);
      if (!window.S?.session?.session_id || !window.S?.session?.workspace) {
        if (typeof window.newSession !== "function") throw new Error("newSession is unavailable");
        await window.newSession(false, { awaitWorkspaceLoad: true, worktree: false });
      }
      const deadline = Date.now() + 30_000;
      while (Date.now() < deadline && (!window.S?.session?.session_id || !window.S?.session?.workspace)) {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      return {
        before,
        sessionId: window.S?.session?.session_id ?? null,
        workspace: Boolean(window.S?.session?.workspace),
      };
    });
    diagnostics.sessionExisted = sessionState.before;
    diagnostics.sessionCreated = !sessionState.before && Boolean(sessionState.sessionId);
    diagnostics.workspacePresent = sessionState.workspace;
    if (!sessionState.sessionId || !sessionState.workspace) throw new Error("Hermes session lacks a terminal workspace");
    await page.evaluate(async ({ sessionId }) => {
      await window.api("/api/terminal/start", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, rows: 24, cols: 120, restart: false }),
      });
    }, { sessionId: sessionState.sessionId });
    diagnostics.terminalStarted = true;
    return { browser, context, page, sessionId: sessionState.sessionId, diagnostics };
  } catch (error) {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
    throw error;
  }
}

async function terminalCommand(hermes, command) {
  if (!/^[A-Za-z0-9 ._:\/-]+$/.test(command)) throw new Error("terminal command contains unexpected characters");
  await hermes.page.evaluate(async ({ sessionId, data }) => {
    await window.api("/api/terminal/input", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, data }),
    });
  }, { sessionId: hermes.sessionId, data: `${command}\r` });
  hermes.diagnostics.inputAccepted = true;
}

async function waitForPairing() {
  const deadline = Date.now() + approvalTimeoutMs;
  let attempts = 0;
  let lastError;
  while (Date.now() < deadline) {
    attempts += 1;
    const gateway = new Gateway();
    try {
      await gateway.connect();
      return { gateway, attempts };
    } catch (error) {
      gateway.close();
      lastError = error;
      if (error?.code !== "NOT_PAIRED") throw error;
      await new Promise((resolve) => setTimeout(resolve, 1_000));
    }
  }
  const error = new Error("fresh OpenClaw pairing was not approved through the Hermes terminal");
  error.details = { attempts, lastError: errorSummary(lastError) };
  throw error;
}

const probes = [
  ["health", {}],
  ["status", {}],
  ["system.info", {}],
  ["system-presence", {}],
  ["agents.list", {}],
  ["models.list", {}],
  ["sessions.list", { limit: 100, ownerFirst: true }],
  ["tasks.list", { limit: 100 }],
  ["cron.status", {}],
  ["cron.list", {}],
  ["channels.status", {}],
  ["nodes.list", {}],
  ["environments.list", {}],
];

async function inventory(gateway) {
  const advertised = new Set(Array.isArray(gateway.hello?.features?.methods) ? gateway.hello.features.methods : []);
  const result = {
    gateway: sanitize({
      protocol: gateway.hello?.protocol,
      server: gateway.hello?.server ? { version: gateway.hello.server.version } : undefined,
      auth: gateway.hello?.auth ? { role: gateway.hello.auth.role, scopes: gateway.hello.auth.scopes } : undefined,
      policy: gateway.hello?.policy ? {
        maxPayload: gateway.hello.policy.maxPayload,
        maxBufferedBytes: gateway.hello.policy.maxBufferedBytes,
        tickIntervalMs: gateway.hello.policy.tickIntervalMs,
      } : undefined,
      appliedConfigHashPresent: Boolean(gateway.hello?.snapshot?.appliedConfigHash),
    }),
    advertised: {
      methodCount: advertised.size,
      methods: [...advertised].sort(),
      eventCount: Array.isArray(gateway.hello?.features?.events) ? gateway.hello.features.events.length : null,
    },
    probes: {},
  };
  for (const [method, params] of probes) {
    if (advertised.size && !advertised.has(method)) {
      result.probes[method] = { advertised: false, skipped: true };
      continue;
    }
    const began = Date.now();
    try {
      result.probes[method] = {
        advertised: advertised.has(method),
        ok: true,
        elapsedMs: Date.now() - began,
        payload: sanitize(await gateway.request(method, params)),
      };
    } catch (error) {
      result.probes[method] = {
        advertised: advertised.has(method),
        ok: false,
        elapsedMs: Date.now() - began,
        error: errorSummary(error),
      };
    }
  }
  result.eventsObserved = Object.fromEntries([...gateway.events.entries()].sort(([a], [b]) => a.localeCompare(b)));
  return result;
}

async function main() {
  const report = {
    schema: "idol.openclaw.hermes-terminal.v1",
    startedAt: new Date().toISOString(),
    completedAt: null,
    paired: false,
    pairing: {},
    hermes: {},
    approval: {},
    cleanup: {},
    inventory: null,
  };
  let hermes;
  let gateway;
  try {
    const pairing = await createPairingRequest();
    report.pairing = sanitize(pairing.alreadyPaired ? { alreadyPaired: true, deviceId: identity.deviceId } : pairing);
    if (pairing.alreadyPaired) {
      gateway = pairing.gateway;
    } else {
      hermes = await openHermesTerminal();
      await terminalCommand(hermes, `openclaw devices approve ${pairing.requestId}`);
      const approved = await waitForPairing();
      gateway = approved.gateway;
      report.approval = { attempts: approved.attempts };
    }
    report.paired = true;
    report.inventory = await inventory(gateway);
    if (hermes) {
      await terminalCommand(hermes, `openclaw devices remove ${identity.deviceId}`);
      report.cleanup.ephemeralDeviceRemovalSubmitted = true;
      await hermes.page.waitForTimeout(1_000);
      await hermes.page.evaluate(async ({ sessionId }) => {
        await window.api("/api/terminal/close", { method: "POST", body: JSON.stringify({ session_id: sessionId }) });
      }, { sessionId: hermes.sessionId }).catch(() => {});
      report.cleanup.terminalClosed = true;
    }
  } catch (error) {
    report.error = errorSummary(error);
    if (error?.details) report.errorDetails = sanitize(error.details);
    process.exitCode = 1;
  } finally {
    gateway?.close();
    if (hermes) {
      report.hermes = sanitize(hermes.diagnostics);
      await hermes.context.close().catch(() => {});
      await hermes.browser.close().catch(() => {});
    }
    report.completedAt = new Date().toISOString();
    console.log(JSON.stringify(report));
  }
}

await main();
