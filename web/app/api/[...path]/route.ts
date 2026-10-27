import type { NextRequest } from "next/server";

import { USER_COOKIE, toHandle } from "@/lib/users";

// Browser → Next.js → FastAPI. The demo identity comes from a cookie because
// EventSource cannot send custom headers (D80).
const FORWARD_REQUEST = ["accept", "content-type", "idempotency-key", "last-event-id"];
const FORWARD_RESPONSE = ["content-type", "content-disposition", "cache-control"];

async function proxy(request: NextRequest, ctx: RouteContext<"/api/[...path]">) {
  const { path } = await ctx.params;
  const base = process.env.CDA_API_URL ?? "http://127.0.0.1:8000";
  const target = new URL(path.map(encodeURIComponent).join("/"), base.replace(/\/?$/, "/"));
  target.search = request.nextUrl.search;

  const headers = new Headers({
    "X-Demo-User": toHandle(request.cookies.get(USER_COOKIE)?.value),
  });
  for (const name of FORWARD_REQUEST) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
      // Required by Node's fetch when streaming a request body.
      ...(hasBody ? { duplex: "half" } : {}),
      cache: "no-store",
      redirect: "manual",
      signal: request.signal,
    } as RequestInit);
  } catch {
    return Response.json(
      { error: "api_unreachable", message: "Couldn't reach the server." },
      { status: 502 },
    );
  }

  const out = new Headers();
  for (const name of FORWARD_RESPONSE) {
    const value = upstream.headers.get(name);
    if (value !== null) out.set(name, value);
  }
  if (out.get("content-type")?.startsWith("text/event-stream")) {
    out.set("cache-control", "no-cache, no-transform");
    out.set("x-accel-buffering", "no");
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
