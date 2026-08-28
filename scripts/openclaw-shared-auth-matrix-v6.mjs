import fs from "node:fs";
import crypto from "node:crypto";
import WebSocket from "ws";

const gatewayUrl = process.env.OPENCLAW_GATEWAY_URL ?? "wss://claw.idol.id";
const secrets = JSON.parse(fs.readFileSync(process.env.OPENCLAW_AUTH_JSON_FILE, "utf8"));
const managementPublicKey = fs.readFileSync(process.env.OPENCLAW_MANAGEMENT_PUBLIC_KEY_FILE, "utf8");
const resultFile = process.env.OPENCLAW_RESULT_FILE ?? "auth-matrix.json";
const envelopeFile = process.env.OPENCLAW_ENVELOPE_FILE ?? "management-envelope.json";
const scopes = [
  "operator.read",
  "operator.write",
  "operator.admin",
  "operator.approvals",
  "operator.questions",
  "operator.pairing",
];
const timeoutMs = 45_000;

const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
const publicRaw = Buffer.from(publicKey.export({ format: "der", type: "spki" })).subarray(-32);
const publicKeyBase64Url = publicRaw.toString("base64url");
const privateKeyPem = privateKey.export({ format: "pem", type: "pkcs8" }).toString();
const deviceId = crypto.createHash("sha256").update(publicRaw).digest("hex");

function sanitize(value, key = "", depth = 0) {
  if (depth > 7) return "[depth-omitted]";
  if (/(token|password|secret|credential|cookie|authorization|api.?key|private.?key|bootstrap)/i.test(key)) return "[redacted]";
  if (/^(message|messages|content|text|prompt|transcript|preview|history|reasoning|body|output)$/i.test(key)) return "[content-omitted]";
  if (/(^|_)(path|cwd|dir|root|home|workspace)(_|$)|repoRoot|stateDir/i.test(key)) return "[path-omitted]";
  if (value == null) return value;
  if (typeof value === "string") return value.length > 500 ? `${value.slice(0, 500)}…` : value;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.slice(0, 100).map((entry) => sanitize(entry, key, depth + 1));
  if (typeof value === "object") {
    const out = {};
    for (const [childKey, childValue] of Object.entries(value)) out[childKey] = sanitize(childValue, childKey, depth + 1);
    return out;
  }
  return String(value);
}

function safeError(error) {
  return sanitize({
    code: error?.code,
    errorText: error?.message ?? String(error),
    details: error?.details,
    closeCode: error?.closeCode,
    closeReason: error?.closeReason,
  });
}

function signPayload({ client, role, requestedScopes, signedAt, signToken, nonce }) {
  const payload = [
    "v2",
    deviceId,
    client.id,
    client.mode,
    role,
    requestedScopes.join(","),
    String(signedAt),
    signToken ?? "",
    nonce ?? "",
  ].join("|");
  return crypto.sign(null, Buffer.from(payload, "utf8"), privateKey).toString("base64url");
}

function connect({ label, client, auth, signToken, withDevice, requestedScopes = scopes }) {
  return new Promise((resolve) => {
    const ws = new WebSocket(gatewayUrl, {
      handshakeTimeout: 20_000,
      perMessageDeflate: false,
      headers: {
        Origin: "https://claw.idol.id",
        "User-Agent": "idol-openclaw-shared-auth-matrix/0.6",
      },
    });
    const pending = new Map();
    let serial = 0;
    let sent = false;
    let finished = false;

    function finish(result) {
      if (finished) return;
      finished = true;
      try { ws.close(1000, "matrix-complete"); } catch {}
      resolve(result);
    }

    function request(method, params = {}, callTimeout = timeoutMs) {
      return new Promise((requestResolve, requestReject) => {
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
      if (sent) return;
      sent = true;
      const nonce = typeof challenge.nonce === "string" ? challenge.nonce : "";
      const signedAt = Number.isSafeInteger(challenge.ts) ? challenge.ts : Date.now();
      const params = {
        minProtocol: 4,
        maxProtocol: 4,
        client,
        role: "operator",
        scopes: requestedScopes,
        caps: ["agent-kind", "approvals", "task-suggestions", "tool-events"],
        commands: [],
        permissions: {},
        auth,
        locale: "en-US",
        userAgent: "idol-openclaw-shared-auth-matrix/0.6",
      };
      if (withDevice) {
        params.device = {
          id: deviceId,
          publicKey: publicKeyBase64Url,
          signature: signPayload({ client, role: "operator", requestedScopes, signedAt, signToken, nonce }),
          signedAt,
          nonce,
        };
      }
      try {
        const hello = await request("connect", params);
        const methods = Array.isArray(hello?.features?.methods) ? hello.features.methods : [];
        const granted = Array.isArray(hello?.auth?.scopes) ? hello.auth.scopes : [];
        const probes = {};
        for (const [method, methodParams] of [["health", {}], ["status", {}], ["device.pair.list", {}], ["agents.list", {}], ["models.list", {}], ["sessions.list", { limit: 25 }]]) {
          if (!methods.includes(method)) continue;
          try { probes[method] = { ok: true, payload: sanitize(await request(method, methodParams)) }; }
          catch (error) { probes[method] = { ok: false, error: safeError(error) }; }
        }
        finish({
          label,
          ok: true,
          withDevice,
          client,
          granted,
          role: hello?.auth?.role,
          deviceTokenIssued: Boolean(hello?.auth?.deviceToken),
          deviceToken: hello?.auth?.deviceToken,
          recoveryScopeIssued: Boolean(hello?.auth?.recoveryScope),
          methodCount: methods.length,
          methods,
          server: sanitize(hello?.server),
          probes,
          request,
          ws,
        });
      } catch (error) {
        finish({ label, ok: false, withDevice, client, error: safeError(error) });
      }
    }

    ws.on("message", (raw) => {
      let frame;
      try { frame = JSON.parse(raw.toString("utf8")); } catch { return; }
      if (frame?.type === "event" && frame.event === "connect.challenge") {
        void sendConnect(frame.payload ?? {});
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
    ws.once("open", () => setTimeout(() => void sendConnect({}), 2500));
    ws.once("error", (error) => finish({ label, ok: false, withDevice, client, error: safeError(error) }));
    ws.once("close", (code, reason) => {
      if (!finished) finish({ label, ok: false, withDevice, client, error: safeError({ code: "SOCKET_CLOSED", message: "gateway closed socket", closeCode: code, closeReason: reason.toString("utf8") }) });
    });
  });
}

function encryptBundle(bundle) {
  const key = crypto.randomBytes(32);
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(bundle), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  const encryptedKey = crypto.publicEncrypt({ key: managementPublicKey, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256" }, key);
  return {
    schema: "idol.openclaw.management-envelope.v1",
    algorithm: "RSA-OAEP-SHA256+A256GCM",
    encryptedKey: encryptedKey.toString("base64"),
    iv: iv.toString("base64"),
    tag: tag.toString("base64"),
    ciphertext: ciphertext.toString("base64"),
  };
}

const controlUi = { id: "openclaw-control-ui", displayName: "Idol Chief Architect", version: "2026.8.28", platform: "web", mode: "webchat" };
const cli = { id: "cli", displayName: "Idol Chief Architect CLI", version: "2026.8.28", platform: "linux", mode: "cli" };
const gatewayClient = { id: "gateway-client", displayName: "Idol Chief Architect Backend", version: "2026.8.28", platform: "linux", mode: "backend" };

const variants = [
  ["token-controlui-device-less", controlUi, { token: secrets.token }, secrets.token, false],
  ["password-controlui-device-less", controlUi, { password: secrets.password }, secrets.password, false],
  ["token-cli-device-less", cli, { token: secrets.token }, secrets.token, false],
  ["password-cli-device-less", cli, { password: secrets.password }, secrets.password, false],
  ["token-backend-device-less", gatewayClient, { token: secrets.token }, secrets.token, false],
  ["password-backend-device-less", gatewayClient, { password: secrets.password }, secrets.password, false],
  ["token-controlui-signed", controlUi, { token: secrets.token }, secrets.token, true],
  ["password-controlui-signed", controlUi, { password: secrets.password }, secrets.password, true],
];

const rawResults = [];
for (const [label, clientInfo, auth, signToken, withDevice] of variants) {
  rawResults.push(await connect({ label, client: clientInfo, auth, signToken, withDevice }));
}

const best = rawResults
  .filter((entry) => entry.ok)
  .sort((a, b) => (b.granted?.length ?? 0) - (a.granted?.length ?? 0))[0];
const signedBest = rawResults
  .filter((entry) => entry.ok && entry.withDevice && entry.deviceTokenIssued)
  .sort((a, b) => (b.granted?.length ?? 0) - (a.granted?.length ?? 0))[0];

const report = {
  schema: "idol.openclaw.shared-auth-matrix.v1",
  observedAt: new Date().toISOString(),
  gatewayUrl,
  deviceIdPrefix: deviceId.slice(0, 16),
  outcome: best ? "scope-bearing-connection-found" : "no-scope-bearing-connection",
  best: best ? sanitize({ label: best.label, granted: best.granted, role: best.role, deviceTokenIssued: best.deviceTokenIssued, recoveryScopeIssued: best.recoveryScopeIssued, methodCount: best.methodCount, probes: best.probes }) : null,
  attempts: rawResults.map((entry) => sanitize({ label: entry.label, ok: entry.ok, withDevice: entry.withDevice, client: entry.client, granted: entry.granted, role: entry.role, deviceTokenIssued: entry.deviceTokenIssued, recoveryScopeIssued: entry.recoveryScopeIssued, methodCount: entry.methodCount, probes: entry.probes, error: entry.error })),
};
fs.writeFileSync(resultFile, JSON.stringify(report, null, 2));

if (signedBest?.deviceToken) {
  fs.writeFileSync(envelopeFile, JSON.stringify(encryptBundle({
    schema: "idol.openclaw.management-credential.v1",
    gatewayUrl,
    deviceId,
    publicKey: publicKeyBase64Url,
    privateKeyPem,
    operatorToken: signedBest.deviceToken,
    scopes: signedBest.granted,
    client: signedBest.client,
    issuedAt: new Date().toISOString(),
  }), null, 2));
}
