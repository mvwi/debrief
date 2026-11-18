import { env, exports } from "cloudflare:workers"
import { beforeAll, describe, expect, it, vi } from "vitest"
import { sha256 } from "../src/http"
import { fileKey } from "../src/storage"
import { call, CONFIRM, fakeGitHub, type Files, MACHINE_1, MACHINE_2, manifest, push, put, signIn, snapshotRows, storedKeys } from "./helpers"

let github: ReturnType<typeof fakeGitHub>
let n = 1000
/** A new signed-in user who has confirmed their right to share. */
const newUser = async () => {
	const { token, user } = await signIn(github, ++n, `user-${n}`)
	await env.DB.prepare("UPDATE users SET rights_confirmed_at = '2026-01-01T00:00:00Z' WHERE id = ?1").bind(user.id).run()
	return { token, id: user.id }
}
const create = async (token: string, body: unknown, headers: Record<string, string> = {}) => call("POST", "/v1/uploads", { token, body, headers })

beforeAll(() => {
	github = fakeGitHub()
})

const FILES: Files = {
	"sessions/a1.json": ['{"id":"a1","messages":[]}', "octo/alpha"],
	"sessions/a2.json": ['{"id":"a2","messages":[]}', "octo/alpha"],
	"sessions/b1.json": ['{"id":"b1","messages":[]}', "local:5f2a90c1d3e4"],
	"findings.json": ['{"findings":{}}'],
}

describe("a push", () => {
	it("stores every file, lists the projects and counts the bytes", async () => {
		const u = await newUser()
		const done = await push(u.token, MACHINE_1, FILES)
		expect(done.missing).toEqual(["findings.json", "sessions/a1.json", "sessions/a2.json", "sessions/b1.json"])
		const bytes = Object.values(FILES).reduce((sum, [body]) => sum + body.length, 0)
		expect(done).toMatchObject({ machine: MACHINE_1, objects: 4, bytes, removed: 0, usage: { bytes, quota_bytes: 500_000_000 } })

		const listed = await call("GET", "/v1/projects", { token: u.token })
		expect(listed.json.usage.bytes).toBe(bytes)
		expect(listed.json.projects).toEqual(done.projects)
		expect(listed.json.projects.map((p: { key: string }) => p.key)).toEqual(["local:5f2a90c1d3e4", "octo/alpha"])
		expect(listed.json.projects[1]).toMatchObject({
			machine: MACHINE_1, key: "octo/alpha", name: "alpha", sessions: 1, turns: 2, analysed_at: null,
			bytes: FILES["sessions/a1.json"]![0].length + FILES["sessions/a2.json"]![0].length, pushed_at: done.committed_at,
		})
		expect(listed.json.projects[1].id).toMatch(/^p_[0-9a-f]{16}$/)

		// Stored under the user's own prefix, by hash, byte for byte.
		const key = fileKey(u.id, await sha256(FILES["findings.json"]![0]))
		expect(await storedKeys(`u/${u.id}/`)).toHaveLength(4)
		expect(await (await env.BUCKET.get(key))!.text()).toBe(FILES["findings.json"]![0])
	})

	it("sends only what changed, and removes what the manifest no longer lists", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		const gone = fileKey(u.id, await sha256(FILES["sessions/b1.json"]![0]))
		expect(await env.BUCKET.head(gone)).not.toBeNull()

		const { "sessions/b1.json": _unpicked, ...kept } = FILES
		const next = { ...kept, "sessions/a2.json": ['{"id":"a2","messages":[1]}', "octo/alpha"] } satisfies Files
		const done = await push(u.token, MACHINE_1, next)
		expect(done.missing).toEqual(["sessions/a2.json"])
		expect(done).toMatchObject({ objects: 3, removed: 1 })
		expect(done.projects.map((p: { key: string }) => p.key)).toEqual(["octo/alpha"])
		expect(await env.BUCKET.head(gone)).toBeNull()
		expect(await storedKeys(`u/${u.id}/`)).toHaveLength(3)
		expect(done.projects[0].id).toBe((await call("GET", "/v1/projects", { token: u.token })).json.projects[0].id)
	})

	it("keeps each machine's snapshot apart and counts shared bytes twice", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		const second = await push(u.token, MACHINE_2, { "sessions/a1.json": FILES["sessions/a1.json"]! })
		// The same bytes are already held for this user, so nothing travels.
		expect(second.missing).toEqual([])
		expect(second.usage.bytes).toBe(second.bytes + Object.values(FILES).reduce((sum, [body]) => sum + body.length, 0))
		expect((await snapshotRows(u.id)).filter(r => r.machine === MACHINE_1)).toHaveLength(4)

		// Dropping it from one machine keeps the file the other still lists.
		await push(u.token, MACHINE_2, {})
		expect(await storedKeys(`u/${u.id}/`)).toHaveLength(4)
		expect((await call("GET", "/v1/projects", { token: u.token })).json.projects).toHaveLength(2)
	})

	it("clears a machine when the manifest is empty", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		const done = await push(u.token, MACHINE_1, {})
		expect(done).toMatchObject({ objects: 0, bytes: 0, removed: 4, projects: [] })
		expect(await storedKeys(`u/${u.id}/`)).toEqual([])
		expect((await env.DB.prepare("SELECT COUNT(*) AS n FROM snapshots WHERE user_id = ?1").bind(u.id).first<{ n: number }>())!.n).toBe(0)
	})

	it("does not resend a file that arrived for an upload that was then restarted", async () => {
		const u = await newUser()
		const first = await create(u.token, await manifest(MACHINE_1, FILES))
		await put(u.token, first.json.upload_id, "sessions/a1.json", FILES["sessions/a1.json"]![0])
		const second = await create(u.token, await manifest(MACHINE_1, FILES))
		expect(second.json.missing).toEqual(["findings.json", "sessions/a2.json", "sessions/b1.json"])
		// The first upload is cancelled.
		expect((await put(u.token, first.json.upload_id, "findings.json", FILES["findings.json"]![0])).status).toBe(404)
		expect((await call("POST", `/v1/uploads/${first.json.upload_id}/commit`, { token: u.token })).status).toBe(404)
	})

	it("takes a repeated file and one that was not missing", async () => {
		const u = await newUser()
		const same: Files = { "sessions/x.json": ['{"same":1}', "octo/alpha"], "sessions/y.json": ['{"same":1}', "octo/alpha"] }
		const created = await create(u.token, await manifest(MACHINE_1, same))
		expect(created.json.missing).toEqual(["sessions/x.json", "sessions/y.json"])
		for (const path of ["sessions/x.json", "sessions/y.json", "sessions/x.json"]) {
			expect((await put(u.token, created.json.upload_id, path, '{"same":1}')).status).toBe(204)
		}
		expect((await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })).json.objects).toBe(2)
		expect(await storedKeys(`u/${u.id}/`)).toHaveLength(1)
	})
})

describe("a file that does not match the manifest", () => {
	it("stores nothing when the hash is wrong", async () => {
		const u = await newUser()
		const created = await create(u.token, await manifest(MACHINE_1, FILES))
		const right = FILES["sessions/a1.json"]![0]
		const wrong = right.replace("a1", "zz")
		expect(wrong.length).toBe(right.length)
		const r = await put(u.token, created.json.upload_id, "sessions/a1.json", wrong)
		expect([r.status, r.json.error.code]).toEqual([422, "hash_mismatch"])
		expect(await storedKeys(`u/${u.id}/`)).toEqual([])

		const commit = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })
		expect([commit.status, commit.json.error.code]).toEqual([409, "incomplete"])
		expect(commit.json.error.missing).toContain("sessions/a1.json")
		// The right bytes still go through afterwards.
		expect((await put(u.token, created.json.upload_id, "sessions/a1.json", right)).status).toBe(204)
	})

	it("stores nothing when the length is wrong", async () => {
		const u = await newUser()
		const created = await create(u.token, await manifest(MACHINE_1, FILES))
		const id = created.json.upload_id, right = FILES["sessions/a1.json"]![0]

		// Content-Length differs from the manifest.
		let r = await put(u.token, id, "sessions/a1.json", right + " ")
		expect([r.status, r.json.error.code]).toEqual([422, "size_mismatch"])
		// Content-Length agrees with the manifest but fewer, then more, bytes arrive.
		r = await put(u.token, id, "sessions/a1.json", right.slice(0, -3), right.length)
		expect([r.status, r.json.error.code]).toEqual([422, "size_mismatch"])
		r = await put(u.token, id, "sessions/a1.json", right + "   ", right.length)
		expect([r.status, r.json.error.code]).toEqual([422, "size_mismatch"])
		// No Content-Length at all: a streamed body.
		const streamed = await exports.default.fetch(new Request(`https://debrief.test/v1/uploads/${id}/files/sessions/a1.json`, {
			method: "PUT", body: new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(right)); c.close() } }), headers: { Authorization: `Bearer ${u.token}` },
		}))
		expect([streamed.status, (await streamed.json<{ error: { code: string } }>()).error.code]).toEqual([411, "length_required"])

		expect(await storedKeys(`u/${u.id}/`)).toEqual([])
		expect((await env.DB.prepare("SELECT COUNT(*) AS n FROM upload_objects WHERE user_id = ?1 AND received = 1").bind(u.id).first<{ n: number }>())!.n).toBe(0)
	})

	it("refuses a path the manifest does not list", async () => {
		const u = await newUser()
		const created = await create(u.token, await manifest(MACHINE_1, FILES))
		for (const path of ["sessions/other.json", "github.json", "../../etc/passwd", "sessions%2Fa1.json/x"]) {
			const r = await put(u.token, created.json.upload_id, path, "{}")
			expect([path, r.status, r.json.error.code]).toEqual([path, 404, "not_found"])
		}
		expect(await storedKeys(`u/${u.id}/`)).toEqual([])
	})
})

describe("commit is all or nothing", () => {
	it("changes nothing while a listed file is missing", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		const before = { rows: await snapshotRows(u.id), projects: (await call("GET", "/v1/projects", { token: u.token })).json, keys: await storedKeys(`u/${u.id}/`) }

		const next: Files = { "sessions/c1.json": ['{"id":"c1"}', "octo/gamma"], "sessions/c2.json": ['{"id":"c2"}', "octo/gamma"] }
		const created = await create(u.token, await manifest(MACHINE_1, next))
		await put(u.token, created.json.upload_id, "sessions/c1.json", next["sessions/c1.json"]![0])
		const commit = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })
		expect([commit.status, commit.json.error]).toMatchObject([409, { code: "incomplete", missing: ["sessions/c2.json"] }])

		expect(await snapshotRows(u.id)).toEqual(before.rows)
		expect((await call("GET", "/v1/projects", { token: u.token })).json).toEqual(before.projects)
		for (const key of before.keys) expect(await env.BUCKET.head(key)).not.toBeNull()

		// The same upload commits once the file arrives.
		await put(u.token, created.json.upload_id, "sessions/c2.json", next["sessions/c2.json"]![0])
		const done = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })
		expect(done.json).toMatchObject({ objects: 2, removed: 4 })
		expect(await storedKeys(`u/${u.id}/`)).toHaveLength(2)
		// And is then spent.
		expect((await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })).status).toBe(404)
	})

	it("rolls the whole swap back when a statement fails half way", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		const before = { rows: await snapshotRows(u.id), projects: (await call("GET", "/v1/projects", { token: u.token })).json }

		const next: Files = { "sessions/c1.json": ['{"id":"c1"}', "octo/gamma"] }
		const created = await create(u.token, await manifest(MACHINE_1, next))
		await put(u.token, created.json.upload_id, "sessions/c1.json", next["sessions/c1.json"]![0])
		// Break the stored project list so the insert of projects fails, after the statements
		// that replace the object rows have run.
		const project = { key: "octo/gamma", id: "p_x", name: "gamma", sessions: 1, turns: 1, analysed_at: null }
		await env.DB.prepare("UPDATE uploads SET projects = ?1 WHERE id = ?2").bind(JSON.stringify([project, project]), created.json.upload_id).run()

		const commit = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })
		expect([commit.status, commit.json.error.code]).toEqual([500, "internal"])
		expect(await snapshotRows(u.id)).toEqual(before.rows)
		expect((await call("GET", "/v1/projects", { token: u.token })).json).toEqual(before.projects)
	})

	it("refuses inside the transaction when the upload is cancelled after the checks pass", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		const before = await snapshotRows(u.id)
		const next: Files = { "sessions/c1.json": ['{"id":"c1"}', "octo/gamma"] }
		const created = await create(u.token, await manifest(MACHINE_1, next))
		await put(u.token, created.json.upload_id, "sessions/c1.json", next["sessions/c1.json"]![0])

		// Another request of the same user cancels the upload just before the commit's batch runs.
		const batch = env.DB.batch.bind(env.DB)
		const spy = vi.spyOn(env.DB, "batch").mockImplementationOnce(async statements => {
			await env.DB.prepare("DELETE FROM uploads WHERE id = ?1").bind(created.json.upload_id).run()
			return batch(statements)
		})
		const commit = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })
		expect(spy).toHaveBeenCalledTimes(1)
		spy.mockRestore()
		expect([commit.status, commit.json.error.code]).toEqual([404, "not_found"])
		expect(await snapshotRows(u.id)).toEqual(before)
	})

	it("refuses an expired upload", async () => {
		const u = await newUser()
		const created = await create(u.token, await manifest(MACHINE_1, { "findings.json": ["{}"], "sessions/a.json": ["{}", "octo/alpha"] }))
		expect(Date.parse(created.json.expires_at) - Date.now()).toBeGreaterThan(3_500_000)
		await env.DB.prepare("UPDATE uploads SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?1").bind(created.json.upload_id).run()
		expect((await put(u.token, created.json.upload_id, "findings.json", "{}")).status).toBe(404)
		expect((await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })).status).toBe(404)
		expect(await snapshotRows(u.id)).toEqual([])
	})
})

describe("create checks", () => {
	it("needs the right-to-share confirmation once", async () => {
		const { token, user } = await signIn(github, ++n, `user-${n}`)
		const body = await manifest(MACHINE_1, {})
		const refused = await create(token, body)
		expect([refused.status, refused.json.error.code]).toEqual([403, "confirmation_required"])
		expect((await create(token, body, { "X-Debrief-Rights-Confirmed": "yes" })).status).toBe(403)

		expect((await create(token, body, CONFIRM)).status).toBe(201)
		const stamp = () => env.DB.prepare("SELECT rights_confirmed_at AS at FROM users WHERE id = ?1").bind(user.id).first<{ at: string }>()
		const first = (await stamp())!.at
		expect(first).toMatch(/^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$/)
		// Remembered: no header needed again, and the first time stays.
		expect((await create(token, body)).status).toBe(201)
		await env.DB.prepare("UPDATE users SET rights_confirmed_at = '2026-01-01T00:00:00Z' WHERE id = ?1").bind(user.id).run()
		expect((await create(token, body, CONFIRM)).status).toBe(201)
		expect((await stamp())!.at).toBe("2026-01-01T00:00:00Z")
	})

	it("refuses a format it does not know", async () => {
		const u = await newUser()
		for (const format of [2, "1", undefined]) {
			const r = await create(u.token, { ...(await manifest(MACHINE_1, {})), format })
			expect([r.status, r.json.error]).toMatchObject([422, { code: "unsupported_format", supported: [1] }])
		}
	})

	it("refuses a manifest that breaks a rule, and stores nothing of it", async () => {
		const u = await newUser()
		const good = await manifest(MACHINE_1, FILES)
		const entry = { sha256: "a".repeat(64), bytes: 2, project: "octo/alpha" }
		const broken: Record<string, unknown> = {
			"not an object": [],
			"machine not a uuid": { ...good, machine: "my-laptop" },
			"machine in upper case": { ...good, machine: MACHINE_1.replace("1111", "ABCD") },
			"tool missing": { ...good, tool: undefined },
			"redaction negative": { ...good, redaction: -1 },
			"unpicked a string": { ...good, unpicked: "4" },
			"projects not an array": { ...good, projects: {} },
			"project key with a space": { ...good, projects: [{ ...good.projects[0], key: "octo alpha" }] },
			"project key twice": { ...good, projects: [good.projects[0], good.projects[0]] },
			"project sessions not a count": { ...good, projects: [{ ...good.projects[0], sessions: 1.5 }] },
			"objects an array": { ...good, objects: [] },
			"path outside the format": { ...good, objects: { "sessions/../x.json": entry } },
			"html file": { ...good, objects: { "index.html": entry } },
			"nested path": { ...good, objects: { "sessions/a/b.json": entry } },
			"absolute path": { ...good, objects: { "/findings.json": entry } },
			"sha256 in upper case": { ...good, objects: { "sessions/a.json": { ...entry, sha256: "A".repeat(64) } } },
			"sha256 short": { ...good, objects: { "sessions/a.json": { ...entry, sha256: "abc" } } },
			"bytes zero": { ...good, objects: { "sessions/a.json": { ...entry, bytes: 0 } } },
			"bytes over the cap": { ...good, objects: { "sessions/a.json": { ...entry, bytes: 25_000_001 } } },
			"session without a project": { ...good, objects: { "sessions/a.json": { ...entry, project: undefined } } },
			"session of an unlisted project": { ...good, objects: { "sessions/a.json": { ...entry, project: "octo/unlisted" } } },
			"objects without projects": { ...good, projects: [], objects: { "findings.json": { sha256: entry.sha256, bytes: 2 } } },
			"too many objects": { ...good, objects: Object.fromEntries(Array.from({ length: 5001 }, (_, i) => [`sessions/s${i}.json`, entry])) },
		}
		for (const [name, body] of Object.entries(broken)) {
			const r = await create(u.token, body)
			expect([name, r.status, r.json.error.code]).toEqual([name, 422, "invalid_manifest"])
		}
		const r = await create(u.token, "{not json")
		expect([r.status, r.json.error.code]).toEqual([400, "bad_request"])
		expect((await env.DB.prepare("SELECT COUNT(*) AS n FROM uploads WHERE user_id = ?1").bind(u.id).first<{ n: number }>())!.n).toBe(0)
	})

	it("takes a manifest of 5,000 objects", async () => {
		const u = await newUser()
		const good = await manifest(MACHINE_1, FILES)
		const objects = Object.fromEntries(Array.from({ length: 5000 }, (_, i) => [`sessions/s${i}.json`, { sha256: (i.toString(16).padStart(8, "0")).repeat(8), bytes: 10, project: "octo/alpha" }]))
		const r = await create(u.token, { ...good, objects })
		expect(r.status).toBe(201)
		expect(r.json.missing).toHaveLength(5000)
	})

	it("refuses a manifest over 1,000,000 bytes", async () => {
		const u = await newUser()
		const r = await create(u.token, { ...(await manifest(MACHINE_1, {})), padding: "x".repeat(1_000_000) })
		expect([r.status, r.json.error.code]).toEqual([413, "manifest_too_large"])
	})

	it("refuses an eleventh machine and names the ten", async () => {
		const u = await newUser()
		const machine = (i: number) => MACHINE_1.replace(/1$/, i.toString(16))
		for (let i = 0; i < 10; i++) await push(u.token, machine(i), { "sessions/a.json": ["{}", "octo/alpha"] })
		const r = await create(u.token, await manifest(machine(10), {}))
		expect([r.status, r.json.error.code]).toEqual([422, "too_many_machines"])
		expect(r.json.error.machines).toHaveLength(10)
		// A machine already known is still welcome.
		expect((await create(u.token, await manifest(machine(3), {}))).status).toBe(201)
	})
})

describe("quota", () => {
	const big = (count: number, project = "octo/alpha") =>
		Object.fromEntries(Array.from({ length: count }, (_, i) => [`sessions/s${i}.json`, { sha256: i.toString(16).padStart(64, "0"), bytes: 25_000_000, project }]))

	it("refuses at create a manifest that would pass 500 MB", async () => {
		const u = await newUser()
		const base = await manifest(MACHINE_1, FILES)
		expect((await create(u.token, { ...base, objects: big(20) })).status).toBe(201)
		const r = await create(u.token, { ...base, objects: big(21) })
		expect([r.status, r.json.error]).toMatchObject([413, { code: "quota_exceeded", quota_bytes: 500_000_000, used_bytes: 0, needed_bytes: 525_000_000 }])
	})

	it("counts what the user's other machines hold, but not the snapshot being replaced", async () => {
		const u = await newUser()
		await push(u.token, MACHINE_1, FILES)
		const held = Object.values(FILES).reduce((sum, [body]) => sum + body.length, 0)
		const base = await manifest(MACHINE_2, FILES)
		const r = await create(u.token, { ...base, objects: big(20) })
		expect([r.status, r.json.error]).toMatchObject([413, { code: "quota_exceeded", used_bytes: held, needed_bytes: 500_000_000 }])
		expect((await create(u.token, { ...base, machine: MACHINE_1, objects: big(20) })).status).toBe(201)
	})

	it("checks again at commit", async () => {
		const u = await newUser()
		const created = await create(u.token, await manifest(MACHINE_1, FILES))
		for (const path of created.json.missing) await put(u.token, created.json.upload_id, path, FILES[path]![0])
		// Another machine fills the quota between create and commit.
		await env.DB.prepare("INSERT INTO objects (user_id, machine, path, sha256, bytes) VALUES (?1, ?2, 'findings.json', ?3, 499999999)").bind(u.id, MACHINE_2, "f".repeat(64)).run()
		const r = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })
		expect([r.status, r.json.error.code]).toEqual([413, "quota_exceeded"])
		expect((await snapshotRows(u.id)).filter(row => row.machine === MACHINE_1)).toEqual([])
	})

	it("counts the open uploads of the user's other machines, at create and at commit", async () => {
		const u = await newUser()
		const base = await manifest(MACHINE_1, FILES)
		// 500 MB listed on one machine, not one byte sent yet.
		expect((await create(u.token, { ...base, objects: big(20) })).status).toBe(201)
		const other = await manifest(MACHINE_2, { "sessions/a.json": ["{}", "octo/alpha"] })
		const r = await create(u.token, other)
		expect([r.status, r.json.error]).toMatchObject([413, { code: "quota_exceeded", used_bytes: 500_000_000, needed_bytes: 2 }])
		// Restarting the first machine's own upload is not held against it.
		expect((await create(u.token, { ...base, objects: big(20) })).status).toBe(201)
		// Once that upload is gone the other machine fits again.
		await env.DB.prepare("UPDATE uploads SET expires_at = '2000-01-01T00:00:00Z' WHERE user_id = ?1").bind(u.id).run()
		const small = await create(u.token, other)
		expect(small.status).toBe(201)
		await put(u.token, small.json.upload_id, "sessions/a.json", "{}")

		// At commit: another machine opened a 500 MB upload after this one was created.
		await env.DB.prepare("INSERT INTO uploads VALUES ('up_big', ?1, ?2, 't', 1, 0, '[]', '2999-01-01T00:00:00Z')").bind(u.id, MACHINE_1).run()
		await env.DB.prepare("INSERT INTO upload_objects VALUES ('up_big', ?1, 'findings.json', ?2, 500000000, NULL, 0)").bind(u.id, "e".repeat(64)).run()
		const commit = await call("POST", `/v1/uploads/${small.json.upload_id}/commit`, { token: u.token })
		expect([commit.status, commit.json.error]).toMatchObject([413, { code: "quota_exceeded", used_bytes: 500_000_000 }])
		expect(await snapshotRows(u.id)).toEqual([])
	})

	it("counts machines with an open upload toward the limit of ten", async () => {
		const u = await newUser()
		const machine = (i: number) => MACHINE_1.replace(/1$/, i.toString(16))
		for (let i = 0; i < 10; i++) expect((await create(u.token, await manifest(machine(i), {}))).status).toBe(201)
		const r = await create(u.token, await manifest(machine(10), {}))
		expect([r.status, r.json.error.code]).toEqual([422, "too_many_machines"])
		expect(r.json.error.machines).toHaveLength(10)
	})

	it("deletes at once the files of a restarted upload that the new manifest does not list", async () => {
		const u = await newUser()
		const first: Files = { "sessions/a.json": ['{"id":"a"}', "octo/alpha"], "sessions/b.json": ['{"id":"b"}', "octo/alpha"] }
		const created = await create(u.token, await manifest(MACHINE_1, first))
		for (const path of Object.keys(first)) await put(u.token, created.json.upload_id, path, first[path]![0])
		expect(await storedKeys(`u/${u.id}/`)).toHaveLength(2)
		// Restart with other content: without the cleanup this loop would store files without end.
		const second = await create(u.token, await manifest(MACHINE_1, { "sessions/a.json": first["sessions/a.json"]!, "sessions/c.json": ['{"id":"c"}', "octo/alpha"] }))
		expect(second.json.missing).toEqual(["sessions/c.json"])
		expect(await storedKeys(`u/${u.id}/`)).toEqual([fileKey(u.id, await sha256(first["sessions/a.json"]![0]))])
	})

	it("refuses a manifest that understates the size of a file the server holds", async () => {
		const u = await newUser()
		const body = '{"id":"big","pad":"' + "x".repeat(1000) + '"}'
		await push(u.token, MACHINE_1, { "sessions/big.json": [body, "octo/alpha"] })
		const honest = await manifest(MACHINE_2, { "sessions/big.json": [body, "octo/alpha"] })
		const lying = (bytes: number) => ({ ...honest, objects: { "sessions/big.json": { ...(honest.objects["sessions/big.json"] as object), bytes } } })

		// Held in a snapshot.
		for (const bytes of [1, body.length - 1, body.length + 1]) {
			const r = await create(u.token, lying(bytes))
			expect([bytes, r.status, r.json.error.code]).toEqual([bytes, 422, "invalid_manifest"])
			expect(r.json.error.message).toContain("sessions/big.json")
		}
		// Held only by an open upload on another machine.
		const other = '{"id":"other","pad":"' + "y".repeat(1000) + '"}'
		const open = await create(u.token, await manifest(MACHINE_1, { "sessions/big.json": [body, "octo/alpha"], "sessions/other.json": [other, "octo/alpha"] }))
		await put(u.token, open.json.upload_id, "sessions/other.json", other)
		const viaUpload = await manifest(MACHINE_2, { "sessions/other.json": [other, "octo/alpha"] })
		const r = await create(u.token, { ...viaUpload, objects: { "sessions/other.json": { ...(viaUpload.objects["sessions/other.json"] as object), bytes: 1 } } })
		expect([r.status, r.json.error.code]).toEqual([422, "invalid_manifest"])

		// The honest manifest is taken, and counted at the true size.
		const ok = await create(u.token, honest)
		expect([ok.status, ok.json.missing]).toEqual([201, []])
		const done = await call("POST", `/v1/uploads/${ok.json.upload_id}/commit`, { token: u.token })
		expect(done.json.usage.bytes).toBe(2 * body.length)
	})

	it("does not let one path's file stand in for a path that lists another size", async () => {
		const u = await newUser()
		const body = '{"id":"twin"}'
		const m = await manifest(MACHINE_1, { "sessions/x.json": [body, "octo/alpha"], "sessions/y.json": [body, "octo/alpha"] })
		m.objects["sessions/y.json"] = { ...(m.objects["sessions/y.json"] as object), bytes: 1 }
		const created = await create(u.token, m)
		expect((await put(u.token, created.json.upload_id, "sessions/x.json", body)).status).toBe(204)
		const commit = await call("POST", `/v1/uploads/${created.json.upload_id}/commit`, { token: u.token })
		expect([commit.status, commit.json.error]).toMatchObject([409, { code: "incomplete", missing: ["sessions/y.json"] }])
		// The understated path cannot be completed either: one byte does not hash to that sha256.
		const short = await put(u.token, created.json.upload_id, "sessions/y.json", "{")
		expect([short.status, short.json.error.code]).toEqual([422, "hash_mismatch"])
		expect(await snapshotRows(u.id)).toEqual([])
	})
})
