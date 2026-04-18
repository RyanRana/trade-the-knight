import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BASE = process.env.EXCHANGE_BASE_URL?.replace(/\/$/, "") ?? "";
const KEY = process.env.EXCHANGE_API_KEY ?? "";

export async function GET(
  req: NextRequest,
  { params }: { params: { path: string[] } },
) {
  if (!BASE) {
    return NextResponse.json(
      { error: "EXCHANGE_BASE_URL is not set. Configure .env.local." },
      { status: 500 },
    );
  }
  if (!KEY) {
    return NextResponse.json(
      { error: "EXCHANGE_API_KEY is not set. Configure .env.local." },
      { status: 500 },
    );
  }

  const search = req.nextUrl.search ?? "";
  const target = `${BASE}/api/exchange/public/${params.path.join("/")}${search}`;

  try {
    const r = await fetch(target, {
      headers: { "X-API-Key": KEY, Accept: "application/json" },
      cache: "no-store",
    });

    const body = await r.text();
    const contentType = r.headers.get("content-type") ?? "application/json";

    return new NextResponse(body, {
      status: r.status,
      headers: {
        "content-type": contentType,
        "x-upstream-url": target,
      },
    });
  } catch (err) {
    return NextResponse.json(
      {
        error: "Upstream fetch failed",
        target,
        detail: err instanceof Error ? err.message : String(err),
      },
      { status: 502 },
    );
  }
}
