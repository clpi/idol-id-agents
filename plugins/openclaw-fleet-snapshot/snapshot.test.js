import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { cp, mkdtemp, mkdir, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import { registerHooks } from "node:module";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";

import {
  COMPONENT_COUNT_KEYS,
  METHOD_NAME,
  PINNED_OPENCLAW,
  createFleetSnapshotObserver,
  registerFleetSnapshot,
} from "./snapshot.js";

const GATEWAY_SOURCE = `
function waitForGatewayActiveWork() {}
function createGatewayActiveWorkSnapshot(inspectors) {
  const counts = {
    queueSize: 2,
    pendingReplies: 0,
    embeddedRuns: 1,
    backgroundExecSessions: 0,
    cronRuns: inspectors.getCronRuns(),
    activeTasks: 0,
    rootRequests: 1,
    sessionAdmissions: 0,
    sessionMutations: 0,
    chatRuns: inspectors.getChatRuns(),
    queuedTurns: inspectors.getQueuedTurns(),
    terminalPersistence: inspectors.getTerminalPersistence(),
    terminalSessions: inspectors.getTerminalSessions(),
    totalActive: 0,
  };
  counts.totalActive = Object.values(counts).reduce((sum, value) => sum + value, 0);
  return {
    idle: counts.totalActive === 0,
    counts,
    blockers: [{ message: "fixture-private-content", task: { prompt: "private" } }],
  };
}
export { waitForGatewayActiveWork as n, createGatewayActiveWorkSnapshot as t };
`;

const SERVER_SOURCE = `
function createGatewayServerActiveWorkInspectors(context) {
  context.callOrder.push(["server", context]);
  return {
    getCronRuns: () => context.cron.getSuspensionBlockerCount(),
    getChatRuns: () => [...context.chatAbortControllers.values()].filter(
      (entry) => !entry.controller.signal.aborted && entry.registrationCleanupRequested !== true,
    ).length,
    getQueuedTurns: () => [...context.chatQueuedTurns.values()].filter(
      (entry) => !entry.controller.signal.aborted,
    ).length,
    getTerminalPersistence: () => [...context.chatAbortControllers.values()].filter(
      (entry) => entry.controlUiVisible !== false &&
        entry.projectSessionTerminalPersisted !== true &&
        (entry.projectSessionTerminalPending === true ||
          entry.projectSessionTerminalPersistence !== undefined),
    ).length,
    getTerminalSessions: () => context.terminalSessions.size,
  };
}
export { createGatewayServerActiveWorkInspectors as t };
`;

const SDK_SOURCE = `
function definePluginEntry(definition) { return definition; }
export { definePluginEntry };
`;

function gatewayFunctionHashes(source) {
  const split = source.indexOf("function createGatewayActiveWorkSnapshot");
  return {
    n: sha256(source.slice(0, split).trim()),
    t: sha256(source.slice(split).split("\nexport {")[0].trim()),
  };
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

const execFileAsync = promisify(execFile);

test("pins the reviewed OpenClaw version and private modules", () => {
  assert.equal(PINNED_OPENCLAW.openclawVersion, "2026.8.1-beta.3");
  assert.deepEqual(PINNED_OPENCLAW.gateway, {
    relativePath: "dist/gateway-active-work-DHoQuaTC.js",
    sha256: "f65a34729ee974c0c4e41e1e2ca7a504aa0bf626b584a11bae5fe66f00e379a7",
    functionSha256: {
      n: "df3a825f22250ba5b5dfa477fff5a0c148f612a52af23cb3a061a8104dc2d2ec",
      t: "f38a92c8197d90d2cfb24ab0d07d616c54513b9c67244ccff2343556e4ecef1b",
    },
    exportKeys: ["n", "t"],
    functionExport: "t",
    functionName: "createGatewayActiveWorkSnapshot",
  });
  assert.deepEqual(PINNED_OPENCLAW.server, {
    relativePath: "dist/server-active-work-DvXAfFIM.js",
    sha256: "8f266b5bde14c43be5d1c1cd426fcff13f2280f505b53775a3152f127b633e9d",
    functionSha256: {
      t: "74fc12f3909e61259258e49d0f1ed68abbbeed1077f4fce109fbb6ff9ec39ef8",
    },
    exportKeys: ["t"],
    functionExport: "t",
    functionName: "createGatewayServerActiveWorkInspectors",
  });
});

async function makePackage(t) {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), "openclaw-snapshot-test-")));
  t.after(async () => {
    await import("node:fs/promises").then(({ rm }) => rm(root, { recursive: true, force: true }));
  });
  await mkdir(path.join(root, "dist", "plugin-sdk"), { recursive: true });
  await writeFile(
    path.join(root, "package.json"),
    JSON.stringify({ name: "openclaw", version: "fixture-version", type: "module" }),
  );
  await writeFile(path.join(root, "dist", "plugin-sdk", "plugin-entry.js"), SDK_SOURCE);
  await writeFile(path.join(root, "dist", "gateway-fixture.js"), GATEWAY_SOURCE);
  await writeFile(path.join(root, "dist", "server-fixture.js"), SERVER_SOURCE);
  return {
    root,
    sdkModuleUrl: pathToFileURL(path.join(root, "dist", "plugin-sdk", "plugin-entry.js")).href,
    pinned: {
      openclawVersion: "fixture-version",
      gateway: {
        relativePath: "dist/gateway-fixture.js",
        sha256: sha256(GATEWAY_SOURCE),
        functionSha256: gatewayFunctionHashes(GATEWAY_SOURCE),
        exportKeys: ["n", "t"],
        functionExport: "t",
        functionName: "createGatewayActiveWorkSnapshot",
      },
      server: {
        relativePath: "dist/server-fixture.js",
        sha256: sha256(SERVER_SOURCE),
        functionSha256: { t: sha256(SERVER_SOURCE.trim().split("\nexport {")[0]) },
        exportKeys: ["t"],
        functionExport: "t",
        functionName: "createGatewayServerActiveWorkInspectors",
      },
    },
  };
}

function activeEntry(overrides = {}) {
  return {
    controller: { signal: { aborted: false } },
    registrationCleanupRequested: false,
    controlUiVisible: true,
    projectSessionTerminalPersisted: false,
    projectSessionTerminalPending: true,
    ...overrides,
  };
}

function context() {
  return {
    cron: { getSuspensionBlockerCount: () => 3 },
    chatAbortControllers: new Map([["run", activeEntry()]]),
    chatQueuedTurns: new Map([["turn", activeEntry()]]),
    terminalSessions: { size: 2 },
    callOrder: [],
  };
}

test("projects only approved metadata from one synchronous canonical composition", async (t) => {
  const fixture = await makePackage(t);
  const observer = createFleetSnapshotObserver({
    openclawPackageRoot: fixture.root,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
    now: () => new Date("2026-09-05T04:00:00.000Z"),
  });
  const liveContext = context();
  const result = await observer(liveContext);

  assert.deepEqual(Object.keys(result), [
    "schema",
    "version",
    "openclawVersion",
    "observedAt",
    "idle",
    "counts",
  ]);
  assert.equal(result.schema, "idol.openclaw.active-work");
  assert.equal(result.version, 1);
  assert.equal(result.openclawVersion, "fixture-version");
  assert.equal(result.observedAt, "2026-09-05T04:00:00.000Z");
  assert.equal(result.idle, false);
  assert.deepEqual(Object.keys(result.counts), [...COMPONENT_COUNT_KEYS, "totalActive"]);
  assert.equal(result.counts.totalActive, 12);
  assert.equal(JSON.stringify(result).includes("fixture-private-content"), false);
  assert.equal(liveContext.callOrder.length, 1);
  assert.strictEqual(liveContext.callOrder[0][1], liveContext);
});

test("an idle canonical snapshot remains idle", async (t) => {
  const fixture = await makePackage(t);
  const gatewayPath = path.join(fixture.root, fixture.pinned.gateway.relativePath);
  const idleSource = GATEWAY_SOURCE
    .replace("queueSize: 2", "queueSize: 0")
    .replace("embeddedRuns: 1", "embeddedRuns: 0")
    .replace("rootRequests: 1", "rootRequests: 0");
  await writeFile(gatewayPath, idleSource);
  fixture.pinned.gateway.sha256 = sha256(idleSource);
  fixture.pinned.gateway.functionSha256 = gatewayFunctionHashes(idleSource);
  const observer = createFleetSnapshotObserver({
    openclawPackageRoot: fixture.root,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
  });
  const liveContext = context();
  liveContext.cron.getSuspensionBlockerCount = () => 0;
  liveContext.chatAbortControllers.clear();
  liveContext.chatQueuedTurns.clear();
  liveContext.terminalSessions.size = 0;

  const result = await observer(liveContext);
  assert.equal(result.idle, true);
  assert.equal(result.counts.totalActive, 0);
});

test("binds the configured root to the loader-resolved public SDK URL", async (t) => {
  const fixture = await makePackage(t);
  const observer = createFleetSnapshotObserver({
    openclawPackageRoot: fixture.root,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
  });
  await observer(context());

  const copy = `${fixture.root}-copy`;
  t.after(async () => {
    await import("node:fs/promises").then(({ rm }) => rm(copy, { recursive: true, force: true }));
  });
  await cp(fixture.root, copy, { recursive: true });
  await writeFile(path.join(copy, "dist", "plugin-sdk", "plugin-entry.js"),
    "globalThis.unverifiedSdkExecuted = true;\n" + SDK_SOURCE);
  const duplicateObserver = createFleetSnapshotObserver({
    openclawPackageRoot: copy,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
  });
  await assert.rejects(duplicateObserver(context()), /unsupported OpenClaw internal bridge/);
  assert.equal(globalThis.unverifiedSdkExecuted, undefined);
});

test("rejects replacement bytes before evaluation and removes temporary hooks", async (t) => {
  for (const target of ["gateway", "server"]) {
    await t.test(target, async (t) => {
      const fixture = await makePackage(t);
      const targetPath = path.join(fixture.root, fixture.pinned[target].relativePath);
      const targetUrl = pathToFileURL(targetPath).href;
      const other = target === "gateway" ? "server" : "gateway";
      const otherUrl = pathToFileURL(path.join(fixture.root, fixture.pinned[other].relativePath)).href;
      const observer = createFleetSnapshotObserver({
        openclawPackageRoot: fixture.root,
        sdkModuleUrl: fixture.sdkModuleUrl,
        pinned: fixture.pinned,
        importModule: async (url) => {
          if (url === otherUrl) return {};
          assert.equal(url, targetUrl);
          await writeFile(targetPath, "globalThis.unverifiedBridgeExecuted = true;\n");
          return await import(url);
        },
      });
      await assert.rejects(observer(context()), /unsupported OpenClaw internal bridge/);
      assert.equal(globalThis.unverifiedBridgeExecuted, undefined);
      // An ordinary import after refusal is no longer intercepted by the bridge.
      await writeFile(path.join(fixture.root, fixture.pinned[other].relativePath), "export const probe = 1;\n");
      assert.equal((await import(otherUrl)).probe, 1);
    });
  }
});

test("retains canonical cached modules and refuses stale cached function bodies", async (t) => {
  const fixture = await makePackage(t);
  const gatewayPath = path.join(fixture.root, fixture.pinned.gateway.relativePath);
  const gatewayUrl = pathToFileURL(gatewayPath).href;
  const canonical = await import(gatewayUrl);
  let sawCanonical = false;
  const observer = createFleetSnapshotObserver({
    openclawPackageRoot: fixture.root,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
    importModule: async (url) => {
      const module = await import(url);
      if (url === gatewayUrl) {
        assert.strictEqual(module, canonical);
        sawCanonical = true;
      }
      return module;
    },
  });
  assert.equal((await observer(context())).counts.totalActive, 12);
  assert.equal(sawCanonical, true);

  const stale = await makePackage(t);
  const stalePath = path.join(stale.root, stale.pinned.gateway.relativePath);
  await writeFile(stalePath, GATEWAY_SOURCE.replace("queueSize: 2", "queueSize: 0"));
  await import(pathToFileURL(stalePath).href);
  await writeFile(stalePath, GATEWAY_SOURCE);
  const staleObserver = createFleetSnapshotObserver({
    openclawPackageRoot: stale.root,
    sdkModuleUrl: stale.sdkModuleUrl,
    pinned: stale.pinned,
  });
  await assert.rejects(staleObserver(context()), /unsupported OpenClaw internal bridge/);
});

test("refuses redirected canonical URLs before evaluation", async (t) => {
  const fixture = await makePackage(t);
  const gatewayUrl = pathToFileURL(path.join(fixture.root, fixture.pinned.gateway.relativePath)).href;
  const redirectedPath = path.join(fixture.root, "redirected.js");
  await writeFile(redirectedPath, "globalThis.redirectedBridgeExecuted = true;\n");
  const redirect = registerHooks({
    resolve(specifier, context, nextResolve) {
      if (specifier === gatewayUrl) {
        return nextResolve(pathToFileURL(redirectedPath).href, context);
      }
      return nextResolve(specifier, context);
    },
  });
  try {
    const observer = createFleetSnapshotObserver({
      openclawPackageRoot: fixture.root,
      sdkModuleUrl: fixture.sdkModuleUrl,
      pinned: fixture.pinned,
    });
    await assert.rejects(observer(context()), /unsupported OpenClaw internal bridge/);
    assert.equal(globalThis.redirectedBridgeExecuted, undefined);
  } finally {
    redirect.deregister();
  }
});

test("refuses a symlinked ancestor of pinned sources", async (t) => {
  const fixture = await makePackage(t);
  const dist = path.join(fixture.root, "dist");
  const redirected = path.join(fixture.root, "redirected-dist");
  await cp(dist, redirected, { recursive: true });
  await rm(dist, { recursive: true });
  await symlink(redirected, dist);
  const observer = createFleetSnapshotObserver({
    openclawPackageRoot: fixture.root,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
  });
  await assert.rejects(observer(context()), /unsupported OpenClaw internal bridge/);
});

test("refuses source drift after a successful observation", async (t) => {
  const fixture = await makePackage(t);
  const observer = createFleetSnapshotObserver({
    openclawPackageRoot: fixture.root,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
  });
  await observer(context());
  await writeFile(
    path.join(fixture.root, fixture.pinned.gateway.relativePath),
    `${GATEWAY_SOURCE}\n// drift\n`,
  );
  await assert.rejects(observer(context()), /unsupported OpenClaw internal bridge/);
});

test("refuses package-version and private-export drift", async (t) => {
  const versionFixture = await makePackage(t);
  await writeFile(
    path.join(versionFixture.root, "package.json"),
    JSON.stringify({ name: "openclaw", version: "other-version", type: "module" }),
  );
  const wrongVersion = createFleetSnapshotObserver({
    openclawPackageRoot: versionFixture.root,
    sdkModuleUrl: versionFixture.sdkModuleUrl,
    pinned: versionFixture.pinned,
  });
  await assert.rejects(wrongVersion(context()), /unsupported OpenClaw internal bridge/);

  const exportFixture = await makePackage(t);
  const gatewayUrl = pathToFileURL(
    path.join(exportFixture.root, exportFixture.pinned.gateway.relativePath),
  ).href;
  const realGateway = await import(gatewayUrl);
  const extraExport = createFleetSnapshotObserver({
    openclawPackageRoot: exportFixture.root,
    sdkModuleUrl: exportFixture.sdkModuleUrl,
    pinned: exportFixture.pinned,
    importModule: async (url) => {
      if (url === gatewayUrl) return { ...realGateway, unexpected: () => {} };
      return await import(url);
    },
  });
  await assert.rejects(extraExport(context()), /unsupported OpenClaw internal bridge/);
});

test("refuses a replaced FIFO without blocking", { timeout: 2_000 }, async (t) => {
  if (process.platform === "win32") t.skip("FIFO test requires a POSIX host");
  const fixture = await makePackage(t);
  const observer = createFleetSnapshotObserver({
    openclawPackageRoot: fixture.root,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
  });
  await observer(context());
  const gatewayPath = path.join(fixture.root, fixture.pinned.gateway.relativePath);
  await rm(gatewayPath);
  await execFileAsync("mkfifo", [gatewayPath]);
  await assert.rejects(observer(context()), /unsupported OpenClaw internal bridge/);
});

test("refuses malformed handler context before private composition", async (t) => {
  const fixture = await makePackage(t);
  const observer = createFleetSnapshotObserver({
    openclawPackageRoot: fixture.root,
    sdkModuleUrl: fixture.sdkModuleUrl,
    pinned: fixture.pinned,
  });
  const invalidContexts = [
    null,
    {},
    { ...context(), cron: {} },
    { ...context(), cron: { getSuspensionBlockerCount: () => Number.NaN } },
    { ...context(), chatAbortControllers: [] },
    { ...context(), chatQueuedTurns: new Map([["bad", { controller: {} }]]) },
    { ...context(), terminalSessions: undefined },
    { ...context(), terminalSessions: { size: -1 } },
  ];
  for (const value of invalidContexts) {
    await assert.rejects(observer(value), /unsupported OpenClaw internal bridge/);
  }
});

test("refuses malformed canonical counter projections", async (t) => {
  const fixture = await makePackage(t);
  const gatewayUrl = pathToFileURL(
    path.join(fixture.root, fixture.pinned.gateway.relativePath),
  ).href;
  const serverUrl = pathToFileURL(
    path.join(fixture.root, fixture.pinned.server.relativePath),
  ).href;
  const sdkUrl = pathToFileURL(
    path.join(fixture.root, "dist", "plugin-sdk", "plugin-entry.js"),
  ).href;
  const realGateway = await import(gatewayUrl);
  const realServer = await import(serverUrl);
  const realSdk = await import(sdkUrl);
  const malformed = [
    { idle: true, counts: {} },
    {
      idle: false,
      counts: Object.fromEntries(
        [...COMPONENT_COUNT_KEYS, "totalActive"].map((key) => [key, key === "totalActive" ? 1 : 0]),
      ),
    },
    {
      idle: true,
      counts: Object.fromEntries(
        [...COMPONENT_COUNT_KEYS, "totalActive"].map((key) => [key, key === "queueSize" ? -1 : 0]),
      ),
    },
  ];

  for (const snapshot of malformed) {
    const aggregate = function createGatewayActiveWorkSnapshot() { return snapshot; };
    fixture.pinned.gateway.functionSha256.t = sha256(aggregate.toString());
    const observer = createFleetSnapshotObserver({
      openclawPackageRoot: fixture.root,
      sdkModuleUrl: fixture.sdkModuleUrl,
      pinned: fixture.pinned,
      importModule: async (url) => {
        if (url === gatewayUrl) {
          return {
            n: realGateway.n,
            t: aggregate,
          };
        }
        if (url === serverUrl) return realServer;
        if (url === sdkUrl) return realSdk;
        throw new Error("unexpected module");
      },
    });
    await assert.rejects(observer(context()), /unsupported OpenClaw internal bridge/);
  }
});

test("refuses swallowed inspector failures and incomplete sampling", async (t) => {
  const fixture = await makePackage(t);
  const gatewayUrl = pathToFileURL(
    path.join(fixture.root, fixture.pinned.gateway.relativePath),
  ).href;
  const serverUrl = pathToFileURL(
    path.join(fixture.root, fixture.pinned.server.relativePath),
  ).href;
  const sdkUrl = pathToFileURL(
    path.join(fixture.root, "dist", "plugin-sdk", "plugin-entry.js"),
  ).href;
  const realGateway = await import(gatewayUrl);
  const realServer = await import(serverUrl);
  const realSdk = await import(sdkUrl);
  const emptyCounts = () => Object.fromEntries(
    [...COMPONENT_COUNT_KEYS, "totalActive"].map((key) => [key, 0]),
  );
  const aggregators = [
    function createGatewayActiveWorkSnapshot(inspectors) {
      for (const key of COMPONENT_COUNT_KEYS.slice(0, 4)) void key;
      inspectors.getCronRuns();
      inspectors.getChatRuns();
      inspectors.getQueuedTurns();
      inspectors.getTerminalPersistence();
      return { idle: true, counts: emptyCounts() };
    },
    function createGatewayActiveWorkSnapshot(inspectors) {
      try {
        inspectors.getCronRuns();
      } catch {}
      inspectors.getChatRuns();
      inspectors.getQueuedTurns();
      inspectors.getTerminalPersistence();
      inspectors.getTerminalSessions();
      return { idle: true, counts: emptyCounts() };
    },
  ];

  for (const [index, aggregate] of aggregators.entries()) {
    fixture.pinned.gateway.functionSha256.t = sha256(aggregate.toString());
    const observer = createFleetSnapshotObserver({
      openclawPackageRoot: fixture.root,
      sdkModuleUrl: fixture.sdkModuleUrl,
      pinned: fixture.pinned,
      importModule: async (url) => {
        if (url === gatewayUrl) return { n: realGateway.n, t: aggregate };
        if (url === serverUrl) return realServer;
        if (url === sdkUrl) return realSdk;
        throw new Error("unexpected module");
      },
    });
    const liveContext = context();
    if (index === 1) liveContext.cron.getSuspensionBlockerCount = () => Number.NaN;
    await assert.rejects(observer(liveContext), /unsupported OpenClaw internal bridge/);
  }
});

test("registers with default admin and profile authorization", async (t) => {
  const fixture = await makePackage(t);
  const registrations = [];
  registerFleetSnapshot(
    {
      pluginConfig: { openclawPackageRoot: fixture.root },
      registerGatewayMethod(...args) {
        registrations.push(args);
      },
    },
    {
      sdkModuleUrl: fixture.sdkModuleUrl,
      pinned: fixture.pinned,
      now: () => new Date("2026-09-05T04:00:00.000Z"),
    },
  );
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].length, 2);
  assert.equal(registrations[0][0], METHOD_NAME);

  const responses = [];
  await registrations[0][1]({
    params: {},
    context: context(),
    respond(...args) {
      responses.push(args);
    },
  });
  assert.equal(responses.length, 1);
  assert.equal(responses[0][0], true);
  assert.equal(responses[0][1].schema, "idol.openclaw.active-work");
});

test("returns generic errors without private content", async (t) => {
  const fixture = await makePackage(t);
  const registrations = [];
  registerFleetSnapshot(
    {
      pluginConfig: { openclawPackageRoot: fixture.root },
      registerGatewayMethod(...args) {
        registrations.push(args);
      },
    },
    { sdkModuleUrl: fixture.sdkModuleUrl, pinned: fixture.pinned },
  );
  const handler = registrations[0][1];

  const unavailable = [];
  await handler({
    params: {},
    context: { private: "fixture-private-content" },
    respond(...args) {
      unavailable.push(args);
    },
  });
  assert.deepEqual(unavailable, [[
    false,
    undefined,
    { code: "UNAVAILABLE", message: "Fleet active-work snapshot unavailable" },
  ]]);
  assert.equal(JSON.stringify(unavailable).includes("fixture-private-content"), false);

  const invalid = [];
  await handler({
    params: { prompt: "fixture-private-content" },
    context: context(),
    respond(...args) {
      invalid.push(args);
    },
  });
  assert.deepEqual(invalid, [[
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "Fleet active-work snapshot takes no parameters" },
  ]]);
  assert.equal(JSON.stringify(invalid).includes("fixture-private-content"), false);
});

test("manifest requires an explicit package root and declares no weaker scope", async () => {
  const packageManifest = JSON.parse(
    await readFile(new URL("./package.json", import.meta.url), "utf8"),
  );
  assert.equal(packageManifest.private, true);
  assert.deepEqual(packageManifest.openclaw.extensions, ["./index.js"]);
  for (const key of ["dependencies", "peerDependencies", "optionalDependencies"]) {
    assert.equal(packageManifest[key], undefined);
  }
  const manifest = JSON.parse(
    await readFile(new URL("./openclaw.plugin.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(manifest.configSchema.required, ["openclawPackageRoot"]);
  assert.equal(manifest.configSchema.additionalProperties, false);
  const indexSource = await readFile(new URL("./index.js", import.meta.url), "utf8");
  assert.match(indexSource, /openclaw\/plugin-sdk\/plugin-entry/);
  assert.doesNotMatch(indexSource, /profileAccess|operator\.(read|write|admin)/);
});
