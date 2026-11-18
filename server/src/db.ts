// The only file that may call `prepare`; test/isolation.test.ts checks that.
//
// A handler that acts for a signed-in user gets a UserDb and nothing else. Its user id is
// always parameter ?1, and a query that does not use ?1 throws. The functions below it are the
// few queries that run with no user: sign-in, before there is one, and the hourly sweep.

export interface UserDb {
	userId: string
	/** A statement with the user id bound to ?1 and `args` to ?2, ?3, ... */
	q(sql: string, ...args: unknown[]): D1PreparedStatement
	batch(statements: D1PreparedStatement[]): Promise<D1Result[]>
}

export function forUser(db: D1Database, userId: string): UserDb {
	return {
		userId,
		q(sql, ...args) {
			if (!/\?1(?!\d)/.test(sql)) throw new Error(`query without the user id: ${sql}`)
			return db.prepare(sql).bind(userId, ...args)
		},
		batch: statements => db.batch(statements),
	}
}

export interface DeviceCode {
	github_code: string
	interval: number
	next_poll_at: string
	expires_at: string
}

export const insertDeviceCode = (db: D1Database, hash: string, code: DeviceCode) =>
	db.prepare("INSERT INTO device_codes (hash, github_code, interval, next_poll_at, expires_at) VALUES (?1, ?2, ?3, ?4, ?5)")
		.bind(hash, code.github_code, code.interval, code.next_poll_at, code.expires_at).run()

export const getDeviceCode = (db: D1Database, hash: string) =>
	db.prepare("SELECT github_code, interval, next_poll_at, expires_at FROM device_codes WHERE hash = ?1").bind(hash).first<DeviceCode>()

export const setDevicePoll = (db: D1Database, hash: string, interval: number, nextPollAt: string) =>
	db.prepare("UPDATE device_codes SET interval = ?2, next_poll_at = ?3 WHERE hash = ?1").bind(hash, interval, nextPollAt).run()

export const deleteDeviceCode = (db: D1Database, hash: string) =>
	db.prepare("DELETE FROM device_codes WHERE hash = ?1").bind(hash).run()

/** Make the account on a first sign-in, refresh the login on a later one. Returns the user id. */
export async function upsertUser(db: D1Database, newId: string, githubId: number, login: string, now: string): Promise<string> {
	const row = await db.prepare(
		"INSERT INTO users (id, github_id, login, created_at) VALUES (?1, ?2, ?3, ?4) " +
		"ON CONFLICT (github_id) DO UPDATE SET login = excluded.login RETURNING id",
	).bind(newId, githubId, login, now).first<{ id: string }>()
	return row!.id
}

export const insertToken = (db: D1Database, hash: string, userId: string, expiresAt: string) =>
	db.prepare("INSERT INTO tokens (hash, user_id, expires_at) VALUES (?1, ?2, ?3)").bind(hash, userId, expiresAt).run()

/** The user a live token belongs to. */
export async function tokenUser(db: D1Database, hash: string, now: string): Promise<string | null> {
	const row = await db.prepare(
		"SELECT t.user_id FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.hash = ?1 AND t.expires_at > ?2",
	).bind(hash, now).first<{ user_id: string }>()
	return row?.user_id ?? null
}

/** Sweep: drop rows that have run out, for every user. */
export const deleteExpired = (db: D1Database, now: string) =>
	db.batch([
		db.prepare("DELETE FROM device_codes WHERE expires_at <= ?1").bind(now),
		db.prepare("DELETE FROM tokens WHERE expires_at <= ?1").bind(now),
		db.prepare("DELETE FROM upload_objects WHERE upload_id IN (SELECT id FROM uploads WHERE expires_at <= ?1)").bind(now),
		db.prepare("DELETE FROM uploads WHERE expires_at <= ?1").bind(now),
	])

/** Sweep: of these [user id, sha256] pairs, the ones a snapshot or a live upload still lists, as "user/sha". */
export async function referenced(db: D1Database, pairs: [string, string][], now: string): Promise<Set<string>> {
	const { results } = await db.prepare(
		"SELECT j.value->>0 AS u, j.value->>1 AS s FROM json_each(?1) j WHERE " +
		"EXISTS (SELECT 1 FROM objects o WHERE o.user_id = j.value->>0 AND o.sha256 = j.value->>1) OR " +
		"EXISTS (SELECT 1 FROM upload_objects x JOIN uploads up ON up.id = x.upload_id " +
		"WHERE x.user_id = j.value->>0 AND x.sha256 = j.value->>1 AND up.expires_at > ?2)",
	).bind(JSON.stringify(pairs), now).all<{ u: string; s: string }>()
	return new Set(results.map(r => `${r.u}/${r.s}`))
}
