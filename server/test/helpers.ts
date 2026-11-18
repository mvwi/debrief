import { env, exports } from "cloudflare:workers"
import { vi } from "vitest"
import { sha256 } from "../src/http"

export const MACHINE_1 = "11111111-1111-4111-8111-111111111111"
export const MACHINE_2 = "22222222-2222-4222-8222-222222222222"
export const CONFIRM = { "X-Debrief-Rights-Confirmed": "1" }

interface CallOptions { token?: string; body?: unknown; headers?: Record<string, string> }

/** One request to the Worker. A string body is sent as it is, anything else as JSON. */
export async function call(method: string, path: string, { token, body, headers = {} }: CallOptions = {}) {
	const response = await exports.default.fetch(new Request(`https://debrief.test${path}`, {
		method,
		body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
		headers: { ...(token && { Authorization: `Bearer ${token}` }), ...headers },
	}))
	const text = await response.text()
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	return { status: response.status, headers: response.headers, json: (text ? JSON.parse(text) : null) as any }
}

type Outcome = "pending" | "slow_down" | "denied" | "expired" | { id: number; login: string }

/** GitHub's three endpoints, faked. Set `outcome` to say how the next polls end. */
export function fakeGitHub() {
	const state = { outcome: "pending" as Outcome, interval: 0, polls: 0, down: false, issued: [] as string[] }
	const users = new Map<string, { id: number; login: string }>()
	const reply = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } })
	vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
		const request = new Request(input as RequestInfo, init)
		if (state.down) return new Response("no", { status: 503 })
		if (request.url === "https://github.com/login/device/code") {
			const n = crypto.randomUUID()
			return reply({ device_code: `gh-device-${n}`, user_code: "WDJB-MJHT", verification_uri: "https://github.com/login/device", expires_in: 900, interval: state.interval })
		}
		if (request.url === "https://github.com/login/oauth/access_token") {
			state.polls++
			const o = state.outcome
			if (o === "pending") return reply({ error: "authorization_pending" })
			if (o === "slow_down") return reply({ error: "slow_down", interval: state.interval + 5 })
			if (o === "denied") return reply({ error: "access_denied" })
			if (o === "expired") return reply({ error: "expired_token" })
			const token = `ghu_fake_${crypto.randomUUID()}`
			users.set(token, o)
			state.issued.push(token)
			return reply({ access_token: token, token_type: "bearer", expires_in: 28800 })
		}
		if (request.url === "https://api.github.com/user") {
			const user = users.get((request.headers.get("Authorization") ?? "").replace("Bearer ", ""))
			return user ? reply(user) : new Response("{}", { status: 401 })
		}
		throw new Error(`unexpected outbound request to ${request.url}`)
	})
	return state
}

let ips = 0
/** A fresh address per sign-in, so tests do not trip the per-address limit on each other. */
export const nextIp = () => ({ "CF-Connecting-IP": `203.0.113.${++ips}` })

/** Run the whole device flow as one GitHub user. */
export async function signIn(github: ReturnType<typeof fakeGitHub>, id: number, login: string) {
	const ip = nextIp()
	const device = await call("POST", "/v1/auth/device", { headers: ip })
	github.outcome = { id, login }
	const done = await call("POST", "/v1/auth/token", { body: { device_code: device.json.device_code }, headers: ip })
	github.outcome = "pending"
	if (done.status !== 200) throw new Error(`sign-in failed: ${done.status} ${JSON.stringify(done.json)}`)
	return done.json as { token: string; expires_at: string; user: { id: string; github_id: number; login: string } }
}

/** path -> [body, project key]. Session paths need the key. */
export type Files = Record<string, [body: string, project?: string]>

export async function manifest(machine: string, files: Files, extra: Record<string, unknown> = {}) {
	const keys = [...new Set(Object.values(files).flatMap(([, p]) => (p ? [p] : [])))]
	const objects: Record<string, unknown> = {}
	for (const [path, [body, project]] of Object.entries(files)) {
		objects[path] = { sha256: await sha256(body), bytes: new TextEncoder().encode(body).length, ...(project && { project }) }
	}
	return {
		format: 1, tool: "debrief 0.0.0-test", machine, redaction: 1, unpicked: 0,
		projects: keys.map(key => ({ key, name: key.split("/").pop(), sessions: 1, turns: 2, analysed_at: null })),
		objects, sent: [],
		...extra,
	}
}

export const put = (token: string, uploadId: string, path: string, body: string, length = new TextEncoder().encode(body).length) =>
	call("PUT", `/v1/uploads/${uploadId}/files/${path}`, { token, body, headers: { "Content-Length": String(length), "Content-Type": "application/json" } })

/** A whole push: create, send what is missing, commit. */
export async function push(token: string, machine: string, files: Files) {
	const created = await call("POST", "/v1/uploads", { token, body: await manifest(machine, files), headers: CONFIRM })
	if (created.status !== 201) throw new Error(`create failed: ${created.status} ${JSON.stringify(created.json)}`)
	for (const path of created.json.missing as string[]) {
		const sent = await put(token, created.json.upload_id, path, files[path]![0])
		if (sent.status !== 204) throw new Error(`put ${path} failed: ${sent.status} ${JSON.stringify(sent.json)}`)
	}
	const committed = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token })
	if (committed.status !== 200) throw new Error(`commit failed: ${committed.status} ${JSON.stringify(committed.json)}`)
	return { missing: created.json.missing as string[], ...committed.json }
}

/** Every key in the bucket. */
export async function storedKeys(prefix = ""): Promise<string[]> {
	return (await env.BUCKET.list({ prefix })).objects.map(o => o.key).sort()
}

export const USER_TABLES = ["tokens", "uploads", "upload_objects", "snapshots", "projects", "objects"]

/** How many rows each table holds for a user. */
export async function rowCounts(userId: string): Promise<Record<string, number>> {
	const counts: Record<string, number> = {}
	for (const table of USER_TABLES) {
		counts[table] = (await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE user_id = ?1`).bind(userId).first<{ n: number }>())!.n
	}
	counts.users = (await env.DB.prepare("SELECT COUNT(*) AS n FROM users WHERE id = ?1").bind(userId).first<{ n: number }>())!.n
	return counts
}

/** The committed snapshot of a user, as rows. */
export async function snapshotRows(userId: string) {
	const { results } = await env.DB.prepare("SELECT machine, path, sha256, project FROM objects WHERE user_id = ?1 ORDER BY machine, path").bind(userId).all()
	return results
}
