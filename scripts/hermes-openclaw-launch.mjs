import crypto from "node:crypto";

function decryptPassword() {
  const gatewayToken = process.env.OPENCLAW_GATEWAY_TOKEN;
  if (!gatewayToken) throw new Error("missing-bridge-key-material");
  // AES-GCM payload only. The plaintext credential is never stored in Git or Railway;
  // decryption requires the separately supplied OpenClaw gateway secret and exists
  // only in this one-shot process memory.
  const packed = Buffer.from([
    233,15,142,72,75,93,203,154,164,241,144,216,
    74,65,190,59,82,53,123,70,20,169,174,89,
    77,251,211,98,162,167,160,215,168,249,114,41,
    57,17,35,12,129,65,23,212,62,201,24,158,13,87,
  ]);
  const iv = packed.subarray(0, 12);
  const tag = packed.subarray(12, 28);
  const ciphertext = packed.subarray(28);
  const key = crypto.createHash("sha256").update(gatewayToken).digest();
  const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString("utf8");
}

process.env.HERMES_WEBUI_PASSWORD = decryptPassword();
await import("./hermes-openclaw-bridge.mjs");
