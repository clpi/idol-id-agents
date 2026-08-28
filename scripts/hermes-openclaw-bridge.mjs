import { spawn } from "node:child_process";
import process from "node:process";

const HERMES_BASE = (process.env.HERMES_BASE_URL || "https://hermes.idol.id").replace(/\/$/, "");
const HERMES_PASSWORD = process.env.HERMES_WEBUI_PASSWORD;
const REQUEST_ID = process.env.OPENCLAW_PAIRING_REQUEST_ID;
const DEVICE_ID = process.env.OPENCLAW_EXPECTED_DEVICE_ID;
const BRIDGE_TIMEOUT_MS = Number(process.env.HERMES_BRIDGE_TIMEOUT_MS || 480000);

if (!HERMES_PASSWORD || !REQUEST_ID || !DEVICE_ID) {
  console.error("IDOL_HERMES_BRIDGE_ERROR=missing-required-environment");
  process.exit(2);
}

const state = {
  cookie: "",
  csrf: "",
  sessionId: "",
  approvals: [],
  denied: [],
};

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function deadlinePromise(ms, label) {
  return new Promise((_, reject) => {
    setTimeout(() => reject(new Error(`${label}-timeout`)), ms).unref?.();
  });
}

function sanitizeText(value) {
  return String(value || "")
    .replaceAll(HERMES_PASSWORD, "[redacted]")
    .replaceAll(process.env.OPENCLAW_GATEWAY_TOKEN || "", "[redacted]")
    .replaceAll(process.env.OPENCLAW_DEVICE_SEED || "", "[redacted]")
    .replaceAll(process.env.OPENCLAW_DEVICE_TOKEN || "", "[redacted]")
    .slice(0, 2000);
}

function captureCookies(headers) {
  const rows = typeof headers.getSetCookie === "function"
    ? headers.getSetCookie()
    : [headers.get("set-cookie")].filter(Boolean);
  if (!rows.length) return;
  const current = new Map(
    state.cookie
      .split(/;\s*/)
      .filter(Boolean)
      .map((part) => {
        const index = part.indexOf("=");
        return index > 0 ? [part.slice(0, index), part.slice(index + 1)] : [part, ""];
      }),
  );
  for (const row of rows) {
    const first = String(row).split(";", 1)[0];
    const index = first.indexOf("=");
    if (index > 0) current.set(first.slice(0, index), first.slice(index + 1));
  }
  state.cookie = [...current.entries()].map(([k, v]) => `${k}=${v}`).join("; ");
}

async function request(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  headers.set("Accept", options.accept || "application/json, text/plain;q=0.9, */*;q=0.8");
  if (options.json !== undefined) headers.set("Content-Type", "application/json");
  if (state.cookie) headers.set("Cookie", state.cookie);
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && state.csrf && !path.endsWith("/api/auth/login")) {
    headers.set("X-Hermes-CSRF-Token", state.csrf);
    headers.set("Origin", HERMES_BASE);
    headers.set("Referer", `${HERMES_BASE}/`);
  }

  const response = await fetch(`${HERMES_BASE}${path}`, {
    method,
    headers,
    body: options.json !== undefined ? JSON.stringify(options.json) : options.body,
    redirect: options.redirect || "manual",
  });
  captureCookies(response.headers);
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`http-${response.status}:${path}:${sanitizeText(text)}`);
  }
  if (!text) return null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(`invalid-json:${path}`);
    }
  }
  return text;
}

async function login() {
  const result = await request("/api/auth/login", {
    method: "POST",
    json: { password: HERMES_PASSWORD },
  });
  if (!result || result.ok !== true || !state.cookie) {
    throw new Error("hermes-login-failed");
  }

  const root = await request("/", { accept: "text/html" });
  const match = String(root).match(/csrfToken:((?:"(?:\\.|[^"\\])*")|null)/);
  if (!match) throw new Error("hermes-csrf-token-absent");
  state.csrf = JSON.parse(match[1]) || "";
  if (!state.csrf) throw new Error("hermes-csrf-token-empty");
}

async function createSession() {
  const data = await request("/api/session/new", {
    method: "POST",
    json: { workspace: null, profile: "default" },
  });
  const session = data?.session;
  if (!session?.session_id) throw new Error("hermes-session-create-failed");
  state.sessionId = session.session_id;
  return session;
}

function normalizeCommand(raw) {
  return String(raw || "").trim().replace(/\s+/g, " ");
}

function allowedCommand(command) {
  const exact = new Set([
    "openclaw devices list --json",
    `openclaw devices approve ${REQUEST_ID} --json`,
    `openclaw devices approve ${REQUEST_ID}`,
  ]);
  return exact.has(normalizeCommand(command));
}

async function approvalWatcher(doneSignal) {
  const seen = new Set();
  while (!doneSignal.done) {
    let data;
    try {
      data = await request(`/api/approval/pending?session_id=${encodeURIComponent(state.sessionId)}`);
    } catch (error) {
      if (doneSignal.done) return;
      throw error;
    }
    const pending = data?.pending;
    if (pending) {
      const approvalId = pending.approval_id || pending.id || null;
      const identity = approvalId || JSON.stringify([pending.command, pending.description]);
      if (!seen.has(identity)) {
        seen.add(identity);
        const command = normalizeCommand(pending.command);
        const choice = allowedCommand(command) ? "once" : "deny";
        await request("/api/approval/respond", {
          method: "POST",
          json: {
            session_id: state.sessionId,
            choice,
            ...(approvalId ? { approval_id: approvalId } : {}),
          },
        });
        const record = { command, choice };
        if (choice === "once") state.approvals.push(record);
        else {
          state.denied.push(record);
          doneSignal.abortReason = `unexpected-command:${sanitizeText(command)}`;
        }
      }
    }
    await sleep(500);
  }
}

async function readSse(streamId, doneSignal) {
  const response = await fetch(`${HERMES_BASE}/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`, {
    headers: state.cookie ? { Cookie: state.cookie, Accept: "text/event-stream" } : { Accept: "text/event-stream" },
  });
  if (!response.ok || !response.body) {
    throw new Error(`hermes-stream-open-failed:${response.status}`);
  }
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "message";
  let dataLines = [];

  function dispatch() {
    if (!dataLines.length) {
      eventName = "message";
      return;
    }
    const rawData = dataLines.join("\n");
    if (eventName === "done") {
      doneSignal.done = true;
      try { doneSignal.donePayload = JSON.parse(rawData); } catch { doneSignal.donePayload = {}; }
    } else if (eventName === "error") {
      doneSignal.done = true;
      doneSignal.error = sanitizeText(rawData);
    }
    eventName = "message";
    dataLines = [];
  }

  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, "");
      buffer = buffer.slice(index + 1);
      if (!line) {
        dispatch();
        if (doneSignal.done) return;
      } else if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (doneSignal.abortReason) throw new Error(doneSignal.abortReason);
  }
  dispatch();
}

async function runHermesApproval(session) {
  const message = [
    "Perform exactly one bounded OpenClaw device-approval task on this host.",
    `1. Run exactly: openclaw devices list --json`,
    `2. Locate requestId ${REQUEST_ID}.`,
    `3. Verify its deviceId is exactly ${DEVICE_ID}, role is exactly operator, and requested scopes are exactly [operator.read] with no additional scope.`,
    `4. Only if all fields match, run exactly: openclaw devices approve ${REQUEST_ID} --json`,
    "Do not use --latest. Do not approve any other request. Do not change OpenClaw configuration, tokens, users, agents, sessions, or models. Do not run shell wrappers, pipes, redirections, command substitutions, or chained commands. If the request is absent or any field differs, stop and report the discrepancy.",
  ].join("\n");

  const start = await request("/api/chat/start", {
    method: "POST",
    json: {
      session_id: session.session_id,
      message,
      model: session.model || "",
      workspace: session.workspace || null,
      model_provider: session.model_provider || null,
    },
  });
  if (!start?.stream_id) throw new Error("hermes-chat-start-failed");

  const signal = { done: false, error: null, abortReason: null, donePayload: null };
  await Promise.race([
    Promise.all([readSse(start.stream_id, signal), approvalWatcher(signal)]),
    deadlinePromise(BRIDGE_TIMEOUT_MS, "hermes-approval"),
  ]);
  signal.done = true;
  if (signal.abortReason) throw new Error(signal.abortReason);
  if (signal.error) throw new Error(`hermes-agent-error:${signal.error}`);
  return signal.donePayload || {};
}

async function deleteSession() {
  if (!state.sessionId) return;
  try {
    await request("/api/session/delete", {
      method: "POST",
      json: { session_id: state.sessionId },
    });
  } catch {
    // Cleanup failure is reported in the final bridge record but does not erase the approval outcome.
  }
}

async function runOpenClawProbe() {
  return await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["scripts/openclaw-probe.mjs"], {
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString("utf8"); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`openclaw-probe-exit-${code}:${sanitizeText(stderr)}`));
        return;
      }
      const line = stdout.split(/\r?\n/).find((row) => row.startsWith("IDOL_OPENCLAW_PROBE_V3="));
      if (!line) {
        reject(new Error(`openclaw-probe-result-absent:${sanitizeText(stdout)}:${sanitizeText(stderr)}`));
        return;
      }
      try {
        resolve(JSON.parse(line.slice("IDOL_OPENCLAW_PROBE_V3=".length)));
      } catch {
        reject(new Error("openclaw-probe-result-invalid-json"));
      }
    });
  });
}

async function main() {
  const startedAt = new Date().toISOString();
  try {
    await login();
    const session = await createSession();
    await runHermesApproval(session);
    const inventory = await runOpenClawProbe();
    const result = {
      schema: "idol.hermes-openclaw-bridge.v1",
      startedAt,
      completedAt: new Date().toISOString(),
      pairing: {
        requestId: REQUEST_ID,
        deviceId: DEVICE_ID,
        requestedScopes: ["operator.read"],
        approvedCommands: state.approvals,
        deniedCommands: state.denied,
      },
      inventory,
    };
    console.log(`IDOL_HERMES_OPENCLAW_BRIDGE=${JSON.stringify(result)}`);
  } finally {
    await deleteSession();
  }
}

main().catch((error) => {
  console.error(`IDOL_HERMES_BRIDGE_ERROR=${JSON.stringify({
    message: sanitizeText(error?.message || error),
    approvedCommands: state.approvals,
    deniedCommands: state.denied,
  })}`);
  process.exitCode = 1;
});
