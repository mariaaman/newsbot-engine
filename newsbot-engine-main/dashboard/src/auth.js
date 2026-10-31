// Password (+ optional TOTP) login, signed cookie sessions, lock-out and "log out everywhere". State lives in KV; secrets in Worker secrets.
import { HttpError, b64url, unb64url, hmacHex, safeEqual, seal, unseal, must } from "./util.js";
import { verifyTotp, newTotpSecret, otpauthUri } from "./totp.js";

export const COOKIE = "nb_session";
const TTL_S = 12 * 3600, FAIL_LIMIT = 5, LOCK_S = 900;

export const clientIp = (request) => request.headers.get("CF-Connecting-IP") || "unknown";

function need(env) {
  if (!env.KV) throw new HttpError(500, "KV namespace is not bound");
  if (!env.SESSION_SECRET || env.SESSION_SECRET.length < 32) throw new HttpError(500, "SESSION_SECRET is missing or too short");
  if (!env.DASHBOARD_PASSWORD || env.DASHBOARD_PASSWORD.length < 14) throw new HttpError(500, "DASHBOARD_PASSWORD is missing or too short");
}

const getCookie = (request, name) => {
  const m = (request.headers.get("Cookie") || "").match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return m ? m[1] : "";
};

export async function readSession(request, env) {
  if (!env.SESSION_SECRET || !env.KV) return null;
  const raw = getCookie(request, COOKIE);
  const [body, sig] = raw.split(".");
  if (!body || !sig) return null;
  if (!safeEqual(sig, await hmacHex(env.SESSION_SECRET, body))) return null;
  let s;
  try { s = JSON.parse(new TextDecoder().decode(unb64url(body))); } catch { return null; }
  if (!s || typeof s.exp !== "number" || s.exp < Date.now()) return null;
  if (String(s.epoch) !== String((await env.KV.get("epoch")) || "0")) return null;      // "log out everywhere" bumps the epoch
  return s;
}

async function newCookie(env) {
  const body = b64url(new TextEncoder().encode(JSON.stringify({
    iat: Date.now(), exp: Date.now() + TTL_S * 1000, epoch: (await env.KV.get("epoch")) || "0", n: b64url(crypto.getRandomValues(new Uint8Array(9))),
  })));
  return `${COOKIE}=${body}.${await hmacHex(env.SESSION_SECRET, body)}; Path=/; Max-Age=${TTL_S}; HttpOnly; Secure; SameSite=Strict`;
}
export const clearCookie = () => `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict`;

async function totpState(env) {
  const raw = await env.KV.get("totp");
  if (!raw) return null;
  try { return JSON.parse(await unseal(env.SESSION_SECRET, raw)); } catch { return null; }
}

export async function authStatus(env) {
  need(env);
  return { totp: !!(await totpState(env)) };
}

async function failed(env, ip) {
  const key = `fail:${ip}`, n = Number((await env.KV.get(key)) || 0) + 1;
  await env.KV.put(key, String(n), { expirationTtl: LOCK_S });
  if (n >= FAIL_LIMIT) await env.KV.put(`lock:${ip}`, "1", { expirationTtl: LOCK_S });
}

/** Returns the Set-Cookie value on success; throws HttpError otherwise. Same message for a wrong password and a wrong code. */
export async function login(request, env, body) {
  need(env);
  const ip = clientIp(request);
  if (await env.KV.get(`lock:${ip}`)) throw new HttpError(429, "Too many attempts. Try again in 15 minutes.");
  const t = await totpState(env);
  const passOk = typeof body.password === "string" && safeEqual(body.password, env.DASHBOARD_PASSWORD);
  let counter = -1;
  if (t) counter = await verifyTotp(t.secret, body.code);
  const last = Number((await env.KV.get("totp_last")) || -1);
  const codeOk = !t || (counter >= 0 && counter > last);
  if (!(passOk && codeOk)) { await failed(env, ip); throw new HttpError(401, t ? "Wrong password or code." : "Wrong password."); }
  if (t) await env.KV.put("totp_last", String(counter));
  await env.KV.delete(`fail:${ip}`);
  return newCookie(env);
}

export async function logoutAll(env) {
  await env.KV.put("epoch", String(Number((await env.KV.get("epoch")) || 0) + 1));
}

export async function totpBegin(env, body) {
  must(typeof body.password === "string" && safeEqual(body.password, env.DASHBOARD_PASSWORD), 403, "Wrong password.");
  const secret = newTotpSecret();
  await env.KV.put("totp_pending", await seal(env.SESSION_SECRET, JSON.stringify({ secret })), { expirationTtl: 600 });
  return { secret, uri: otpauthUri(secret, "dashboard", "NewsBot") };
}

export async function totpConfirm(env, body) {
  const raw = await env.KV.get("totp_pending");
  must(raw, 400, "Start again: the setup expired.");
  const { secret } = JSON.parse(await unseal(env.SESSION_SECRET, raw));
  const counter = await verifyTotp(secret, body.code);
  must(counter >= 0, 400, "That code is not right.");
  await env.KV.put("totp", await seal(env.SESSION_SECRET, JSON.stringify({ secret })));
  await env.KV.put("totp_last", String(counter));
  await env.KV.delete("totp_pending");
  await logoutAll(env);                                      // every other session must now pass the second step
}

export async function totpDisable(env, body) {
  must(typeof body.password === "string" && safeEqual(body.password, env.DASHBOARD_PASSWORD), 403, "Wrong password.");
  const t = await totpState(env);
  must(t, 400, "Two-step is not on.");
  must((await verifyTotp(t.secret, body.code)) >= 0, 403, "Wrong code.");
  await env.KV.delete("totp");
  await logoutAll(env);
}

export async function audit(env, request, action, detail = "") {
  try {
    const list = JSON.parse((await env.KV.get("audit")) || "[]");
    list.unshift({ t: new Date().toISOString(), ip: clientIp(request), action, detail: String(detail).slice(0, 200) });
    await env.KV.put("audit", JSON.stringify(list.slice(0, 150)));
  } catch { /* the audit trail is best effort */ }
}
