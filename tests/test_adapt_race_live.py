"""Regression test for the bugs the audit found. Runs on the device."""
import asyncio, json, time, aiohttp

CK = open('/tmp/mbck').read().strip()
H  = {"Cookie": CK}
B  = "http://127.0.0.1:8080"

async def main():
    async with aiohttp.ClientSession(headers=H) as s:
        async def post(path, body):
            async with s.post(B+path, json=body) as r:
                return r.status, await r.json()
        async def get(path):
            async with s.get(B+path) as r:
                return await r.json()

        # reset to a known ceiling
        await post("/api/stream/settings", {"min_qp": 30})
        await asyncio.sleep(2)
        # wait out any cooldown from a previous run
        cd = (await get("/api/stream/adapt"))["adapt"]["cooldown"]
        if cd: 
            print("   (waiting %ss for cooldown)" % cd); await asyncio.sleep(cd+1)

        print("A. CONCURRENCY: fire 5 simultaneous 'down' requests")
        t0 = time.time()
        res = await asyncio.gather(*[post("/api/stream/adapt", {"dir":"down","reason":"race %d"%i})
                                     for i in range(5)])
        applied = [r for _, r in res if r.get("applied")]
        qp = (await get("/api/status"))["stream"]["min_qp"]
        print("   applied=%d of 5   min_qp now=%s   elapsed=%.1fs" % (len(applied), qp, time.time()-t0))
        assert len(applied) == 1, "FAIL: %d requests applied, expected exactly 1" % len(applied)
        assert qp == 34, "FAIL: expected exactly ONE step (30->34), got %s" % qp
        print("   -> exactly one encoder restart. Race fixed.")

        print("B. WORST-LINK-WINS: an 'up' right after a 'down' must be held off")
        await asyncio.sleep((await get("/api/stream/adapt"))["adapt"]["cooldown"] + 1)
        st, up = await post("/api/stream/adapt", {"dir":"up","reason":"clean viewer"})
        print("   up: applied=%s held_by_other=%s cooldown=%ss"
              % (up.get("applied"), up.get("held_by_other"), up.get("cooldown")))
        assert up.get("applied") is False and up.get("held_by_other"), \
            "FAIL: a clean viewer undid the struggling viewer's step"
        assert (await get("/api/status"))["stream"]["min_qp"] == 34
        print("   -> the struggling link still wins.")

        print("C. CEILING: off-ladder setting must never be recovered PAST")
        await post("/api/stream/settings", {"min_qp": 31})   # ceiling 31 -> rung 34
        await asyncio.sleep(2)
        a = (await get("/api/stream/adapt"))["adapt"]
        print("   ceiling=%s min_qp=%s" % (a["ceiling"], a["min_qp"]))
        await asyncio.sleep(a["cooldown"] + 1)
        st, d1 = await post("/api/stream/adapt", {"dir":"down","reason":"t"})
        print("   down ->", d1["adapt"]["min_qp"])
        await asyncio.sleep(d1["adapt"]["cooldown"] + 1)
        # up as far as it will go
        for _ in range(4):
            st, u = await post("/api/stream/adapt", {"dir":"up","reason":"t"})
            if not u.get("applied"): break
            await asyncio.sleep(u["adapt"]["cooldown"] + 1)
        final = (await get("/api/status"))["stream"]["min_qp"]
        print("   recovered to min_qp=%s (operator asked 31)" % final)
        assert final >= 31, "FAIL: recovered to %s, SHARPER than the operator asked (31)" % final
        print("   -> never sharper than the operator's setting.")

        print("D. SELF-KICK: device refuses to kick the caller's own session")
        async with s.ws_connect(B+"/ws") as ws:
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            sid = json.loads(m.data)["sid"]
            st, r = await post("/api/sessions", {"action":"kick","sid":sid,"self_sid":sid})
            print("   kick self -> HTTP %s %s" % (st, r))
            assert st == 400 and not r.get("ok"), "FAIL: device allowed a self-kick"
            # and a normal kick still works
            st2, r2 = await post("/api/sessions", {"action":"kick","sid":sid})
            print("   kick without self_sid -> %s" % r2.get("ok"))
            assert r2.get("ok")
        print("   -> self-lockout blocked, real kick still works.")

        await post("/api/stream/settings", {"min_qp": 30})
        print("\nALL REGRESSION CHECKS PASSED")

asyncio.run(main())
