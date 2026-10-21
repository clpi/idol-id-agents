const password = (process.env.HERMES_WEBUI_PASSWORD || "").trim();

if (!password) {
  console.error("IDOL_HERMES_BRIDGE_ERROR=missing-hermes-webui-password");
  process.exit(2);
}

process.env.HERMES_WEBUI_PASSWORD = password;
await import("./hermes-openclaw-bridge.mjs");
