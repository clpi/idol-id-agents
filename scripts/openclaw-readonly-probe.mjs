import fs from "node:fs";
import crypto from "node:crypto";
import WebSocket from "ws";

const tokenFile = process.env.OPENCLAW_TOKEN_FILE ?? "/tmp/idol-openclaw-token";
const resultFile = process.env.OPENCLAW_RESULT_FILE ?? "probe-result.json";
const token = fs.readFileSync(tokenFile, "utf8").trim();
const gatewayUrl = process.env.OPENCLAW_GATEWAY_URL ?? "wss://claw.idol.id";
const timeoutMs = 45_000;
const pending = new Map();
const events = new Map();
let serial = 0;
let connectPromise;
let connected = false;

const sensitiveKey = /(token|password|secret|credential|cookie|authorization|api.?key|private.?key|bootstrap)/i;
const contentKey = /^(message|messages|content|text|prompt|transcript|preview|history|reasoning|summary|body|output)$/i;
const pathKey = /(^|_)(path|cwd|dir|root|home|workspace)(_|$)|repoRoot|stateDir/i;

function sanitize(value, key = "", depth = 0) {
  if (depth > 8) return "[depth-omitted]";
  if (sensitiveKey.test(key)) return "[redacted]";
  if (contentKey.test(key)) return "[content-omitted]";
  if (pathKey.test(key)) return "[path-omitted]";
  if (value === null || value === undefined) return value;
  if (typeof value === "string") return value.length > 500 ? `${value.slice(0, 500)}…` : value;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.slice(0, 100).map((entry) => sanitize(entry, key, depth + 1));
  if (typeof value === "object") {
    const out = {};
    for (const [childKey, childValue] of Object.entries(value)) {
      out[childKey] = sanitize(childValue, childKey, depth + 1);
    }
    return out;
  }
  return String(value);
}

function compactError(error) {
  if (!error) return { message: "unknown" };
  return sanitize({
    code: error.code,
    message: error.message ?? String(error),
    details: error.details,
    retryable: error.retryable,
    retryAfterMs: error.retryAfterMs,
    closeCode: error.closeCode,
    closeReason: error.closeReason,
  });
}

const ws = new WebSocket(gatewayUrl, {
  handshakeTimeout: 20_000,
  perMessageDeflate: false,
  headers: { "User-Agent": "idol-openclaw-readonly-probe/0.2" },
});

function rejectPending(error) {
  for (const [id, waiter] of pending) {
    clearTimeout(waiter.timer);
    waiter.reject(error);
    pending.delete(id);
  }
}

function request(method, params = {}, callTimeoutMs = timeoutMs) {
  return new Promise((resolve, reject) => {
    if (ws.readyState !== WebSocket.OPEN) {
      reject({ code: "SOCKET_NOT_OPEN", message: `socket state ${ws.readyState}` });
      return;
    }
    const id = `idol-${++serial}-${crypto.randomUUID()}`;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject({ code: "CLIENT_TIMEOUT", message: `timeout:${method}` });
    }, callTimeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    ws.send(JSON.stringify({ type: "req", id, method, params }));
  });
}

function sendConnect() {
  if (!connectPromise) {
    connectPromise = request("connect", {
      minProtocol: 4,
      maxProtocol: 4,
      client: {
        id: "gateway-client",
        version: "0.2.0",
        platform: "linux",
        mode: "backend",
      },
      role: "operator",
      scopes: ["operator.read"],
      caps: ["agent-kind"],
      commands: [],
      permissions: {},
      auth: { token },
      locale: "en-US",
      userAgent: "idol-openclaw-readonly-probe/0.2",
    }, 45_000);
  }
  return connectPromise;
}

ws.on("message", (raw) => {
  let frame;
  try {
    frame = JSON.parse(raw.toString("utf8"));
  } catch {
    return;
  }

  if (frame?.type === "event" && typeof frame.event === "string") {
    events.set(frame.event, (events.get(frame.event) ?? 0) + 1);
    if (frame.event === "connect.challenge") void sendConnect();
    return;
  }

  if (frame?.type === "res" && typeof frame.id === "string") {
    const waiter = pending.get(frame.id);
    if (!waiter) return;
    clearTimeout(waiter.timer);
    pending.delete(frame.id);
    if (frame.ok) waiter.resolve(frame.payload);
    else waiter.reject(frame.error ?? { message: "gateway request failed" });
  }
});

ws.on("close", (code, reason) => {
  rejectPending({
    code: "SOCKET_CLOSED",
    message: "gateway closed the WebSocket",
    closeCode: code,
    closeReason: reason.toString("utf8"),
  });
});

const opened = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject({ code: "OPEN_TIMEOUT", message: "websocket open timeout" }), 25_000);
  ws.once("open", () => {
    clearTimeout(timer);
    setTimeout(() => { if (!connectPromise) void sendConnect(); }, 1500);
    resolve();
  });
  ws.once("error", (error) => {
    clearTimeout(timer);
    reject(error);
  });
});

async function probe(method, params = {}) {
  const started = Date.now();
  try {
    const payload = await request(method, params);
    return { ok: true, elapsedMs: Date.now() - started, payload: sanitize(payload) };
  } catch (error) {
    return { ok: false, elapsedMs: Date.now() - started, error: compactError(error) };
  }
}

const report = {
  schema: "idol.openclaw.probe.v1",
  observedAt: new Date().toISOString(),
  gatewayUrl,
  outcome: "unknown",
  gateway: {},
  advertised: {},
  probes: {},
};

try {
  await opened;
  const hello = await sendConnect();
  connected = true;
  const methods = Array.isArray(hello?.features?.methods) ? hello.features.methods : [];
  const methodSet = new Set(methods);

  report.outcome = "connected-readonly";
  report.gateway = sanitize({
    protocol: hello?.protocol,
    server: hello?.server,
    auth: hello?.auth,
    policy: hello?.policy,
    snapshot: {
      appliedConfigHash: hello?.snapshot?.appliedConfigHash,
      health: hello?.snapshot?.health,
      stateVersion: hello?.snapshot?.stateVersion,
    },
  });
  report.advertised = {
    methodCount: methods.length,
    methods: methods.slice().sort(),
    eventCount: Array.isArray(hello?.features?.events) ? hello.features.events.length : null,
  };

  const specs = [
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
    ["environments.list", {}],
    ["config.get", {}],
    ["usage.status", {}],
  ];

  for (const [method, params] of specs) {
    report.probes[method] = methodSet.has(method)
      ? await probe(method, params)
      : { ok: false, skipped: "not-advertised" };
  }
} catch (error) {
  report.outcome = connected ? "probe-failed" : "connect-failed";
  report.error = compactError(error);
} finally {
  report.eventsObserved = Object.fromEntries([...events.entries()].sort(([a], [b]) => a.localeCompare(b)));
  fs.writeFileSync(resultFile, JSON.stringify(report, null, 2));
  try { ws.close(1000, "probe-complete"); } catch {}
}
