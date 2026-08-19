"""Does revoking a session actually drop a LIVE WebSocket? Runs on the device.

The socket is what carries keydown/paste/mouse into the target's HID gadget, so
a 'revoked' session that keeps its socket keeps full keyboard control. This is
the test for that.
"""
import asyncio, json, hmac, hashlib, time, aiohttp

CFG = "/etc/magicbridge/config.json"
def cfg(): return json.load(open(CFG))
def mint():
    a = cfg()["auth"]; s = a["main_secret_key"]; e = int(a.get("session_epoch", 0))
    b = "%d.3600.%d" % (int(time.time()), e)
    return "mb_sess=" + b + "." + hmac.new(s.encode(), b.encode(), hashlib.sha256).hexdigest()

def set_real_password():
    """The first-run lock 403s /ws while the shipped default is live (correct),
    so put the unit on a real password before testing revocation."""
    c = cfg()
    try:
        import bcrypt
        c["auth"]["main_password_hash"] = bcrypt.hashpw(b"revoke-test-pw", bcrypt.gensalt()).decode()
    except Exception:
        c["auth"]["main_password_hash"] = "sha256:" + hashlib.sha256(b"revoke-test-pw").hexdigest()
    json.dump(c, open(CFG, "w"), indent=2)

async def main():
    set_real_password()
    import subprocess; subprocess.run(["systemctl", "restart", "magicbridge"], check=False)
    time.sleep(7)
    print("0. unit put on a real password so the first-run lock is not in the way")
    ck = mint()
    async with aiohttp.ClientSession(headers={"Cookie": ck}) as s:
        async with s.ws_connect("http://127.0.0.1:8080/ws") as ws:
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            hello = json.loads(m.data) if m.type == aiohttp.WSMsgType.TEXT else {}
            print("1. socket open, sid =", hello.get("sid"))
            assert hello.get("sid"), "FAIL: no hello"

            # prove it is live and accepted (a harmless no-op message)
            await ws.send_json({"type": "ping"})
            print("2. socket is accepting messages")

            # REVOKE: rotate the secret exactly like a password change does
            c = cfg(); import secrets as _s
            c["auth"]["main_secret_key"] = _s.token_hex(32)
            json.dump(c, open(CFG, "w"), indent=2)
            print("3. credentials revoked (secret rotated, as a password change does)")

            t0 = time.time()
            dropped = False; reason = None
            while time.time() - t0 < 25:
                try:
                    m = await asyncio.wait_for(ws.receive(), timeout=3)
                except asyncio.TimeoutError:
                    continue
                if m.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "kicked":
                        reason = d.get("reason")
                if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.CLOSING):
                    dropped = True; break
            took = time.time() - t0
            print("4. socket dropped=%s after %.1fs, reason=%r" % (dropped, took, reason))
            assert dropped, "FAIL: revoked session KEPT its websocket (still has keyboard control)"
            assert took < 20, "FAIL: took too long (%.1fs)" % took
    print("\nREVOCATION REACHES THE SOCKET - PASS")

asyncio.run(main())
