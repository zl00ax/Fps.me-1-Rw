import hashlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

from patchright.sync_api import sync_playwright

from singbox_helper import SingBoxManager
from telegram_notifier import send_telegram

PANEL_BASE = "https://panel.fps.ms"

# 常见真实桌面分辨率池（每个账号根据名称稳定映射一个，避免每次特征跳变）
VIEWPORT_POOL = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1600, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]


def human_sleep(min_s: float = 1.2, max_s: float = 3.2):
    time.sleep(random.uniform(min_s, max_s))


def get_account_viewport(account_name: str) -> dict:
    idx = int(hashlib.sha256(account_name.encode("utf-8")).hexdigest(), 16) % len(VIEWPORT_POOL)
    return VIEWPORT_POOL[idx]


def parse_cookies(raw) -> list[dict]:
    """支持 JSON 数组、JSON 字符串或标准 Cookie Header 字符串。"""
    if not raw:
        return []

    items = None
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        if raw.startswith("["):
            items = json.loads(raw)
        else:
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

    result = []
    for c in items or []:
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
    print("  [Turnstile] 检测人机验证状态...")
    deadline = time.time() + timeout_s
    clicked = False

    while time.time() < deadline:
        token = page.evaluate(
            """() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                return el ? el.value : '';
            }"""
        )
        if token and len(token) > 20:
            print("  [Turnstile] 验证已通过")
            return True

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
                        print("  [Turnstile] 已模拟点击验证框")
            except Exception:
                pass

        time.sleep(1.5)

    print("  [Turnstile] 等待验证超时")
    return False


def login_with_credentials(page, email: str, password: str) -> bool:
    print("  [Login] 尝试邮箱密码登录...")
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

    has_turnstile = page.locator('iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]').count() > 0
    if has_turnstile:
        try_pass_turnstile(page, timeout_s=28)
        human_sleep(0.8, 1.6)

    sign_in_btn = page.get_by_role("button", name=re.compile(r"sign\s*in", re.I)).first
    sign_in_btn.click()

    try:
        page.wait_for_url(lambda u: "/auth/login" not in u, timeout=25000)
        human_sleep(2.0, 3.5)
        print(f"  [Login] 登录成功 -> {page.url}")
        return True
    except Exception:
        print(f"  [Login] 登录未跳转 -> {page.url}")
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
    return "未识别到剩余时间"


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


def load_accounts() -> list[dict]:
    raw = os.getenv("FPS_ACCOUNTS", "").strip()
    if not raw:
        raise SystemExit("错误：未配置 FPS_ACCOUNTS 环境变量")
    accounts = json.loads(raw)
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit("错误：FPS_ACCOUNTS 必须是非空的 JSON 数组")
    return accounts


def process_single_account(account: dict, local_port: int, used_exit_ips: dict[str, str]) -> tuple[bool, str]:
    name = str(account.get("name") or account.get("email") or f"acc-{local_port}")
    proxy_url = str(account.get("proxy_url") or "").strip()
    cookies = parse_cookies(account.get("cookies"))
    email = str(account.get("email") or "").strip()
    password = str(account.get("password") or "").strip()
    server_id = str(account.get("server_id") or "").strip()
    custom_tz = str(account.get("timezone") or "").strip()

    print(f"\n========== 开始处理账号: {name} ==========")

    if not cookies and not (email and password):
        msg = f"❌ [{name}] 配置缺失：未提供 cookies 或 email/password"
        send_telegram(msg)
        return False, "配置缺失"

    if not proxy_url:
        msg = f"🛑 [{name}] 安全拦截：多账号模式下必须为每个账号配置独立的 proxy_url"
        print(msg)
        send_telegram(msg)
        return False, "未配置独立节点(已拦截)"

    singbox = None
    profile_dir = tempfile.mkdtemp(prefix=f"fps_profile_{local_port}_")
    screenshot_path = os.path.join(profile_dir, "result.png")
    exit_ip = "未知"
    country = "??"
    tz_id = custom_tz or "Europe/Amsterdam"

    try:
        # 1. 启动该账号专属 sing-box 进程
        singbox = SingBoxManager(proxy_url, local_port=local_port)
        ip_info = singbox.start()
        exit_ip = ip_info["ip"]
        country = ip_info["country"]
        if not custom_tz:
            tz_id = ip_info["timezone"]

        # 2. 出口 IP 碰撞检测（防止多账号串 IP 被风控关联）
        if exit_ip in used_exit_ips:
            conflict_acc = used_exit_ips[exit_ip]
            warn = (
                f"🛑 <b>[{name}] 检测到出口 IP 冲突，已强制中止！</b>\n"
                f"冲突 IP：<code>{exit_ip}</code> ({country})\n"
                f"已被账号 <b>{conflict_acc}</b> 使用，为防止多号关联封禁，本次跳过。"
            )
            print(f"  [Anti-Ban] IP 冲突: {exit_ip} 与 {conflict_acc} 重复，中止执行")
            send_telegram(warn)
            return False, f"IP冲突({exit_ip})"

        used_exit_ips[exit_ip] = name
        viewport = get_account_viewport(name)
        print(f"  [Isolation] 独立环境 | 分辨率: {viewport['width']}x{viewport['height']} | 时区: {tz_id}")

        # 3. 启动完全隔离的独立浏览器与临时数据目录
        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            ]

            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                args=launch_args,
                proxy={"server": f"socks5://127.0.0.1:{local_port}"},
                viewport=viewport,
                locale="en-US",
                timezone_id=tz_id,
            )

            try:
                if cookies:
                    context.add_cookies(cookies)
                    print(f"  [Auth] 已注入 {len(cookies)} 条 Cookie")

                page = context.pages[0] if context.pages else context.new_page()

                # Step 1: 访问 Dashboard
                print("  [Step 1] 访问 Dashboard...")
                page.goto(f"{PANEL_BASE}/dashboard", wait_until="domcontentloaded", timeout=45000)
                human_sleep(2.5, 4.5)
                dismiss_cookie_banner(page)

                # Step 2: 检查登录状态
                if "/auth/login" in page.url:
                    print("  [Auth] Cookie 无效或未配置，回退账密登录")
                    if not (email and password):
                        page.screenshot(path=screenshot_path, full_page=False)
                        send_telegram(
                            f"⚠️ <b>FPS.ms [{name}] 续期失败</b>\n"
                            f"原因：Cookie 已过期且未配置账密\n"
                            f"出口：<code>{exit_ip}</code> ({country})",
                            screenshot_path,
                        )
                        return False, "Cookie过期"

                    if not login_with_credentials(page, email, password):
                        page.screenshot(path=screenshot_path, full_page=False)
                        send_telegram(
                            f"❌ <b>FPS.ms [{name}] 登录失败（已止损）</b>\n"
                            f"原因：可能被 Turnstile 拦截或账密错误，建议更新 Cookie\n"
                            f"出口：<code>{exit_ip}</code> ({country})",
                            screenshot_path,
                        )
                        return False, "登录失败"

                # Step 3: 进入服务器控制台
                print("  [Step 2] 进入服务器控制台...")
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

                # Step 4: 进入 Billing 页面
                print(f"  [Step 3] 进入 Billing 页面 (Server: {server_id})...")
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
                renew_btn = find_renew_button(page)
                now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

                if not renew_btn:
                    page.screenshot(path=screenshot_path, full_page=False)
                    send_telegram(
                        f"⚠️ <b>FPS.ms [{name}] 未找到续期按钮</b>\n"
                        f"服务器：<code>{server_id}</code>\n"
                        f"状态：{before_time}\n"
                        f"出口：<code>{exit_ip}</code> ({country})\n"
                        f"时间：{now_utc}",
                        screenshot_path,
                    )
                    return False, "未找到续期按钮"

                if renew_btn.is_disabled():
                    page.screenshot(path=screenshot_path, full_page=False)
                    send_telegram(
                        f"ℹ️ <b>FPS.ms [{name}] 暂无需续期</b>\n"
                        f"服务器：<code>{server_id}</code>\n"
                        f"剩余时间：{before_time}\n"
                        f"出口：<code>{exit_ip}</code> ({country})\n"
                        f"时间：{now_utc}",
                        screenshot_path,
                    )
                    return True, f"暂无需续期({before_time})"

                # Step 5: 模拟鼠标点击续期
                print("  [Step 4] 点击续期按钮...")
                box = renew_btn.bounding_box()
                if box:
                    page.mouse.move(
                        box["x"] + box["width"] / 2 + random.uniform(-10, 10),
                        box["y"] + box["height"] / 2 + random.uniform(-4, 4),
                        steps=random.randint(10, 18),
                    )
                    human_sleep(0.3, 0.8)
                renew_btn.click()

                human_sleep(4.5, 6.5)
                page.reload(wait_until="domcontentloaded", timeout=30000)
                human_sleep(2.0, 3.0)

                after_time = extract_remaining_text(page)
                page.screenshot(path=screenshot_path, full_page=False)

                send_telegram(
                    f"✅ <b>FPS.ms [{name}] 续期完成</b>\n"
                    f"服务器：<code>{server_id}</code>\n"
                    f"续期前：{before_time}\n"
                    f"续期后：{after_time}\n"
                    f"出口：<code>{exit_ip}</code> ({country}) | {tz_id}\n"
                    f"时间：{now_utc}",
                    screenshot_path,
                )
                return True, f"成功({after_time})"

            finally:
                context.close()

    except Exception as e:
        print(f"  [Error] 账号 [{name}] 异常: {e}")
        send_telegram(
            f"❌ <b>FPS.ms [{name}] 运行异常</b>\n"
            f"错误：<code>{str(e)[:280]}</code>\n"
            f"出口：<code>{exit_ip}</code> ({country})",
            screenshot_path if os.path.exists(screenshot_path) else None,
        )
        return False, "运行异常"

    finally:
        # 彻底销毁该账号的代理进程与临时浏览器目录
        if singbox:
            singbox.stop()
        shutil.rmtree(profile_dir, ignore_errors=True)
        print(f"  [Cleanup] 账号 [{name}] 代理与浏览器环境已销毁")


def main():
    skip_jitter = os.getenv("SKIP_JITTER", "").lower() == "true"
    gap_min = int(os.getenv("ACCOUNT_GAP_MIN", "120"))
    gap_max = int(os.getenv("ACCOUNT_GAP_MAX", "360"))

    accounts = load_accounts()

    # 定时任务启动前随机抖动（3~10分钟），避开整点规律
    if not skip_jitter:
        init_wait = random.randint(180, 600)
        print(f"[Anti-Ban] 启动前随机等待 {init_wait} 秒...")
        time.sleep(init_wait)

    # 每次运行随机打乱账号执行顺序
    random.shuffle(accounts)
    print(f"[Scheduler] 本次共 {len(accounts)} 个账号，已随机打乱执行顺序")

    used_exit_ips: dict[str, str] = {}
    summary_lines: list[str] = []
    has_failure = False

    for idx, acc in enumerate(accounts):
        acc_name = str(acc.get("name") or acc.get("email") or f"acc-{idx + 1}")
        local_port = 10808 + idx

        ok, status = process_single_account(acc, local_port, used_exit_ips)
        icon = "✅" if ok else "❌"
        summary_lines.append(f"{icon} <b>{acc_name}</b>: {status}")
        if not ok:
            has_failure = True

        # 账号之间增加随机冷却间隔
        if idx < len(accounts) - 1:
            if skip_jitter:
                gap = random.randint(10, 20)
            else:
                gap = random.randint(gap_min, gap_max)
            print(f"[Anti-Ban] 冷却等待 {gap} 秒后处理下一个账号...")
            time.sleep(gap)

    if len(accounts) > 1:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        send_telegram(
            f"📊 <b>FPS.ms 多账号续期汇总</b> ({now_utc})\n\n" + "\n".join(summary_lines)
        )

    if has_failure:
        sys.exit(1)


if __name__ == "__main__":
    main()
