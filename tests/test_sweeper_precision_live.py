"""The revocation sweeper must not fire on the wrong things. Runs on the device.

Two defects the sweeper introduced, both tested here:
  A. an unreadable config.json read as "credentials changed" and disconnected
     EVERY operator. The stealth panel rewrites that file non-atomically from
     19 call sites, so a brief empty read is routine, not exotic.
  B. changing your own password kicked your own live KVM session, with no
     reconnect - i.e. the most encouraged security action broke the panel.
"""
import asyncio, json, hmac, hashlib, secrets, shutil, time, aiohttp

CFG = "/etc/magicbridge/config.json"
def cfg(): return json.load(open(CFG))
def mint():
    a = cfg()["auth"]; b = "%d.3600.%d" % (int(time.time()), int(a.get("session_epoch", 0)))
    return "mb_sess=" + b + "." + hmac.new(a["main_secret_key"].encode(), b.encode(), hashlib.sha256).hexdigest()

async def open_ws(s):
    ws = await s.ws_connect("http://127.0.0.1:8080/ws")
    m = await asyncio.wait_for(ws.receive(), timeout=5)
    return ws, json.loads(m.data)["sid"]

async def still_open(ws, secs):
    """True if the socket survives `secs` without being closed."""
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=2)
        except asyncio.TimeoutError:
            continue
        if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            return False
        if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("type") == "kicked":
            return False
    return True

async def main():
    shutil.copy(CFG, "/tmp/cfg.regfix.bak")
    try:
        # ---- A: a corrupt/unreadable config must NOT disconnect anyone -------
        async with aiohttp.ClientSession(headers={"Cookie": mint()}) as s:
            ws, sid = await open_ws(s)
            print("A. socket open (sid %s); now corrupting config.json mid-flight" % sid)
            good = open(CFG).read()
            open(CFG, "w").write("")            # exactly what a truncate-then-write exposes
            survived = await still_open(ws, 14)  # longer than the 10s sweep
            open(CFG, "w").write(good)           # restore before asserting
            print("   socket survived an unreadable config:", survived)
            assert survived, "FAIL: a bad config read disconnected a VALID session"
            await ws.close()

        # ---- B: changing my own password must not kick ME --------------------
        async with aiohttp.ClientSession(headers={"Cookie": mint()}) as s:
            ws, sid = await open_ws(s)
            # set a known current password so the endpoint accepts the change
            import bcrypt
            c = cfg(); c["auth"]["main_password_hash"] = bcrypt.hashpw(b"cur-pass-1", bcrypt.gensalt()).decode()
            json.dump(c, open(CFG, "w"), indent=2)
            await asyncio.sleep(1)
            async with s.post("http://127.0.0.1:8080/api/auth/change-password",
                              json={"current": "cur-pass-1", "new": "new-pass-2", "sid": sid}) as r:
                body = await r.json()
            print("B. change-password ->", body)
            assert body.get("ok"), "FAIL: password change rejected (%s)" % body
            survived = await still_open(ws, 14)
            print("   my own session survived my own password change:", survived)
            assert survived, "FAIL: changing my password killed my own KVM session"
            await ws.close()

        # ---- and a REAL revocation still works -------------------------------
        async with aiohttp.ClientSession(headers={"Cookie": mint()}) as s:
            ws, sid = await open_ws(s)
            c = cfg(); c["auth"]["main_secret_key"] = secrets.token_hex(32)
            json.dump(c, open(CFG, "w"), indent=2)
            dropped = not await still_open(ws, 16)
            print("C. a genuine revocation still drops the socket:", dropped)
            assert dropped, "FAIL: revocation no longer works"
    finally:
        shutil.copy("/tmp/cfg.regfix.bak", CFG)
    print("\nSWEEPER FIRES ONLY WHEN IT SHOULD - PASS")

asyncio.run(main())
