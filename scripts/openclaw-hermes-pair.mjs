import crypto from "node:crypto";
import process from "node:process";
import { chromium } from "playwright-core";
import WebSocket from "ws";

const GATEWAY_URL = process.env.OPENCLAW_GATEWAY_URL ?? "wss://claw.idol.id";
const GATEWAY_ORIGIN = process.env.OPENCLAW_GATEWAY_ORIGIN ?? "https://claw.idol.id";
const GATEWAY_TOKEN = process.env.OPENCLAW_SHARED_TOKEN?.trim();
const HERMES_URL = process.env.HERMES_URL ?? "https://hermes.idol.id";
const HERMES_PASSWORD = process.env.HERMES_PASSWORD?.trim();
const CHROME_BIN = process.env.CHROME_BIN?.trim();
const REQUEST_TIMEOUT_MS = 30_000;
const CONNECT_TIMEOUT_MS = 30_000;
const PAIRING_WAIT_MS = 7 * 60_000;

if (!GATEWAY_TOKEN || !HERMES_PASSWORD || !CHROME_BIN) {
  throw new Error("required encrypted runtime inputs are absent");
}

const sensitiveValues = [GATEWAY_TOKEN, HERMES_PASSWORD].filter(Boolean);
const sensitiveKey = /(token|password|secret|credential|cookie|authorization|private.?key|bootstrap|signature|sessionid|csrf)/i;
const contentKey = /(^|_)(message|messages|content|text|prompt|transcript|preview|history|reasoning|summary|response|answer)(_|$)/i;
const pathKey = /(^|_)(path|cwd|dir|root|home|workspace|repo)(_|$)|stateDir|repoRoot/i;

function redactString(value) {
  let result = String(value ?? "");
  for (const secret of sensitiveValues) {
    if (secret) result = result.split(secret).join("[redacted]");
  }
  return result.length > 700 ? `${result.slice(0, 700)}…` : result;
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
    const out = {};
    for (const [childKey, childValue] of Object.entries(value)) {
      out[childKey] = sanitize(childValue, childKey, depth + 1);
    }
    return out;
  }
  return redactString(value);
}

function errorSummary(error) {
  if (!error || typeof error !== "object") return { message: redactString(error) };
  return sanitize({
    name: error.name,
    code: error.code,
    message: error.message,
    details: error.details,
    retryable: error.retryable,
    retryAfterMs: error.retryAfterMs,
  });
}

function lower(value) {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function createDeviceIdentity() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
  const publicKeyPem = publicKey.export({ format: "pem", type: "spki" }).toString();
  const privateKeyPem = privateKey.export({ format: "pem", type: "pkcs8" }).toString();
  const publicDer = publicKey.export({ format: "der", type: "spki" });
  const rawPublicKey = publicDer.subarray(publicDer.length - 32);
  const publicKeyRawBase64Url = rawPublicKey.toString("base64url");
  const deviceId = crypto.createHash("sha256").update(rawPublicKey).digest("hex");
  return { deviceId, privateKeyPem, publicKeyRawBase64Url };
}

const identity = createDeviceIdentity();

function buildDeviceProof({ clientId, clientMode, role, scopes, signedAt, nonce, platform, deviceFamily }) {
  const payload = [
    "v3",
    identity.deviceId,
    clientId,
    clientMode,
    role,
    scopes.join(","),
    String(signedAt),
    GATEWAY_TOKEN,
    nonce,
    lower(platform),
    lower(deviceFamily),
  ].join("|");
  return {
    id: identity.deviceId,
    publicKey: identity.publicKeyRawBase64Url,
    signature: crypto.sign(null, Buffer.from(payload, "utf8"), identity.privateKeyPem).toString("base64url"),
    signedAt,
    nonce,
  };
}

class GatewayConnection {
  constructor() {
    this.pending = new Map();
    this.events = new Map();
    this.ws = null;
    this.hello = null;
  }

  async connect() {
    const ws = new WebSocket(GATEWAY_URL, {
      origin: GATEWAY_ORIGIN,
      handshakeTimeout: 20_000,
      perMessageDeflate: false,
      headers: { "User-Agent": "idol-openclaw-hermes-pair/1" },
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
      if (frame?.type === "res" && typeof frame.id === "string") {
        const pending = this.pending.get(frame.id);
        if (!pending) return;
        clearTimeout(pending.timer);
        this.pending.delete(frame.id);
        if (frame.ok) pending.resolve(frame.payload);
        else {
          const error = new Error(frame.error?.message ?? "gateway request failed");
          error.code = frame.error?.code;
          error.details = frame.error?.details;
          error.retryable = frame.error?.retryable;
          error.retryAfterMs = frame.error?.retryAfterMs;
          pending.reject(error);
        }
      }
    });

    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("websocket open timeout")), CONNECT_TIMEOUT_MS);
      ws.once("open", () => { clearTimeout(timer); resolve(); });
      ws.once("error", (error) => { clearTimeout(timer); reject(error); });
    });

    const challengeTimer = setTimeout(() => challengeReject(new Error("connect challenge timeout")), CONNECT_TIMEOUT_MS);
    const challenge = await challengePromise.finally(() => clearTimeout(challengeTimer));
    if (!challenge || typeof challenge.nonce !== "string" || !Number.isInteger(challenge.ts)) {
      throw new Error("malformed gateway challenge");
    }

    const clientId = "openclaw-probe";
    const clientMode = "probe";
    const role = "operator";
    const scopes = ["operator.read"];
    const platform = "linux";
    const deviceFamily = "github-actions-hermes-pair";
    const device = buildDeviceProof({
      clientId,
      clientMode,
      role,
      scopes,
      signedAt: challenge.ts,
      nonce: challenge.nonce,
      platform,
      deviceFamily,
    });

    const hello = await this.request("connect", {
      minProtocol: 4,
      maxProtocol: 4,
      client: {
        id: clientId,
        displayName: "Idol read-only fleet probe",
        version: "1.0.0",
        platform,
        deviceFamily,
        mode: clientMode,
        instanceId: crypto.randomUUID(),
      },
      role,
      scopes,
      caps: ["agent-kind"],
      commands: [],
      permissions: {},
      auth: { token: GATEWAY_TOKEN },
      locale: "en-US",
      userAgent: "idol-openclaw-hermes-pair/1",
      device,
    }, CONNECT_TIMEOUT_MS);
    this.hello = hello;
    return hello;
  }

  request(method, params = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("gateway socket is not open"));
    }
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
    for (const pending of this.pending.values()) clearTimeout(pending.timer);
    this.pending.clear();
    try { this.ws?.close(1000, "probe transition"); } catch {}
  }
}

async function createPairingRequest() {
  const connection = new GatewayConnection();
  try {
    await connection.connect();
    return { alreadyPaired: true, connection };
  } catch (error) {
    connection.close();
    const details = error?.details;
    if (error?.code !== "NOT_PAIRED" || details?.code !== "PAIRING_REQUIRED") throw error;
    if (details.deviceId !== identity.deviceId) throw new Error("gateway pairing response changed the requested device identity");
    if (details.requestedRole !== "operator") throw new Error("gateway pairing response changed the requested role");
    if (!Array.isArray(details.requestedScopes) || !details.requestedScopes.includes("operator.read")) {
      throw new Error("gateway pairing response omitted operator.read");
    }
    return {
      alreadyPaired: false,
      requestId: details.requestId,
      deviceId: details.deviceId,
      requestedRole: details.requestedRole,
      requestedScopes: details.requestedScopes,
    };
  }
}

async function firstVisible(page, locators) {
  for (const locator of locators) {
    try {
      if (await locator.count() > 0 && await locator.first().isVisible()) return locator.first();
    } catch {}
  }
  return null;
}

async function loginHermesAndRequestApproval(pairing) {
  const browser = await chromium.launch({
    executablePath: CHROME_BIN,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  const context = await browser.newContext({
    ignoreHTTPSErrors: false,
    userAgent: "idol-hermes-pairing-client/1",
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  const diagnostics = {
    loginPasswordFieldSeen: false,
    loginSubmitted: false,
    loginSucceeded: false,
    newChatClicked: false,
    composerFound: false,
    messageSubmitted: false,
    requestIdVisibleAfterSubmit: false,
  };

  try {
    await page.goto(HERMES_URL, { waitUntil: "domcontentloaded", timeout: 45_000 });
    await page.waitForTimeout(1_500);

    const password = page.locator('input[type="password"]');
    if (await password.count() > 0 && await password.first().isVisible()) {
      diagnostics.loginPasswordFieldSeen = true;
      await password.first().fill(HERMES_PASSWORD);
      const signIn = await firstVisible(page, [
        page.getByRole("button", { name: "Sign in", exact: true }),
        page.locator('button[type="submit"]'),
      ]);
      if (signIn) await signIn.click();
      else await password.first().press("Enter");
      diagnostics.loginSubmitted = true;
      await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 45_000 }).catch(() => {});
      await page.waitForTimeout(2_000);
    }

    diagnostics.loginSucceeded = !new URL(page.url()).pathname.startsWith("/login") && !(await password.count() > 0 && await password.first().isVisible().catch(() => false));
    if (!diagnostics.loginSucceeded) throw new Error("Hermes login did not leave the sign-in surface");

    const newChat = await firstVisible(page, [
      page.getByRole("button", { name: /new chat/i }),
      page.getByRole("link", { name: /new chat/i }),
      page.getByRole("button", { name: /new conversation/i }),
      page.getByRole("link", { name: /new conversation/i }),
      page.getByRole("button", { name: /start chat/i }),
      page.getByRole("button", { name: /new session/i }),
      page.locator('[aria-label*="new chat" i]'),
      page.locator('[title*="new chat" i]'),
    ]);
    if (newChat) {
      await newChat.click();
      diagnostics.newChatClicked = true;
      await page.waitForTimeout(1_500);
    }

    const composer = await firstVisible(page, [
      page.locator('textarea[placeholder*="message" i]'),
      page.locator('textarea[placeholder*="ask" i]'),
      page.locator('textarea[placeholder*="prompt" i]'),
      page.locator('textarea'),
      page.locator('[contenteditable="true"][role="textbox"]'),
      page.locator('[contenteditable="true"]'),
      page.locator('input[type="text"][placeholder*="message" i]'),
    ]);
    if (!composer) {
      const counts = {
        visibleButtons: await page.locator('button:visible').count(),
        visibleLinks: await page.locator('a:visible').count(),
        textareas: await page.locator('textarea').count(),
        contenteditables: await page.locator('[contenteditable="true"]').count(),
      };
      const error = new Error("Hermes chat composer was not found");
      error.details = counts;
      throw error;
    }
    diagnostics.composerFound = true;

    const command = `openclaw devices approve ${pairing.requestId}`;
    const instruction = [
      "Explicit operator instruction from Chris. This is an exact, bounded pairing task on the local OpenClaw gateway host.",
      `First run: openclaw devices list --json`,
      `Approve only request ${pairing.requestId}, device ${pairing.deviceId}, role operator, requested scope operator.read.`,
      `Run exactly: ${command}`,
      `Then run: openclaw devices list --json`,
      "Do not approve, reject, rotate, remove, or alter any other device. Do not change gateway configuration. Report the exact command outcome.",
    ].join("\n");

    const tag = await composer.evaluate((element) => element.tagName.toLowerCase());
    if (tag === "textarea" || tag === "input") {
      await composer.fill(instruction);
    } else {
      await composer.click();
      await page.keyboard.press("Control+A");
      await page.keyboard.type(instruction);
    }

    const send = await firstVisible(page, [
      page.getByRole("button", { name: /^send$/i }),
      page.getByRole("button", { name: /send message/i }),
      page.locator('button[type="submit"]'),
      page.locator('[aria-label*="send" i]'),
      page.locator('[title*="send" i]'),
    ]);
    if (send) await send.click();
    else await composer.press("Enter");
    diagnostics.messageSubmitted = true;
    await page.waitForTimeout(4_000);
    diagnostics.requestIdVisibleAfterSubmit = (await page.locator(`text=${pairing.requestId}`).count()) > 0;
    return diagnostics;
  } finally {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}

async function waitForApproval() {
  const deadline = Date.now() + PAIRING_WAIT_MS;
  let attempts = 0;
  let lastError;
  while (Date.now() < deadline) {
    attempts += 1;
    const connection = new GatewayConnection();
    try {
      await connection.connect();
      return { connection, attempts };
    } catch (error) {
      connection.close();
      lastError = error;
      if (error?.code !== "NOT_PAIRED") throw error;
      await new Promise((resolve) => setTimeout(resolve, 5_000));
    }
  }
  const error = new Error("OpenClaw pairing was not approved within the bounded wait");
  error.details = { attempts, lastError: errorSummary(lastError) };
  throw error;
}

const PROBES = [
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

async function inventory(connection) {
  const advertised = new Set(Array.isArray(connection.hello?.features?.methods) ? connection.hello.features.methods : []);
  const result = {
    gateway: sanitize({
      protocol: connection.hello?.protocol,
      server: connection.hello?.server ? { version: connection.hello.server.version } : undefined,
      auth: connection.hello?.auth ? { role: connection.hello.auth.role, scopes: connection.hello.auth.scopes } : undefined,
      policy: connection.hello?.policy ? {
        maxPayload: connection.hello.policy.maxPayload,
        maxBufferedBytes: connection.hello.policy.maxBufferedBytes,
        tickIntervalMs: connection.hello.policy.tickIntervalMs,
      } : undefined,
      appliedConfigHashPresent: Boolean(connection.hello?.snapshot?.appliedConfigHash),
    }),
    advertised: {
      methodCount: advertised.size,
      methods: [...advertised].sort(),
      eventCount: Array.isArray(connection.hello?.features?.events) ? connection.hello.features.events.length : null,
    },
    probes: {},
  };

  for (const [method, params] of PROBES) {
    if (advertised.size > 0 && !advertised.has(method)) {
      result.probes[method] = { advertised: false, skipped: true };
      continue;
    }
    const began = Date.now();
    try {
      const payload = await connection.request(method, params);
      result.probes[method] = { advertised: advertised.has(method), ok: true, elapsedMs: Date.now() - began, payload: sanitize(payload) };
    } catch (error) {
      result.probes[method] = { advertised: advertised.has(method), ok: false, elapsedMs: Date.now() - began, error: errorSummary(error) };
    }
  }
  result.eventsObserved = Object.fromEntries([...connection.events.entries()].sort(([a], [b]) => a.localeCompare(b)));
  return result;
}

async function main() {
  const report = {
    schema: "idol.openclaw.hermes-pair.v1",
    startedAt: new Date().toISOString(),
    completedAt: null,
    paired: false,
    pairing: {},
    hermes: {},
    approval: {},
    inventory: null,
  };

  let liveConnection;
  try {
    const pairing = await createPairingRequest();
    report.pairing = sanitize(pairing.alreadyPaired ? { alreadyPaired: true, deviceId: identity.deviceId } : pairing);
    if (pairing.alreadyPaired) {
      liveConnection = pairing.connection;
    } else {
      report.hermes = sanitize(await loginHermesAndRequestApproval(pairing));
      const approved = await waitForApproval();
      report.approval = { attempts: approved.attempts };
      liveConnection = approved.connection;
    }
    report.paired = true;
    report.inventory = await inventory(liveConnection);
  } catch (error) {
    report.error = errorSummary(error);
    if (error?.details) report.errorDetails = sanitize(error.details);
    process.exitCode = 1;
  } finally {
    liveConnection?.close();
    report.completedAt = new Date().toISOString();
    console.log(JSON.stringify(report));
  }
}

await main();
