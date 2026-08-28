import fs from "node:fs";
import crypto from "node:crypto";
import WebSocket from "ws";

const gatewayUrl = process.env.OPENCLAW_GATEWAY_URL ?? "wss://claw.idol.id";
const bootstrapToken = fs.readFileSync(process.env.OPENCLAW_BOOTSTRAP_TOKEN_FILE, "utf8").trim();
const managementPublicKey = fs.readFileSync(process.env.OPENCLAW_MANAGEMENT_PUBLIC_KEY_FILE, "utf8");
const resultFile = process.env.OPENCLAW_RESULT_FILE ?? "probe-result.json";
const envelopeFile = process.env.OPENCLAW_ENVELOPE_FILE ?? "management-envelope.json";
const timeoutMs = 60_000;

const client = {
  id: "openclaw-ios",
  displayName: "Idol Chief Architect",
  version: "2026.8.28",
  platform: "iOS 26.6",
  deviceFamily: "iPhone",
  mode: "node",
};

const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
const publicDer = publicKey.export({ format: "der", type: "spki" });
const publicRaw = Buffer.from(publicDer).subarray(-32);
const publicKeyBase64Url = publicRaw.toString("base64url");
const privateKeyPem = privateKey.export({ format: "pem", type: "pkcs8" }).toString();
const deviceId = crypto.createHash("sha256").update(publicRaw).digest("hex");

const secretKey = /(token|password|secret|credential|cookie|authorization|api.?key|private.?key|bootstrap)/i;
const contentKey = /^(message|messages|content|text|prompt|transcript|preview|history|reasoning|summary|body|output|lastMessage|initialPrompt)$/i;
const pathKey = /(^|_)(path|cwd|dir|root|home|workspace)(_|$)|repoRoot|stateDir/i;

function sanitize(value, key = "", depth = 0) {
  if (depth > 8) return "[depth-omitted]";
  if (secretKey.test(key)) return "[redacted]";
  if (contentKey.test(key)) return "[content-omitted]";
  if (pathKey.test(key)) return "[path-omitted]";
  if (value == null) return value;
  if (typeof value === "string") {
    const clean = value
      .replace(/[A-Fa-f0-9]{48,}/g, "[hex-redacted]")
      .replace(/(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}/g, "[key-redacted]");
    return clean.length > 600 ? `${clean.slice(0, 600)}…` : clean;
  }
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.slice(0, 200).map((entry) => sanitize(entry, key, depth + 1));
  if (typeof value === "object") {
    const out = {};
    for (const [childKey, childValue] of Object.entries(value)) {
      out[childKey] = sanitize(childValue, childKey, depth + 1);
    }
    return out;
  }
  return String(value);
}

function safeError(error) {
  return sanitize({
    code: error?.code,
    errorText: error?.message ?? String(error),
    details: error?.details,
    retryable: error?.retryable,
    retryAfterMs: error?.retryAfterMs,
    closeCode: error?.closeCode,
    closeReason: error?.closeReason,
  });
}

function signPayload({ role, scopes, signedAt, signToken, nonce }) {
  const payload = [
    "v2",
    deviceId,
    client.id,
    client.mode,
    role,
    scopes.join(","),
    String(signedAt),
    signToken ?? "",
    nonce ?? "",
  ].join("|");
  return crypto.sign(null, Buffer.from(payload, "utf8"), privateKey).toString("base64url");
}

function openConnection({ auth, signToken, role, scopes }) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(gatewayUrl, {
      handshakeTimeout: 25_000,
      perMessageDeflate: false,
      headers: {
        Origin: "https://claw.idol.id",
        "User-Agent": "idol-openclaw-bootstrap-inventory/0.5",
      },
    });
    const pending = new Map();
    const events = new Map();
    let serial = 0;
    let connectSent = false;
    let hello;

    function rejectAll(error) {
      for (const [id, waiter] of pending) {
        clearTimeout(waiter.timer);
        waiter.reject(error);
        pending.delete(id);
      }
    }

    function request(method, params = {}, callTimeout = timeoutMs) {
      return new Promise((requestResolve, requestReject) => {
        if (ws.readyState !== WebSocket.OPEN) {
          requestReject({ code: "SOCKET_NOT_OPEN", message: `socket state ${ws.readyState}` });
          return;
        }
        const id = `idol-${++serial}-${crypto.randomUUID()}`;
        const timer = setTimeout(() => {
          pending.delete(id);
          requestReject({ code: "CLIENT_TIMEOUT", message: `timeout:${method}` });
        }, callTimeout);
        pending.set(id, { resolve: requestResolve, reject: requestReject, timer });
        ws.send(JSON.stringify({ type: "req", id, method, params }));
      });
    }

    async function sendConnect(challenge = {}) {
      if (connectSent) return;
      connectSent = true;
      const nonce = typeof challenge.nonce === "string" ? challenge.nonce : "";
      const signedAt = Number.isSafeInteger(challenge.ts) && challenge.ts >= 0 ? challenge.ts : Date.now();
      const signature = signPayload({ role, scopes, signedAt, signToken, nonce });
      try {
        hello = await request("connect", {
          minProtocol: 4,
          maxProtocol: 4,
          client,
          role,
          scopes,
          caps: ["agent-kind", "task-suggestions", "tool-events"],
          commands: [],
          permissions: {},
          auth,
          locale: "en-US",
          userAgent: "idol-openclaw-bootstrap-inventory/0.5",
          device: {
            id: deviceId,
            publicKey: publicKeyBase64Url,
            signature,
            signedAt,
            nonce,
          },
        });
        resolve({ ws, hello, request, events });
      } catch (error) {
        try { ws.close(4008, "connect-failed"); } catch {}
        reject(error);
      }
    }

    ws.on("message", (raw) => {
      let frame;
      try { frame = JSON.parse(raw.toString("utf8")); } catch { return; }
      if (frame?.type === "event" && typeof frame.event === "string") {
        events.set(frame.event, (events.get(frame.event) ?? 0) + 1);
        if (frame.event === "connect.challenge") void sendConnect(frame.payload ?? {});
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

    ws.once("open", () => {
      setTimeout(() => { if (!connectSent) void sendConnect({}); }, 3000);
    });
    ws.once("error", (error) => {
      rejectAll(error);
      if (!hello) reject(error);
    });
    ws.once("close", (code, reason) => {
      const error = {
        code: "SOCKET_CLOSED",
        message: "gateway closed the WebSocket",
        closeCode: code,
        closeReason: reason.toString("utf8"),
      };
      rejectAll(error);
      if (!hello) reject(error);
    });
  });
}

function summarizeSessions(payload) {
  const rows = Array.isArray(payload) ? payload : Array.isArray(payload?.sessions) ? payload.sessions : [];
  return {
    count: rows.length,
    sessions: rows.slice(0, 300).map((row) => sanitize({
      sessionId: row?.sessionId ?? row?.id,
      key: row?.key,
      agentId: row?.agentId,
      provider: row?.provider,
      model: row?.model,
      kind: row?.kind,
      state: row?.state,
      status: row?.status,
      channel: row?.channel,
      owner: row?.owner,
      createdAt: row?.createdAt,
      updatedAt: row?.updatedAt,
      startedAt: row?.startedAt,
      endedAt: row?.endedAt,
      usage: row?.usage,
    })),
  };
}

function summarizeTasks(payload) {
  const rows = Array.isArray(payload) ? payload : Array.isArray(payload?.tasks) ? payload.tasks : [];
  return {
    count: rows.length,
    tasks: rows.slice(0, 300).map((row) => sanitize({
      id: row?.id ?? row?.taskId,
      agentId: row?.agentId,
      provider: row?.provider,
      model: row?.model,
      state: row?.state,
      status: row?.status,
      priority: row?.priority,
      createdAt: row?.createdAt,
      updatedAt: row?.updatedAt,
      startedAt: row?.startedAt,
      completedAt: row?.completedAt,
      runId: row?.runId,
      sessionId: row?.sessionId,
    })),
  };
}

function summarizeCron(payload) {
  const rows = Array.isArray(payload) ? payload : Array.isArray(payload?.jobs) ? payload.jobs : [];
  return {
    count: rows.length,
    jobs: rows.slice(0, 200).map((row) => sanitize({
      id: row?.id,
      name: row?.name,
      enabled: row?.enabled,
      schedule: row?.schedule,
      agentId: row?.agentId,
      provider: row?.provider,
      model: row?.model,
      nextRunAt: row?.nextRunAt,
      lastRunAt: row?.lastRunAt,
      lastStatus: row?.lastStatus,
    })),
  };
}

function summarizeDevices(payload) {
  const pending = Array.isArray(payload?.pending) ? payload.pending : [];
  const paired = Array.isArray(payload?.paired) ? payload.paired : [];
  const shape = (row) => sanitize({
    requestId: row?.requestId,
    deviceId: typeof row?.deviceId === "string" ? row.deviceId.slice(0, 16) : row?.deviceId,
    displayName: row?.displayName,
    operatorLabel: row?.operatorLabel,
    roles: row?.roles,
    scopes: row?.scopes ?? row?.approvedScopes,
    clientId: row?.clientId,
    clientMode: row?.clientMode,
    platform: row?.platform,
    deviceFamily: row?.deviceFamily,
    connected: row?.connected,
    approvedVia: row?.approvedVia,
    createdAtMs: row?.createdAtMs,
    approvedAtMs: row?.approvedAtMs,
    lastSeenAtMs: row?.lastSeenAtMs,
  });
  return {
    pendingCount: pending.length,
    pairedCount: paired.length,
    pending: pending.slice(0, 100).map(shape),
    paired: paired.slice(0, 200).map(shape),
  };
}

function summarizeConfig(payload) {
  const config = payload?.config ?? payload;
  const agents = config?.agents;
  const providers = config?.providers ?? config?.models?.providers;
  const channels = config?.channels;
  return sanitize({
    hash: payload?.hash,
    configRevisionHash: payload?.configRevisionHash,
    appliedConfigHash: payload?.appliedConfigHash,
    topLevelSections: config && typeof config === "object" ? Object.keys(config).sort() : [],
    agentIds: agents && typeof agents === "object" ? Object.keys(agents).filter((key) => key !== "defaults").sort() : [],
    agentDefaults: agents?.defaults,
    providerIds: providers && typeof providers === "object" ? Object.keys(providers).sort() : [],
    channelIds: channels && typeof channels === "object" ? Object.keys(channels).sort() : [],
    gateway: config?.gateway,
  });
}

function project(method, payload) {
  if (method === "sessions.list") return summarizeSessions(payload);
  if (method === "tasks.list") return summarizeTasks(payload);
  if (method === "cron.list") return summarizeCron(payload);
  if (method === "device.pair.list") return summarizeDevices(payload);
  if (method === "config.get") return summarizeConfig(payload);
  return sanitize(payload);
}

function encryptManagementBundle(bundle) {
  const key = crypto.randomBytes(32);
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(bundle), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  const encryptedKey = crypto.publicEncrypt({
    key: managementPublicKey,
    padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
    oaepHash: "sha256",
  }, key);
  return {
    schema: "idol.openclaw.management-envelope.v1",
    algorithm: "RSA-OAEP-SHA256+A256GCM",
    encryptedKey: encryptedKey.toString("base64"),
    iv: iv.toString("base64"),
    tag: tag.toString("base64"),
    ciphertext: ciphertext.toString("base64"),
  };
}

const report = {
  schema: "idol.openclaw.bootstrap-inventory.v1",
  observedAt: new Date().toISOString(),
  gatewayUrl,
  outcome: "unknown",
  bootstrap: {},
  operator: {},
  advertised: {},
  probes: {},
};

let bootstrapConnection;
let operatorConnection;
try {
  bootstrapConnection = await openConnection({
    auth: { bootstrapToken },
    signToken: bootstrapToken,
    role: "node",
    scopes: [],
  });
  const bootstrapHello = bootstrapConnection.hello;
  const handoffs = Array.isArray(bootstrapHello?.auth?.deviceTokens) ? bootstrapHello.auth.deviceTokens : [];
  const operatorHandoff = handoffs.find((entry) => entry?.role === "operator");
  const operatorToken = operatorHandoff?.deviceToken;
  const operatorScopes = Array.isArray(operatorHandoff?.scopes) ? operatorHandoff.scopes : [];
  if (!operatorToken) throw { code: "NO_OPERATOR_HANDOFF", message: "bootstrap did not issue an operator token" };

  report.bootstrap = sanitize({
    role: bootstrapHello?.auth?.role,
    scopes: bootstrapHello?.auth?.scopes,
    issuedRoles: handoffs.map((entry) => ({ role: entry?.role, scopes: entry?.scopes })),
    server: bootstrapHello?.server,
    protocol: bootstrapHello?.protocol,
  });
  bootstrapConnection.ws.close(1000, "bootstrap-complete");

  operatorConnection = await openConnection({
    auth: { deviceToken: operatorToken },
    signToken: operatorToken,
    role: "operator",
    scopes: operatorScopes,
  });
  const hello = operatorConnection.hello;
  const methods = Array.isArray(hello?.features?.methods) ? hello.features.methods : [];
  const methodSet = new Set(methods);
  report.outcome = "connected-operator";
  report.operator = sanitize({
    role: hello?.auth?.role,
    scopes: hello?.auth?.scopes,
    server: hello?.server,
    protocol: hello?.protocol,
    policy: hello?.policy,
    snapshot: {
      appliedConfigHash: hello?.snapshot?.appliedConfigHash,
      stateVersion: hello?.snapshot?.stateVersion,
      health: hello?.snapshot?.health,
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
    ["sessions.list", { limit: 200, ownerFirst: true }],
    ["tasks.list", { limit: 200 }],
    ["cron.status", {}],
    ["cron.list", {}],
    ["channels.status", {}],
    ["environments.list", {}],
    ["config.get", {}],
    ["usage.status", {}],
    ["device.pair.list", {}],
    ["node.list", {}],
    ["tools.catalog", {}],
    ["agent.capabilities", {}],
  ];

  for (const [method, params] of specs) {
    if (!methodSet.has(method)) {
      report.probes[method] = { ok: false, skipped: "not-advertised" };
      continue;
    }
    const started = Date.now();
    try {
      const payload = await operatorConnection.request(method, params);
      report.probes[method] = { ok: true, elapsedMs: Date.now() - started, payload: project(method, payload) };
    } catch (error) {
      report.probes[method] = { ok: false, elapsedMs: Date.now() - started, error: safeError(error) };
    }
  }

  const envelope = encryptManagementBundle({
    schema: "idol.openclaw.management-credential.v1",
    gatewayUrl,
    deviceId,
    publicKey: publicKeyBase64Url,
    privateKeyPem,
    operatorToken,
    scopes: operatorScopes,
    client,
    issuedAt: new Date().toISOString(),
  });
  fs.writeFileSync(envelopeFile, JSON.stringify(envelope, null, 2));
} catch (error) {
  report.outcome = "bootstrap-or-operator-connect-failed";
  report.error = safeError(error);
} finally {
  report.eventsObserved = {
    bootstrap: bootstrapConnection ? Object.fromEntries([...bootstrapConnection.events.entries()].sort(([a], [b]) => a.localeCompare(b))) : {},
    operator: operatorConnection ? Object.fromEntries([...operatorConnection.events.entries()].sort(([a], [b]) => a.localeCompare(b))) : {},
  };
  fs.writeFileSync(resultFile, JSON.stringify(report, null, 2));
  try { bootstrapConnection?.ws.close(1000, "done"); } catch {}
  try { operatorConnection?.ws.close(1000, "done"); } catch {}
}
