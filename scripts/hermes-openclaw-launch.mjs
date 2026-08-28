import crypto from "node:crypto";

function decryptPassword() {
  const encrypted = process.env.HERMES_WEBUI_PASSWORD_ENC;
  const gatewayToken = process.env.OPENCLAW_GATEWAY_TOKEN;
  if (!encrypted || !gatewayToken) {
    throw new Error("missing-encrypted-hermes-credential");
  }
  const packed = Buffer.from(encrypted, "base64url");
  if (packed.length < 29) throw new Error("invalid-hermes-credential-payload");
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
