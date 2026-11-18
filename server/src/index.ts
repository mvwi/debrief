import { authenticate, logout, pollToken, startDevice } from "./auth"
import type { UserDb } from "./db"
import { HttpError, json } from "./http"
import { deleteAccount, deleteProject, getProjects } from "./projects"
import { sweep } from "./storage"
import { commitUpload, createUpload, putFile } from "./uploads"

interface Call { env: Env; request: Request; udb: UserDb; tokenHash: string; args: string[] }
type Route = [method: string, path: RegExp, run: (c: Call) => Promise<Response>]

const ID = "([A-Za-z0-9_-]{1,200})"

/** Every route that needs a token. The two sign-in routes are handled before these. */
const ROUTES: Route[] = [
	["DELETE", /^\/v1\/auth\/token$/, c => logout(c.udb, c.tokenHash)],
	["POST", /^\/v1\/uploads$/, c => createUpload(c.env, c.udb, c.request)],
	["PUT", new RegExp(`^/v1/uploads/${ID}/files/(.+)$`), c => putFile(c.env, c.udb, c.request, c.args[0]!, c.args[1]!)],
	["POST", new RegExp(`^/v1/uploads/${ID}/commit$`), c => commitUpload(c.env, c.udb, c.args[0]!)],
	["GET", /^\/v1\/projects$/, c => getProjects(c.udb)],
	["DELETE", new RegExp(`^/v1/projects/${ID}$`), c => deleteProject(c.env, c.udb, c.args[0]!)],
	["DELETE", /^\/v1\/me$/, c => deleteAccount(c.env, c.udb)],
]

async function limit(limiter: RateLimit, key: string): Promise<void> {
	if (!(await limiter.limit({ key })).success) {
		throw new HttpError(429, "rate_limited", "Too many requests. Wait and try again.", {}, { "Retry-After": "60" })
	}
}

async function handle(request: Request, env: Env): Promise<Response> {
	const { pathname } = new URL(request.url)
	const { method } = request
	const ip = request.headers.get("CF-Connecting-IP") ?? "unknown"

	const signIn = pathname === "/v1/auth/device" || pathname === "/v1/auth/token"
	if (signIn && method === "POST") {
		if (pathname === "/v1/auth/device") {
			await limit(env.AUTH_LIMITER, ip)
			return startDevice(env)
		}
		await limit(env.API_LIMITER, `ip:${ip}`)
		return pollToken(env, request)
	}

	const matches = ROUTES.flatMap(([m, path, run]) => {
		const hit = path.exec(pathname)
		return hit ? [{ m, run, args: hit.slice(1).map(decodeURIComponent) }] : []
	})
	const route = matches.find(r => r.m === method)
	if (!route) {
		if (matches.length || signIn) throw new HttpError(405, "method_not_allowed", `${method} is not allowed here.`)
		throw new HttpError(404, "not_found", "No such route.")
	}
	const { udb, tokenHash } = await authenticate(env, request)
	await limit(env.API_LIMITER, udb.userId)
	return route.run({ env, request, udb, tokenHash, args: route.args })
}

export default {
	async fetch(request, env): Promise<Response> {
		try {
			return await handle(request, env)
		} catch (e) {
			if (e instanceof HttpError) return json({ error: { code: e.code, message: e.message, ...e.extra } }, e.status, e.headers)
			if (e instanceof URIError) return json({ error: { code: "not_found", message: "No such route." } }, 404)
			// The error only: a request may hold a token, and a body holds transcripts.
			console.error("unhandled", String(e))
			return json({ error: { code: "internal", message: "Something went wrong on the server. Retrying is safe." } }, 500)
		}
	},
	async scheduled(_controller, env): Promise<void> {
		await sweep(env)
	},
} satisfies ExportedHandler<Env>
