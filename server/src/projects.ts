import type { UserDb } from "./db"
import { HttpError, json, noContent } from "./http"
import { collect, deleteAllFiles } from "./storage"

export const QUOTA_BYTES = 500_000_000
/** Objects that belong to a machine, not to one of its projects. */
const SHARED = "('findings.json', 'github.json')"

export async function listProjects(udb: UserDb, machine: string | null = null) {
	const { results } = await udb.q(
		"SELECT p.id, p.machine, p.key, p.name, p.sessions, p.turns, p.analysed_at, " +
		"(SELECT COALESCE(SUM(o.bytes), 0) FROM objects o WHERE o.user_id = ?1 AND o.machine = p.machine AND o.project = p.key) AS bytes, " +
		"s.committed_at AS pushed_at " +
		"FROM projects p JOIN snapshots s ON s.user_id = p.user_id AND s.machine = p.machine " +
		"WHERE p.user_id = ?1 AND (?2 IS NULL OR p.machine = ?2) ORDER BY s.committed_at DESC, p.key",
		machine,
	).all()
	return results
}

export async function usage(udb: UserDb) {
	const row = await udb.q("SELECT COALESCE(SUM(bytes), 0) AS bytes FROM objects WHERE user_id = ?1").first<{ bytes: number }>()
	return { bytes: row!.bytes, quota_bytes: QUOTA_BYTES }
}

/** GET /v1/projects */
export const getProjects = async (udb: UserDb) => json({ projects: await listProjects(udb), usage: await usage(udb) })

/** DELETE /v1/projects/{id} */
export async function deleteProject(env: Env, udb: UserDb, id: string): Promise<Response> {
	const p = await udb.q("SELECT machine, key FROM projects WHERE user_id = ?1 AND id = ?2", id).first<{ machine: string; key: string }>()
	if (!p) throw new HttpError(404, "not_found", "No such project.")
	// The machine's findings.json and github.json may quote this project and cannot be cut, so they go too.
	const mine = `user_id = ?1 AND machine = ?2 AND (project = ?3 OR path IN ${SHARED})`
	// Files to look at afterwards: the project's own, and those of the open uploads this cancels.
	const { results } = await udb.q(
		`SELECT sha256 FROM objects WHERE ${mine} UNION SELECT sha256 FROM upload_objects WHERE user_id = ?1 AND received = 1`, p.machine, p.key,
	).all<{ sha256: string }>()
	await udb.batch([
		// An open upload may count on a file this is about to delete.
		udb.q("DELETE FROM upload_objects WHERE user_id = ?1"),
		udb.q("DELETE FROM uploads WHERE user_id = ?1"),
		udb.q(`DELETE FROM objects WHERE ${mine}`, p.machine, p.key),
		udb.q("DELETE FROM projects WHERE user_id = ?1 AND machine = ?2 AND key = ?3", p.machine, p.key),
		udb.q(
			"DELETE FROM snapshots WHERE user_id = ?1 AND machine = ?2 " +
			"AND NOT EXISTS (SELECT 1 FROM projects WHERE user_id = ?1 AND machine = ?2)",
			p.machine,
		),
	])
	await collect(env, udb, results.map(r => r.sha256))
	return noContent()
}

/** DELETE /v1/me */
export async function deleteAccount(env: Env, udb: UserDb): Promise<Response> {
	// Files first: if that fails the token still works and the user can try again.
	await deleteAllFiles(env, udb.userId)
	await udb.batch([
		...["upload_objects", "uploads", "objects", "projects", "snapshots", "tokens"].map(t => udb.q(`DELETE FROM ${t} WHERE user_id = ?1`)),
		udb.q("DELETE FROM users WHERE id = ?1"),
	])
	return noContent()
}
