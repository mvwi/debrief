export class HttpError extends Error {
	constructor(
		public status: number,
		public code: string,
		message: string,
		public extra: Record<string, unknown> = {},
		public headers: Record<string, string> = {},
	) {
		super(message)
	}
}

const HEADERS = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" }

export const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
	new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json; charset=utf-8", ...HEADERS, ...headers } })

export const noContent = () => new Response(null, { status: 204, headers: HEADERS })

/** UTC to the second, the one time format the API and the database use. */
export const iso = (ms: number) => new Date(ms).toISOString().slice(0, 19) + "Z"

export const hex = (buf: ArrayBuffer) => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("")

export const sha256 = async (text: string) => hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)))

/** `bytes` random bytes as URL-safe text. */
export function random(bytes: number): string {
	const raw = String.fromCharCode(...crypto.getRandomValues(new Uint8Array(bytes)))
	return btoa(raw).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "")
}

/** The request body as JSON, or null when it is longer than `cap` bytes. Reads no more than `cap`. */
export async function readJson(request: Request, cap: number): Promise<unknown> {
	if (Number(request.headers.get("Content-Length") ?? 0) > cap) return null
	const chunks: Uint8Array[] = []
	let size = 0
	if (request.body) {
		for await (const chunk of request.body) {
			size += chunk.byteLength
			if (size > cap) return null
			chunks.push(chunk)
		}
	}
	try {
		return JSON.parse(await new Blob(chunks).text())
	} catch {
		throw new HttpError(400, "bad_request", "The body is not JSON.")
	}
}
