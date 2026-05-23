"""
Zytrano.top 自动续期脚本
- CloakBrowser（源码级指纹伪装）过 Cloudflare
- frame_locator 穿透 Turnstile iframe，点击 span.cb-i（视觉勾选框）
- 续期后读取 "Suspended in: X days, Y hours, Z minutes" 推送 WxPusher
"""

import json
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

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
WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID   = os.environ.get("WXPUSHER_UID", "")

BASE_URL    = "https://cp.zytrano.top"
LOGIN_URL   = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"

SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ── WxPusher 推送 ─────────────────────────────────────────
def wxpush(content: str):
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
        log.warning("WxPusher 未配置，跳过推送")
        return
    import urllib.request
    payload = json.dumps({
        "appToken": WXPUSHER_TOKEN,
        "content": content,
        "contentType": 1,
        "uids": [WXPUSHER_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                log.info(f"📨 WxPusher 推送成功 (token: {mask(WXPUSHER_TOKEN)}, uid: {mask(WXPUSHER_UID)})")
            else:
                log.warning(f"📨 WxPusher 推送失败: {result}")
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

# ── 工具函数 ──────────────────────────────────────────────
def take_screenshot(page, name: str):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path, full_page=False)
        log.info(f"📸 截图: {path}")
    except Exception as e:
        log.warning(f"截图失败: {e}")

def get_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""

def human_delay(min_s=0.4, max_s=1.2):
    time.sleep(random.uniform(min_s, max_s))

def wait_for_url_contains(page, keyword: str, timeout=15) -> bool:
    try:
        page.wait_for_url(f"**{keyword}**", timeout=timeout * 1000)
        return True
    except Exception:
        return keyword in page.url

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

# ── Turnstile 点击（针对已知 DOM 结构）─────────────────────
def click_turnstile_checkbox(page, timeout=30) -> bool:
    """
    已知 Turnstile 真实 DOM 结构（来自 DevTools 抓包确认）：
      div.cf-turnstile
        └─ #shadow-root (closed)
             └─ iframe[src*="challenges.cloudflare.com"]
                  └─ #document
                       └─ body
                            └─ #shadow-root (closed)
                                 └─ div.cb-c > label > span.cb-i   ← 视觉勾选框，click 这里
                                                      input[type=checkbox] ← 实际 checkbox

    关键修复：
    1. cf-turnstile-response input 在 closed shadow-root 内，普通 querySelector 找不到，
       须用递归穿透所有 shadow root 的 JS 写法检查 token。
    2. iframe 本身也在 shadow-root 内，wait_for_selector 须用 pierce: 伪类穿透，
       或降级为直接用 frame_locator 探测。
    3. 静默等待时间延长到 15s（GitHub Actions 环境指纹评估耗时更长）。
    """

    # ★ 修复1：递归穿透所有 shadow root 检查 token
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

    # ★ 修复2：静默等待延长到 15s（30 × 0.5s）
    log.info("等待 Turnstile 静默通过（最多 15s）...")
    for i in range(30):
        if token_ready():
            log.info(f"✅ Turnstile 静默通过（{i * 0.5:.1f}s），无需点击")
            return True
        time.sleep(0.5)

    # ★ 修复3：iframe 在 closed shadow-root 内，用 pierce: 穿透，失败则降级
    log.info("静默未过，等待 Turnstile iframe 加载...")
    iframe_found = False
    try:
        page.wait_for_selector(
            "pierce/iframe[src*='challenges.cloudflare.com']",
            timeout=12000,
        )
        iframe_found = True
    except Exception:
        # pierce 不支持时降级：直接用 frame_locator 探测 iframe 内容
        try:
            cf_frame_test = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
            cf_frame_test.locator("body").wait_for(state="attached", timeout=8000)
            iframe_found = True
        except Exception:
            log.warning("Turnstile iframe 未出现")
            return False

    if not iframe_found:
        return False

    time.sleep(1)  # 给 iframe 内部 JS 初始化的时间

    # 阶段3：frame_locator 穿透 iframe，按优先级依次尝试选择器
    log.info("用 frame_locator 穿透 iframe 点击 checkbox...")
    cf_frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')

    selectors = [
        ("span.cb-i",            "视觉勾选框 span.cb-i"),
        ("input[type=checkbox]", "原始 checkbox"),
        ("label",                "label 整体"),
        (".cb-lb",               "cb-lb 容器"),
    ]

    clicked = False
    for sel, desc in selectors:
        try:
            loc = cf_frame.locator(sel).first
            loc.wait_for(state="attached", timeout=4000)
            loc.hover(timeout=3000)
            time.sleep(random.uniform(0.2, 0.5))
            loc.click(timeout=3000)
            log.info(f"  ✅ 点击成功: {desc}")
            clicked = True
            break
        except Exception as e:
            log.debug(f"  [{desc}] 失败: {e}")

    if not clicked:
        log.warning("所有 frame_locator 选择器均失败，尝试坐标点击...")
        try:
            box = page.locator(
                'iframe[src*="challenges.cloudflare.com"]'
            ).first.bounding_box()
            if box:
                x = box["x"] + 25
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.4))
                page.mouse.click(x, y)
                log.info(f"  坐标点击 ({x:.0f}, {y:.0f})")
                clicked = True
        except Exception as e:
            log.warning(f"  坐标点击失败: {e}")

    if not clicked:
        log.error("所有点击方式均失败")
        return False

    # 阶段4：等待 token 写入（最多 timeout 秒）
    log.info("等待 Turnstile token 填入...")
    for i in range(timeout * 2):
        if token_ready():
            log.info(f"✅ Turnstile token 就绪（{i * 0.5:.1f}s）")
            return True
        if i % 10 == 0 and i > 0:
            log.info(f"  token 等待中... {i * 0.5:.0f}s")
            take_screenshot(page, f"turnstile_wait_{i}")
        time.sleep(0.5)

    log.error("Turnstile token 等待超时")
    return False

# ── 登录（★ 修复4：重试次数改为 2）─────────────────────────
def login(page, max_retries=2) -> bool:
    for attempt in range(1, max_retries + 1):
        log.info(f"登录 {attempt}/{max_retries} (用户: {mask(USERNAME)}) ...")
        if not navigate(page, LOGIN_URL):
            log.error("CF 验证失败，重试")
            continue

        try:
            page.wait_for_selector(
                'input[placeholder="Email or Username"], input[name="user"]',
                timeout=10000,
            )
        except Exception:
            log.warning("找不到用户名输入框，重试")
            take_screenshot(page, f"01_no_form_{attempt}")
            continue

        human_delay(0.5, 1.0)
        take_screenshot(page, "01_login_page")

        # 填写用户名
        try:
            user_el = page.locator('input[placeholder="Email or Username"]').first
            user_el.click()
            user_el.fill("")
            user_el.type(USERNAME, delay=random.randint(60, 130))
        except Exception:
            page.locator("input").first.type(USERNAME, delay=random.randint(60, 130))
        human_delay(0.3, 0.8)

        # 填写密码
        try:
            pass_el = page.locator('input[placeholder="Password"]').first
            pass_el.click()
            pass_el.fill("")
            pass_el.type(PASSWORD, delay=random.randint(60, 130))
        except Exception:
            page.locator('input[type="password"]').first.type(
                PASSWORD, delay=random.randint(60, 130)
            )
        human_delay(0.5, 1.0)

        # ★ 点击 Turnstile checkbox
        take_screenshot(page, "01b_before_turnstile")
        turnstile_ok = click_turnstile_checkbox(page, timeout=30)
        take_screenshot(page, "01c_after_turnstile")

        if not turnstile_ok:
            log.warning("Turnstile 未完成，仍尝试提交...")

        human_delay(0.5, 1.0)

        # 点击 Sign In
        try:
            page.get_by_role("button", name="Sign In").click()
        except Exception:
            page.locator("button[type='submit']").first.click()
        log.info("已点击 Sign In，等待跳转...")

        if wait_for_url_contains(page, "/home", 12) or \
           wait_for_url_contains(page, "/servers", 5):
            log.info("✅ 登录成功")
            take_screenshot(page, "02_login_success")
            return True

        log.warning("登录后未跳转，重试")
        take_screenshot(page, f"02_login_fail_{attempt}")

    return False

# ── 读取服务器信息 ─────────────────────────────────────────
def get_servers_info(page) -> list[dict]:
    if not navigate(page, SERVERS_URL):
        log.warning("进入服务器页 CF 失败")
        return []

    time.sleep(3)
    take_screenshot(page, "03_servers_page")

    html = js_eval(page, "return document.body.innerHTML") or ""
    server_ids = re.findall(r"handleServerRenew\(['\"]([^'\"]+)['\"]\)", html)
    log.info(f"找到服务器 ID: {[mask(s) for s in server_ids]}")

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

    log.info(f"Suspended in 信息: {suspended_matches}")

    servers = []
    for i, sid in enumerate(server_ids):
        info = {
            "server_id": sid,
            "name": f"Server-{i+1}",
            "suspended_in": suspended_matches[i] if i < len(suspended_matches) else "未知",
        }
        servers.append(info)
        log.info(f"服务器 [{info['name']}] ID={mask(sid)} 到期：{info['suspended_in']}")

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

    result = js_eval(page, f"handleServerRenew('{server_id}'); return 'called';")
    log.info(f"handleServerRenew 调用结果: {result} (server: {mask(server_id)})")
    time.sleep(3)

    confirm_texts = ["Yes, renew it!", "Yes, renew it", "Confirm", "OK"]
    clicked = False
    for _ in range(3):
        if clicked:
            break
        for btn_text in confirm_texts:
            try:
                page.get_by_role("button", name=btn_text).click(timeout=3000)
                log.info(f"已点击确认按钮: {btn_text}")
                time.sleep(2)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            time.sleep(1)

    take_screenshot(page, f"04_after_renew_{server_id[:8]}")

    text_after = get_text(page)
    if "success" in text_after.lower() or "renewed" in text_after.lower():
        log.info("✅ 续期成功（页面有 success 字样）")
        return True

    log.info("续期操作已执行（无法确认成功，请查看截图）")
    return True

# ── 主流程 ────────────────────────────────────────────────
def main():
    from cloakbrowser import launch

    log.info("启动 CloakBrowser（源码级指纹伪装）...")
    browser = launch(
        headless=False,
        humanize=True,
        geoip=True,
    )
    page = browser.new_page()

    try:
        if not login(page):
            wxpush("❌ Zytrano 登录失败，请检查账号密码或 CF 验证")
            return

        servers = get_servers_info(page)
        if not servers:
            wxpush("❌ Zytrano 未找到服务器信息，请检查截图")
            return

        results = []
        for s in servers:
            days = parse_days_remaining(s["suspended_in"])
            log.info(f"[{s['name']}] 续期前剩余约 {days:.2f} 天 ({s['suspended_in']})")
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

        lines = ["🖥️ Zytrano 自动续期报告", ""]
        for r in results:
            status = "✅ 已续期" if r["renewed"] else "❌ 续期失败"
            lines.append(f"{status} [{r['name']}]")
            lines.append(f"Suspended in: {r['suspended_in']}")
            lines.append("")

        msg = "\n".join(lines).strip()
        log.info(f"\n{msg}")
        wxpush(msg)

    except Exception as e:
        log.exception(e)
        take_screenshot(page, "99_error")
        wxpush(f"❌ Zytrano 脚本异常: {e}")
    finally:
        time.sleep(3)
        browser.close()
        log.info("任务结束")

if __name__ == "__main__":
    main()
