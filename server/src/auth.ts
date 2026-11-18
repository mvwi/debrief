import * as db from "./db"
import { HttpError, iso, json, noContent, random, readJson, sha256 } from "./http"

const TOKEN_DAYS = 90
const DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

const githubDown = () => new HttpError(502, "github_unavailable", "GitHub did not answer as expected. Try again shortly.")

async function github(url: string, init: RequestInit): Promise<Record<string, unknown>> {
	let response: Response
	try {
		response = await fetch(url, { ...init, headers: { Accept: "application/json", "User-Agent": "debrief-server", ...init.headers } })
	} catch {
		throw githubDown()
	}
	if (!response.ok) throw githubDown()
	return (await response.json().catch(() => { throw githubDown() })) as Record<string, unknown>
}

/** POST /v1/auth/device */
export async function startDevice(env: Env): Promise<Response> {
	const g = await github("https://github.com/login/device/code", {
		method: "POST",
		body: new URLSearchParams({ client_id: env.GITHUB_CLIENT_ID }),
	})
	if (typeof g.device_code !== "string" || typeof g.user_code !== "string") throw githubDown()
	const interval = Number(g.interval ?? 5), expiresIn = Number(g.expires_in ?? 900), now = Date.now()
	// The CLI gets our code, not GitHub's: with GitHub's it could fetch a GitHub token itself.
	const code = random(32)
	await db.insertDeviceCode(env.DB, await sha256(code), {
		github_code: g.device_code, interval, next_poll_at: iso(now), expires_at: iso(now + expiresIn * 1000),
	})
	return json({
		device_code: code,
		user_code: g.user_code,
		verification_uri: g.verification_uri ?? "https://github.com/login/device",
		interval,
		expires_in: expiresIn,
	})
}

/** POST /v1/auth/token */
export async function pollToken(env: Env, request: Request): Promise<Response> {
	const body = await readJson(request, 10_000) as { device_code?: unknown } | null
	if (typeof body?.device_code !== "string") throw new HttpError(400, "bad_request", "device_code is missing.")
	const hash = await sha256(body.device_code), now = Date.now()
	const row = await db.getDeviceCode(env.DB, hash)
	if (!row) throw new HttpError(400, "invalid_device_code", "Unknown or spent device code. Start the sign-in again.")
	const expired = async () => {
		await db.deleteDeviceCode(env.DB, hash)
		return new HttpError(410, "expired_device_code", "The sign-in took too long. Start it again.")
	}
	if (row.expires_at <= iso(now)) throw await expired()
	const pending = (interval: number) => json({ status: "pending", interval }, 202)
	if (iso(now) < row.next_poll_at) return pending(row.interval)
	await db.setDevicePoll(env.DB, hash, row.interval, iso(now + row.interval * 1000))

	const g = await github("https://github.com/login/oauth/access_token", {
		method: "POST",
		body: new URLSearchParams({ client_id: env.GITHUB_CLIENT_ID, device_code: row.github_code, grant_type: DEVICE_GRANT }),
	})
	if (typeof g.access_token === "string") {
		// GitHub's token is used for this one call and never stored.
		const who = await github("https://api.github.com/user", { headers: { Authorization: `Bearer ${g.access_token}` } })
		if (typeof who.id !== "number" || typeof who.login !== "string") throw githubDown()
		const userId = await db.upsertUser(env.DB, `u_${random(16)}`, who.id, who.login, iso(now))
		const token = `dbf_${random(32)}`, expiresAt = iso(now + TOKEN_DAYS * 86_400_000)
		await db.insertToken(env.DB, await sha256(token), userId, expiresAt)
		await db.deleteDeviceCode(env.DB, hash)
		return json({ token, expires_at: expiresAt, user: { id: userId, github_id: who.id, login: who.login } })
	}
	switch (g.error) {
		case "authorization_pending":
			return pending(row.interval)
		case "slow_down": {
			const interval = typeof g.interval === "number" ? g.interval : row.interval + 5
			await db.setDevicePoll(env.DB, hash, interval, iso(now + interval * 1000))
			return pending(interval)
		}
		case "expired_token":
			throw await expired()
		case "access_denied":
			await db.deleteDeviceCode(env.DB, hash)
			throw new HttpError(403, "access_denied", "The sign-in was refused at GitHub.")
		default:
			throw githubDown()
	}
}

/** The signed-in user's database handle and the hash of the token that carried the request. */
export async function authenticate(env: Env, request: Request): Promise<{ udb: db.UserDb; tokenHash: string }> {
	const token = /^Bearer (\S+)$/.exec(request.headers.get("Authorization") ?? "")?.[1]
	const tokenHash = token ? await sha256(token) : ""
	const userId = token ? await db.tokenUser(env.DB, tokenHash, iso(Date.now())) : null
	if (!userId) throw new HttpError(401, "invalid_token", "Sign in again: `debrief login`.", {}, { "WWW-Authenticate": "Bearer" })
	return { udb: db.forUser(env.DB, userId), tokenHash }
}

/** DELETE /v1/auth/token */
export async function logout(udb: db.UserDb, tokenHash: string): Promise<Response> {
	await udb.q("DELETE FROM tokens WHERE user_id = ?1 AND hash = ?2", tokenHash).run()
	return noContent()
}
