import crypto from "node:crypto";
import process from "node:process";
import WebSocket from "ws";

const gatewayUrl = process.env.OPENCLAW_GATEWAY_URL;
const gatewayToken = process.env.OPENCLAW_GATEWAY_TOKEN;
const bootstrapToken = process.env.OPENCLAW_BOOTSTRAP_TOKEN;
const timeoutMs = Number(process.env.OPENCLAW_PROBE_TIMEOUT_MS ?? 120_000);

if (!gatewayUrl || (!gatewayToken && !bootstrapToken)) {
  console.error("IDOL_OPENCLAW_PROBE_ERROR=missing-required-environment");
  process.exit(2);
}

const sensitiveKey = /(token|password|secret|credential|cookie|authorization|api.?key|private.?key|public.?key|bootstrap|signature|recoveryScope)/i;
const contentKey = /(message|messages|content|text|prompt|transcript|preview|history|summary|reasoning)/i;
const pathKey = /(^|_)(path|cwd|dir|root|home|workspace)(_|$)|repoRoot|stateDir/i;

function sanitizeUrl(value) {
  try {
    const url = new URL(value);
    return `${url.protocol}//${url.host}${url.pathname}`;
  } catch {
    return value;
  }
}

function sanitize(value, key = "", depth = 0) {
  if (depth > 8) return "[depth-omitted]";
  if (sensitiveKey.test(key)) return "[redacted]";
  if (contentKey.test(key)) return "[content-omitted]";
  if (pathKey.test(key)) return "[path-omitted]";
  if (value === null || value === undefined) return value;
  if (typeof value === "string") {
    const clean = /^wss?:\/\//i.test(value) || /^https?:\/\//i.test(value)
      ? sanitizeUrl(value)
      : value;
    return clean.length > 1000 ? `${clean.slice(0, 1000)}…` : clean;
  }
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (Array.isArray(value)) {
    return value.slice(0, 250).map((entry) => sanitize(entry, key, depth + 1));
  }
  if (typeof value === "object") {
    const out = {};
    for (const [childKey, childValue] of Object.entries(value)) {
      out[childKey] = sanitize(childValue, childKey, depth + 1);
    }
    return out;
  }
  return String(value);
}

function rpcErrorSummary(error) {
  if (!error || typeof error !== "object") return { message: String(error) };
  return sanitize({
    code: error.code,
    message: error.message,
    details: error.details,
    retryable: error.retryable,
    retryAfterMs: error.retryAfterMs,
  });
}

class GatewayRpcError extends Error {
  constructor(rpcError) {
    super(rpcError?.message ?? "gateway RPC failed");
    this.rpcError = rpcError;
  }
}

const ws = new WebSocket(gatewayUrl, {
  handshakeTimeout: 20_000,
  perMessageDeflate: false,
  headers: { "User-Agent": "idol-openclaw-probe/0.2" },
});

let requestNumber = 0;
const pending = new Map();
const eventCounts = new Map();
let challengeResolve;
let challengeReject;
const challenge = new Promise((resolve, reject) => {
  challengeResolve = resolve;
  challengeReject = reject;
});

function call(method, params = {}, methodTimeoutMs = timeoutMs) {
  return new Promise((resolve, reject) => {
    if (ws.readyState !== WebSocket.OPEN) {
      reject(new Error(`socket-not-open:${ws.readyState}`));
      return;
    }
    const id = `idol-probe-${++requestNumber}-${crypto.randomUUID()}`;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`rpc-timeout:${method}`));
    }, methodTimeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    ws.send(JSON.stringify({ type: "req", id, method, params }));
  });
}

ws.on("message", (raw) => {
  let frame;
  try {
    frame = JSON.parse(raw.toString("utf8"));
  } catch {
    return;
  }

  if (frame?.type === "res" && typeof frame.id === "string") {
    const waiter = pending.get(frame.id);
    if (!waiter) return;
    clearTimeout(waiter.timer);
    pending.delete(frame.id);
    if (frame.ok) waiter.resolve(frame.payload);
    else waiter.reject(new GatewayRpcError(frame.error));
    return;
  }

  if (frame?.type === "event" && typeof frame.event === "string") {
    eventCounts.set(frame.event, (eventCounts.get(frame.event) ?? 0) + 1);
    if (frame.event === "connect.challenge") {
      challengeResolve(frame.payload ?? {});
    }
  }
});

ws.once("error", (error) => challengeReject(error));

function opened() {
  return new Promise((resolve, reject) => {
    if (ws.readyState === WebSocket.OPEN) return resolve();
    const timer = setTimeout(() => reject(new Error("websocket-open-timeout")), 25_000);
    ws.once("open", () => {
      clearTimeout(timer);
      resolve();
    });
    ws.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });
}

async function withDeadline(promise, ms, label) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label}-timeout`)), ms);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function createDeviceProof(params) {
  const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
  const publicJwk = publicKey.export({ format: "jwk" });
  if (publicJwk.kty !== "OKP" || publicJwk.crv !== "Ed25519" || !publicJwk.x) {
    throw new Error("unexpected-ed25519-public-key-shape");
  }
  const rawPublicKey = Buffer.from(publicJwk.x, "base64url");
  const deviceId = crypto.createHash("sha256").update(rawPublicKey).digest("hex");
  const signaturePayload = [
    "v2",
    deviceId,
    params.clientId,
    params.clientMode,
    params.role,
    params.scopes.join(","),
    String(params.signedAt),
    params.authMaterial ?? "",
    params.nonce,
  ].join("|");
  const signature = crypto.sign(null, Buffer.from(signaturePayload, "utf8"), privateKey);
  return {
    id: deviceId,
    publicKey: publicJwk.x,
    signature: signature.toString("base64url"),
    signedAt: params.signedAt,
    nonce: params.nonce,
  };
}

async function probe(method, params = {}, methodTimeoutMs = timeoutMs) {
  const startedAt = Date.now();
  try {
    const payload = await call(method, params, methodTimeoutMs);
    return {
      ok: true,
      elapsedMs: Date.now() - startedAt,
      payload: sanitize(payload),
    };
  } catch (error) {
    return {
      ok: false,
      elapsedMs: Date.now() - startedAt,
      error: error instanceof GatewayRpcError
        ? rpcErrorSummary(error.rpcError)
        : { message: sanitize(String(error?.message ?? error)) },
    };
  }
}

async function main() {
  await opened();

  const clientId = "gateway-client";
  const clientMode = "backend";
  const role = "operator";
  const bootstrapScopes = [
    "operator.admin",
    "operator.approvals",
    "operator.questions",
    "operator.read",
    "operator.talk.secrets",
    "operator.write",
  ];
  const tokenScopes = ["operator.read"];
  const scopes = bootstrapToken ? bootstrapScopes : tokenScopes;
  const connectChallenge = await withDeadline(challenge, 10_000, "connect-challenge");
  const nonce = typeof connectChallenge?.nonce === "string" ? connectChallenge.nonce : "";
  const signedAt = Number(connectChallenge?.ts);
  if (!nonce || !Number.isSafeInteger(signedAt) || signedAt < 0) {
    throw new Error("invalid-connect-challenge");
  }
  const authMaterial = bootstrapToken ?? gatewayToken;
  const device = bootstrapToken
    ? createDeviceProof({
        clientId,
        clientMode,
        role,
        scopes,
        signedAt,
        nonce,
        authMaterial,
      })
    : undefined;

  const hello = await call("connect", {
    minProtocol: 4,
    maxProtocol: 4,
    client: {
      id: clientId,
      version: "0.2.0",
      platform: "linux",
      mode: clientMode,
      displayName: "Idol authority probe",
    },
    role,
    scopes,
    caps: ["tool-events", "session-scoped-events", "usage-refreshing"],
    commands: [],
    permissions: {},
    auth: bootstrapToken ? { bootstrapToken } : { token: gatewayToken },
    ...(device ? { device } : {}),
    locale: "en-US",
    userAgent: "idol-openclaw-probe/0.2",
  }, 60_000);

  const advertised = new Set(
    Array.isArray(hello?.features?.methods) ? hello.features.methods : [],
  );

  const report = {
    schema: "idol.openclaw.probe.v2",
    observedAt: new Date().toISOString(),
    authMode: bootstrapToken ? "signed-bootstrap-device" : "shared-token",
    gateway: sanitize({
      protocol: hello?.protocol,
      server: hello?.server,
      auth: hello?.auth,
      policy: hello?.policy,
      appliedConfigHash: hello?.snapshot?.appliedConfigHash,
    }),
    advertised: {
      methodCount: advertised.size,
      methods: [...advertised].sort(),
      eventCount: Array.isArray(hello?.features?.events) ? hello.features.events.length : null,
    },
    probes: {},
  };

  const specifications = [
    ["health", {}, 45_000],
    ["status", {}, 45_000],
    ["system.info", {}, 45_000],
    ["system-presence", {}, 45_000],
    ["users.self", {}, 45_000],
    ["agents.list", {}, 60_000],
    ["models.list", {}, 90_000],
    ["models.authStatus", {}, 90_000],
    ["sessions.list", { limit: 100, ownerFirst: true }, 150_000],
    ["tasks.list", { limit: 100 }, 60_000],
    ["cron.status", {}, 60_000],
    ["cron.list", {}, 90_000],
    ["channels.status", {}, 90_000],
    ["environments.list", {}, 60_000],
    ["projects.list", {}, 60_000],
    ["plugins.list", {}, 90_000],
    ["tools.catalog", {}, 90_000],
    ["skills.status", {}, 90_000],
    ["usage.status", {}, 120_000],
    ["usage.cost", {}, 120_000],
  ];

  for (const [method, params, methodTimeoutMs] of specifications) {
    report.probes[method] = await probe(method, params, methodTimeoutMs);
  }

  report.eventsObserved = Object.fromEntries(
    [...eventCounts.entries()].sort(([a], [b]) => a.localeCompare(b)),
  );

  console.log(`IDOL_OPENCLAW_PROBE_V2=${JSON.stringify(report)}`);
  ws.close(1000, "probe-complete");
}

main().catch((error) => {
  const safe = error instanceof GatewayRpcError
    ? rpcErrorSummary(error.rpcError)
    : { message: sanitize(String(error?.message ?? error)) };
  console.error(`IDOL_OPENCLAW_PROBE_ERROR=${JSON.stringify(safe)}`);
  try {
    ws.close(1011, "probe-failed");
  } catch {
    // Ignore close failures during terminal reporting.
  }
  process.exitCode = 1;
});
