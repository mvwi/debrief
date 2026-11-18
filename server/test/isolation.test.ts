import { env } from "cloudflare:workers"
import { beforeAll, describe, expect, it } from "vitest"
import { forUser } from "../src/db"
import { call, CONFIRM, fakeGitHub, type Files, MACHINE_1, manifest, push, put, rowCounts, signIn, snapshotRows, storedKeys } from "./helpers"

// User A has a committed snapshot and an open upload. User B, signed in and otherwise a normal
// user, tries every route against A's things. Each test ends by checking A lost nothing.

const A_FILES: Files = {
	"sessions/a1.json": ['{"id":"a1","secret":"of A"}', "octo/alpha"],
	"findings.json": ['{"findings":{"1":"about A"}}'],
}
const A_NEXT: Files = { ...A_FILES, "sessions/a2.json": ['{"id":"a2"}', "octo/alpha"] }

let a: { token: string; id: string; project: string; upload: string }
let b: { token: string; id: string }
let before: { rows: unknown; counts: unknown; keys: string[]; projects: unknown }

const stateOfA = async () => ({
	rows: await snapshotRows(a.id),
	counts: await rowCounts(a.id),
	keys: await storedKeys(`u/${a.id}/`),
	projects: (await call("GET", "/v1/projects", { token: a.token })).json,
})

beforeAll(async () => {
	const github = fakeGitHub()
	const userA = await signIn(github, 1, "user-a"), userB = await signIn(github, 2, "user-b")
	const pushed = await push(userA.token, MACHINE_1, A_FILES)
	const open = await call("POST", "/v1/uploads", { token: userA.token, body: await manifest(MACHINE_1, A_NEXT) })
	a = { token: userA.token, id: userA.user.id, project: pushed.projects[0].id, upload: open.json.upload_id }
	b = { token: userB.token, id: userB.user.id }
	// B is a real user with data of their own, on a machine with the very same id.
	await push(b.token, MACHINE_1, { "sessions/b1.json": ['{"id":"b1"}', "octo/beta"] })
	before = await stateOfA()
	expect(before.keys).toHaveLength(2)
})

describe("user B cannot reach anything of user A", () => {
	it("list: sees only their own projects and usage", async () => {
		const listed = await call("GET", "/v1/projects", { token: b.token })
		expect(listed.json.projects.map((p: { key: string }) => p.key)).toEqual(["octo/beta"])
		expect(JSON.stringify(listed.json)).not.toContain("alpha")
		expect(listed.json.usage.bytes).toBe('{"id":"b1"}'.length)
	})

	it("upload: cannot send a file to A's upload", async () => {
		const r = await put(b.token, a.upload, "sessions/a2.json", A_NEXT["sessions/a2.json"]![0])
		expect([r.status, r.json.error.code]).toEqual([404, "not_found"])
		expect(await stateOfA()).toEqual(before)
		expect(await storedKeys(`u/${b.id}/`)).toHaveLength(1)
	})

	it("upload: cannot commit A's upload", async () => {
		const r = await call("POST", `/v1/uploads/${a.upload}/commit`, { token: b.token })
		expect([r.status, r.json.error.code]).toEqual([404, "not_found"])
		expect(await stateOfA()).toEqual(before)
	})

	it("upload: a push for the same machine id leaves A's snapshot and open upload alone", async () => {
		// B's create would cancel an earlier upload of (B, machine). A's must survive it.
		const created = await call("POST", "/v1/uploads", { token: b.token, body: await manifest(MACHINE_1, {}), headers: CONFIRM })
		expect(created.status).toBe(201)
		expect((await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: b.token })).status).toBe(200)
		expect(await stateOfA()).toEqual(before)
		await push(b.token, MACHINE_1, { "sessions/b1.json": ['{"id":"b1"}', "octo/beta"] })
	})

	it("read: cannot learn that A holds a file, and gets no copy of it", async () => {
		// B lists A's exact bytes. If the server shared files between users, `missing` would be empty.
		const created = await call("POST", "/v1/uploads", { token: b.token, body: await manifest(MACHINE_1, { "sessions/a1.json": A_FILES["sessions/a1.json"]! }), headers: CONFIRM })
		expect(created.json.missing).toEqual(["sessions/a1.json"])
		const commit = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: b.token })
		expect([commit.status, commit.json.error.code]).toEqual([409, "incomplete"])
		expect(await stateOfA()).toEqual(before)
	})

	it("delete: cannot delete A's project", async () => {
		const r = await call("DELETE", `/v1/projects/${a.project}`, { token: b.token })
		expect([r.status, r.json.error.code]).toEqual([404, "not_found"])
		expect(await stateOfA()).toEqual(before)
	})

	it("sign-out: revokes only B's token", async () => {
		const github = fakeGitHub()
		const other = await signIn(github, 2, "user-b")
		expect((await call("DELETE", "/v1/auth/token", { token: other.token })).status).toBe(204)
		expect((await call("GET", "/v1/projects", { token: a.token })).status).toBe(200)
		expect(await stateOfA()).toEqual(before)
	})

	it("A's open upload still works after all of that", async () => {
		expect((await put(a.token, a.upload, "sessions/a2.json", A_NEXT["sessions/a2.json"]![0])).status).toBe(204)
		const done = await call("POST", `/v1/uploads/${a.upload}/commit`, { token: a.token })
		expect(done.json).toMatchObject({ objects: 3, removed: 0 })
		before = await stateOfA()
	})

	it("delete: B deleting their account takes nothing of A's", async () => {
		expect((await call("DELETE", "/v1/me", { token: b.token })).status).toBe(204)
		expect(await storedKeys(`u/${b.id}/`)).toEqual([])
		expect(await stateOfA()).toEqual(before)
		expect(before.keys).toHaveLength(3)
		for (const key of before.keys) expect(key.startsWith(`u/${a.id}/b/`)).toBe(true)
	})
})

describe("the database helper", () => {
	it("refuses a query that does not use the user id", () => {
		const udb = forUser(env.DB, "u_x")
		expect(() => udb.q("SELECT * FROM objects")).toThrow(/without the user id/)
		expect(() => udb.q("SELECT * FROM objects WHERE machine = ?12")).toThrow(/without the user id/)
		expect(() => udb.q("SELECT * FROM objects WHERE user_id = ?1")).not.toThrow()
	})

	it("is the only place a statement is prepared", () => {
		const sources = import.meta.glob<string>("../src/*.ts", { query: "?raw", import: "default", eager: true })
		expect(Object.keys(sources).length).toBeGreaterThan(5)
		const offenders = Object.entries(sources).filter(([file, text]) => !file.endsWith("/db.ts") && /\.prepare\(|env\.DB\.(batch|exec)/.test(text))
		expect(offenders.map(([file]) => file)).toEqual([])
	})
})
