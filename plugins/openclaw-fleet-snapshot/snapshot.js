import { createHash } from "node:crypto";
import { constants as fsConstants } from "node:fs";
import { open, realpath, stat } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

export const METHOD_NAME = "idol.fleet.activeWork.snapshot";

export const COMPONENT_COUNT_KEYS = Object.freeze([
  "queueSize",
  "pendingReplies",
  "embeddedRuns",
  "backgroundExecSessions",
  "cronRuns",
  "activeTasks",
  "rootRequests",
  "sessionAdmissions",
  "sessionMutations",
  "chatRuns",
  "queuedTurns",
  "terminalPersistence",
  "terminalSessions",
]);

export const PINNED_OPENCLAW = Object.freeze({
  openclawVersion: "2026.8.1-beta.3",
  gateway: Object.freeze({
    relativePath: "dist/gateway-active-work-DHoQuaTC.js",
    sha256: "f65a34729ee974c0c4e41e1e2ca7a504aa0bf626b584a11bae5fe66f00e379a7",
    exportKeys: Object.freeze(["n", "t"]),
    functionExport: "t",
    functionName: "createGatewayActiveWorkSnapshot",
  }),
  server: Object.freeze({
    relativePath: "dist/server-active-work-DvXAfFIM.js",
    sha256: "8f266b5bde14c43be5d1c1cd426fcff13f2280f505b53775a3152f127b633e9d",
    exportKeys: Object.freeze(["t"]),
    functionExport: "t",
    functionName: "createGatewayServerActiveWorkInspectors",
  }),
});

const SERVER_INSPECTOR_KEYS = Object.freeze([
  "getCronRuns",
  "getChatRuns",
  "getQueuedTurns",
  "getTerminalPersistence",
  "getTerminalSessions",
]);
const MAX_PINNED_FILE_BYTES = 256 * 1024;
const UNSUPPORTED_MESSAGE = "unsupported OpenClaw internal bridge";
const UNAVAILABLE_ERROR = Object.freeze({
  code: "UNAVAILABLE",
  message: "Fleet active-work snapshot unavailable",
});
const INVALID_REQUEST_ERROR = Object.freeze({
  code: "INVALID_REQUEST",
  message: "Fleet active-work snapshot takes no parameters",
});

function unsupported() {
  return new Error(UNSUPPORTED_MESSAGE);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isPlainRecord(value) {
  if (!isRecord(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function sameFileState(left, right) {
  return left.dev === right.dev &&
    left.ino === right.ino &&
    left.mode === right.mode &&
    left.uid === right.uid &&
    left.gid === right.gid &&
    left.size === right.size &&
    left.mtimeNs === right.mtimeNs &&
    left.ctimeNs === right.ctimeNs;
}

async function readStableRegularFile(filePath) {
  const noFollow = fsConstants.O_NOFOLLOW ?? 0;
  let handle;
  try {
    handle = await open(
      filePath,
      fsConstants.O_RDONLY | fsConstants.O_CLOEXEC | fsConstants.O_NONBLOCK | noFollow,
    );
    const before = await handle.stat({ bigint: true });
    if (!before.isFile() || before.uid !== BigInt(process.getuid?.() ?? -1)) throw unsupported();
    if ((before.mode & 0o022n) !== 0n) throw unsupported();
    if (before.size < 0n || before.size > BigInt(MAX_PINNED_FILE_BYTES)) throw unsupported();

    const expectedSize = Number(before.size);
    const buffer = Buffer.alloc(expectedSize + 1);
    let offset = 0;
    while (offset < buffer.length) {
      const { bytesRead } = await handle.read(buffer, offset, buffer.length - offset, offset);
      if (bytesRead === 0) break;
      offset += bytesRead;
    }
    const after = await handle.stat({ bigint: true });
    if (offset !== expectedSize || !sameFileState(before, after)) throw unsupported();
    return buffer.subarray(0, expectedSize);
  } catch {
    throw unsupported();
  } finally {
    await handle?.close().catch(() => {});
  }
}

function fileUrl(filePath) {
  return pathToFileURL(filePath).href;
}

async function verifyPackageRoot(packageRoot) {
  if (typeof packageRoot !== "string" || !path.isAbsolute(packageRoot)) throw unsupported();
  const normalized = path.resolve(packageRoot);
  if (normalized !== packageRoot || await realpath(normalized) !== normalized) throw unsupported();
  const rootState = await stat(normalized, { bigint: true });
  if (!rootState.isDirectory() || rootState.uid !== BigInt(process.getuid?.() ?? -1)) {
    throw unsupported();
  }
  if ((rootState.mode & 0o022n) !== 0n) throw unsupported();
  return normalized;
}

async function verifyPackageVersion(packageRoot, expectedVersion) {
  const bytes = await readStableRegularFile(path.join(packageRoot, "package.json"));
  let manifest;
  try {
    manifest = JSON.parse(bytes.toString("utf8"));
  } catch {
    throw unsupported();
  }
  if (!isPlainRecord(manifest) || manifest.name !== "openclaw" ||
      manifest.version !== expectedVersion) {
    throw unsupported();
  }
}

async function verifyPinnedSource(packageRoot, descriptor) {
  if (!isPlainRecord(descriptor) || typeof descriptor.relativePath !== "string" ||
      typeof descriptor.sha256 !== "string") {
    throw unsupported();
  }
  const sourcePath = path.resolve(packageRoot, descriptor.relativePath);
  if (!sourcePath.startsWith(`${packageRoot}${path.sep}`)) throw unsupported();
  const bytes = await readStableRegularFile(sourcePath);
  if (createHash("sha256").update(bytes).digest("hex") !== descriptor.sha256) {
    throw unsupported();
  }
  return sourcePath;
}

function validateModule(module, descriptor) {
  if (!isRecord(module) || !Array.isArray(descriptor.exportKeys) ||
      Object.keys(module).sort().join("\0") !== [...descriptor.exportKeys].sort().join("\0")) {
    throw unsupported();
  }
  const exported = module[descriptor.functionExport];
  if (typeof exported !== "function" || exported.name !== descriptor.functionName) {
    throw unsupported();
  }
  for (const key of descriptor.exportKeys) {
    if (typeof module[key] !== "function") throw unsupported();
  }
  return exported;
}

function validateRunEntry(entry, includePersistenceFlags) {
  if (!isRecord(entry) || !isRecord(entry.controller) ||
      !isRecord(entry.controller.signal) ||
      typeof entry.controller.signal.aborted !== "boolean") {
    throw unsupported();
  }
  const booleanFields = includePersistenceFlags
    ? [
        "registrationCleanupRequested",
        "controlUiVisible",
        "projectSessionTerminalPersisted",
        "projectSessionTerminalPending",
      ]
    : [];
  for (const field of booleanFields) {
    if (entry[field] !== undefined && typeof entry[field] !== "boolean") throw unsupported();
  }
}

function validateGatewayContext(context) {
  if (!isRecord(context) || !isRecord(context.cron) ||
      typeof context.cron.getSuspensionBlockerCount !== "function" ||
      !(context.chatAbortControllers instanceof Map) ||
      !(context.chatQueuedTurns instanceof Map) ||
      !isRecord(context.terminalSessions) || !isCount(context.terminalSessions.size)) {
    throw unsupported();
  }
  for (const entry of context.chatAbortControllers.values()) validateRunEntry(entry, true);
  for (const entry of context.chatQueuedTurns.values()) validateRunEntry(entry, false);
}

function validateServerInspectors(inspectors) {
  if (!isPlainRecord(inspectors) ||
      Object.keys(inspectors).sort().join("\0") !== [...SERVER_INSPECTOR_KEYS].sort().join("\0")) {
    throw unsupported();
  }
  const observations = new Map();
  const guarded = Object.fromEntries(SERVER_INSPECTOR_KEYS.map((key) => {
    if (typeof inspectors[key] !== "function") throw unsupported();
    const observation = { calls: 0, valid: false };
    observations.set(key, observation);
    return [key, () => {
      observation.calls += 1;
      try {
        const value = inspectors[key]();
        if (!isCount(value)) throw unsupported();
        observation.valid = true;
        return value;
      } catch {
        observation.valid = false;
        throw unsupported();
      }
    }];
  }));
  return {
    inspectors: guarded,
    assertComplete() {
      for (const observation of observations.values()) {
        if (observation.calls !== 1 || !observation.valid) throw unsupported();
      }
    },
  };
}

function projectSnapshot(snapshot, openclawVersion, observedAt) {
  if (!isPlainRecord(snapshot) || typeof snapshot.idle !== "boolean" ||
      !isPlainRecord(snapshot.counts)) {
    throw unsupported();
  }
  const expectedKeys = [...COMPONENT_COUNT_KEYS, "totalActive"];
  if (Object.keys(snapshot.counts).join("\0") !== expectedKeys.join("\0")) throw unsupported();

  const counts = {};
  let total = 0;
  for (const key of COMPONENT_COUNT_KEYS) {
    const value = snapshot.counts[key];
    if (!isCount(value)) throw unsupported();
    counts[key] = value;
    total += value;
    if (!Number.isSafeInteger(total)) throw unsupported();
  }
  if (!isCount(snapshot.counts.totalActive) || snapshot.counts.totalActive !== total ||
      snapshot.idle !== (total === 0)) {
    throw unsupported();
  }
  counts.totalActive = total;

  if (!(observedAt instanceof Date) || !Number.isFinite(observedAt.getTime())) throw unsupported();
  return {
    schema: "idol.openclaw.active-work",
    version: 1,
    openclawVersion,
    observedAt: observedAt.toISOString(),
    idle: snapshot.idle,
    counts,
  };
}

export function createFleetSnapshotObserver({
  openclawPackageRoot,
  sdkDefinePluginEntry,
  pinned = PINNED_OPENCLAW,
  importModule = (url) => import(url),
  now = () => new Date(),
}) {
  if (typeof sdkDefinePluginEntry !== "function" || !isPlainRecord(pinned)) {
    throw unsupported();
  }

  let functionsPromise;

  async function verifyInstallation() {
    const packageRoot = await verifyPackageRoot(openclawPackageRoot);
    await verifyPackageVersion(packageRoot, pinned.openclawVersion);
    const gatewayPath = await verifyPinnedSource(packageRoot, pinned.gateway);
    const serverPath = await verifyPinnedSource(packageRoot, pinned.server);
    const publicSdk = await importModule(fileUrl(path.join(
      packageRoot,
      "dist",
      "plugin-sdk",
      "plugin-entry.js",
    )));
    if (!isRecord(publicSdk) || publicSdk.definePluginEntry !== sdkDefinePluginEntry) {
      throw unsupported();
    }
    return { packageRoot, gatewayPath, serverPath };
  }

  async function loadFunctions() {
    const verified = await verifyInstallation();
    const [gatewayModule, serverModule] = await Promise.all([
      importModule(fileUrl(verified.gatewayPath)),
      importModule(fileUrl(verified.serverPath)),
    ]);
    const functions = {
      createSnapshot: validateModule(gatewayModule, pinned.gateway),
      createServerInspectors: validateModule(serverModule, pinned.server),
    };
    await verifyInstallation();
    return functions;
  }

  return async function observe(context) {
    try {
      const functions = await (functionsPromise ??= loadFunctions());
      await verifyInstallation();
      validateGatewayContext(context);

      const serverInspectors = functions.createServerInspectors(context);
      const guardedInspectors = validateServerInspectors(serverInspectors);
      const snapshot = functions.createSnapshot(guardedInspectors.inspectors);
      guardedInspectors.assertComplete();
      const result = projectSnapshot(snapshot, pinned.openclawVersion, now());

      await verifyInstallation();
      return result;
    } catch {
      functionsPromise = undefined;
      throw unsupported();
    }
  };
}

export function registerFleetSnapshot(api, options) {
  if (!isRecord(api) || typeof api.registerGatewayMethod !== "function") throw unsupported();
  const observer = createFleetSnapshotObserver({
    ...options,
    openclawPackageRoot: api.pluginConfig?.openclawPackageRoot,
  });
  api.registerGatewayMethod(METHOD_NAME, async ({ params, context, respond }) => {
    if (!isPlainRecord(params) || Object.keys(params).length !== 0) {
      respond(false, undefined, INVALID_REQUEST_ERROR);
      return;
    }
    try {
      respond(true, await observer(context));
    } catch {
      respond(false, undefined, UNAVAILABLE_ERROR);
    }
  });
}
