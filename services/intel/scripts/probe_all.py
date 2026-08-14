"""Probe every source in the database for reachability. Writes a verdict per slug."""
import asyncio, json, os, httpx, asyncpg

CONCURRENCY = 12
ATTEMPTS = 2
TIMEOUT = 45

async def probe(sem, slug, url):
    async with sem:
        for i in range(ATTEMPTS):
            try:
                async with httpx.AsyncClient(proxy="socks5://tor:9050", timeout=TIMEOUT,
                        verify=False, follow_redirects=True,
                        headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"}) as c:
                    r = await c.get(url)
                return {"slug": slug, "alive": True, "status": r.status_code, "bytes": len(r.text)}
            except Exception as e:
                err = type(e).__name__
                if i < ATTEMPTS - 1:
                    await asyncio.sleep(2)
        return {"slug": slug, "alive": False, "error": err}

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    rows = await conn.fetch("select slug, base_url from sources where base_url not like '%example.onion%' order by slug")
    await conn.close()
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(probe(sem, r["slug"], r["base_url"]) for r in rows))
    alive = [r for r in results if r["alive"]]
    dead  = [r for r in results if not r["alive"]]
    print(f"PROBED {len(results)}  ALIVE {len(alive)}  DEAD {len(dead)}")
    for r in sorted(alive, key=lambda x: x["slug"]):
        print(f"ALIVE  {r['slug']:22} HTTP {r['status']}  {r['bytes']} bytes")
    with open("/probe/probe_results.json", "w") as f:
        json.dump(results, f, indent=1)

asyncio.run(main())
