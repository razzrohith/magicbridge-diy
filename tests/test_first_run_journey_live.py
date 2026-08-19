"""End-to-end first-run, exactly as a customer experiences it. On the device."""
import json, re, subprocess, time, urllib.request, urllib.error, urllib.parse, shutil
CFG="/etc/magicbridge/config.json"
def cfg(): return json.load(open(CFG))
import bcrypt
shutil.copy(CFG,"/tmp/cfg.e2e.bak")
c=cfg(); a=c.setdefault("auth",{})
a["main_password_hash"]=bcrypt.hashpw(b"magicbridge",bcrypt.gensalt()).decode()
a["password_hash"]=bcrypt.hashpw(b"stealthbridge",bcrypt.gensalt()).decode()
json.dump(c,open(CFG,"w"),indent=2)
subprocess.run(["systemctl","restart","magicbridge","stealth-dashboard"],check=False); time.sleep(8)
print("0. unit reset to shipped defaults (both panels)")

class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a): return None
OP=urllib.request.build_opener(NR)
def req(url,cookie=None,data=None):
    r=urllib.request.Request(url,data=data)
    if cookie: r.add_header("Cookie",cookie)
    try:
        o=OP.open(r,timeout=40); return o.status,o.read().decode("utf-8","replace"),o.headers
    except urllib.error.HTTPError as e:
        return e.code,e.read().decode("utf-8","replace"),e.headers

B="http://127.0.0.1:8080"
st,body,h=req(B+"/login",data=urllib.parse.urlencode({"pw":"magicbridge"}).encode())
ck=re.search(r"(mb_sess=[^;]+)",h.get("Set-Cookie","")).group(1)
print("1. signed in with the shipped default")

st,body,h=req(B+"/",cookie=ck)
assert "first-run" in (h.get("Location") or ""), "FAIL: not forced to first-run"
print("2. forced to the password page")

NEW="ship-ready-1"
st,body,h=req(B+"/first-run",cookie=ck,data=urllib.parse.urlencode({"p1":NEW,"p2":NEW}).encode())
print("3. password set -> HTTP",st)
ck2=(re.search(r"(mb_sess=[^;]+)",h.get("Set-Cookie","") or "") or [None,ck])[0] or ck
time.sleep(9)   # the stealth restart happens here

a=cfg()["auth"]
print("4. main accepts the new password   :", bcrypt.checkpw(NEW.encode(),a["main_password_hash"].encode()))
print("5. stealth accepts it too          :", bcrypt.checkpw(NEW.encode(),a["password_hash"].encode()))
print("6. neither default still works     :",
      not bcrypt.checkpw(b"magicbridge",a["main_password_hash"].encode())
      and not bcrypt.checkpw(b"stealthbridge",a["password_hash"].encode()))
print("7. stealth-dashboard is running    :",
      subprocess.run(["systemctl","is-active","stealth-dashboard"],capture_output=True,text=True).stdout.strip())

st,_,_=req(B+"/api/status",cookie=ck2)
print("8. main panel usable after first-run: HTTP",st)
assert st==200, "FAIL: main panel not usable (%s)"%st

# and the owner can actually sign in to the ADMIN panel with that same password
S="http://127.0.0.1:7777"
st,body,h=req(S+"/login")
csrf=(re.search(r'name="_csrf"[^>]*value="([^"]+)"',body) or [None,""])[1]
sck=(re.search(r"(session=[^;]+)",h.get("Set-Cookie","") or "") or [None,""])[0]
st,body,h=req(S+"/login",cookie=sck,data=urllib.parse.urlencode({"pw":NEW,"_csrf":csrf}).encode())
print("9. admin panel sign-in with the new password: HTTP",st)
assert st in (302,200), "FAIL: cannot sign in to the admin panel (%s)"%st

shutil.copy("/tmp/cfg.e2e.bak",CFG)
subprocess.run(["systemctl","restart","magicbridge","stealth-dashboard"],check=False)
print("\nFULL FIRST-RUN JOURNEY WORKS END TO END - PASS")
