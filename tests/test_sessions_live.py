#!/usr/bin/env python3
"""Live session list + kick test. RUN THIS ON THE DEVICE, not on a workstation.

    scp tests/test_sessions_live.py raj@<pi>:/tmp/ && ssh raj@<pi> python3 /tmp/test_sessions_live.py

It needs an authenticated cookie in /tmp/mbck (format: "mb_sess=<token>"). Mint
one on the device with the secret from the config:

    sudo python3 -c "
    import json,hmac,hashlib,time
    a=json.load(open('/etc/magicbridge/config.json'))['auth']
    b='%d.3600.%d'%(int(time.time()), int(a.get('session_epoch',0)))
    t=b+'.'+hmac.new(a['main_secret_key'].encode(),b.encode(),hashlib.sha256).hexdigest()
    open('/tmp/mbck','w').write('mb_sess='+t)"

WHAT IT PROVES, in order: the server names each connection on connect (hello),
the connection appears in /api/sessions, kicking it returns ok, the client is
TOLD it was kicked before the socket closes (a close code alone does not
survive aiohttp's reader or a proxy), the session leaves the list, and - the
one that matters - no OTHER session was disconnected. That last assert is why
this test exists: a kick that takes down the operator's own browser, or a
second viewer's, is worse than no kick at all.

Safe to run against a device in use: it only ever kicks the connection it just
opened itself, and it never touches the target computer.
"""

import asyncio, json, aiohttp

CK = open('/tmp/mbck').read().strip()
H  = {"Cookie": CK}
BASE = "http://127.0.0.1:8080"

async def main():
    async with aiohttp.ClientSession(headers=H) as s:
        async with s.ws_connect(BASE + "/ws") as ws:
            hello = None
            try:
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == aiohttp.WSMsgType.TEXT:
                    hello = json.loads(m.data)
            except asyncio.TimeoutError:
                pass
            print("1. hello frame:", hello)
            mysid = (hello or {}).get("sid")
            assert mysid, "FAIL: no hello/sid on connect"

            async with s.get(BASE + "/api/sessions") as r:
                d = await r.json()
            ours = [x for x in d["sessions"] if x["sid"] == mysid]
            print("2. listed  : %d sessions, ours present=%s ua=%r"
                  % (d["count"], bool(ours), ours[0]["ua"] if ours else None))
            assert ours, "FAIL: our session not listed"

            others = [x["sid"] for x in d["sessions"] if x["sid"] != mysid]
            print("3. leaving alone:", others or "(none)")

            async with s.post(BASE + "/api/sessions",
                              json={"action": "kick", "sid": mysid}) as r:
                k = await r.json()
            print("4. kick    :", k)
            assert k.get("ok"), "FAIL: kick rejected"

            saw_kicked = False
            for _ in range(5):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("type") == "kicked":
                    saw_kicked = True
                if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.CLOSING):
                    break
            print("5. socket  : kicked-msg=%s closed=%s" % (saw_kicked, ws.closed))
            assert saw_kicked, "FAIL: no {'type':'kicked'} before the close"

        await asyncio.sleep(0.5)
        async with s.get(BASE + "/api/sessions") as r:
            d2 = await r.json()
        gone = [x for x in d2["sessions"] if x["sid"] == mysid]
        print("6. after    : %d sessions, ours gone=%s" % (d2["count"], not gone))
        assert not gone, "FAIL: session still listed after kick"
        still = [x["sid"] for x in d2["sessions"]]
        print("7. survivors:", still)
        assert all(o in still for o in others), "FAIL: kicked someone else's session!"

        async with s.post(BASE + "/api/sessions",
                          json={"action": "kick", "sid": "deadbeef"}) as r:
            print("8. bad sid  : %d %s" % (r.status, await r.json()))
        async with s.post(BASE + "/api/sessions", json={"action": "nope"}) as r:
            print("9. bad act  : %d %s" % (r.status, await r.json()))

    print("\nALL SESSION CHECKS PASSED")

asyncio.run(main())
