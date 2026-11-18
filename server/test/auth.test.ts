import { env } from "cloudflare:workers"
import { beforeEach, describe, expect, it } from "vitest"
import { sha256 } from "../src/http"
import { call, fakeGitHub, nextIp, signIn } from "./helpers"

let github: ReturnType<typeof fakeGitHub>
beforeEach(() => {
	github = fakeGitHub()
})

const start = async () => (await call("POST", "/v1/auth/device", { headers: nextIp() })).json
const poll = (device_code: unknown) => call("POST", "/v1/auth/token", { body: { device_code } })

describe("device flow", () => {
	it("signs a user in and returns a token that works", async () => {
		const device = await start()
		expect(device).toMatchObject({ user_code: "WDJB-MJHT", verification_uri: "https://github.com/login/device", expires_in: 900 })
		expect(device.device_code).not.toContain("gh-device-")

		expect((await poll(device.device_code)).status).toBe(202)
		github.outcome = { id: 583231, login: "octocat" }
		const done = await poll(device.device_code)
		expect(done.status).toBe(200)
		expect(done.json.user).toMatchObject({ github_id: 583231, login: "octocat" })
		expect(done.json.token).toMatch(/^dbf_/)
		expect(Date.parse(done.json.expires_at) - Date.now()).toBeGreaterThan(89 * 86_400_000)

		expect((await call("GET", "/v1/projects", { token: done.json.token })).status).toBe(200)
		// The code is spent.
		expect((await poll(device.device_code)).json.error.code).toBe("invalid_device_code")
	})

	it("keeps the same account across sign-ins and follows a renamed login", async () => {
		const first = await signIn(github, 77, "old-name")
		const second = await signIn(github, 77, "new-name")
		expect(second.user.id).toBe(first.user.id)
		expect(second.user.login).toBe("new-name")
		expect(second.token).not.toBe(first.token)
	})

	it("stores the hash of its token, and nothing of GitHub's", async () => {
		const { token } = await signIn(github, 78, "hash-check")
		const dump = JSON.stringify([
			(await env.DB.prepare("SELECT * FROM tokens").all()).results,
			(await env.DB.prepare("SELECT * FROM users").all()).results,
			(await env.DB.prepare("SELECT * FROM device_codes").all()).results,
		])
		expect(dump).toContain(await sha256(token))
		expect(dump).not.toContain(token)
		expect(github.issued.length).toBeGreaterThan(0)
		for (const githubToken of github.issued) expect(dump).not.toContain(githubToken)
	})

	it("does not ask GitHub again before the interval has passed", async () => {
		github.interval = 5
		const device = await start()
		expect(device.interval).toBe(5)
		expect((await poll(device.device_code)).json).toEqual({ status: "pending", interval: 5 })
		expect((await poll(device.device_code)).status).toBe(202)
		expect(github.polls).toBe(1)
	})

	it("passes on a longer interval when GitHub says slow down", async () => {
		const device = await start()
		github.outcome = "slow_down"
		expect((await poll(device.device_code)).json).toEqual({ status: "pending", interval: 5 })
	})

	it("reports a refusal, an expiry and an unknown code", async () => {
		let device = await start()
		github.outcome = "denied"
		let r = await poll(device.device_code)
		expect([r.status, r.json.error.code]).toEqual([403, "access_denied"])

		device = await start()
		github.outcome = "expired"
		r = await poll(device.device_code)
		expect([r.status, r.json.error.code]).toEqual([410, "expired_device_code"])

		device = await start()
		await env.DB.prepare("UPDATE device_codes SET expires_at = '2000-01-01T00:00:00Z'").run()
		r = await poll(device.device_code)
		expect([r.status, r.json.error.code]).toEqual([410, "expired_device_code"])

		r = await poll("never-issued")
		expect([r.status, r.json.error.code]).toEqual([400, "invalid_device_code"])
		r = await poll(42)
		expect([r.status, r.json.error.code]).toEqual([400, "bad_request"])
	})

	it("answers 502 when GitHub is down", async () => {
		const device = await start()
		github.down = true
		expect((await call("POST", "/v1/auth/device", { headers: nextIp() })).json.error.code).toBe("github_unavailable")
		const r = await poll(device.device_code)
		expect([r.status, r.json.error.code]).toEqual([502, "github_unavailable"])
	})

	it("limits sign-in starts per address", async () => {
		const ip = nextIp()
		const statuses = []
		for (let i = 0; i < 11; i++) statuses.push((await call("POST", "/v1/auth/device", { headers: ip })).status)
		expect(statuses.slice(0, 10)).toEqual(Array(10).fill(200))
		const last = await call("POST", "/v1/auth/device", { headers: ip })
		expect(statuses[10]).toBe(429)
		expect(last.json.error.code).toBe("rate_limited")
		expect(last.headers.get("Retry-After")).toBe("60")
	})
})

describe("tokens", () => {
	const ROUTES: [string, string][] = [
		["DELETE", "/v1/auth/token"], ["POST", "/v1/uploads"], ["PUT", "/v1/uploads/up_x/files/findings.json"],
		["POST", "/v1/uploads/up_x/commit"], ["GET", "/v1/projects"], ["DELETE", "/v1/projects/p_x"], ["DELETE", "/v1/me"],
	]

	it("refuses every route without a token, with a made-up one and with a GitHub one", async () => {
		await signIn(github, 80, "someone")
		for (const [method, path] of ROUTES) {
			for (const token of [undefined, "dbf_made_up", github.issued[0]]) {
				const r = await call(method, path, { token })
				expect([method, path, r.status, r.json.error.code]).toEqual([method, path, 401, "invalid_token"])
			}
		}
	})

	it("refuses an expired token on every route", async () => {
		const { token, user } = await signIn(github, 81, "expired")
		await env.DB.prepare("UPDATE tokens SET expires_at = ?1 WHERE user_id = ?2").bind(new Date(Date.now() - 1000).toISOString(), user.id).run()
		for (const [method, path] of ROUTES) {
			const r = await call(method, path, { token })
			expect([method, path, r.status, r.json.error.code]).toEqual([method, path, 401, "invalid_token"])
		}
	})

	it("revokes one token on sign-out and leaves the user's other token alone", async () => {
		const laptop = await signIn(github, 82, "two-tokens")
		const desktop = await signIn(github, 82, "two-tokens")
		expect((await call("DELETE", "/v1/auth/token", { token: laptop.token })).status).toBe(204)
		for (const [method, path] of ROUTES) expect((await call(method, path, { token: laptop.token })).status).toBe(401)
		expect((await call("GET", "/v1/projects", { token: desktop.token })).status).toBe(200)
	})
})

describe("routing", () => {
	it("answers 404 and 405 in the error shape, with the safety headers", async () => {
		let r = await call("GET", "/v1/nothing")
		expect([r.status, r.json.error.code]).toEqual([404, "not_found"])
		r = await call("GET", "/v1/auth/device")
		expect([r.status, r.json.error.code]).toEqual([405, "method_not_allowed"])
		r = await call("PATCH", "/v1/projects")
		expect([r.status, r.json.error.code]).toEqual([405, "method_not_allowed"])
		expect(typeof r.json.error.message).toBe("string")
		expect(r.headers.get("Cache-Control")).toBe("no-store")
		expect(r.headers.get("X-Content-Type-Options")).toBe("nosniff")
		expect(r.headers.get("Content-Type")).toBe("application/json; charset=utf-8")
	})
})
