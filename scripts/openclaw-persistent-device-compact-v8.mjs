import fs from "node:fs";
import crypto from "node:crypto";
import WebSocket from "ws";

const input = JSON.parse(fs.readFileSync(process.env.OPENCLAW_INPUT_FILE, "utf8"));
const managementPublicKey = fs.readFileSync(process.env.OPENCLAW_MANAGEMENT_PUBLIC_KEY_FILE, "utf8");
const resultFile = process.env.OPENCLAW_RESULT_FILE ?? "connect-result.json";
const envelopeFile = process.env.OPENCLAW_ENVELOPE_FILE ?? "credential-envelope.json";
const gatewayUrl = "wss://claw.idol.id";
const privateKey = crypto.createPrivateKey({ key: Buffer.from(input.k, "base64"), format: "der", type: "pkcs8" });
const publicKey = crypto.createPublicKey(privateKey);
const publicRaw = Buffer.from(publicKey.export({ format: "der", type: "spki" })).subarray(-32);
const device = {
  schema: "idol.openclaw.persistent-device.v1",
  gatewayUrl,
  deviceId: crypto.createHash("sha256").update(publicRaw).digest("hex"),
  publicKey: publicRaw.toString("base64url"),
  privateKeyPem: privateKey.export({ format: "pem", type: "pkcs8" }).toString(),
  client: { id: "openclaw-control-ui", displayName: "Idol Chief Architect", version: "2026.8.28", platform: "web", mode: "webchat" },
  role: "operator",
  scopes: ["operator.read", "operator.write", "operator.admin", "operator.approvals", "operator.questions", "operator.pairing"],
};
const sharedToken = input.t;
const timeoutMs = 60_000;

function sanitize(value, key = "", depth = 0) {
  if (depth > 8) return "[depth-omitted]";
  if (/(token|password|secret|credential|cookie|authorization|api.?key|private.?key|bootstrap)/i.test(key)) return "[redacted]";
  if (/^(message|messages|content|text|prompt|transcript|preview|history|reasoning|summary|body|output)$/i.test(key)) return "[content-omitted]";
  if (/(^|_)(path|cwd|dir|root|home|workspace)(_|$)|repoRoot|stateDir/i.test(key)) return "[path-omitted]";
  if (value == null) return value;
  if (typeof value === "string") return value.length > 600 ? `${value.slice(0, 600)}…` : value;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.slice(0, 200).map((entry) => sanitize(entry, key, depth + 1));
  if (typeof value === "object") {
    const out = {};
    for (const [childKey, childValue] of Object.entries(value)) out[childKey] = sanitize(childValue, childKey, depth + 1);
    return out;
  }
  return String(value);
}

function safeError(error) {
  return sanitize({ code: error?.code, errorText: error?.message ?? String(error), details: error?.details, retryable: error?.retryable, retryAfterMs: error?.retryAfterMs, closeCode: error?.closeCode, closeReason: error?.closeReason });
}

function encryptBundle(bundle) {
  const key = crypto.randomBytes(32);
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(bundle), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  const encryptedKey = crypto.publicEncrypt({ key: managementPublicKey, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256" }, key);
  return { schema: "idol.openclaw.credential-envelope.v1", algorithm: "RSA-OAEP-SHA256+A256GCM", encryptedKey: encryptedKey.toString("base64"), iv: iv.toString("base64"), tag: tag.toString("base64"), ciphertext: ciphertext.toString("base64") };
}

function signPayload(nonce, signedAt, signToken) {
  const payload = ["v2", device.deviceId, device.client.id, device.client.mode, device.role, device.scopes.join(","), String(signedAt), signToken ?? "", nonce ?? ""].join("|");
  return crypto.sign(null, Buffer.from(payload, "utf8"), privateKey).toString("base64url");
}

const ws = new WebSocket(gatewayUrl, { handshakeTimeout: 25_000, perMessageDeflate: false, headers: { Origin: "https://claw.idol.id", "User-Agent": "idol-openclaw-persistent-device/0.8" } });
const pending = new Map();
const events = new Map();
let serial = 0;
let sent = false;
let finished = false;

function request(method, params = {}, callTimeout = timeoutMs) {
  return new Promise((resolve, reject) => {
    if (ws.readyState !== WebSocket.OPEN) return reject({ code: "SOCKET_NOT_OPEN", message: `socket state ${ws.readyState}` });
    const id = `idol-${++serial}-${crypto.randomUUID()}`;
    const timer = setTimeout(() => { pending.delete(id); reject({ code: "CLIENT_TIMEOUT", message: `timeout:${method}` }); }, callTimeout);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ type: "req", id, method, params }));
  });
}

async function sendConnect(challenge = {}) {
  if (sent) return;
  sent = true;
  const nonce = typeof challenge.nonce === "string" ? challenge.nonce : "";
  const signedAt = Number.isSafeInteger(challenge.ts) ? challenge.ts : Date.now();
  const deviceToken = input.d || undefined;
  const auth = deviceToken ? { deviceToken } : { token: sharedToken };
  const signToken = deviceToken ?? sharedToken;
  try {
    const hello = await request("connect", {
      minProtocol: 4, maxProtocol: 4, client: device.client, role: device.role, scopes: device.scopes,
      caps: ["agent-kind", "approvals", "task-suggestions", "tool-events"], commands: [], permissions: {}, auth,
      locale: "en-US", userAgent: "idol-openclaw-persistent-device/0.8",
      device: { id: device.deviceId, publicKey: device.publicKey, signature: signPayload(nonce, signedAt, signToken), signedAt, nonce },
    });
    const methods = Array.isArray(hello?.features?.methods) ? hello.features.methods : [];
    const probes = {};
    for (const [method, params] of [["health", {}], ["status", {}], ["device.pair.list", {}], ["agents.list", {}], ["models.list", {}], ["sessions.list", { limit: 100 }], ["tasks.list", { limit: 100 }], ["cron.list", {}], ["channels.status", {}], ["config.get", {}]]) {
      if (!methods.includes(method)) continue;
      try { probes[method] = { ok: true, payload: sanitize(await request(method, params)) }; }
      catch (error) { probes[method] = { ok: false, error: safeError(error) }; }
    }
    fs.writeFileSync(resultFile, JSON.stringify({ schema: "idol.openclaw.persistent-connect.v1", observedAt: new Date().toISOString(), outcome: "connected", deviceId: device.deviceId, deviceIdPrefix: device.deviceId.slice(0, 16), role: hello?.auth?.role, scopes: hello?.auth?.scopes, deviceTokenIssued: Boolean(hello?.auth?.deviceToken), recoveryScopeIssued: Boolean(hello?.auth?.recoveryScope), server: sanitize(hello?.server), methodCount: methods.length, probes }, null, 2));
    fs.writeFileSync(envelopeFile, JSON.stringify(encryptBundle({ schema: "idol.openclaw.persistent-credential.v1", gatewayUrl, device, sharedToken, deviceToken: hello?.auth?.deviceToken ?? deviceToken, scopes: hello?.auth?.scopes, recoveryScope: hello?.auth?.recoveryScope, issuedAt: new Date().toISOString() }), null, 2));
    finished = true;
    ws.close(1000, "complete");
  } catch (error) {
    fs.writeFileSync(resultFile, JSON.stringify({ schema: "idol.openclaw.persistent-connect.v1", observedAt: new Date().toISOString(), outcome: error?.code === "PAIRING_REQUIRED" ? "pairing-required" : "connect-failed", deviceId: device.deviceId, deviceIdPrefix: device.deviceId.slice(0, 16), error: safeError(error), events: Object.fromEntries(events) }, null, 2));
    fs.writeFileSync(envelopeFile, JSON.stringify(encryptBundle({ schema: "idol.openclaw.pending-device.v1", gatewayUrl, device, sharedToken, pairing: error?.details, createdAt: new Date().toISOString() }), null, 2));
    finished = true;
    ws.close(4008, "pairing-pending");
  }
}

ws.on("message", (raw) => {
  let frame; try { frame = JSON.parse(raw.toString("utf8")); } catch { return; }
  if (frame?.type === "event" && typeof frame.event === "string") { events.set(frame.event, (events.get(frame.event) ?? 0) + 1); if (frame.event === "connect.challenge") void sendConnect(frame.payload ?? {}); return; }
  if (frame?.type === "res" && typeof frame.id === "string") { const waiter = pending.get(frame.id); if (!waiter) return; clearTimeout(waiter.timer); pending.delete(frame.id); if (frame.ok) waiter.resolve(frame.payload); else waiter.reject(frame.error ?? { message: "gateway request failed" }); }
});
ws.once("open", () => setTimeout(() => void sendConnect({}), 2500));
ws.once("error", (error) => { if (!finished) fs.writeFileSync(resultFile, JSON.stringify({ schema: "idol.openclaw.persistent-connect.v1", outcome: "transport-error", error: safeError(error) }, null, 2)); });
ws.once("close", (code, reason) => { if (!finished) fs.writeFileSync(resultFile, JSON.stringify({ schema: "idol.openclaw.persistent-connect.v1", outcome: "socket-closed", error: safeError({ code: "SOCKET_CLOSED", message: "gateway closed socket", closeCode: code, closeReason: reason.toString("utf8") }) }, null, 2)); });
