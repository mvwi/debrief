import { deleteExpired, referenced, type UserDb } from "./db"
import { iso } from "./http"

export const UPLOAD_LIFE_MS = 3_600_000

/** Where a user's file lives. Named by its hash, so the same bytes are stored once per user. */
export const fileKey = (userId: string, sha: string) => `u/${userId}/b/${sha}`

/** A subquery: is this sha256 listed by a live upload of user ?1. `nowParam` is the parameter holding the time. */
export const inLiveUpload = (sha: string, nowParam: string) =>
	`EXISTS (SELECT 1 FROM upload_objects x JOIN uploads up ON up.id = x.upload_id ` +
	`WHERE x.user_id = ?1 AND x.sha256 = ${sha} AND up.expires_at > ${nowParam})`

async function deleteKeys(bucket: R2Bucket, keys: string[]): Promise<void> {
	for (let i = 0; i < keys.length; i += 1000) await bucket.delete(keys.slice(i, i + 1000))
}

/**
 * Delete the files among `shas` that no snapshot and no live upload of the user lists any more.
 * Call it after the rows that listed them are gone.
 *
 * shortcut: a file re-uploaded between the check and the delete is lost while its row stays;
 * that takes the same user pushing the same bytes within milliseconds of deleting them.
 * Upgrade to a per-user lock (a Durable Object) if it ever shows up.
 */
export async function collect(env: Env, udb: UserDb, shas: string[]): Promise<void> {
	if (!shas.length) return
	const { results } = await udb.q(
		"SELECT j.value AS sha FROM json_each(?2) j WHERE " +
		"NOT EXISTS (SELECT 1 FROM objects o WHERE o.user_id = ?1 AND o.sha256 = j.value) " +
		`AND NOT ${inLiveUpload("j.value", "?3")}`,
		JSON.stringify(shas), iso(Date.now()),
	).all<{ sha: string }>()
	await deleteKeys(env.BUCKET, results.map(r => fileKey(udb.userId, r.sha)))
}

/** Delete every file of one user. */
export async function deleteAllFiles(env: Env, userId: string): Promise<void> {
	let cursor: string | undefined
	do {
		const page = await env.BUCKET.list({ prefix: `u/${userId}/`, cursor })
		await deleteKeys(env.BUCKET, page.objects.map(o => o.key))
		cursor = page.truncated ? page.cursor : undefined
	} while (cursor)
}

/**
 * The hourly sweep: drop rows that have run out, then delete every stored file older than
 * `graceMs` that nothing lists. That covers files of abandoned uploads and whatever a failed
 * commit or delete left behind. The grace keeps a file that arrived for an upload still open.
 *
 * shortcut: this lists the whole bucket each hour, 1,000 keys a call. Fine for thousands of
 * users; past that, sweep a slice of the key space per run.
 */
export async function sweep(env: Env, graceMs = UPLOAD_LIFE_MS): Promise<number> {
	const now = Date.now()
	await deleteExpired(env.DB, iso(now))
	let cursor: string | undefined, deleted = 0
	do {
		const page = await env.BUCKET.list({ cursor })
		const old = page.objects.filter(o => o.uploaded.getTime() < now - graceMs)
		const pairs = old.flatMap(o => {
			const m = /^u\/([^/]+)\/b\/([0-9a-f]{64})$/.exec(o.key)
			return m ? [[m[1]!, m[2]!] as [string, string]] : []
		})
		const live = await referenced(env.DB, pairs, iso(now))
		const dead = pairs.filter(([u, s]) => !live.has(`${u}/${s}`)).map(([u, s]) => fileKey(u, s))
		await deleteKeys(env.BUCKET, dead)
		deleted += dead.length
		cursor = page.truncated ? page.cursor : undefined
	} while (cursor)
	return deleted
}
