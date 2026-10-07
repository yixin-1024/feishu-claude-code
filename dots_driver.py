"""在 browser-harness 里执行的 Dots 驱动脚本（由 dots_runner 通过 stdin 喂给 `browser-harness`）。

browser-harness 把 stdin 读进来后 `exec(code, globals())`，所以这里能直接用它预置的
`cdp()`；参数全部走环境变量（同一进程，os.environ 可见）。输出是一行一个 JSON 事件：

    {"ev":"room","room":..,"name":..}        选中的 Dot 房间
    {"ev":"sent","id":..}                    发出去的那条已经在服务端落库
    {"ev":"msg","id":..,"text":..,"files":[{name,mime,path}|{name,error}],"created_at":..}
                                             豆包的一条消息（附件已下载到本机）
    {"ev":"cursor","id":..}                  stream：这一批处理完后的位置（含用户自己的消息）
    {"ev":"done","reason":..,"last_id":..,"busy":bool}
    {"ev":"error","code":..,"msg":..}

DOTS_ACTION：
    send    把 DOTS_PROMPT_FILE / DOTS_FILES 发给豆包，落库即返回（不等回复）
    stream  从 DOTS_AFTER（空 = 当前最新）往后一直跟，豆包每说一条就吐一条；常驻不退出
    status  连通性体检
    screen  截一张豆包云电脑的当前画面（能认出二维码就再裁一张）

为什么这样读写（2026-09-30 实测）：
- 读消息走页面同源接口 /backend-api/messaging/rooms/<room>/messages，用的是用户自己
  的登录态。豆包的回复**不是流式**：网页上先出「正在输入」，然后整条一次性出现，接口里
  created_at == updated_at。一次提问它可能连发好几条。
- 附件在 content.attachments[].file 里（name / mime_type / download_url），download_url
  在页面里带登录态直接 fetch 就能拿到原文件。
- 发消息必须在页面里模拟输入 + 点「发送」：服务端直调会被反爬令牌拦，不去绕。
- 全程用**显式 CDP session**（Target.attachToTarget + session_id=），不调 switch_tab，
  所以不会抢 browser-harness daemon 的「当前标签」，也不会把标签切到前台打扰用户。
- Chrome 里没开 Dots 页面时自己开一个（10-07：用户关了标签，转发器报了一晚上 no_tab）：
  先在 CDP 的默认上下文里开后台标签，核对是同一个 Dot 才留下；不行再用 `open
  --profile-directory` 让 Chrome 在登录了 Dots 的资料里打开，开完把用户原来的前台 app 切回去。
  CDP 只能在默认上下文里建标签，别的资料会报「Failed to find browser context」，所以要有后一条。
"""

import json as _json
import os as _os
import re as _re
import time as _time


def _emit(ev, **kw):
    kw["ev"] = ev
    print(_json.dumps(kw, ensure_ascii=False), flush=True)


class _DotsError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _env(name, default=""):
    v = _os.environ.get(name)
    return default if v is None or v == "" else v


def _env_float(name, default):
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return float(default)


_ACTION = _env("DOTS_ACTION", "send")
_URL_MATCH = _env("DOTS_URL_MATCH", "chatgpt.com/dots")
_ROOM_ID = _env("DOTS_ROOM_ID")
_DOT_NAME = _env("DOTS_NAME")
_AFTER = _env("DOTS_AFTER")
_FAST = max(1.0, _env_float("DOTS_POLL_SEC", 2.5))          # 豆包在忙 / 刚有动静时的轮询间隔
_SLOW = max(_FAST, _env_float("DOTS_IDLE_POLL_SEC", 15))    # 长时间没动静时放慢，别一直刷接口
_ACTIVE_WINDOW = max(30.0, _env_float("DOTS_ACTIVE_WINDOW_SEC", 300))
_WAKE_FILE = _env("DOTS_WAKE_FILE")                          # runner 往这里 touch 一下 = 马上进快轮询
_DL_DIR = _env("DOTS_DL_DIR", "/tmp/cc-dots-files")
_DL_MAX = _env_float("DOTS_DL_MAX_BYTES", 30 * 1024 * 1024)  # Lark 文件上限 30MB
_SEND_CONFIRM = max(5.0, _env_float("DOTS_SEND_CONFIRM_SEC", 30))
_HOME_URL = _env("DOTS_HOME_URL", "https://chatgpt.com/dots")
_CHROME_PROFILE = _env("DOTS_CHROME_PROFILE")                # 如 "Profile 38"；不配就用自己记下的
_OPEN_WAIT = max(10.0, _env_float("DOTS_OPEN_WAIT_SEC", 30))  # 新开的标签最多等这么久加载完
_SETTLE = max(3.0, _env_float("DOTS_SETTLE_SEC", 20))        # 发送前最多等页面跳完这么久

_SID = None


def _ev(expr, timeout=60):
    r = cdp(
        "Runtime.evaluate", session_id=_SID, expression=expr,
        returnByValue=True, awaitPromise=True, _response_timeout=timeout,
    )
    if "exceptionDetails" in r:
        ed = r["exceptionDetails"] or {}
        desc = (ed.get("exception") or {}).get("description") or ed.get("text") or str(ed)
        # 只留第一行（"TypeError: Failed to fetch"），别把整段调用栈转进用户的私聊
        raise _DotsError("js", desc.strip().splitlines()[0][:200])
    return r.get("result", {}).get("value")


# accessToken 缓存在页面 window 上，过期或 401 再换；每次轮询不必多打一次 /api/auth/session
_API_JS = r"""(async()=>{
  const path=%s, method=%s, body=%s;
  async function tok(force){
    if(force||!window.__ccDotsTok||Date.now()>window.__ccDotsTokExp){
      const s=await (await fetch('/api/auth/session',{credentials:'include'})).json();
      if(!s||!s.accessToken) return null;
      window.__ccDotsTok=s.accessToken; window.__ccDotsTokExp=Date.now()+10*60*1000;
    }
    return window.__ccDotsTok;
  }
  async function go(force){
    const t=await tok(force);
    if(!t) return {status:401,text:'no session (logged out?)'};
    const init={method,headers:{Authorization:'Bearer '+t,'Content-Type':'application/json'}};
    if(body!==null) init.body=JSON.stringify(body);
    const x=await fetch(path,init);
    return {status:x.status,text:await x.text()};
  }
  let r=await go(false);
  if(r.status===401||r.status===403) r=await go(true);
  return r;
})()"""


def _api(path, method="GET", body=None):
    expr = _API_JS % (_json.dumps(path), _json.dumps(method), _json.dumps(body))
    for attempt in range(3):
        # 接口偶发 500/502、网络抖一下就「Failed to fetch」（10-01 实测都出现过），
        # 稍等重试，别让常驻转发器为一次抖动整个重启
        try:
            r = _ev(expr, 60)
        except _DotsError as e:
            if e.code != "js" or attempt == 2:
                raise
            _time.sleep(1.5 * (attempt + 1))
            continue
        if not (isinstance(r, dict) and int(r.get("status") or 0) >= 500) or attempt == 2:
            break
        _time.sleep(1.5 * (attempt + 1))
    if not isinstance(r, dict):
        raise _DotsError("api", "页面接口无返回：%r" % (r,))
    if r.get("status") != 200:
        code = "logged_out" if r.get("status") in (401, 403) else "api"
        raise _DotsError(code, "%s %s → HTTP %s %s" % (method, path, r.get("status"), (r.get("text") or "")[:200]))
    return _json.loads(r["text"])


_BUSY_JS = r"""(()=>{
  const out=[];
  const ti=document.querySelector('.typing-indicator');
  if(ti){ const s=(ti.textContent||'').trim(); if(s) out.push(s); }
  for(const b of document.querySelectorAll('button[class*="aeon-status"]')){
    const s=(b.textContent||'').trim();
    if(s && /正在|思考|Thinking|Working|typing|…|\.\.\./i.test(s) && !/连接|Connecting/i.test(s)) out.push(s);
  }
  return out.join(' · ');
})()"""


def _busy():
    try:
        return _ev(_BUSY_JS, 20) or ""
    except Exception:
        # 读 DOM 失败不代表豆包闲着：保守当忙，让静默计时别提前收尾
        return "?"


# ── 找 / 开 Dots 标签页 ─────────────────────────────────
_PAGE_JS = r"""(()=>({ready:document.readyState, path:location.pathname,
  composer:!!document.querySelector('.ProseMirror[contenteditable=true]')}))()"""
# 豆包页面打开后会自己跳两次：/dots → /dots/home → /dots/<对话 id>，每跳一次输入框就重建一次。
# 10-07 23:53「写进去的是 ''」就是字写进了马上要被换掉的那个输入框。停在对话页才算跳完。
_ROOM_PATH_RE = _re.compile(r"/dots/[0-9a-f]{8}-[0-9a-f]{4}-")

_CTX = None  # 当前挂着的标签在哪个 browserContext（= 哪个 Chrome 资料）


def _memo_path():
    # 每个 bot profile 一份：runner 给的 wake 文件名里带着 profile 名（cc-dots-wake-<profile>）
    key = _os.path.basename(_WAKE_FILE).replace("cc-dots-wake-", "") if _WAKE_FILE else ""
    return _os.path.join(_os.path.expanduser("~/.cache/cc-dots"), "chrome-%s.json" % (key or "default"))


def _memo():
    """自己记下的 {room: Dot 房间, profile_dir: 登录了 Dots 的 Chrome 资料目录}。"""
    try:
        with open(_memo_path(), encoding="utf-8") as f:
            d = _json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(**kw):
    d = _memo()
    if all(d.get(k) == v for k, v in kw.items()):
        return
    d.update(kw)
    try:
        _os.makedirs(_os.path.dirname(_memo_path()), exist_ok=True)
        tmp = _memo_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(d, f, ensure_ascii=False)
        _os.replace(tmp, _memo_path())
    except OSError:
        pass


def _dots_pages():
    targets = cdp("Target.getTargets")["targetInfos"]
    pages = [t for t in targets if t.get("type") == "page" and _URL_MATCH in (t.get("url") or "")]
    pages.sort(key=lambda t: 0 if _ROOM_PATH_RE.search(t.get("url") or "") else 1)  # 停在对话页的优先
    return pages


def _attach_to(target_id):
    global _SID, _CTX
    _detach()
    _SID = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
    try:
        _CTX = cdp("Target.getTargetInfo", targetId=target_id)["targetInfo"].get("browserContextId")
    except Exception:
        _CTX = None


def _detach():
    global _SID
    if _SID:
        try:
            cdp("Target.detachFromTarget", sessionId=_SID)
        except Exception:
            pass
    _SID = None


def _settle(timeout, need_composer=False, need_room=False):
    """等页面加载完；need_room 还要等它跳到对话页，need_composer 还要等输入框出来。
    超时了但页面本身能用（比如 Dots 改了路由）就照常往下走，页面一直没加载出来才报错。"""
    deadline = _time.time() + timeout
    reloaded = False
    while True:
        try:
            st = _ev(_PAGE_JS, 10) or {}
        except _DotsError:
            st = {}
        loaded = st.get("ready") in ("interactive", "complete")
        usable = loaded and (st.get("composer") or not need_composer)
        path = st.get("path") or ""
        # 跳出了 /dots（没登录会被带去登录页）就别干等，交给调用方去核对
        if usable and (not need_room or _ROOM_PATH_RE.search(path) or "/dots" not in path):
            return
        now = _time.time()
        if now >= deadline:
            if usable:
                return
            raise _DotsError(
                "tab_unavailable",
                "Dots 标签页没加载出来（%s），可能被 Chrome 休眠了或掉了登录，点开看一下再试。"
                % ("找不到输入框" if loaded else "readyState=%r" % st.get("ready")),
            )
        # 后台标签被 Chrome 省内存丢弃后是个空壳，刷新一次把它叫回来
        if not loaded and not reloaded and now > deadline - timeout / 2:
            reloaded = True
            try:
                cdp("Page.reload", session_id=_SID)
            except Exception:
                pass
        _time.sleep(0.5)


def _front_app():
    import subprocess as _sp
    try:
        out = _sp.run(["/bin/sh", "-c", 'lsappinfo info -only bundleid "$(lsappinfo front)"'],
                      capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""
    m = _re.search(r'bundleID="([^"]+)"', out or "")
    return m.group(1) if m else ""


def _is_our_dot(memo):
    """新开的标签登录的是不是我们那个 Dot：账号下得有记下来的房间（还没记过就有 Dot 就行）。
    CDP 的默认上下文不一定是登录了 Dots 的那个 Chrome 资料，别把话发给别的账号。"""
    try:
        if not memo.get("room"):
            _resolve_room()
            return True
        items = _api("/backend-api/messaging/rooms?limit=32").get("items") or []
        return any(r.get("id") == memo["room"] for r in items)
    except _DotsError:
        return False


def _open_in_default_context(memo):
    """在 CDP 默认上下文里开一个后台标签（不抢前台、不弹窗）。不是我们的 Dot 就关掉。"""
    try:
        tid = cdp("Target.createTarget", url=_HOME_URL, background=True)["targetId"]
    except Exception:
        return False
    try:
        _attach_to(tid)
        _settle(_OPEN_WAIT, need_room=True)
        if _is_our_dot(memo):
            return True
    except _DotsError:
        pass
    _detach()
    try:
        cdp("Target.closeTarget", targetId=tid)
    except Exception:
        pass
    return False


def _open_with_profile(profile_dir):
    """让 Chrome 自己在指定资料里打开 Dots。open 会把 Chrome 拉到前台（-g 也拦不住，10-07
    实测），所以开完把用户原来在用的 app 切回去。"""
    import subprocess as _sp
    front = _front_app()
    try:
        _sp.run(["/usr/bin/open", "-g", "-na", "Google Chrome", "--args",
                 "--profile-directory=" + profile_dir, _HOME_URL], capture_output=True, timeout=15)
        deadline = _time.time() + 20
        while _time.time() < deadline:
            pages = _dots_pages()
            if pages:
                _attach_to(pages[0]["targetId"])
                _settle(_OPEN_WAIT, need_room=True)
                return True
            _time.sleep(0.5)
        return False
    except _DotsError:
        return False
    finally:
        if front and front != "com.google.Chrome":
            try:
                _sp.run(["/usr/bin/open", "-b", front], capture_output=True, timeout=10)
            except Exception:
                pass


def _open_tab():
    import fcntl as _fcntl
    memo = _memo()
    prof = _CHROME_PROFILE or memo.get("profile_dir") or ""
    _os.makedirs(_os.path.dirname(_memo_path()), exist_ok=True)
    with open(_memo_path() + ".lock", "w") as lk:
        _fcntl.flock(lk, _fcntl.LOCK_EX)  # 转发器和发消息可能同时发现没标签，别各开一个
        pages = _dots_pages()
        if pages:  # 等锁的时候别人已经开好了
            _attach_to(pages[0]["targetId"])
            _settle(_OPEN_WAIT, need_room=True)
            return
        if _open_in_default_context(memo):
            return
        if prof and _open_with_profile(prof):
            return
    raise _DotsError(
        "no_tab",
        "Chrome 里没有 Dots 页面，自动打开也没成功%s。请在登录了 Dots 的 Chrome 资料里打开 %s 并保持标签页开着。"
        % ("" if prof else "（还不知道是哪个 Chrome 资料，可以配 DOTS_CHROME_PROFILE）", _HOME_URL),
    )


def _attach(need_composer=False):
    pages = _dots_pages()
    if pages:
        _attach_to(pages[0]["targetId"])
    else:
        _open_tab()
    _settle(_SETTLE, need_composer=need_composer, need_room=need_composer)


def _learn_profile_dir():
    """记下 Dots 标签在哪个 Chrome 资料目录，留给以后 open --profile-directory 兜底。
    只能在默认上下文里开 chrome://version 读（CDP 只能在那里建标签），不在就算了。"""
    if _CHROME_PROFILE or _memo().get("profile_dir") or not _CTX:
        return
    if _CTX != cdp("Target.getBrowserContexts").get("defaultBrowserContextId"):
        return
    tid = cdp("Target.createTarget", url="chrome://version", background=True)["targetId"]
    sid = None
    try:
        sid = cdp("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
        for _ in range(20):
            r = cdp("Runtime.evaluate", session_id=sid, returnByValue=True, _response_timeout=10,
                    expression="(document.getElementById('profile_path')||{}).innerText||''")
            path = ((r.get("result") or {}).get("value") or "").strip().rstrip("/")
            if path:
                _remember(profile_dir=_os.path.basename(path))
                return
            _time.sleep(0.3)
    finally:
        if sid:
            try:
                cdp("Target.detachFromTarget", sessionId=sid)
            except Exception:
                pass
        try:
            cdp("Target.closeTarget", targetId=tid)
        except Exception:
            pass


def _resolve_room():
    """返回 (room_id, dot 名字, dot 的 account_user_id 集合)。"""
    data = _api("/backend-api/messaging/rooms?limit=32")  # 接口上限 32
    cands = []
    for r in data.get("items") or []:
        dots = [m for m in (r.get("members") or []) if str(m.get("account_user_id", "")).startswith("calpico")]
        if not dots:
            continue
        if _ROOM_ID and r.get("id") != _ROOM_ID:
            continue
        if _DOT_NAME and r.get("name") != _DOT_NAME and not any(m.get("name") == _DOT_NAME for m in dots):
            continue
        cands.append((r.get("updated_at") or "", r["id"], dots[0].get("name") or r.get("name") or "Dot",
                      {m["account_user_id"] for m in dots}))
    if not cands:
        want = _ROOM_ID or _DOT_NAME or "任意 Dot"
        raise _DotsError("no_room", "当前账号下没找到 Dot 房间（%s）。" % want)
    cands.sort(reverse=True)
    _, room, name, dot_ids = cands[0]
    return room, name, dot_ids


_PAGE = 30  # messages 接口 limit 上限同样很小（rooms 是 32），留点余量


def _messages_after(room, after, limit=_PAGE):
    q = "?limit=%d" % limit
    if after:
        q += "&after=" + after
    return _api("/backend-api/messaging/rooms/%s/messages%s" % (room, q)).get("items") or []


def _latest_id(room):
    items = _api("/backend-api/messaging/rooms/%s/messages?limit=1" % room).get("items") or []
    return items[-1]["id"] if items else ""


def _is_dot(m, dot_ids):
    uid = str(m.get("account_user_id") or "")
    return uid in dot_ids or uid.startswith("calpico")


_DL_JS = r"""(async()=>{
  const url=%s, max=%d;
  // 附件有两种地址：截图这类在 chatgpt.com 同源（要带登录态），Excel 这类是 oaiusercontent.com
  // 的签名链接（10-01 实测）。跨域那种带 cookie 会被浏览器 CORS 直接拦成「Failed to fetch」，
  // 签名本身就是授权，所以跨域一律裸请求，也不把 token 发给存储域名。
  const same=new URL(url,location.href).origin===location.origin;
  const t=window.__ccDotsTok;
  const init=same?{headers:t?{Authorization:'Bearer '+t}:{}}:{};
  const r=await fetch(url,init);
  if(!r.ok) return {status:r.status};
  const b=await r.arrayBuffer();
  if(b.byteLength>max) return {status:413,size:b.byteLength};
  const u8=new Uint8Array(b); let s='';
  for(let i=0;i<u8.length;i+=0x8000) s+=String.fromCharCode.apply(null,u8.subarray(i,i+0x8000));
  return {status:200,mime:r.headers.get('content-type')||'',b64:btoa(s)};
})()"""


def _safe_name(name):
    keep = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (name or "file"))
    return keep.strip("._") or "file"


def _download(att):
    """把豆包消息里的附件拉到本机，返回 {name, mime, path} 或 {name, error}。"""
    f = att.get("file") or {}
    name = f.get("name") or att.get("name") or "附件"
    url = f.get("download_url") or ""
    if not url:
        return {"name": name, "error": "no download_url"}
    r = _ev(_DL_JS % (_json.dumps(url), int(_DL_MAX)), 120)
    if not isinstance(r, dict) or r.get("status") != 200:
        return {"name": name, "error": "HTTP %s" % (r or {}).get("status")}
    import base64 as _b64
    _os.makedirs(_DL_DIR, exist_ok=True)
    fid = (f.get("id") or att.get("attachment_id") or str(int(_time.time() * 1000)))[-16:]
    path = _os.path.join(_DL_DIR, "%s_%s" % (_safe_name(fid), _safe_name(name)))
    with open(path, "wb") as fh:
        fh.write(_b64.b64decode(r["b64"]))
    return {"name": name, "mime": f.get("mime_type") or r.get("mime") or "", "path": path}


# 豆包的云电脑画面：网页右侧那块 <video>（WebRTC 推流）。用户自己在网页上本来就看得到它，
# 这里只是替只用 Lark 的用户「看一眼」。能识别出二维码就另外裁一张放大的，方便手机相册扫码。
_SCREEN_JS = r"""(async()=>{
  const v=[...document.querySelectorAll('video')].find(v=>v.srcObject||v.videoWidth>0);
  if(!v) return {ok:false,why:'网页右侧的电脑面板没开'};
  let src=null, w=0, h=0;
  // 优先直接从 WebRTC 轨道取帧：标签页在后台时 Chrome 会把 <video> 暂停，元素上只剩黑帧
  //（10-01 实测：豆包云电脑夜里重启、画面在后台重连后 video.paused=true，截出来全黑），
  // 轨道本身照样在收画面。
  const tr=v.srcObject&&v.srcObject.getVideoTracks&&v.srcObject.getVideoTracks()[0];
  if(tr&&tr.readyState==='live'&&'ImageCapture' in window){
    try{
      src=await Promise.race([new ImageCapture(tr).grabFrame(),
        new Promise((_,j)=>setTimeout(()=>j(new Error('timeout')),8000))]);
      w=src.width; h=src.height;
    }catch(e){ src=null; }
  }
  if(!src&&v.videoWidth>0&&v.readyState>=2){ src=v; w=v.videoWidth; h=v.videoHeight; }
  if(!src) return {ok:false,why:'云电脑画面还没连上'};
  const c=document.createElement('canvas'); c.width=w; c.height=h;
  const g0=c.getContext('2d'); g0.drawImage(src,0,0);
  const d=g0.getImageData(0,0,w,h).data; let mx=0;
  for(let i=0;i<d.length;i+=4*997){ mx=Math.max(mx,(d[i]+d[i+1]+d[i+2])/3); }
  if(mx<12) return {ok:false,why:'云电脑画面是黑的（可能正在重连或屏幕休眠）'};
  const out={ok:true,full:c.toDataURL('image/png')};
  try{
    if('BarcodeDetector' in window){
      const codes=await new BarcodeDetector({formats:['qr_code']}).detect(c);
      if(codes.length){
        const b=codes[0].boundingBox, pad=Math.max(24,b.width*0.25);
        const x=Math.max(0,b.x-pad), y=Math.max(0,b.y-pad);
        const cw=Math.min(w-x,b.width+2*pad), ch=Math.min(h-y,b.height+2*pad);
        const k=Math.max(1,Math.round(640/Math.max(cw,ch)));
        const q=document.createElement('canvas'); q.width=cw*k; q.height=ch*k;
        const g=q.getContext('2d'); g.imageSmoothingEnabled=false; g.drawImage(c,x,y,cw,ch,0,0,q.width,q.height);
        out.qr=q.toDataURL('image/png');
      }
    }
  }catch(e){ out.qr_err=String(e).slice(0,120); }
  return out;
})()"""

# 这些消息在网页上是要点一下才能看到东西的（登录授权卡片 / 「点这里打开我的云端浏览器」），
# 或者在说扫码——Lark 那边点不了，就把云电脑当前画面一起带过去。
_SCREEN_HINT_RE = None


def _wants_screen(m):
    global _SCREEN_HINT_RE
    import re as _re
    if _SCREEN_HINT_RE is None:
        # 只认「要扫码」的说法；光提到云端浏览器（比如汇报「浏览器里仍登录着」）不截图——
        # 「点这里打开云端浏览器」那种真要看画面的，消息上带 cloud_browser_handoff，下面单独认
        _SCREEN_HINT_RE = _re.compile(r"二维码|扫码|扫一扫|QR ?code", _re.I)
    c = m.get("content") or {}
    meta = m.get("message_metadata") or {}
    return bool(c.get("elicitation") or meta.get("cloud_browser_handoff")
                or _SCREEN_HINT_RE.search(c.get("text") or ""))


# 10-08 网页改版后右侧的电脑面板默认不显示了，入口在 Dot 的个人资料弹窗里：
# 顶栏头像「打开豆包的个人资料」→「电脑」一栏的「豆包的电脑 / 已连接」。只点这两个；
# 同一个弹窗里还有「暂停 豆包」「呼叫」，面板上还有「获取控制权」，都不能碰。
# 等待都放在 Python 这边：后台标签的 setTimeout 会被 Chrome 节流到一分钟一次。
# 「开着」看右侧那块大画面：个人资料弹窗里也有一个 48×32 的缩略图 video，截图够用，但不算面板开着
_PANEL_LIVE_JS = r"""[...document.querySelectorAll('video')].some(v=>(v.srcObject||v.videoWidth>0)&&v.getBoundingClientRect().width>=200)"""
# 头像是个动画 iframe，后台新开、从没显示过的标签里它不渲染（10-08 实测，切成窗口当前标签也不行），
# 那种标签就找不到这个入口，只能等它被显示过一次
_PANEL_PROFILE_JS = r"""(()=>{
  const b=[...document.querySelectorAll('button')].find(b=>!b.closest('aside,nav')
    &&/^(打开|关闭).+的个人资料$|^(Open|Close) .+'s profile$/i.test(b.getAttribute('aria-label')||''));
  if(!b) return 'missing';
  if(!/^(关闭|Close)/i.test(b.getAttribute('aria-label'))) b.click();  // 已经开着就别再点，点了是关
  return 'ok';})()"""
_PANEL_COMPUTER_JS = r"""(()=>{
  const first=b=>((b.innerText||'').trim().split('\n')[0]||'').trim();
  const b=[...document.querySelectorAll('[role=dialog] button,[role=dialog] [role=button]')]
    .find(b=>/.的电脑$|.'s computer$/i.test(first(b)));
  if(!b) return 'missing';
  if(/正在加载|Loading/i.test(b.innerText||'')) return 'loading';
  b.click(); return 'ok';})()"""
_ESCAPE_JS = r"""document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',keyCode:27,bubbles:true}))"""


def _ensure_panel():
    """右侧的电脑面板没开就打开它。返回 None = 画面已经在了；否则返回打不开的原因。"""
    if _ev(_PANEL_LIVE_JS, 10):
        return None
    if _ev(_PANEL_PROFILE_JS, 10) != "ok":
        return "找不到豆包的个人资料入口"
    st = "missing"
    deadline = _time.time() + 10
    while _time.time() < deadline:
        st = _ev(_PANEL_COMPUTER_JS, 10)
        if st == "ok":
            break
        _time.sleep(0.4)
    if st != "ok":
        _ev(_ESCAPE_JS, 10)  # 把打开的个人资料弹窗关掉，别留在页面上
        return "个人资料里没找到电脑入口" if st == "missing" else "电脑状态一直在加载"
    deadline = _time.time() + 15
    while _time.time() < deadline:
        if _ev(_PANEL_LIVE_JS, 10):
            return None
        _time.sleep(0.5)
    return "电脑面板打开了，但画面没连上"


def _screen_files(delay=1.5):
    """截一张云电脑画面（能认出二维码就再裁一张），返回 files 列表项。"""
    import base64 as _b64
    _time.sleep(delay)  # 豆包通常是先开页面再说话，给页面一点渲染时间
    try:
        why = _ensure_panel()
    except _DotsError as e:
        why = str(e)
    r = _ev(_SCREEN_JS, 30)  # 面板没打开也试一下：缩略图那路 video 照样能取到整帧
    if not isinstance(r, dict) or not r.get("ok"):
        err = (r or {}).get("why") or "截不到画面"
        if why:
            err += "；自动打开面板也没成功：%s" % why
        return [{"name": "豆包云电脑画面", "error": err}]
    _os.makedirs(_DL_DIR, exist_ok=True)
    stamp = str(int(_time.time() * 1000))
    out = []
    for key, name in (("qr", "登录二维码.png"), ("full", "豆包云电脑画面.png")):
        if r.get(key):
            path = _os.path.join(_DL_DIR, "%s_%s" % (stamp, name))
            with open(path, "wb") as fh:
                fh.write(_b64.b64decode(r[key].split(",", 1)[1]))
            out.append({"name": name, "mime": "image/png", "path": path})
    return out


def _emit_msg(m):
    c = m.get("content") or {}
    meta = m.get("message_metadata") or {}
    text = c.get("text") or ""
    files = []
    for att in c.get("attachments") or []:
        try:
            files.append(_download(att))
        except Exception as e:  # noqa: BLE001 — 附件拉不下来也要把正文带回去
            files.append({"name": (att.get("file") or {}).get("name") or "附件", "error": str(e)[:200]})
    if c.get("elicitation"):
        text = "🔐 豆包发来登录授权请求：%s\n（网页上是一张要点的授权卡片，Lark 里点不了，下面附上它云电脑的当前画面）" % text
    elif meta.get("cloud_browser_handoff"):
        text += "\n\n📺 （网页上这里能点开它的云端浏览器；下面附上它云电脑的当前画面）"
    if _wants_screen(m):
        try:
            files.extend(_screen_files())
        except Exception as e:  # noqa: BLE001
            files.append({"name": "豆包云电脑画面", "error": str(e)[:200]})
    _emit("msg", id=m["id"], text=text, files=files, created_at=m.get("created_at") or "")


# ── 输入框 ─────────────────────────────────────────────
_COMPOSER_JS = r"""(()=>{const e=document.querySelector('.ProseMirror[contenteditable=true]');
  if(!e) return 'missing'; if((e.innerText||'').trim()) return 'dirty'; e.focus(); return 'ok';})()"""
_COMPOSER_TEXT_JS = r"""(()=>{const e=document.querySelector('.ProseMirror[contenteditable=true]');return e?(e.innerText||''):null;})()"""
_SEND_BTN_JS = r"""(()=>{
  const labels=['发送','Send','Send message','Send prompt'];
  const b=[...document.querySelectorAll('button')].find(b=>labels.includes(b.getAttribute('aria-label'))||b.getAttribute('data-testid')==='send-button');
  if(!b) return 'missing'; if(b.disabled||b.getAttribute('aria-disabled')==='true') return 'disabled';
  %s return 'ok';})()"""


def _key(key, code, vk, modifiers=0, commands=None):
    down = dict(type="keyDown", key=key, code=code, windowsVirtualKeyCode=vk, modifiers=modifiers)
    if commands:
        down["commands"] = commands
    cdp("Input.dispatchKeyEvent", session_id=_SID, **down)
    cdp("Input.dispatchKeyEvent", session_id=_SID, type="keyUp", key=key, code=code,
        windowsVirtualKeyCode=vk, modifiers=modifiers)


def _clear_composer():
    try:
        _ev(r"""(()=>{const e=document.querySelector('.ProseMirror[contenteditable=true]');if(e)e.focus();})()""", 10)
        _key("a", "KeyA", 65, modifiers=4, commands=["selectAll"])  # 4 = Meta（macOS）
        _key("Backspace", "Backspace", 8)
    except Exception:
        pass


def _type_prompt(text):
    # 换行用 Shift+Enter：直接 insertText("\n") 在 ProseMirror 里行为不稳，裸 Enter 会直接发出去
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i:
            _key("Enter", "Enter", 13, modifiers=8)  # 8 = Shift
        if line:
            cdp("Input.insertText", session_id=_SID, text=line)


def _norm(s):
    return "".join((s or "").split())


def _send(room, dot_ids, text, files):
    anchor = _latest_id(room)
    st = _ev(_COMPOSER_JS, 20)
    if st == "missing":
        raise _DotsError("no_composer", "Dots 页面上找不到输入框（页面改版或还没加载完）。")
    if st == "dirty":
        raise _DotsError(
            "composer_dirty",
            "Dots 网页输入框里有没发出去的草稿，为了不把它冲掉，这条没有代发。清空网页输入框后重发即可。",
        )
    if files:
        _attach_files(files)
    _type_prompt(text)
    _time.sleep(0.5)
    typed = _ev(_COMPOSER_TEXT_JS, 10) or ""
    if text.strip() and _norm(text)[:20] not in _norm(typed) and not files:
        # 输入框在打字的半路被换掉了（页面又跳了一次）：等它稳下来再打一遍。
        # 带附件的不重来，附件可能还挂在上面，再传一遍会重复。
        _clear_composer()
        _settle(_SETTLE, need_composer=True, need_room=True)
        if _ev(_COMPOSER_JS, 20) == "ok":
            _type_prompt(text)
            _time.sleep(0.5)
            typed = _ev(_COMPOSER_TEXT_JS, 10) or ""
    if text.strip() and _norm(text)[:20] not in _norm(typed):
        _clear_composer()
        raise _DotsError("type_failed", "往 Dots 输入框写入失败（写进去的是 %r）。" % typed[:80])
    deadline = _time.time() + (60 if files else 5)
    while True:
        r = _ev(_SEND_BTN_JS % "b.click();", 10)
        if r == "ok":
            break
        if _time.time() > deadline:
            _clear_composer()
            raise _DotsError("send_failed", "Dots 的发送按钮不可用（%s）。" % r)
        _time.sleep(0.5)
    # 等我们这条落库：锚点之后第一条非豆包消息
    t0 = _time.time()
    while _time.time() - t0 < _SEND_CONFIRM:
        for m in _messages_after(room, anchor, 20):
            if not _is_dot(m, dot_ids):
                return m["id"]
        _time.sleep(1.0)
    raise _DotsError("send_unconfirmed", "点了发送，但 %ds 内服务端没看到这条消息。" % _SEND_CONFIRM)


def _attach_files(files):
    doc = cdp("DOM.getDocument", session_id=_SID, depth=1)
    node = cdp("DOM.querySelector", session_id=_SID, nodeId=doc["root"]["nodeId"], selector="input[type=file]")
    if not node.get("nodeId"):
        raise _DotsError("no_file_input", "Dots 页面上找不到上传附件的入口。")
    cdp("DOM.setFileInputFiles", session_id=_SID, nodeId=node["nodeId"], files=files)
    _time.sleep(1.0)


def _wake_mtime():
    try:
        return _os.path.getmtime(_WAKE_FILE) if _WAKE_FILE else 0.0
    except OSError:
        return 0.0


def _stream(room, dot_ids, cursor):
    """常驻跟随：豆包每说一条就吐一条。有动静时 _FAST 轮询，安静久了退到 _SLOW；
    runner touch 了 wake 文件（用户刚发了消息）就立刻回到快轮询。"""
    last_activity = _time.time()
    wake_seen = _wake_mtime()
    while True:
        moved = False
        while True:  # 一次可能不止一页
            items = _messages_after(room, cursor)
            for m in items:
                cursor = m["id"]
                moved = True
                if _is_dot(m, dot_ids):
                    _emit_msg(m)
            if len(items) < _PAGE:
                break
        now = _time.time()
        if moved:
            last_activity = now
            _emit("cursor", id=cursor)
        if now - last_activity < _ACTIVE_WINDOW and _busy():
            last_activity = now  # 还在思考 / 打字：保持快轮询
        interval = _FAST if now - last_activity < _ACTIVE_WINDOW else _SLOW
        deadline = now + interval
        while _time.time() < deadline:
            _time.sleep(0.5)
            w = _wake_mtime()
            if w > wake_seen:
                wake_seen = w
                last_activity = _time.time()
                break


def _main():
    try:
        _attach(need_composer=_ACTION == "send")
        room, name, dot_ids = _resolve_room()
        _remember(room=room)
        try:
            _learn_profile_dir()
        except Exception:  # noqa: BLE001 — 只是给以后兜底用的，学不到不影响这次
            pass
        _emit("room", room=room, name=name)
        if _ACTION == "status":
            label = _busy()
            _emit("done", reason="status", last_id=_latest_id(room), busy=bool(label), label=label)
        elif _ACTION == "screen":
            _emit("screen", files=_screen_files(delay=0))
            _emit("done", reason="screen", last_id="", busy=False)
        elif _ACTION == "stream":
            cursor = _AFTER or _latest_id(room)
            _emit("cursor", id=cursor)
            try:
                _ensure_panel()  # 用户要电脑面板默认开着（10-08）；截图前还会再确认一次
            except Exception:  # noqa: BLE001
                pass
            _stream(room, dot_ids, cursor)
        else:
            with open(_env("DOTS_PROMPT_FILE"), encoding="utf-8") as f:
                text = f.read()
            files = [p for p in _env("DOTS_FILES").split("\n") if p.strip()]
            mid = _send(room, dot_ids, text, files)
            _emit("sent", id=mid)
            _emit("done", reason="sent", last_id=mid, busy=False)
    except _DotsError as e:
        _emit("error", code=e.code, msg=str(e))
    except Exception as e:  # noqa: BLE001 — 任何意外都要以事件形式交回 runner
        _emit("error", code="unexpected", msg="%s: %s" % (type(e).__name__, str(e)[:400]))
    finally:
        _detach()


_main()
