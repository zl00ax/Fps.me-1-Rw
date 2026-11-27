import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

from patchright.sync_api import sync_playwright

from singbox_helper import SingBoxManager
from telegram_notifier import send_telegram

PANEL_BASE = "https://panel.fps.ms"
SCREENSHOT_PATH = "/tmp/fps_renew_result.png"


def human_sleep(min_s: float = 1.2, max_s: float = 3.2):
    time.sleep(random.uniform(min_s, max_s))


def parse_cookies(raw: str) -> list[dict]:
    """支持 Cookie-Editor 导出的 JSON 数组，或标准 Cookie Header 字符串。"""
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        items = json.loads(raw)
        result = []
        for c in items:
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", "panel.fps.ms"),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            }
            ss = str(c.get("sameSite", "Lax")).capitalize()
            if ss in ("Strict", "Lax", "None"):
                cookie["sameSite"] = ss
            result.append(cookie)
        return result

    # 兼容 "a=1; b=2" 格式
    result = []
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            result.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": "panel.fps.ms",
                "path": "/",
                "secure": True,
            })
    return result


def dismiss_cookie_banner(page):
    for text in ("Accept optional", "Reject optional"):
        btn = page.get_by_role("button", name=text)
        try:
            if btn.is_visible(timeout=1500):
                btn.click()
                human_sleep(0.5, 1.0)
                return
        except Exception:
            pass


def try_pass_turnstile(page, timeout_s: int = 25) -> bool:
    """等待 Cloudflare Turnstile 自动通过，必要时模拟点击复选框。"""
    print("[Turnstile] 正在检测人机验证状态...")
    deadline = time.time() + timeout_s
    clicked = False

    while time.time() < deadline:
        # 检查隐藏 token 输入框是否已获得值
        token = page.evaluate(
            """() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                return el ? el.value : '';
            }"""
        )
        if token and len(token) > 20:
            print("[Turnstile] 验证已通过")
            return True

        # 尝试点击 Turnstile iframe 内的复选框区域（仅点一次，避免频繁触发风控）
        if not clicked:
            iframe_loc = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            try:
                if iframe_loc.is_visible(timeout=1000):
                    box = iframe_loc.bounding_box()
                    if box and box["width"] > 0:
                        target_x = box["x"] + random.uniform(25, 38)
                        target_y = box["y"] + box["height"] / 2 + random.uniform(-4, 4)
                        page.mouse.move(target_x, target_y, steps=random.randint(12, 22))
                        human_sleep(0.3, 0.7)
                        page.mouse.click(target_x, target_y)
                        clicked = True
                        print("[Turnstile] 已模拟点击验证框")
            except Exception:
                pass

        time.sleep(1.5)

    print("[Turnstile] 等待验证超时")
    return False


def login_with_credentials(page, email: str, password: str) -> bool:
    print("[Login] 尝试通过邮箱密码登录...")
    if "/auth/login" not in page.url:
        page.goto(f"{PANEL_BASE}/auth/login", wait_until="domcontentloaded", timeout=45000)
        human_sleep(2.0, 4.0)

    dismiss_cookie_banner(page)

    email_input = page.locator('input[type="email"], input[name="email"], input[placeholder*="Email" i]').first
    pwd_input = page.locator('input[type="password"], input[name="password"]').first

    email_input.wait_for(state="visible", timeout=15000)
    email_input.click()
    email_input.type(email, delay=random.randint(55, 130))
    human_sleep(0.6, 1.3)

    pwd_input.click()
    pwd_input.type(password, delay=random.randint(60, 140))
    human_sleep(1.0, 2.0)

    # 等待 Turnstile 验证完成
    has_turnstile = page.locator('iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]').count() > 0
    if has_turnstile:
        try_pass_turnstile(page, timeout_s=28)
        human_sleep(0.8, 1.6)

    sign_in_btn = page.get_by_role("button", name=re.compile(r"sign\s*in", re.I)).first
    sign_in_btn.click()

    # 等待跳转离开登录页
    try:
        page.wait_for_url(lambda u: "/auth/login" not in u, timeout=25000)
        human_sleep(2.0, 3.5)
        print(f"[Login] 登录成功，当前页面: {page.url}")
        return True
    except Exception:
        print(f"[Login] 登录未跳转，当前页面: {page.url}")
        return False


def extract_remaining_text(page) -> str:
    body = page.inner_text("body")
    patterns = [
        r"\d+h\s*\d+m\s*remaining",
        r"\d+\s*hours?\s*\d*\s*minutes?\s*remaining",
        r"剩余时间[:：]?\s*[^\n]+",
        r"Expires?[:：]?\s*[^\n]+",
    ]
    for pat in patterns:
        m = re.search(pat, body, re.I)
        if m:
            return m.group(0).strip()
    return "未识别到剩余时间文本"


def find_renew_button(page):
    candidates = [
        page.get_by_role("button", name=re.compile(r"\+\s*Add\s*\d+\s*hours?", re.I)),
        page.get_by_role("button", name=re.compile(r"Add\s*\d+\s*hours?", re.I)),
        page.get_by_role("button", name=re.compile(r"Renew", re.I)),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0 and loc.first.is_visible(timeout=2000):
                return loc.first
        except Exception:
            continue
    return None


def main():
    # 非手动触发时增加随机延迟，避免固定时间点特征
    if os.getenv("SKIP_JITTER", "").lower() != "true":
        jitter = random.randint(180, 900)
        print(f"[Anti-Ban] 随机等待 {jitter} 秒后开始执行...")
        time.sleep(jitter)

    proxy_uri = os.getenv("PROXY_URL", "").strip()
    cookies_raw = os.getenv("FPS_COOKIES", "").strip()
    email = os.getenv("FPS_EMAIL", "").strip()
    password = os.getenv("FPS_PASSWORD", "").strip()
    server_id = os.getenv("FPS_SERVER_ID", "").strip()

    if not cookies_raw and not (email and password):
        raise SystemExit("错误：请至少配置 FPS_COOKIES 或 FPS_EMAIL + FPS_PASSWORD")

    singbox = None
    exit_ip = "直连 (未配置代理)"
    proxy_port = 10808

    try:
        if proxy_uri:
            singbox = SingBoxManager(proxy_uri, local_port=proxy_port)
            exit_ip = singbox.start()

        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            ]
            proxy_cfg = None
            if proxy_uri:
                proxy_cfg = {"server": f"socks5://127.0.0.1:{proxy_port}"}
                # 强制 DNS 通过代理远程解析，杜绝本地 DNS 泄漏真实机房 IP
                launch_args.append("--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1")

            browser = p.chromium.launch(
                headless=False,  # 配合 xvfb-run 运行真实有头模式
                args=launch_args,
                proxy=proxy_cfg,
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="Europe/Amsterdam",
            )

            if cookies_raw:
                parsed_cookies = parse_cookies(cookies_raw)
                if parsed_cookies:
                    context.add_cookies(parsed_cookies)
                    print(f"[Auth] 已注入 {len(parsed_cookies)} 条 Cookie")

            page = context.new_page()

            # 1. 访问 Dashboard（自然入口）
            print("[Step 1] 访问 Dashboard...")
            page.goto(f"{PANEL_BASE}/dashboard", wait_until="domcontentloaded", timeout=45000)
            human_sleep(2.5, 4.5)
            dismiss_cookie_banner(page)

            # 2. 检查是否需要登录
            if "/auth/login" in page.url:
                print("[Auth] Cookie 无效或未提供，切换到账号密码登录")
                if not (email and password):
                    page.screenshot(path=SCREENSHOT_PATH, full_page=False)
                    send_telegram(
                        f"⚠️ <b>FPS.ms 续期失败</b>\n"
                        f"原因：Cookie 已过期且未配置账号密码\n"
                        f"出口 IP：<code>{exit_ip}</code>",
                        SCREENSHOT_PATH,
                    )
                    sys.exit(1)

                if not login_with_credentials(page, email, password):
                    page.screenshot(path=SCREENSHOT_PATH, full_page=False)
                    send_telegram(
                        f"❌ <b>FPS.ms 登录失败（已止损退出）</b>\n"
                        f"原因：可能被 Cloudflare Turnstile 拦截或凭据错误，建议手动更新 FPS_COOKIES\n"
                        f"出口 IP：<code>{exit_ip}</code>",
                        SCREENSHOT_PATH,
                    )
                    sys.exit(1)

            # 3. 定位目标服务器并按自然路径进入控制台
            print("[Step 2] 进入服务器控制台...")
            if not server_id:
                manage_btn = page.get_by_role("link", name=re.compile(r"Manage", re.I)).first
                if manage_btn.count() == 0:
                    manage_btn = page.get_by_role("button", name=re.compile(r"Manage", re.I)).first
                manage_btn.click()
                page.wait_for_url(re.compile(r"/server/[a-zA-Z0-9]+"), timeout=25000)
                m = re.search(r"/server/([a-zA-Z0-9]+)", page.url)
                server_id = m.group(1) if m else "unknown"
            else:
                page.goto(f"{PANEL_BASE}/server/{server_id}", wait_until="domcontentloaded", timeout=45000)

            human_sleep(2.5, 4.5)
            dismiss_cookie_banner(page)
            print(f"[Step 2] 当前服务器 ID: {server_id}")

            # 4. 跳转到 Billing 页面（优先模拟点击页面内的 Renew on the Billing page 按钮）
            print("[Step 3] 进入 Billing 续期页面...")
            billing_link = page.locator('a[href*="/billing"], button:has-text("Renew on the Billing page")').first
            try:
                if billing_link.is_visible(timeout=3000):
                    billing_link.click()
                    page.wait_for_url(re.compile(r"/billing"), timeout=20000)
                else:
                    page.goto(f"{PANEL_BASE}/server/{server_id}/billing", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                page.goto(f"{PANEL_BASE}/server/{server_id}/billing", wait_until="domcontentloaded", timeout=45000)

            human_sleep(2.5, 4.5)
            dismiss_cookie_banner(page)

            before_time = extract_remaining_text(page)
            print(f"[Step 4] 续期前状态: {before_time}")

            renew_btn = find_renew_button(page)
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            if not renew_btn:
                page.screenshot(path=SCREENSHOT_PATH, full_page=False)
                send_telegram(
                    f"⚠️ <b>FPS.ms 未找到续期按钮</b>\n"
                    f"服务器：<code>{server_id}</code>\n"
                    f"当前状态：{before_time}\n"
                    f"出口 IP：<code>{exit_ip}</code>\n"
                    f"时间：{now_utc}",
                    SCREENSHOT_PATH,
                )
                sys.exit(1)

            if renew_btn.is_disabled():
                page.screenshot(path=SCREENSHOT_PATH, full_page=False)
                send_telegram(
                    f"ℹ️ <b>FPS.ms 暂无需续期</b>\n"
                    f"服务器：<code>{server_id}</code>\n"
                    f"状态：续期按钮当前不可用（尚未进入 24h 续期窗口）\n"
                    f"剩余时间：{before_time}\n"
                    f"出口 IP：<code>{exit_ip}</code>\n"
                    f"时间：{now_utc}",
                    SCREENSHOT_PATH,
                )
                print("[Done] 按钮不可用，跳过本次续期")
                return

            # 5. 模拟人类移动并点击续期按钮
            print("[Step 5] 点击续期按钮...")
            box = renew_btn.bounding_box()
            if box:
                page.mouse.move(
                    box["x"] + box["width"] / 2 + random.uniform(-10, 10),
                    box["y"] + box["height"] / 2 + random.uniform(-4, 4),
                    steps=random.randint(10, 18),
                )
                human_sleep(0.3, 0.8)
            renew_btn.click()

            # 等待接口返回及页面更新
            human_sleep(4.5, 6.5)
            page.reload(wait_until="domcontentloaded", timeout=30000)
            human_sleep(2.0, 3.0)

            after_time = extract_remaining_text(page)
            page.screenshot(path=SCREENSHOT_PATH, full_page=False)
            print(f"[Done] 续期后状态: {after_time}")

            send_telegram(
                f"✅ <b>FPS.ms 免费服务器续期完成</b>\n"
                f"服务器：<code>{server_id}</code>\n"
                f"续期前：{before_time}\n"
                f"续期后：{after_time}\n"
                f"出口 IP：<code>{exit_ip}</code>\n"
                f"时间：{now_utc}",
                SCREENSHOT_PATH,
            )

            browser.close()

    except Exception as e:
        print(f"[Error] 运行异常: {e}")
        send_telegram(
            f"❌ <b>FPS.ms 续期脚本运行异常</b>\n"
            f"错误：<code>{str(e)[:300]}</code>\n"
            f"出口 IP：<code>{exit_ip}</code>",
            SCREENSHOT_PATH if os.path.exists(SCREENSHOT_PATH) else None,
        )
        sys.exit(1)
    finally:
        if singbox:
            singbox.stop()


if __name__ == "__main__":
    main()
