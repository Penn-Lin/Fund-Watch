# -*- coding: utf-8 -*-
"""用 CDP 真实打开生产页面，模拟点击「浏览器推送」开关，捕获全部反应。
回答：点开关到底有没有反应？走到哪一步？"""
import json, subprocess, time, sys
import requests
import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = r"C:\Users\Zupeng Lin\WorkBuddy\基金涨跌监控\.chrome-test-profile"
PORT = 9333
URL = "https://fund-watch.onrender.com/"

# 1. 启动带远程调试的 headless Chrome
proc = subprocess.Popen([
    CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
    f"--remote-debugging-port={PORT}",
    "--remote-allow-origins=*",
    f"--user-data-dir={PROFILE}",
    "--window-size=412,915",
    "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    # 等调试端口就绪
    ws_url = None
    for _ in range(30):
        time.sleep(1)
        try:
            tabs = requests.get(f"http://127.0.0.1:{PORT}/json", timeout=3).json()
            pages = [t for t in tabs if t.get("type") == "page"]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                break
        except Exception:
            continue
    if not ws_url:
        print("FATAL: 无法连接 Chrome 调试端口"); sys.exit(1)

    ws = websocket.create_connection(ws_url, timeout=30)
    mid = [0]
    console_logs, exceptions = [], []

    def cmd(method, **params):
        mid[0] += 1
        ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            m = msg.get("method", "")
            if m == "Runtime.consoleAPICalled":
                console_logs.append({
                    "type": msg["params"]["type"],
                    "text": " ".join(str(a.get("value", a.get("description", "")))
                                     for a in msg["params"]["args"])[:300],
                })
            elif m == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                exceptions.append(str(d.get("text", "")) + " " +
                                  str(d.get("exception", {}).get("description", ""))[:300])
            elif msg.get("id") == mid[0]:
                return msg.get("result", {})

    def ev(js_expr):
        r = cmd("Runtime.evaluate", expression=js_expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    cmd("Runtime.enable")
    cmd("Page.enable")
    cmd("Page.navigate", url=URL)
    time.sleep(12)  # 等 JS 全部执行完（含 initPush / 各 API 请求）

    # 2. 点击前的状态
    before = ev("""(() => ({
        status: document.getElementById('push-status').textContent,
        badge: document.getElementById('push-badge').textContent,
        checked: document.getElementById('cf-push').checked,
        disabled: document.getElementById('cf-push').disabled,
        perm: (typeof Notification !== 'undefined') ? Notification.permission : 'NO Notification API',
        swOk: 'serviceWorker' in navigator,
        pushOk: 'PushManager' in window,
        iframe: window.self !== window.top,
    }))()""")
    print("=== 点击前状态 ===")
    for k, v in before.items():
        print(f"  {k}: {v}")

    # 3. 模拟点击开关
    ev("document.getElementById('cf-push').click()")
    time.sleep(5)  # 等 change handler（requestPermission → subscribePush）跑完

    # 4. 点击后的状态
    after = ev("""(() => ({
        status: document.getElementById('push-status').textContent,
        badge: document.getElementById('push-badge').textContent,
        checked: document.getElementById('cf-push').checked,
        disabled: document.getElementById('cf-push').disabled,
        perm: (typeof Notification !== 'undefined') ? Notification.permission : 'NO Notification API',
        toast: document.getElementById('toast') ? document.getElementById('toast').textContent : '(无toast元素)',
        toastVisible: document.getElementById('toast') ? document.getElementById('toast').classList.contains('show') : false,
    }))()""")
    print("\n=== 点击后状态 ===")
    for k, v in after.items():
        print(f"  {k}: {v}")

    # 5. 直接调用 subscribePush 内部路径，看具体异常
    diag = ev("""(async () => {
        try {
            const reg = await navigator.serviceWorker.ready;
            const sub = await reg.pushManager.getSubscription();
            let subErr = null;
            try {
                const r = await fetch('/api/vapid_public_key').then(x => x.json());
                await reg.pushManager.subscribe({ userVisibleOnly: true,
                    applicationServerKey: Uint8Array.from(atob((r.public_key + '='.repeat((4 - r.public_key.length % 4) % 4)).replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)) });
            } catch (e) { subErr = e.name + ': ' + e.message; }
            return { swScope: reg.scope, swActive: !!reg.active, existingSub: !!sub, subscribeErr: subErr };
        } catch (e) { return { outerErr: e.name + ': ' + e.message }; }
    })()""")
    print("\n=== 订阅链路诊断 ===")
    for k, v in diag.items():
        print(f"  {k}: {v}")

    print("\n=== 控制台消息 (%d条) ===" % len(console_logs))
    for c in console_logs[:15]:
        print(f"  [{c['type']}] {c['text'][:150]}")
    print("\n=== 未捕获异常 (%d个) ===" % len(exceptions))
    for e in exceptions[:10]:
        print(f"  {e[:200]}")

    ws.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
