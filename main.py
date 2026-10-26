#!/usr/bin/env python3
"""
Godlike 主机自动续期 + 开机脚本 (增强修复版)
"""

import os, sys, time, traceback, random, json, re, asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

import requests
import websockets
from playwright.sync_api import sync_playwright

# ---------- 配置 ----------
FRONT_BASE = "https://ultra.panel.godlike.host"
API_BASE = "https://panel.godlike.host/api/v2"
LOGIN_URL = f"{FRONT_BASE}/login"
OUTPUT_DIR = Path("scripts/Godlike")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CN_TZ = timezone(timedelta(hours=8))

UUID_PATTERN = re.compile(
    r'\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b',
    re.IGNORECASE
)

# ---------- 工具函数 ----------
def cn_time():
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

def mask_email(email: str) -> str:
    """仅用于工作流日志"""
    if not email or "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    domain_parts = domain.split(".")
    masked_domain = "***." + domain_parts[-1] if len(domain_parts) >= 2 else "***"
    return f"{user[:3]}***@{masked_domain}"

def mask_server(server_id: str) -> str:
    """服务器ID脱敏：只显示前3位"""
    if not server_id:
        return "***"
    return f"{server_id[:3]}***"

def get_proxies(proxy: str = None) -> Optional[dict]:
    if not proxy:
        proxy = os.environ.get("PROXY_SERVER", "").strip()
    if proxy:
        return {"http": proxy, "https": proxy}
    return None

def notify_tg(ok: bool, email: str = "", server: str = "",
              before: str = "", after: str = "",
              cooldown: str = "", remaining: str = "",
              start_note: str = "",
              error_msg: str = "", screenshot: str = None):
    """TG通知使用真实邮箱和服务器ID"""
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return

    if after and not cooldown and not start_note:
        parts = after.split("\n", 1)
        cooldown = parts[0]
        if len(parts) > 1:
            start_note = parts[1]

    if ok:
        msg = (
            f"🎮Godlike 续期通知\n"
            f"⏰运行时间: {cn_time()}\n"
        )
        if email:
            msg += f"🖥️账号: {email}\n"
        msg += f"🖥️服务器: {server}\n"
        if cooldown:
            msg += f"🔢下次可续期: {cooldown}\n"
        if remaining and remaining != cooldown:
            msg += f"⏳剩余时间: {remaining}\n"
        if start_note:
            msg += f"📊续期结果: ✅续期成功\n"
            msg += f"📊开机状态: {start_note}"
        else:
            msg += f"📊续期结果: ✅续期成功"
    else:
        msg = (
            f"🎮Godlike 续期通知\n"
            f"⏰运行时间: {cn_time()}\n"
        )
        if email:
            msg += f"🖥️账号: {email}\n"
        msg += f"🖥️服务器: {server}\n"
        msg += f"📊续期结果: ❌操作失败!\n"
        msg += f"📊原因: {error_msg}"

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
            timeout=30)
        print("[INFO] TG 通知已发送", flush=True)
    except Exception as e:
        print(f"[WARN] TG 通知发送失败: {e}", flush=True)

# ---------- API 公共 headers ----------
def api_headers(bearer_token: str) -> dict:
    return {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": FRONT_BASE,
        "Referer": f"{FRONT_BASE}/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

# ---------- 从 JSON 结构中递归提取服务器 UUID ----------
def extract_uuid_from_json(data) -> Tuple[Optional[str], Optional[str]]:
    """从 API 响应 JSON 中提取 full_uuid 和 short_id"""
    if isinstance(data, dict):
        # 常见 Pterodactyl / Godlike 服务器对象结构
        attrs = data.get("attributes", data)
        if isinstance(attrs, dict):
            u = attrs.get("uuid") or attrs.get("server_uuid")
            sid = attrs.get("identifier") or attrs.get("short_id") or attrs.get("uuidShort")
            if isinstance(u, str) and UUID_PATTERN.fullmatch(u):
                return u, (sid if isinstance(sid, str) and len(sid) == 8 else u.split("-")[0])

        for k, v in data.items():
            if k in ("uuid", "server_uuid", "serverId") and isinstance(v, str) and UUID_PATTERN.fullmatch(v):
                return v, v.split("-")[0]
            res_u, res_s = extract_uuid_from_json(v)
            if res_u:
                return res_u, res_s

    elif isinstance(data, list):
        for item in data:
            res_u, res_s = extract_uuid_from_json(item)
            if res_u:
                return res_u, res_s

    return None, None

# ---------- 主动请求 API 获取服务器列表 (核心兜底) ----------
def fetch_server_from_api(bearer_token: str, short_id: str = None, proxy: str = None) -> Tuple[Optional[str], Optional[str], bool]:
    """
    通过 Bearer Token 直接查询 API 获取服务器 UUID
    返回: (full_uuid, short_id, is_empty_account)
    """
    headers = api_headers(bearer_token)
    proxies = get_proxies(proxy)

    candidate_urls = [
        f"{API_BASE}/servers?page=1",
        f"{API_BASE}/servers",
        "https://panel.godlike.host/api/client?page=1",
        "https://panel.godlike.host/api/client",
        f"{FRONT_BASE}/api/client?page=1",
        f"{FRONT_BASE}/api/v2/servers?page=1",
    ]
    if short_id:
        candidate_urls.insert(0, f"{API_BASE}/servers/{short_id}")
        candidate_urls.insert(1, f"https://panel.godlike.host/api/client/servers/{short_id}")

    empty_account_detected = False

    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=15)
            if resp.status_code != 200:
                continue

            try:
                data = resp.json()
            except Exception:
                continue

            # 1. 尝试结构化提取
            u, s = extract_uuid_from_json(data)
            if u:
                print(f"[DEBUG] 通过 API ({url}) 成功获取到服务器 UUID", flush=True)
                return u, s, False

            # 2. 检查是否明确返回了空的服务器列表
            if isinstance(data, dict):
                arr = data.get("data")
                if isinstance(arr, list) and len(arr) == 0:
                    empty_account_detected = True

            # 3. 退化为正则全文匹配（排除 user 自身的 uuid）
            text = resp.text
            if short_id:
                m = re.search(rf'{re.escape(short_id)}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}', text, re.I)
                if m:
                    return m.group(0), short_id, False

        except Exception as e:
            print(f"[DEBUG] 请求 {url} 异常: {e}", flush=True)

    return None, short_id, empty_account_detected

# ---------- 登录并获取 Bearer Token + UUID ----------
def login_and_get_token(user: str, pwd: str, proxy: str = None) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
    """
    返回: (bearer_token, full_uuid, short_id, error_reason)
    """
    bearer_token = None
    full_uuid = None
    short_id = None

    def on_request(request):
        nonlocal bearer_token, full_uuid, short_id
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and "ptlc_" in auth:
            token = auth.replace("Bearer ", "").strip()
            if bearer_token != token:
                bearer_token = token
        url = request.url
        m = re.search(r'/servers/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url, re.I)
        if m and not full_uuid:
            full_uuid = m.group(1)
            short_id = full_uuid.split('-')[0]

    def on_response(response):
        nonlocal full_uuid, short_id
        if full_uuid:
            return
        url = response.url
        if any(k in url for k in ("/api/", "/servers", "/client")):
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype:
                    data = response.json()
                    u, s = extract_uuid_from_json(data)
                    if u:
                        full_uuid = u
                        short_id = s or u.split("-")[0]
                        print(f"[DEBUG] 从响应拦截中获取 UUID: {mask_server(short_id)}", flush=True)
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        try:
            print(f"[DEBUG] 正在打开登录页: {LOGIN_URL}", flush=True)
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            print(f"[DEBUG] 当前 URL: {page.url}", flush=True)

            switch = page.locator('button:has-text("Through login/password")')
            try:
                switch.wait_for(state="visible", timeout=15000)
                switch.click()
                print("[DEBUG] 已点击 'Through login/password' 按钮", flush=True)
            except Exception as e:
                print(f"[ERROR] 未找到 'Through login/password' 按钮: {e}", flush=True)
                try:
                    page.screenshot(path="login_no_switch_btn.png")
                except Exception:
                    pass
                return None, None, None, "未找到登录切换按钮(可能遇到Cloudflare拦截)"

            page.wait_for_timeout(2000)

            try:
                page.locator('input[placeholder="Username or Email"]').wait_for(state="visible", timeout=15000)
                page.locator('input[placeholder="Password"]').wait_for(state="visible", timeout=15000)
                print("[DEBUG] 找到邮箱和密码输入框", flush=True)
            except Exception as e:
                print(f"[ERROR] 未找到输入框: {e}", flush=True)
                try:
                    page.screenshot(path="login_no_input.png")
                except Exception:
                    pass
                return None, None, None, "未找到账号密码输入框"

            page.fill('input[placeholder="Username or Email"]', user)
            page.fill('input[placeholder="Password"]', pwd)
            print("[DEBUG] 已填写邮箱和密码", flush=True)

            login_clicked = False
            for sel in ['button[type="submit"]', 'button:has-text("Login")']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        btn.click(timeout=5000)
                        login_clicked = True
                        print(f"[DEBUG] 已点击登录按钮: {sel}", flush=True)
                        break
                except Exception:
                    pass

            if not login_clicked:
                print("[ERROR] 未找到登录按钮", flush=True)
                return None, None, None, "未找到登录提交按钮"

            # 等待跳转并让网络请求完成
            page.wait_for_timeout(6000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            print(f"[DEBUG] 登录后 URL: {page.url}", flush=True)

            if '/login' in page.url:
                print("[ERROR] 登录后仍在登录页，登录被拒绝", flush=True)
                try:
                    page.screenshot(path="login_rejected.png", full_page=True)
                except Exception:
                    pass
                return None, None, None, "登录被拒绝(密码错误或触发验证码)"

            # 尝试从存储中兜底提取 ptlc_ token
            if not bearer_token:
                try:
                    storage_text = page.evaluate(
                        "() => JSON.stringify(Object.assign({}, localStorage, sessionStorage)) + ' ' + document.cookie"
                    )
                    m_tok = re.search(r'(ptlc_[A-Za-z0-9_\-]+)', storage_text)
                    if m_tok:
                        bearer_token = m_tok.group(1)
                        print("[DEBUG] 从浏览器 Storage 中提取到 bearer_token", flush=True)
                except Exception:
                    pass

            # 尝试点击弹窗或引导按钮，并检测服务器卡片
            for _ in range(8):
                if full_uuid:
                    break
                for sel in [
                    'button:has-text("Go to my server")',
                    'button:has-text("Skip")',
                    'a[href*="/server/"]',
                ]:
                    try:
                        el = page.locator(sel)
                        if el.count() > 0 and el.first.is_visible():
                            href = el.first.get_attribute("href") or ""
                            if "/server/" in href and not short_id:
                                parts = href.rstrip('/').split('/')
                                for j, p_part in enumerate(parts):
                                    if p_part == 'server' and j + 1 < len(parts) and len(parts[j + 1]) == 8:
                                        short_id = parts[j + 1]
                                        break
                            el.first.click(timeout=3000)
                            print(f"[DEBUG] 已点击页面元素: {sel}", flush=True)
                            page.wait_for_timeout(2500)
                            break
                    except Exception:
                        pass

                if '/server/' in page.url:
                    print(f"[DEBUG] 已进入服务器页: {page.url}", flush=True)
                    parts = page.url.rstrip('/').split('/')
                    for i, p_part in enumerate(parts):
                        if p_part == 'server' and i + 1 < len(parts) and len(parts[i + 1]) == 8:
                            short_id = parts[i + 1]
                            break
                    break
                page.wait_for_timeout(1000)

            # 如果拿到了 short_id 但还没进服务器详情页，主动跳转触发详情 API
            if short_id and not full_uuid:
                try:
                    print(f"[DEBUG] 主动访问服务器详情页: {FRONT_BASE}/server/{short_id}", flush=True)
                    page.goto(f"{FRONT_BASE}/server/{short_id}", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(4000)
                except Exception:
                    pass

            # 从网页 HTML 源码正则兜底匹配
            if not full_uuid:
                try:
                    html = page.content()
                    if short_id:
                        m = re.search(
                            rf'{re.escape(short_id)}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}',
                            html, re.I
                        )
                        if m:
                            full_uuid = m.group(0)
                    else:
                        # 查找页面中是否存在 href="/server/xxxxxxxx"
                        m_short = re.search(r'/server/([0-9a-f]{8})\b', html, re.I)
                        if m_short:
                            short_id = m_short.group(1)
                            m_full = re.search(
                                rf'{re.escape(short_id)}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}',
                                html, re.I
                            )
                            if m_full:
                                full_uuid = m_full.group(0)
                except Exception:
                    pass

            # 如果依然没拿到 UUID，但已经有了 bearer_token，直接调后端 API 查询！
            empty_account = False
            if bearer_token and not full_uuid:
                print("[DEBUG] 正在通过 Bearer Token 主动查询服务器列表 API...", flush=True)
                api_uuid, api_short, empty_account = fetch_server_from_api(bearer_token, short_id, proxy)
                if api_uuid:
                    full_uuid = api_uuid
                    short_id = api_short or full_uuid.split('-')[0]

            # 如果还是失败，截图留存并在日志打印页面可见文字方便排查
            if not full_uuid:
                try:
                    snap_file = f"no_uuid_{int(time.time())}.png"
                    page.screenshot(path=snap_file, full_page=True)
                    body_preview = page.locator("body").inner_text(timeout=3000)
                    clean_preview = " | ".join([line.strip() for line in body_preview.splitlines() if line.strip()][:15])
                    print(f"[DEBUG] 未获取到 UUID 时页面文本预览: {clean_preview}", flush=True)
                except Exception:
                    pass

            print(f"[DEBUG] 最终 URL: {page.url}", flush=True)
            print(f"[DEBUG] bearer_token: {'已获取' if bearer_token else '未获取'}, short_id: {short_id or '未获取'}", flush=True)

            if empty_account and not full_uuid:
                return bearer_token, None, None, "该账号下暂无服务器(可能未创建或过期已被系统回收)"

            return bearer_token, full_uuid, short_id, ("未能获取服务器 UUID" if not full_uuid else "")

        except Exception as e:
            print(f"[ERROR] 登录异常: {e}", flush=True)
            traceback.print_exc()
            return None, None, None, f"登录异常: {str(e)[:100]}"
        finally:
            context.close()
            browser.close()

# ---------- 检查续期状态 ----------
def check_video_status(full_uuid: str, bearer_token: str, proxy: str = None) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/status?type=youtube_iter1&locale=en"
    try:
        resp = requests.get(url, headers=api_headers(bearer_token), proxies=get_proxies(proxy), timeout=30)
        return resp.json()
    except Exception as e:
        print(f"[ERROR] 状态查询失败: {e}", flush=True)
        return {}

# ---------- 调用 start API ----------
def call_video_start(full_uuid: str, bearer_token: str, proxy: str = None) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/start?locale=en"
    body = {"type": "youtube_iter1"}
    try:
        resp = requests.post(url, headers=api_headers(bearer_token), json=body, proxies=get_proxies(proxy), timeout=30)
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Start API失败: {e}", flush=True)
        return {}

# ---------- 单次 update-time ----------
def call_update_once(full_uuid: str, bearer_token: str,
                     watch_uuid: str, renewal_id: int,
                     video_time: int, proxy: str = None) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/update-time?locale=en"
    body = {
        "uuid": watch_uuid,
        "renewal_uuid": watch_uuid,
        "renewal_id": renewal_id,
        "video_time_watched": video_time,
    }
    try:
        resp = requests.post(url, headers=api_headers(bearer_token), json=body, proxies=get_proxies(proxy), timeout=30)
        return resp.json()
    except Exception as e:
        print(f"[ERROR] update-time异常: {e}", flush=True)
        return {}

# ---------- 模拟视频观看 ----------
def simulate_video_watching(full_uuid: str, bearer_token: str,
                             watch_uuid: str, renewal_id: int, proxy: str = None) -> Tuple[bool, str]:
    steps = [30, 60, 90, 120, 150, 180, 210, 240]
    wait_seconds = 28
    total = len(steps) * wait_seconds

    print(f"[INFO] 开始续期（共{len(steps)}步 × {wait_seconds}s，预计{total}s）", flush=True)

    last_resp = {}
    for i, video_time in enumerate(steps):
        print(f"[INFO] 步骤 {i+1}/{len(steps)} 等待{wait_seconds}s → 上报{video_time}s", flush=True)
        time.sleep(wait_seconds)

        resp = call_update_once(full_uuid, bearer_token, watch_uuid, renewal_id, video_time, proxy)
        last_resp = resp

        success = resp.get("success", False)
        msg = resp.get("message", "")
        new_timer = resp.get("new_free_timer")

        if not success:
            if "Invalid time increment" in msg:
                current = resp.get("current_time", 0)
                if current > 0:
                    adjusted = current + 30
                    print(f"[WARN] 时间偏差，调整为{adjusted}s重试", flush=True)
                    time.sleep(2)
                    resp2 = call_update_once(full_uuid, bearer_token, watch_uuid, renewal_id, adjusted, proxy)
                    if resp2.get("success"):
                        last_resp = resp2
                        new_timer = resp2.get("new_free_timer")
                        steps[i+1:] = [adjusted + 30*(j+1) for j in range(len(steps)-i-1)]
                        if new_timer:
                            print(f"[INFO] ✅ 续期完成 new_free_timer={new_timer}", flush=True)
                            return True, new_timer
                        continue
                    return False, resp2.get("message", "重试失败")
            return False, msg

        if new_timer:
            print(f"[INFO] ✅ 续期完成 new_free_timer={new_timer}", flush=True)
            return True, new_timer

    new_timer = last_resp.get("new_free_timer")
    if last_resp.get("success"):
        return True, new_timer or "续期已提交"
    return False, last_resp.get("message", "未知错误")

# ---------- 获取 WS JWT ----------
def get_websocket_credentials(full_uuid: str, bearer_token: str, proxy: str = None) -> Tuple[Optional[str], Optional[str]]:
    url = f"{API_BASE}/servers/{full_uuid}/websocket?locale=en"
    try:
        resp = requests.get(url, headers=api_headers(bearer_token), proxies=get_proxies(proxy), timeout=30)
        if resp.status_code == 200:
            data = resp.json().get("data", resp.json())
            return data.get("token"), data.get("socket")
        print(f"[ERROR] WS凭证获取失败: {resp.status_code}", flush=True)
        return None, None
    except Exception as e:
        print(f"[ERROR] 获取WS凭证异常: {e}", flush=True)
        return None, None

# ---------- WebSocket 开机 ----------
async def ws_start_server(socket_url: str, jwt: str) -> str:
    print(f"[INFO] 连接 WebSocket...", flush=True)
    try:
        async with websockets.connect(
            socket_url,
            origin="https://ultra.panel.godlike.host",
            additional_headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            },
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as ws:
            await ws.send(json.dumps({"event": "auth", "args": [jwt]}))

            auth_success = False
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(msg)
                    if data.get("event") == "auth success":
                        auth_success = True
                        print("[INFO] ✅ WS认证成功", flush=True)
                        break
                except asyncio.TimeoutError:
                    break

            if not auth_success:
                print("[ERROR] WS认证失败", flush=True)
                return "auth_failed"

            current_status = "unknown"
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    data = json.loads(msg)
                    event = data.get("event", "")

                    if event == "status":
                        current_status = data.get("args", ["unknown"])[0]
                        print(f"[INFO] 服务器当前状态: {current_status}", flush=True)
                        break

                    elif event == "stats":
                        try:
                            state = json.loads(data["args"][0]).get("state", "")
                            if state:
                                current_status = state
                                print(f"[INFO] 服务器当前状态: {current_status}", flush=True)
                                break
                        except Exception:
                            pass

                except asyncio.TimeoutError:
                    break
                except Exception:
                    break

            if current_status in ("running", "starting"):
                print(f"[INFO] 服务器已在运行中（{current_status}），无需开机", flush=True)
                return "already_running"

            await ws.send(json.dumps({"event": "set state", "args": ["start"]}))
            print("[INFO] 已发送开机指令，等待启动...", flush=True)

            for _ in range(60):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)
                    event = data.get("event", "")

                    if event == "status":
                        new_status = data.get("args", [""])[0]
                        print(f"[INFO] 状态变更: {new_status}", flush=True)
                        if new_status in ("starting", "running"):
                            print(f"[INFO] ✅ 服务器启动成功（{new_status}）", flush=True)
                            return "started"

                    elif event == "stats":
                        try:
                            state = json.loads(data["args"][0]).get("state", "")
                            if state in ("starting", "running"):
                                print(f"[INFO] ✅ 服务器启动成功（{state}）", flush=True)
                                return "started"
                        except Exception:
                            pass

                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

            print("[WARN] 等待超时，开机指令已发送", flush=True)
            return "sent"

    except Exception as e:
        print(f"[ERROR] WebSocket异常: {e}", flush=True)
        return "error"

def start_server_via_ws(full_uuid: str, bearer_token: str, proxy: str = None) -> str:
    jwt, socket_url = get_websocket_credentials(full_uuid, bearer_token, proxy)
    if not jwt or not socket_url:
        print("[ERROR] 无法获取WS凭证", flush=True)
        return "error"
    try:
        return asyncio.run(ws_start_server(socket_url, jwt))
    except Exception as e:
        print(f"[ERROR] WS开机异常: {e}", flush=True)
        return "error"

# ---------- 单账号主流程 ----------
def process_account(key: str, proxy: str = None) -> bool:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return True

    try:
        parts = raw.split("-----")
        user = parts[0].strip()
        pwd = parts[1].strip()
    except Exception:
        print(f"[ERROR] {key} 格式错误", flush=True)
        notify_tg(False, error_msg="格式错误")
        return False

    masked = mask_email(user)
    print(f"\n{'='*60}", flush=True)
    print(f"[INFO] 处理 {key} ({masked})", flush=True)
    print(f"{'='*60}", flush=True)

    # 1. 登录并获取 Token 和 UUID
    bearer_token, full_uuid, short_id, err_reason = login_and_get_token(user, pwd, proxy)
    if not bearer_token:
        msg = err_reason or "未能获取 Bearer Token"
        print(f"[ERROR] {msg}", flush=True)
        notify_tg(False, email=user, error_msg=msg)
        return False
    if not full_uuid:
        msg = err_reason or "未能获取服务器 UUID"
        print(f"[ERROR] {msg}", flush=True)
        notify_tg(False, email=user, error_msg=msg)
        return False

    if not short_id:
        short_id = full_uuid.split("-")[0]

    masked_server = mask_server(short_id)
    print(f"[INFO] 🔑 登录成功 | 服务器: {masked_server}", flush=True)

    # 2. 检查续期状态
    status = check_video_status(full_uuid, bearer_token, proxy)
    can_watch = status.get("can_watch", None)
    time_until_next = status.get("time_until_next_video") or 0

    renew_ok = False
    cooldown_after = ""

    if can_watch is False and time_until_next > 0:
        h = time_until_next // 3600
        m = (time_until_next % 3600) // 60
        cooldown_after = f"{h}h {m}m"
        print(f"[INFO] 已在冷却期，下次可续期: {cooldown_after}", flush=True)
        renew_ok = True
    else:
        # 3. 续期
        start_resp = call_video_start(full_uuid, bearer_token, proxy)
        if not start_resp.get("success"):
            err = start_resp.get("message", "start API失败")
            print(f"[ERROR] 续期启动失败: {err}", flush=True)
            notify_tg(False, email=user, server=short_id, error_msg=err)
            return False

        watch_uuid = start_resp.get("uuid", "")
        renewal_id = start_resp.get("renewal_id", 0)
        print(f"[INFO] 续期会话已建立 (renewal_id={renewal_id})", flush=True)

        renew_ok, result = simulate_video_watching(full_uuid, bearer_token, watch_uuid, renewal_id, proxy)

        if not renew_ok:
            print(f"[ERROR] 续期失败: {result}", flush=True)
            notify_tg(False, email=user, server=short_id, error_msg=result)
            return False

        time.sleep(2)
        status_after = check_video_status(full_uuid, bearer_token, proxy)
        time_until = status_after.get("time_until_next_video") or 0
        h = time_until // 3600
        m_min = (time_until % 3600) // 60
        cooldown_after = f"{h}h {m_min}m" if time_until > 0 else result
        print(f"[INFO] 续期成功，下次可续期: {cooldown_after}", flush=True)

    # 4. WS 开机
    print(f"[INFO] ── 开机 ──", flush=True)
    start_result = start_server_via_ws(full_uuid, bearer_token, proxy)

    start_note_map = {
        "already_running": "✅ 服务器已在运行中，无需开机",
        "started":         "✅ 开机成功",
        "sent":            "✅ 开机指令已发送（等待超时）",
        "auth_failed":     "⚠️ WS认证失败",
        "error":           "⚠️ 开机异常",
    }
    start_note = start_note_map.get(start_result, "⚠️ 未知状态")
    print(f"[INFO] {start_note}", flush=True)

    # 5. TG 通知
    notify_tg(
        ok=renew_ok,
        email=user,
        server=short_id,
        cooldown=cooldown_after,
        remaining=cooldown_after,
        start_note=start_note,
    )

    print(f"[INFO] ✅ {key} 处理完成", flush=True)
    return renew_ok

def main():
    proxy = os.environ.get("PROXY_SERVER", "").strip()
    if proxy:
        print(f"[INFO] 使用代理", flush=True)

    accounts = [f"GODLIKE_{i}" for i in range(1, 6)]
    all_ok = True
    for idx, acc in enumerate(accounts):
        try:
            ok = process_account(acc, proxy if proxy else None)
            if not ok:
                all_ok = False
        except Exception as e:
            print(f"[FATAL] {acc} 崩溃: {e}", flush=True)
            traceback.print_exc()
            notify_tg(False, email=acc, error_msg=f"脚本崩溃: {str(e)[:200]}")
            all_ok = False
        if idx < len(accounts) - 1:
            time.sleep(random.randint(5, 15))

    if all_ok:
        print("\n[INFO] 🎉 所有账号处理成功", flush=True)
        sys.exit(0)
    else:
        print("\n[ERROR] 部分账号失败", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
