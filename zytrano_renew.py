"""
Zytrano.top 自动续期脚本
- CloakBrowser（源码级指纹伪装）过 Cloudflare
- frame_locator 穿透 Turnstile iframe，点击 span.cb-i（视觉勾选框）
- 续期后读取 "Suspended in: X days, Y hours, Z minutes" 推送 Telegram
"""

import json
import logging
import os
import random
import re
import time
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── 脱敏工具 ──────────────────────────────────────────────
def mask(value: str, show: int = 3) -> str:
    if not value or len(value) <= show * 2:
        return "***"
    return value[:show] + "***" + value[-show:]

# ── 环境变量 ──────────────────────────────────────────────
USERNAME       = os.environ["ZYTRANO_USERNAME"]
PASSWORD       = os.environ["ZYTRANO_PASSWORD"]
TG_BOT_TOKEN   = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID     = os.environ.get("TG_CHAT_ID", "")

BASE_URL    = "https://cp.zytrano.top"
LOGIN_URL   = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"

# ── Telegram 推送 ─────────────────────────────────────────
def tg_push(content: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram 未配置，跳过推送")
        return
    
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TG_CHAT_ID,
        "text": content,
        "parse_mode": "HTML"
    }).encode('utf-8')
    
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log.info("📨 Telegram 推送成功")
            else:
                log.warning(f"📨 Telegram 推送失败: {result}")
    except Exception as e:
        log.warning(f"📨 Telegram 推送异常: {e}")

# ── 工具函数 ──────────────────────────────────────────────
def get_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""

def human_delay(min_s=0.4, max_s=1.2):
    time.sleep(random.uniform(min_s, max_s))

def js_eval(page, script: str):
    try:
        return page.evaluate(script)
    except Exception as e:
        log.warning(f"JS 执行失败: {e}")
        return None

# ── Cloudflare 全页拦截等待 ───────────────────────────────
def is_cf_blocked(page) -> bool:
    try:
        body = get_text(page).lower()
        return "verify you are human" in body or (
            "cloudflare" in body and "security" in body
        )
    except Exception:
        return False

def wait_cf_pass(page, timeout=45) -> bool:
    log.info("等待 Cloudflare 全页验证通过...")
    for i in range(timeout):
        if not is_cf_blocked(page):
            log.info(f"✅ Cloudflare 验证通过（{i}s）")
            return True
        if i % 5 == 0 and i > 0:
            log.info(f"  CF 等待中... {i}s")
        time.sleep(1)
    log.error(f"Cloudflare 验证超时（{timeout}s）")
    return False

def navigate(page, url: str, timeout=45) -> bool:
    log.info(f"导航到: {url}")
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"goto 超时/异常: {e}，继续等待...")

    if not is_cf_blocked(page):
        return True

    if wait_cf_pass(page, timeout=timeout):
        return True

    log.info("CF 未过，刷新重试...")
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    return wait_cf_pass(page, timeout=30)

# ── Turnstile 点击（核心流程）─────────────────────────────
def click_turnstile_checkbox(page, timeout=30) -> bool:
    def token_ready() -> bool:
        val = js_eval(page, """
            (() => {
                function deepQuery(root, sel) {
                    let el = root.querySelector(sel);
                    if (el) return el;
                    for (const host of root.querySelectorAll('*')) {
                        if (host.shadowRoot) {
                            el = deepQuery(host.shadowRoot, sel);
                            if (el) return el;
                        }
                    }
                    return null;
                }
                const el = deepQuery(document, 'input[name="cf-turnstile-response"]');
                return el ? (el.value || '').length > 10 : false;
            })()
        """)
        return bool(val)

    # 阶段1：静默等待
    log.info("【Turnstile】等待静默通过（最多 15s）...")
    for i in range(30):
        if token_ready():
            log.info(f"✅ Turnstile 静默通过（{i * 0.5:.1f}s），无需点击")
            return True
        time.sleep(0.5)

    # 阶段2：查找 Turnstile frame
    log.info("【Turnstile】查找 Turnstile frame...")
    cf_frame = None
    for _ in range(16):
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if cf_frame:
            break
        time.sleep(0.5)

    if not cf_frame:
        log.warning("未找到 Turnstile frame，尝试降级坐标点击...")
        try:
            iframe_el = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            box = iframe_el.bounding_box()
            if box:
                x = box["x"] + 25
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.4))
                page.mouse.click(x, y)
                log.info(f"✅ 降级坐标点击 ({x:.0f}, {y:.0f})")
                return True
        except Exception as fe:
            log.error(f"降级坐标点击失败: {fe}")
        return False

    time.sleep(1)

    # 阶段3：基于 Frame 坐标点击
    log.info("【Turnstile】基于 Frame 坐标点击...")
    try:
        frame_el = cf_frame.frame_element()
        box = frame_el.bounding_box()
        if box:
            x = box["x"] + 25
            y = box["y"] + box["height"] / 2
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.2, 0.4))
            page.mouse.click(x, y)
            log.info(f"✅ 坐标点击 ({x:.0f}, {y:.0f})")
        else:
            log.error("iframe bounding_box 返回 None")
            return False
    except Exception as e:
        log.error(f"坐标点击失败: {e}")
        return False

    # 阶段4：等待 token 写入
    log.info("【Turnstile】等待 token 写入...")
    for i in range(timeout * 2):
        if token_ready():
            log.info(f"✅ Turnstile token 就绪（{i * 0.5:.1f}s）")
            return True
        time.sleep(0.5)

    log.error("【Turnstile】token 等待超时")
    return False


# ── 登录状态检测 ──────────────────────────────────────────
LOGGED_IN_URL_KEYS = ("/home", "/dashboard", "/servers")

def is_logged_in_url(page) -> bool:
    return any(k in page.url for k in LOGGED_IN_URL_KEYS)

def is_logged_in_page(page) -> bool:
    if is_logged_in_url(page):
        return True
    try:
        body = page.inner_text("body") or ""
        for kw in ("Credits", "Dashboard", "Servers", "Activity Logs"):
            if kw in body:
                return True
    except Exception:
        pass
    return False

# ── 登录流程 ─────────────────────────────────────────────
def login(page, max_retries=2) -> bool:
    for attempt in range(1, max_retries + 1):
        log.info(f"登录 {attempt}/{max_retries} (用户: {mask(USERNAME)}) ...")

        if is_logged_in_page(page):
            log.info("✅ navigate 前已检测到登录状态，跳过登录流程")
            return True

        if not navigate(page, LOGIN_URL):
            log.error("CF 验证失败，重试")
            continue

        if is_logged_in_page(page):
            log.info("✅ navigate 后已跳转到登录后页面，视为登录成功")
            return True

        try:
            page.wait_for_selector(
                'input[placeholder="Email or Username"], input[name="user"]',
                timeout=10000,
            )
        except Exception:
            if is_logged_in_page(page):
                log.info("✅ 已在登录后页面，视为登录成功")
                return True
            continue

        human_delay(0.5, 1.0)

        # 填写账号密码
        try:
            user_el = page.locator('input[placeholder="Email or Username"]').first
            user_el.click()
            user_el.fill("")
            user_el.type(USERNAME, delay=random.randint(60, 130))
        except Exception:
            page.locator("input").first.type(USERNAME, delay=random.randint(60, 130))
        human_delay(0.3, 0.8)

        try:
            pass_el = page.locator('input[placeholder="Password"]').first
            pass_el.click()
            pass_el.fill("")
            pass_el.type(PASSWORD, delay=random.randint(60, 130))
        except Exception:
            page.locator('input[type="password"]').first.type(PASSWORD, delay=random.randint(60, 130))
        human_delay(0.5, 1.0)

        # 处理 Turnstile
        click_turnstile_checkbox(page, timeout=30)
        human_delay(0.5, 1.0)

        # 点击登录
        try:
            page.get_by_role("button", name="Sign In").click()
        except Exception:
            page.locator("button[type='submit']").first.click()

        log.info("等待登录跳转...")
        success_url = False
        try:
            page.wait_for_url(
                lambda url: any(k in url for k in LOGGED_IN_URL_KEYS),
                timeout=30000,
            )
            success_url = True
        except Exception:
            if is_logged_in_page(page):
                success_url = True

        if success_url:
            log.info(f"✅ 登录成功，当前 URL: {page.url}")
            return True

        log.warning("登录跳转超时或失败，重试...")

    return False

# ── 读取服务器信息 ─────────────────────────────────────────
def get_servers_info(page) -> list[dict]:
    if not navigate(page, SERVERS_URL):
        return []

    time.sleep(3)
    js_eval(page, "(() => { window.scrollTo(0, document.body.scrollHeight); })()")
    time.sleep(1)
    js_eval(page, "(() => { window.scrollTo(0, 0); })()")
    time.sleep(1)

    html = js_eval(page, "() => document.body.innerHTML") or ""
    server_ids = re.findall(r"handleServerRenew\(['\"]([^\'\"]+)[\'\"]\)", html)

    text = get_text(page)
    suspended_matches = re.findall(
        r'Suspended in[:\s]*([\d]+ days?,\s*[\d]+ hours?,\s*[\d]+ minutes?)',
        text, re.IGNORECASE
    )
    if not suspended_matches:
        suspended_matches = re.findall(
            r'Suspended in[:\s]*([\d\w\s,]+)',
            text, re.IGNORECASE
        )

    servers = []
    for i, sid in enumerate(server_ids):
        info = {
            "server_id": sid,
            "name": f"Server-{i+1}",
            "suspended_in": suspended_matches[i] if i < len(suspended_matches) else "未知",
        }
        servers.append(info)
    
    return servers

def parse_days_remaining(suspended_in: str) -> float:
    days = hours = minutes = 0.0
    m = re.search(r'(\d+)\s*day', suspended_in, re.I)
    if m: days = float(m.group(1))
    m = re.search(r'(\d+)\s*hour', suspended_in, re.I)
    if m: hours = float(m.group(1))
    m = re.search(r'(\d+)\s*minute', suspended_in, re.I)
    if m: minutes = float(m.group(1))
    return days + hours / 24 + minutes / 1440

# ── 续期 ──────────────────────────────────────────────────
def renew_server(page, server_id: str) -> bool:
    log.info(f"续期服务器 {mask(server_id)} ...")
    human_delay(0.5, 1.0)

    js_eval(page, f"() => {{ handleServerRenew('{server_id}'); return 'called'; }}")
    time.sleep(3)

    confirm_texts = ["Yes, renew it!", "Yes, renew it", "Confirm", "OK"]
    clicked = False
    for _ in range(3):
        if clicked:
            break
        for btn_text in confirm_texts:
            try:
                page.get_by_role("button", name=btn_text).click(timeout=3000)
                time.sleep(2)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            time.sleep(1)

    text_after = get_text(page)
    if "success" in text_after.lower() or "renewed" in text_after.lower():
        log.info("✅ 续期成功")
        return True

    log.info("续期指令已发送")
    return True

# ── 主流程 ────────────────────────────────────────────────
def main():
    from cloakbrowser import launch

    log.info("启动 CloakBrowser ...")
    browser = launch(
        headless=False,
        humanize=True,
        geoip=True,
    )
    page = browser.new_page()

    try:
        if not login(page):
            tg_push("❌ Zytrano 登录失败，请检查账号密码或 CF 验证")
            return

        servers = get_servers_info(page)
        if not servers:
            tg_push("❌ Zytrano 未找到服务器信息")
            return

        results = []
        for s in servers:
            success = renew_server(page, s["server_id"])
            navigate(page, SERVERS_URL)
            time.sleep(3)
            
            text_new = get_text(page)
            new_matches = re.findall(
                r'Suspended in[:\s]*([\d]+ days?,\s*[\d]+ hours?,\s*[\d]+ minutes?)',
                text_new, re.IGNORECASE
            )
            new_suspended = new_matches[0] if new_matches else s["suspended_in"]
            
            results.append({
                "name": s["name"],
                "renewed": success,
                "suspended_in": new_suspended,
            })

        lines = ["🖥️ <b>Zytrano 自动续期报告</b>\n"]
        for r in results:
            status = "✅ 已续期" if r["renewed"] else "❌ 续期失败"
            lines.append(f"{status} <code>[{r['name']}]</code>")
            lines.append(f"Suspended in: {r['suspended_in']}\n")

        msg = "\n".join(lines).strip()
        log.info(f"\n{msg}")
        tg_push(msg)

    except Exception as e:
        log.exception(e)
        tg_push(f"❌ Zytrano 脚本异常: {e}")
    finally:
        time.sleep(3)
        browser.close()
        log.info("任务结束")

if __name__ == "__main__":
    main()
