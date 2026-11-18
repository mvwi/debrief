import type { UserDb } from "./db"
import { hex, HttpError, iso, json, noContent, random, readJson, sha256 } from "./http"
import { listProjects, QUOTA_BYTES, usage } from "./projects"
import { collect, fileKey, inLiveUpload, UPLOAD_LIFE_MS } from "./storage"

export const FORMAT = 1
export const MAX_MANIFEST_BYTES = 1_000_000
export const MAX_OBJECTS = 5_000
export const MAX_PROJECTS = 200
export const MAX_OBJECT_BYTES = 25_000_000
export const MAX_MACHINES = 10

const OBJECT_PATH = /^(findings\.json|github\.json|sessions\/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.json)$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

interface Project { key: string; id: string; name: string; sessions: number; turns: number; analysed_at: string | null }
interface Entry { sha256: string; bytes: number; project: string | null }
interface Manifest { machine: string; tool: string; redaction: number; unpicked: number; projects: Project[]; objects: Record<string, Entry> }

const count = (v: unknown): v is number => Number.isInteger(v) && (v as number) >= 0
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v)

/** The parts of a manifest the server keeps, checked. Unknown fields, `sent` among them, are dropped. */
async function validate(userId: string, m: unknown): Promise<Manifest> {
	const bad = (what: string) => new HttpError(422, "invalid_manifest", `manifest: ${what}`)
	if (!isObject(m)) throw bad("it must be an object")
	if (m.format !== FORMAT) {
		throw new HttpError(422, "unsupported_format", `This server reads upload format ${FORMAT}.`, { supported: [FORMAT] })
	}
	if (typeof m.tool !== "string" || m.tool.length > 100) throw bad("tool must be a string of at most 100 characters")
	if (typeof m.machine !== "string" || !UUID.test(m.machine)) throw bad("machine must be a lower-case UUID")
	if (!count(m.redaction)) throw bad("redaction must be an integer, 0 or more")
	if (!count(m.unpicked)) throw bad("unpicked must be an integer, 0 or more")

	if (!Array.isArray(m.projects) || m.projects.length > MAX_PROJECTS) throw bad(`projects must be an array of at most ${MAX_PROJECTS}`)
	const projects: Project[] = []
	const keys = new Set<string>()
	for (const p of m.projects as unknown[]) {
		if (!isObject(p)) throw bad("each project must be an object")
		const { key, name, sessions, turns, analysed_at = null } = p
		if (typeof key !== "string" || !/^[A-Za-z0-9_.:/-]{1,200}$/.test(key)) throw bad("a project key must be 1 to 200 characters of [A-Za-z0-9_.:/-]")
		if (keys.has(key)) throw bad(`project key ${key} appears twice`)
		if (typeof name !== "string" || name.length > 200) throw bad(`projects[${key}].name must be a string of at most 200 characters`)
		if (!count(sessions) || !count(turns)) throw bad(`projects[${key}].sessions and .turns must be integers, 0 or more`)
		if (analysed_at !== null && (typeof analysed_at !== "string" || analysed_at.length > 40)) throw bad(`projects[${key}].analysed_at must be a string or null`)
		keys.add(key)
		// Stable across pushes, and no use to anyone who is not this user.
		const id = `p_${(await sha256(`${userId}:${m.machine}:${key}`)).slice(0, 16)}`
		projects.push({ key, id, name, sessions, turns, analysed_at })
	}

	if (!isObject(m.objects)) throw bad("objects must be an object")
	const paths = Object.keys(m.objects)
	if (paths.length > MAX_OBJECTS) throw bad(`objects must hold at most ${MAX_OBJECTS}`)
	if (paths.length && !projects.length) throw bad("objects need at least one project")
	const objects: Record<string, Entry> = {}
	for (const path of paths) {
		const o = m.objects[path]
		if (path.length > 200 || !OBJECT_PATH.test(path)) throw bad("an object path must be findings.json, github.json or sessions/<name>.json")
		if (!isObject(o)) throw bad(`objects[${path}] must be an object`)
		if (typeof o.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(o.sha256)) throw bad(`objects[${path}].sha256 must be 64 lower-case hex digits`)
		if (!count(o.bytes) || o.bytes < 1 || o.bytes > MAX_OBJECT_BYTES) throw bad(`objects[${path}].bytes must be an integer from 1 to ${MAX_OBJECT_BYTES}`)
		const session = path.startsWith("sessions/")
		if (session && (typeof o.project !== "string" || !keys.has(o.project))) throw bad(`objects[${path}].project must be the key of one of the projects`)
		objects[path] = { sha256: o.sha256, bytes: o.bytes, project: session ? (o.project as string) : null }
	}
	return { machine: m.machine, tool: m.tool, redaction: m.redaction, unpicked: m.unpicked, projects, objects }
}

const overQuota = (used: number, needed: number) =>
	new HttpError(413, "quota_exceeded", `This push needs ${needed} bytes beside the ${used} your other machines hold or are sending; your quota is ${QUOTA_BYTES}.`, {
		quota_bytes: QUOTA_BYTES, used_bytes: used, needed_bytes: needed,
	})

/** SQL for the machines that count toward the limit: those with a snapshot or an open upload. ?2 is the time. */
const MACHINES_SQL = "SELECT machine FROM snapshots WHERE user_id = ?1 UNION SELECT machine FROM uploads WHERE user_id = ?1 AND expires_at > ?2"

/**
 * SQL for the bytes the user's other machines hold in snapshots, plus the bytes their open
 * uploads list. An open upload counts in full from the moment it is created: a file can only be
 * stored for a path of an open upload, at the size listed, so this sum bounds what PUT can store.
 */
const usedElsewhereSql = (machine: string, now: string) =>
	`((SELECT COALESCE(SUM(bytes), 0) FROM objects WHERE user_id = ?1 AND machine != ${machine}) + ` +
	`(SELECT COALESCE(SUM(x.bytes), 0) FROM upload_objects x JOIN uploads up ON up.id = x.upload_id ` +
	`WHERE x.user_id = ?1 AND up.machine != ${machine} AND up.expires_at > ${now}))`

async function usedElsewhere(udb: UserDb, machine: string, now: string): Promise<number> {
	return (await udb.q(`SELECT ${usedElsewhereSql("?2", "?3")} AS n`, machine, now).first<{ n: number }>())!.n
}

/** POST /v1/uploads */
export async function createUpload(env: Env, udb: UserDb, request: Request): Promise<Response> {
	const confirms = request.headers.get("X-Debrief-Rights-Confirmed") === "1"
	const user = await udb.q("SELECT rights_confirmed_at FROM users WHERE id = ?1").first<{ rights_confirmed_at: string | null }>()
	if (!user?.rights_confirmed_at && !confirms) {
		throw new HttpError(403, "confirmation_required", "Confirm that you have the right to share what you upload, then send X-Debrief-Rights-Confirmed: 1.")
	}
	const body = await readJson(request, MAX_MANIFEST_BYTES)
	if (body === null) throw new HttpError(413, "manifest_too_large", `The manifest is over ${MAX_MANIFEST_BYTES} bytes.`)
	const m = await validate(udb.userId, body)

	const now = Date.now(), id = `up_${random(16)}`, expiresAt = iso(now + UPLOAD_LIFE_MS)
	const needed = Object.values(m.objects).reduce((sum, o) => sum + o.bytes, 0)
	// Throws the answer for a manifest that is over a limit. The batch below repeats both tests.
	const checkLimits = async () => {
		const { results: known } = await udb.q(`${MACHINES_SQL} ORDER BY machine`, iso(now)).all<{ machine: string }>()
		const machines = known.map(r => r.machine)
		if (machines.length >= MAX_MACHINES && !machines.includes(m.machine)) {
			throw new HttpError(422, "too_many_machines", `You already push from ${MAX_MACHINES} machines. Delete the projects of one to free it.`, { machines })
		}
		const used = await usedElsewhere(udb, m.machine, iso(now))
		if (used + needed > QUOTA_BYTES) throw overQuota(used, needed)
	}
	await checkLimits()
	const objectsJson = JSON.stringify(m.objects)
	// The quota is summed from these sizes, so a size must be one the server has checked: when
	// it already holds the bytes of a hash, the manifest has to state their true length.
	const lie = await udb.q(
		"SELECT j.key AS path FROM json_each(?2) j WHERE " +
		"EXISTS (SELECT 1 FROM objects o WHERE o.user_id = ?1 AND o.sha256 = j.value->>'sha256' AND o.bytes != j.value->>'bytes') OR " +
		"EXISTS (SELECT 1 FROM upload_objects x JOIN uploads up ON up.id = x.upload_id " +
		"WHERE x.user_id = ?1 AND x.sha256 = j.value->>'sha256' AND x.received = 1 AND x.bytes != j.value->>'bytes' AND up.expires_at > ?3) LIMIT 1",
		objectsJson, iso(now),
	).first<{ path: string }>()
	if (lie) throw new HttpError(422, "invalid_manifest", `manifest: objects[${lie.path}].bytes is not the size of the bytes with that sha256`)

	// Files that arrived for the upload this one replaces. Those the new manifest does not list
	// are deleted below instead of waiting for the sweep, so restarting uploads stores nothing extra.
	const { results: replaced } = await udb.q(
		"SELECT DISTINCT x.sha256 FROM upload_objects x JOIN uploads up ON up.id = x.upload_id WHERE x.user_id = ?1 AND up.machine = ?2 AND x.received = 1", m.machine,
	).all<{ sha256: string }>()
	try {
		await udb.batch([
			// The limits again, inside the transaction that makes the upload: creates that run at
			// the same time on other machine ids each passed `checkLimits` before any of them was
			// stored. D1 runs batches one after another, so here each sees the ones before it.
			udb.q(
				"INSERT INTO guard (ok) SELECT 0 WHERE NOT (" +
				`${usedElsewhereSql("?2", "?3")} + ?4 <= ?5 ` +
				`AND (EXISTS (SELECT 1 FROM (${MACHINES_SQL.replace("?2", "?3")}) WHERE machine = ?2) ` +
				`OR (SELECT COUNT(*) FROM (${MACHINES_SQL.replace("?2", "?3")})) < ?6))`,
				m.machine, iso(now), needed, QUOTA_BYTES, MAX_MACHINES,
			),
			udb.q("UPDATE users SET rights_confirmed_at = ?2 WHERE id = ?1 AND rights_confirmed_at IS NULL AND ?3", iso(now), confirms ? 1 : 0),
			udb.q(
				"INSERT INTO uploads (id, user_id, machine, tool, redaction, unpicked, projects, expires_at) VALUES (?2, ?1, ?3, ?4, ?5, ?6, ?7, ?8)",
				id, m.machine, m.tool, m.redaction, m.unpicked, JSON.stringify(m.projects), expiresAt,
			),
			// A file is held when a snapshot of this user lists its hash at this size, or it arrived
			// at this size for an upload that is still open. That includes the upload the next two
			// statements cancel, so a retry does not send a file twice.
			udb.q(
				"INSERT INTO upload_objects (upload_id, user_id, path, sha256, bytes, project, received) " +
				"SELECT ?2, ?1, j.key, j.value->>'sha256', j.value->>'bytes', j.value->>'project', " +
				"EXISTS (SELECT 1 FROM objects o WHERE o.user_id = ?1 AND o.sha256 = j.value->>'sha256' AND o.bytes = j.value->>'bytes') OR " +
				"EXISTS (SELECT 1 FROM upload_objects x JOIN uploads up ON up.id = x.upload_id " +
				"WHERE x.user_id = ?1 AND x.sha256 = j.value->>'sha256' AND x.bytes = j.value->>'bytes' AND x.received = 1 AND up.expires_at > ?4) " +
				"FROM json_each(?3) j",
				id, objectsJson, iso(now),
			),
			udb.q("DELETE FROM upload_objects WHERE user_id = ?1 AND upload_id IN (SELECT id FROM uploads WHERE user_id = ?1 AND machine = ?2 AND id != ?3)", m.machine, id),
			udb.q("DELETE FROM uploads WHERE user_id = ?1 AND machine = ?2 AND id != ?3", m.machine, id),
		])
	} catch (e) {
		if (String(e).includes("CHECK constraint failed")) await checkLimits()
		throw e
	}
	await collect(env, udb, replaced.map(r => r.sha256)).catch(e => console.error("collect after create failed", String(e)))
	return json({ upload_id: id, missing: await missing(udb, id), expires_at: expiresAt }, 201)
}

async function missing(udb: UserDb, uploadId: string): Promise<string[]> {
	const { results } = await udb.q("SELECT path FROM upload_objects WHERE user_id = ?1 AND upload_id = ?2 AND received = 0 ORDER BY path", uploadId).all<{ path: string }>()
	return results.map(r => r.path)
}

const noUpload = () => new HttpError(404, "not_found", "No such upload. Start again with POST /v1/uploads.")

/** PUT /v1/uploads/{id}/files/{path}. The body goes to storage as a stream; it is never parsed. */
export async function putFile(env: Env, udb: UserDb, request: Request, uploadId: string, path: string): Promise<Response> {
	const row = await udb.q(
		"SELECT o.sha256, o.bytes, o.received FROM upload_objects o JOIN uploads up ON up.id = o.upload_id " +
		"WHERE o.user_id = ?1 AND o.upload_id = ?2 AND o.path = ?3 AND up.expires_at > ?4",
		uploadId, path, iso(Date.now()),
	).first<{ sha256: string; bytes: number; received: number }>()
	if (!row) throw noUpload()
	const sizeMismatch = () => new HttpError(422, "size_mismatch", `The manifest says ${path} is ${row.bytes} bytes.`)
	const length = request.headers.get("Content-Length")
	if (length === null) throw new HttpError(411, "length_required", "Send Content-Length.")
	if (Number(length) !== row.bytes || !request.body) throw sizeMismatch()
	// The server already holds bytes that passed this check: nothing to store.
	if (row.received) return noContent()

	// Count and hash the bytes on their way through, to say what went wrong if the store fails.
	// The checks themselves are FixedLengthStream (length) and R2's own sha256 test; both fail
	// the put, so a bad file is never stored.
	const digest = new crypto.DigestStream("SHA-256"), hasher = digest.getWriter()
	digest.digest.catch(() => {}) // read below only when it is wanted
	let seen = 0
	const tap = new TransformStream<Uint8Array, Uint8Array>({
		async transform(chunk, controller) {
			seen += chunk.byteLength
			await hasher.write(chunk)
			controller.enqueue(chunk)
		},
	})
	try {
		const fixed = new FixedLengthStream(row.bytes)
		// A failed pipe also fails the put, which is where it is handled.
		void request.body.pipeThrough(tap).pipeTo(fixed.writable).catch(() => {})
		await env.BUCKET.put(fileKey(udb.userId, row.sha256), fixed.readable, { sha256: row.sha256 })
	} catch (e) {
		if (seen !== row.bytes) {
			void hasher.abort().catch(() => {})
			throw sizeMismatch()
		}
		await hasher.close()
		if (hex(await digest.digest) !== row.sha256) throw new HttpError(422, "hash_mismatch", `The bytes of ${path} do not match the manifest's sha256.`)
		throw e
	}
	// Only rows that list the size just checked: a row with another size has to fail its own PUT.
	await udb.q("UPDATE upload_objects SET received = 1 WHERE user_id = ?1 AND sha256 = ?2 AND bytes = ?3", row.sha256, row.bytes).run()
	return noContent()
}

/** POST /v1/uploads/{id}/commit */
export async function commitUpload(env: Env, udb: UserDb, uploadId: string): Promise<Response> {
	const now = iso(Date.now())
	// Throws the answer for an upload that cannot be committed.
	const check = async () => {
		const up = await udb.q(
			"SELECT machine, tool, redaction, unpicked, projects FROM uploads WHERE user_id = ?1 AND id = ?2 AND expires_at > ?3", uploadId, now,
		).first<{ machine: string; tool: string; redaction: number; unpicked: number; projects: string }>()
		if (!up) throw noUpload()
		const lacks = await missing(udb, uploadId)
		if (lacks.length) throw new HttpError(409, "incomplete", `${lacks.length} listed objects have not arrived.`, { missing: lacks })
		const used = await usedElsewhere(udb, up.machine, now)
		const needed = (await udb.q("SELECT COALESCE(SUM(bytes), 0) AS n FROM upload_objects WHERE user_id = ?1 AND upload_id = ?2", uploadId).first<{ n: number }>())!.n
		if (used + needed > QUOTA_BYTES) throw overQuota(used, needed)
		return up
	}
	const up = await check()
	const m = up.machine
	const { results: old } = await udb.q("SELECT DISTINCT sha256 FROM objects WHERE user_id = ?1 AND machine = ?2", m).all<{ sha256: string }>()
	const removed = (await udb.q(
		"SELECT COUNT(*) AS n FROM objects WHERE user_id = ?1 AND machine = ?2 " +
		"AND path NOT IN (SELECT path FROM upload_objects WHERE user_id = ?1 AND upload_id = ?3)", m, uploadId,
	).first<{ n: number }>())!.n

	try {
		// One transaction. The first statement repeats `check` inside it: a row that breaks the
		// guard table's CHECK rolls everything back, so a request that raced this one cannot
		// leave half a snapshot.
		await udb.batch([
			udb.q(
				"INSERT INTO guard (ok) SELECT 0 WHERE NOT (" +
				"EXISTS (SELECT 1 FROM uploads WHERE user_id = ?1 AND id = ?2 AND expires_at > ?3) " +
				"AND NOT EXISTS (SELECT 1 FROM upload_objects WHERE user_id = ?1 AND upload_id = ?2 AND received = 0) " +
				`AND ${usedElsewhereSql("?4", "?3")} ` +
				"+ (SELECT COALESCE(SUM(bytes), 0) FROM upload_objects WHERE user_id = ?1 AND upload_id = ?2) <= ?5)",
				uploadId, now, m, QUOTA_BYTES,
			),
			udb.q("DELETE FROM objects WHERE user_id = ?1 AND machine = ?2", m),
			udb.q(
				"INSERT INTO objects (user_id, machine, path, sha256, bytes, project) " +
				"SELECT ?1, ?2, path, sha256, bytes, project FROM upload_objects WHERE user_id = ?1 AND upload_id = ?3", m, uploadId,
			),
			udb.q("DELETE FROM projects WHERE user_id = ?1 AND machine = ?2", m),
			udb.q(
				"INSERT INTO projects (user_id, machine, key, id, name, sessions, turns, analysed_at) " +
				"SELECT ?1, ?2, j.value->>'key', j.value->>'id', j.value->>'name', j.value->>'sessions', j.value->>'turns', j.value->>'analysed_at' " +
				"FROM json_each(?3) j", m, up.projects,
			),
			// A machine with no project has no snapshot and does not count toward the machine limit.
			udb.q("DELETE FROM snapshots WHERE user_id = ?1 AND machine = ?2", m),
			udb.q(
				"INSERT INTO snapshots (user_id, machine, tool, redaction, unpicked, committed_at) " +
				"SELECT ?1, ?2, ?3, ?4, ?5, ?6 WHERE json_array_length(?7) > 0", m, up.tool, up.redaction, up.unpicked, now, up.projects,
			),
			udb.q("DELETE FROM upload_objects WHERE user_id = ?1 AND upload_id = ?2", uploadId),
			udb.q("DELETE FROM uploads WHERE user_id = ?1 AND id = ?2", uploadId),
		])
	} catch (e) {
		if (String(e).includes("CHECK constraint failed")) await check()
		throw e
	}

	// The commit stands whether or not this works; the hourly sweep takes what it leaves.
	await collect(env, udb, old.map(r => r.sha256)).catch(e => console.error("collect after commit failed", String(e)))

	const total = await udb.q("SELECT COUNT(*) AS objects, COALESCE(SUM(bytes), 0) AS bytes FROM objects WHERE user_id = ?1 AND machine = ?2", m).first<{ objects: number; bytes: number }>()
	return json({ machine: m, committed_at: now, ...total, removed, projects: await listProjects(udb, m), usage: await usage(udb) })
}
