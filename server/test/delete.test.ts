import { createScheduledController } from "cloudflare:test"
import { env } from "cloudflare:workers"
import { beforeAll, describe, expect, it } from "vitest"
import worker from "../src/index"
import { sha256 } from "../src/http"
import { fileKey, sweep } from "../src/storage"
import { call, fakeGitHub, type Files, MACHINE_1, MACHINE_2, manifest, push, put, rowCounts, signIn, snapshotRows, storedKeys, USER_TABLES } from "./helpers"

let github: ReturnType<typeof fakeGitHub>
let n = 5000
const newUser = async () => {
	const { token, user } = await signIn(github, ++n, `user-${n}`)
	await env.DB.prepare("UPDATE users SET rights_confirmed_at = '2026-01-01T00:00:00Z' WHERE id = ?1").bind(user.id).run()
	return { token, id: user.id, githubId: n }
}
beforeAll(() => {
	github = fakeGitHub()
})

const FILES: Files = {
	"sessions/a1.json": ['{"id":"a1"}', "octo/alpha"],
	"sessions/a2.json": ['{"id":"a2"}', "octo/alpha"],
	"sessions/b1.json": ['{"id":"b1"}', "octo/beta"],
	"findings.json": ['{"findings":"quote both projects"}'],
	"github.json": ['{"prs":{}}'],
}
const projectId = async (token: string, key: string, machine = MACHINE_1) =>
	(await call("GET", "/v1/projects", { token })).json.projects.find((p: { key: string; machine: string }) => p.key === key && p.machine === machine).id

describe("deleting a project", () => {
	it("removes its sessions and the machine's shared files, and keeps the other project", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		expect((await call("DELETE", `/v1/projects/${await projectId(u.token, "octo/alpha")}`, { token: u.token })).status).toBe(204)

		expect((await snapshotRows(u.id)).map(r => r.path)).toEqual(["sessions/b1.json"])
		expect(await storedKeys(`u/${u.id}/`)).toEqual([fileKey(u.id, await sha256(FILES["sessions/b1.json"]![0]))])
		const listed = (await call("GET", "/v1/projects", { token: u.token })).json
		expect(listed.projects.map((p: { key: string }) => p.key)).toEqual(["octo/beta"])
		expect(listed.usage.bytes).toBe(FILES["sessions/b1.json"]![0].length)

		// Gone means gone: a second try finds nothing.
		expect((await call("DELETE", `/v1/projects/${await sha256("x")}`, { token: u.token })).status).toBe(404)
		// The next push sends the shared files again.
		const { "sessions/a1.json": _1, "sessions/a2.json": _2, ...rest } = FILES
		expect((await push(u.token, MACHINE_1, rest)).missing).toEqual(["findings.json", "github.json"])
	})

	it("keeps a file another machine of the user still lists", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		await push(u.token, MACHINE_2, { "sessions/a1.json": FILES["sessions/a1.json"]! })
		await call("DELETE", `/v1/projects/${await projectId(u.token, "octo/alpha", MACHINE_1)}`, { token: u.token })
		const keys = await storedKeys(`u/${u.id}/`)
		expect(keys).toContain(fileKey(u.id, await sha256(FILES["sessions/a1.json"]![0])))
		expect(keys).not.toContain(fileKey(u.id, await sha256(FILES["sessions/a2.json"]![0])))
		expect((await snapshotRows(u.id)).filter(r => r.machine === MACHINE_2)).toHaveLength(1)
	})

	it("frees the machine with its last project, and cancels open uploads", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, { "sessions/a1.json": FILES["sessions/a1.json"]!, "findings.json": FILES["findings.json"]! })
		const open = await call("POST", "/v1/uploads", { token: u.token, body: await manifest(MACHINE_1, FILES) })
		await put(u.token, open.json.upload_id, "sessions/b1.json", FILES["sessions/b1.json"]![0])
		expect(await storedKeys(`u/${u.id}/`)).toHaveLength(3)
		expect((await call("DELETE", `/v1/projects/${await projectId(u.token, "octo/alpha")}`, { token: u.token })).status).toBe(204)

		const { tokens: _tokens, users: _users, ...rest } = await rowCounts(u.id)
		expect(rest).toEqual({ uploads: 0, upload_objects: 0, snapshots: 0, projects: 0, objects: 0 })
		// Nothing is left, not even the file that had arrived for the cancelled upload.
		expect(await storedKeys(`u/${u.id}/`)).toEqual([])
		expect((await call("POST", `/v1/uploads/${open.json.upload_id}/commit`, { token: u.token })).status).toBe(404)
	})
})

describe("deleting the account", () => {
	it("leaves no row and no file", async () => {
		const u = await newUser()
		const other = await signIn(github, u.githubId, "second-token")
		await push(u.token, MACHINE_1, FILES)
		await push(u.token, MACHINE_2, { "sessions/c1.json": ['{"id":"c1"}', "octo/gamma"] })
		// An open upload with a file that arrived, too.
		const open = await call("POST", "/v1/uploads", { token: u.token, body: await manifest(MACHINE_1, { ...FILES, "sessions/a3.json": ['{"id":"a3"}', "octo/alpha"] }) })
		await put(u.token, open.json.upload_id, "sessions/a3.json", '{"id":"a3"}')
		expect(await storedKeys(`u/${u.id}/`)).toHaveLength(7)
		expect(Object.values(await rowCounts(u.id)).every(count => count > 0)).toBe(true)

		expect((await call("DELETE", "/v1/me", { token: u.token })).status).toBe(204)

		expect(await storedKeys(`u/${u.id}/`)).toEqual([])
		expect(await rowCounts(u.id)).toEqual(Object.fromEntries([...USER_TABLES, "users"].map(t => [t, 0])))
		// No table mentions the user's id or GitHub id anywhere.
		for (const table of [...USER_TABLES, "users", "device_codes", "guard"]) {
			const dump = JSON.stringify((await env.DB.prepare(`SELECT * FROM ${table}`).all()).results)
			expect(dump, table).not.toContain(u.id)
			expect(dump, table).not.toContain(`"github_id":${u.githubId}`)
		}
		// Every token of the user is dead.
		for (const token of [u.token, other.token]) expect((await call("GET", "/v1/projects", { token })).status).toBe(401)

		// Signing in again makes a new, empty account.
		const again = await signIn(github, u.githubId, "back-again")
		expect(again.user.id).not.toBe(u.id)
		expect((await call("GET", "/v1/projects", { token: again.token })).json).toEqual({ projects: [], usage: { bytes: 0, quota_bytes: 500_000_000 } })
		const refused = await call("POST", "/v1/uploads", { token: again.token, body: await manifest(MACHINE_1, {}) })
		expect(refused.json.error.code).toBe("confirmation_required")
	})

	it("deletes more files than one listing returns", async () => {
		const u = await newUser()
		for (let i = 0; i < 1100; i++) await env.BUCKET.put(`u/${u.id}/b/${i.toString(16).padStart(64, "0")}`, "x")
		expect((await call("DELETE", "/v1/me", { token: u.token })).status).toBe(204)
		expect(await storedKeys(`u/${u.id}/`)).toEqual([])
	})
})

describe("the sweep", () => {
	it("deletes files nothing lists, and rows that have run out", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, { "sessions/a1.json": FILES["sessions/a1.json"]! })
		// An abandoned upload with one file that arrived, an orphan, and a file of a user who is gone.
		const open = await call("POST", "/v1/uploads", { token: u.token, body: await manifest(MACHINE_2, { "sessions/b1.json": FILES["sessions/b1.json"]! }) })
		await put(u.token, open.json.upload_id, "sessions/b1.json", FILES["sessions/b1.json"]![0])
		const orphan = fileKey(u.id, "0".repeat(64)), ghost = fileKey("u_gone", "1".repeat(64))
		await env.BUCKET.put(orphan, "x")
		await env.BUCKET.put(ghost, "x")
		const kept = fileKey(u.id, await sha256(FILES["sessions/a1.json"]![0])), pending = fileKey(u.id, await sha256(FILES["sessions/b1.json"]![0]))

		// On the hour, with the grace of one hour: everything here is too young to judge.
		await worker.scheduled(createScheduledController(), env)
		for (const key of [kept, pending, orphan, ghost]) expect(await env.BUCKET.head(key), key).not.toBeNull()

		// Without the grace: the orphans go, the snapshot's file and the open upload's file stay.
		expect(await sweep(env, -1000)).toBe(2)
		expect(await env.BUCKET.head(orphan)).toBeNull()
		expect(await env.BUCKET.head(ghost)).toBeNull()
		expect(await env.BUCKET.head(kept)).not.toBeNull()
		expect(await env.BUCKET.head(pending)).not.toBeNull()

		// Once the upload has expired its rows go, and so does its file.
		await env.DB.prepare("UPDATE uploads SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?1").bind(open.json.upload_id).run()
		await env.DB.prepare("INSERT INTO device_codes VALUES ('h', 'g', 5, '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z')").run()
		await env.DB.prepare("INSERT INTO tokens VALUES ('old', ?1, '2000-01-01T00:00:00Z')").bind(u.id).run()
		expect(await sweep(env, -1000)).toBe(1)
		expect(await env.BUCKET.head(pending)).toBeNull()
		expect(await env.BUCKET.head(kept)).not.toBeNull()
		expect(await rowCounts(u.id)).toMatchObject({ uploads: 0, upload_objects: 0, tokens: 1, objects: 1 })
		expect((await env.DB.prepare("SELECT COUNT(*) AS n FROM device_codes").first<{ n: number }>())!.n).toBe(0)
	})
})
