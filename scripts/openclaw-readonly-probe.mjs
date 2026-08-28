import crypto from "node:crypto";
import process from "node:process";
import WebSocket from "ws";

const URL = process.env.OPENCLAW_GATEWAY_URL ?? "wss://claw.idol.id";
const ORIGIN = process.env.OPENCLAW_GATEWAY_ORIGIN ?? "https://claw.idol.id";
const SHARED_TOKEN = process.env.OPENCLAW_SHARED_TOKEN?.trim() || undefined;
const BOOTSTRAP_TOKEN = process.env.OPENCLAW_BOOTSTRAP_TOKEN?.trim() || undefined;
const REQUEST_TIMEOUT_MS = 30_000;
const CONNECT_TIMEOUT_MS = 30_000;

if (!SHARED_TOKEN && !BOOTSTRAP_TOKEN) {
  throw new Error("no OpenClaw authentication material supplied");
}

const sensitiveValues = [SHARED_TOKEN, BOOTSTRAP_TOKEN].filter(Boolean);
const sensitiveKey = /(token|password|secret|credential|cookie|authorization|private.?key|bootstrap|signature)/i;
const contentKey = /(^|_)(message|messages|content|text|prompt|transcript|preview|history|reasoning|summary)(_|$)/i;
const pathKey = /(^|_)(path|cwd|dir|root|home|workspace|repo)(_|$)|stateDir|repoRoot/i;

function redactString(value) {
  let result = String(value);
  for (const secret of sensitiveValues) {
    if (secret) result = result.split(secret).join("[redacted]");
  }
  return result.length > 600 ? `${result.slice(0, 600)}…` : result;
}

function sanitize(value, key = "", depth = 0) {
  if (depth > 7) return "[depth-omitted]";
  if (sensitiveKey.test(key)) return "[redacted]";
  if (contentKey.test(key)) return "[content-omitted]";
  if (pathKey.test(key)) return "[path-omitted]";
  if (value == null || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return redactString(value);
  if (Array.isArray(value)) return value.slice(0, 100).map((item) => sanitize(item, key, depth + 1));
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
  return { deviceId, publicKeyPem, privateKeyPem, publicKeyRawBase64Url };
}

const identity = createDeviceIdentity();

function buildDeviceProof({ clientId, clientMode, role, scopes, signedAt, nonce, signatureToken, platform, deviceFamily }) {
  const payload = [
    "v3",
    identity.deviceId,
    clientId,
    clientMode,
    role,
    scopes.join(","),
    String(signedAt),
    signatureToken ?? "",
    nonce,
    lower(platform),
    lower(deviceFamily),
  ].join("|");
  const signature = crypto.sign(null, Buffer.from(payload, "utf8"), identity.privateKeyPem).toString("base64url");
  return {
    id: identity.deviceId,
    publicKey: identity.publicKeyRawBase64Url,
    signature,
    signedAt,
    nonce,
  };
}

class GatewayConnection {
  constructor({ auth, signatureToken, clientId, clientMode, role, scopes, platform = "linux", deviceFamily = "github-actions" }) {
    this.auth = auth;
    this.signatureToken = signatureToken;
    this.clientId = clientId;
    this.clientMode = clientMode;
    this.role = role;
    this.scopes = scopes;
    this.platform = platform;
    this.deviceFamily = deviceFamily;
    this.pending = new Map();
    this.events = new Map();
    this.hello = null;
    this.ws = null;
  }

  async connect() {
    const ws = new WebSocket(URL, {
      origin: ORIGIN,
      handshakeTimeout: 20_000,
      perMessageDeflate: false,
      headers: { "User-Agent": "idol-openclaw-readonly-probe/1" },
    });
    this.ws = ws;

    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("websocket open timeout")), CONNECT_TIMEOUT_MS);
      ws.once("open", () => { clearTimeout(timer); resolve(); });
      ws.once("error", (error) => { clearTimeout(timer); reject(error); });
    });

    let challengeResolve;
    let challengeReject;
    const challengePromise = new Promise((resolve, reject) => {
      challengeResolve = resolve;
      challengeReject = reject;
    });
    const challengeTimer = setTimeout(() => challengeReject(new Error("connect challenge timeout")), CONNECT_TIMEOUT_MS);

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

    const challenge = await challengePromise.finally(() => clearTimeout(challengeTimer));
    if (!challenge || typeof challenge.nonce !== "string" || !Number.isInteger(challenge.ts)) {
      throw new Error("malformed connect challenge");
    }

    const device = buildDeviceProof({
      clientId: this.clientId,
      clientMode: this.clientMode,
      role: this.role,
      scopes: this.scopes,
      signedAt: challenge.ts,
      nonce: challenge.nonce,
      signatureToken: this.signatureToken,
      platform: this.platform,
      deviceFamily: this.deviceFamily,
    });

    const hello = await this.request("connect", {
      minProtocol: 4,
      maxProtocol: 4,
      client: {
        id: this.clientId,
        displayName: "Idol read-only fleet probe",
        version: "1.0.0",
        platform: this.platform,
        deviceFamily: this.deviceFamily,
        mode: this.clientMode,
        instanceId: crypto.randomUUID(),
      },
      role: this.role,
      scopes: this.scopes,
      caps: ["agent-kind"],
      commands: [],
      permissions: {},
      auth: this.auth,
      locale: "en-US",
      userAgent: "idol-openclaw-readonly-probe/1",
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
      this.pending.set(id, { resolve, reject, timer, method });
      this.ws.send(JSON.stringify({ type: "req", id, method, params }));
    });
  }

  close() {
    try { this.ws?.close(1000, "probe complete"); } catch {}
  }
}

async function connectOperatorWithSharedToken() {
  if (!SHARED_TOKEN) throw new Error("shared token unavailable");
  const connection = new GatewayConnection({
    auth: { token: SHARED_TOKEN },
    signatureToken: SHARED_TOKEN,
    clientId: "openclaw-probe",
    clientMode: "probe",
    role: "operator",
    scopes: ["operator.read"],
  });
  await connection.connect();
  return { connection, authSource: "shared-token" };
}

async function bootstrapOperator() {
  if (!BOOTSTRAP_TOKEN) throw new Error("bootstrap token unavailable");
  const node = new GatewayConnection({
    auth: { bootstrapToken: BOOTSTRAP_TOKEN },
    signatureToken: BOOTSTRAP_TOKEN,
    clientId: "openclaw-ios",
    clientMode: "node",
    role: "node",
    scopes: [],
    platform: "ios",
    deviceFamily: "github-actions-probe",
  });
  const hello = await node.connect();
  const handoffs = Array.isArray(hello?.auth?.deviceTokens) ? hello.auth.deviceTokens : [];
  const operator = handoffs.find((entry) => entry?.role === "operator" && typeof entry?.deviceToken === "string");
  node.close();
  if (!operator) throw new Error("bootstrap succeeded but did not issue an operator handoff token");
  const scopes = Array.isArray(operator.scopes) && operator.scopes.length ? operator.scopes : ["operator.read"];
  const connection = new GatewayConnection({
    auth: { deviceToken: operator.deviceToken },
    signatureToken: operator.deviceToken,
    clientId: "openclaw-control-ui",
    clientMode: "ui",
    role: "operator",
    scopes: scopes.includes("operator.read") ? ["operator.read"] : scopes,
  });
  await connection.connect();
  return { connection, authSource: "bootstrap-operator-handoff" };
}

async function establishOperator() {
  const attempts = [];
  if (SHARED_TOKEN) {
    try {
      return { ...(await connectOperatorWithSharedToken()), attempts };
    } catch (error) {
      attempts.push({ source: "shared-token", error: errorSummary(error) });
    }
  }
  if (BOOTSTRAP_TOKEN) {
    try {
      return { ...(await bootstrapOperator()), attempts };
    } catch (error) {
      attempts.push({ source: "bootstrap-token", error: errorSummary(error) });
    }
  }
  const error = new Error("all OpenClaw operator authentication paths failed");
  error.attempts = attempts;
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

async function main() {
  const startedAt = new Date().toISOString();
  let established;
  try {
    established = await establishOperator();
  } catch (error) {
    const report = {
      schema: "idol.openclaw.readonly-probe.v1",
      startedAt,
      completedAt: new Date().toISOString(),
      connected: false,
      error: errorSummary(error),
      attempts: sanitize(error.attempts ?? []),
    };
    console.log(JSON.stringify(report));
    process.exitCode = 1;
    return;
  }

  const { connection, authSource, attempts } = established;
  const advertised = new Set(Array.isArray(connection.hello?.features?.methods) ? connection.hello.features.methods : []);
  const report = {
    schema: "idol.openclaw.readonly-probe.v1",
    startedAt,
    completedAt: null,
    connected: true,
    authSource,
    priorAttempts: sanitize(attempts),
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
      report.probes[method] = { advertised: false, skipped: true };
      continue;
    }
    const began = Date.now();
    try {
      const payload = await connection.request(method, params);
      report.probes[method] = { advertised: advertised.has(method), ok: true, elapsedMs: Date.now() - began, payload: sanitize(payload) };
    } catch (error) {
      report.probes[method] = { advertised: advertised.has(method), ok: false, elapsedMs: Date.now() - began, error: errorSummary(error) };
    }
  }

  report.eventsObserved = Object.fromEntries([...connection.events.entries()].sort(([a], [b]) => a.localeCompare(b)));
  report.completedAt = new Date().toISOString();
  connection.close();
  console.log(JSON.stringify(report));
}

await main();
