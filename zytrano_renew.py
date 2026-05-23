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

# ── Turnstile 点击（终极修复版）─────────────────────────────
def click_turnstile_checkbox(page, timeout=30) -> bool:
    """
    关键洞察：
    - Zytrano 使用 Cloudflare Turnstile managed 模式（有勾选框），需要点击
    - iframe 在 div.cf-turnstile 的 closed shadow-root 内
    - wait_for_selector / pierce: 都无法可靠定位该 iframe
    - 正确方案：用 page.frames 枚举所有 frame（CDP 协议层面，不受 shadow DOM 限制）
      然后找到 URL 含 challenges.cloudflare.com 的那个 frame
    """

    # ── 诊断工具：打印当前所有 frame 列表 ──────────────────────
    def dump_frames(label: str):
        try:
            frames = page.frames
            log.info(f"[诊断/{label}] 当前共 {len(frames)} 个 frame：")
            for i, f in enumerate(frames):
                url = (f.url or "about:blank")[:120]
                log.info(f"  [{i}] {url}")
        except Exception as e:
            log.warning(f"[诊断/{label}] dump_frames 失败: {e}")

    # ── 诊断工具：打印 token input 的实际状态 ──────────────────
    def dump_token_state(label: str):
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
                if (!el) return 'INPUT_NOT_FOUND';
                const v = el.value || '';
                return v.length === 0 ? 'EMPTY' : `len=${v.length} prefix=${v.slice(0,20)}`;
            })()
        """)
        log.info(f"[诊断/{label}] cf-turnstile-response: {val}")

    # ── 诊断工具：打印页面当前 URL + 标题 ──────────────────────
    def dump_page_state(label: str):
        try:
            url = page.url
            title = page.title()
            log.info(f"[诊断/{label}] URL={url}  title={title!r}")
        except Exception as e:
            log.warning(f"[诊断/{label}] dump_page_state 失败: {e}")

    # ★ 递归穿透所有 shadow root 检查 token
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

    # ── 阶段1：静默等待（最多 15s）──────────────────────────────
    log.info("【Turnstile 阶段1】等待静默通过（最多 15s）...")
    dump_page_state("阶段1开始")
    dump_token_state("阶段1开始")
    for i in range(30):
        if token_ready():
            log.info(f"✅ Turnstile 静默通过（{i * 0.5:.1f}s），无需点击")
            return True
        time.sleep(0.5)
    dump_token_state("阶段1结束_未过")

    # ── 阶段2：枚举 frames 找 Turnstile iframe ───────────────────
    log.info("【Turnstile 阶段2】用 page.frames 枚举查找 Turnstile frame（最多 8s）...")
    dump_frames("阶段2开始")
    cf_frame = None
    for tick in range(16):
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if cf_frame:
            log.info(f"  ✅ 第 {tick * 0.5:.1f}s 找到 Turnstile frame")
            break
        time.sleep(0.5)

    if not cf_frame:
        # 枚举失败：打印完整诊断后降级
        log.warning("【Turnstile 阶段2】frames 枚举 8s 内未找到 Turnstile frame")
        dump_frames("枚举失败")
        dump_page_state("枚举失败")
        take_screenshot(page, "turnstile_frame_not_found")

        # 降级：用 iframe 元素坐标点击（选择器对 shadow-root 无效）
        log.info("  降级：尝试 iframe 坐标点击...")
        fallback_clicked = False
        try:
            iframe_el = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            box = iframe_el.bounding_box()
            log.info(f"  [诊断] 降级 iframe bounding_box={box}")
            if box:
                x = box["x"] + 25
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.4))
                page.mouse.click(x, y)
                log.info(f"  ✅ 降级坐标点击 ({x:.0f}, {y:.0f})")
                fallback_clicked = True
            else:
                log.error("  [诊断] 降级 iframe bounding_box 为 None")
        except Exception as fe:
            log.error(f"  降级坐标点击失败: {fe}")

        if not fallback_clicked:
            log.error("【Turnstile 阶段2】降级坐标点击也失败，放弃 Turnstile")
            return False
    else:
        log.info(f"【Turnstile 阶段2】frame URL: {cf_frame.url[:120]}")
        time.sleep(1)  # 给 iframe 内部 JS 初始化

        # ── 阶段3：直接坐标点击（选择器方式对二层 shadow-root 无效，已确认）──
        log.info("【Turnstile 阶段3】坐标点击 checkbox...")

        # 诊断：打印 frame body，确认 iframe 内 DOM 状态
        try:
            inner_html_snippet = cf_frame.locator("body").inner_html(timeout=2000)[:200]
            log.info(f"  [诊断/frame body 前200字符] {inner_html_snippet!r}")
        except Exception as e:
            log.debug(f"  [诊断/frame body] 读取失败（正常，二层 shadow-root）: {e}")

        clicked = False
        try:
            frame_el = cf_frame.frame_element()
            box = frame_el.bounding_box()
            log.info(f"  [诊断] frame bounding_box={box}")
            if box:
                x = box["x"] + 25
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.4))
                page.mouse.click(x, y)
                log.info(f"  ✅ 坐标点击 ({x:.0f}, {y:.0f})")
                clicked = True
            else:
                log.error("  [诊断] bounding_box 返回 None，iframe 可能不可见或未渲染")
        except Exception as e:
            log.error(f"  坐标点击失败: {e}")

        if not clicked:
            log.error("【Turnstile 阶段3】坐标点击失败")
            dump_frames("阶段3失败")
            take_screenshot(page, "turnstile_click_failed")
            return False

    # ── 阶段4：等待 token 写入 ──────────────────────────────────
    log.info("【Turnstile 阶段4】等待 token 写入（最多 30s）...")
    for i in range(timeout * 2):
        if token_ready():
            log.info(f"✅ Turnstile token 就绪（{i * 0.5:.1f}s）")
            dump_token_state("token就绪")
            return True
        if i % 10 == 0 and i > 0:
            log.info(f"  token 等待中... {i * 0.5:.0f}s")
            dump_token_state(f"等待{i * 0.5:.0f}s")
            take_screenshot(page, f"turnstile_wait_{i}")
        time.sleep(0.5)

    log.error("【Turnstile 阶段4】token 等待超时（30s）")
    dump_token_state("超时")
    dump_frames("超时")
    take_screenshot(page, "turnstile_token_timeout")
    return False


# ── 登录状态检测 ──────────────────────────────────────────
LOGGED_IN_URL_KEYS = ("/home", "/dashboard", "/servers")

def is_logged_in_url(page) -> bool:
    """仅凭 URL 判断是否已登录"""
    return any(k in page.url for k in LOGGED_IN_URL_KEYS)

def is_logged_in_page(page) -> bool:
    """凭 URL 或页面内容（Credits / Dashboard 关键词）判断是否已登录"""
    if is_logged_in_url(page):
        return True
    try:
        # Home 页有 Credits 信息，可作为登录成功的强信号
        body = page.inner_text("body") or ""
        for kw in ("Credits", "Dashboard", "Servers", "Activity Logs"):
            if kw in body:
                log.info(f"[登录检测] 页面含关键词 '{kw}'，判断为已登录")
                return True
    except Exception:
        pass
    return False

# ── 登录（重试次数 2）────────────────────────────────────
def login(page, max_retries=2) -> bool:
    for attempt in range(1, max_retries + 1):
        log.info(f"登录 {attempt}/{max_retries} (用户: {mask(USERNAME)}) ...")

        # ★ 修复1：navigate 前先检查——如果上次已登录，session cookie 还在，
        #   服务器会直接 302 跳回 /home，根本不需要重新填表
        if is_logged_in_page(page):
            log.info(f"✅ navigate 前已检测到登录状态（URL: {page.url}），跳过登录流程")
            return True

        if not navigate(page, LOGIN_URL):
            log.error("CF 验证失败，重试")
            continue

        # ★ 修复2：navigate 之后立即再检查一次
        #   /login 若被 302 跳转到 /home，page.url 此时已变，无需等表单
        if is_logged_in_page(page):
            log.info(f"✅ navigate 后已跳转到登录后页面（URL: {page.url}），视为登录成功")
            take_screenshot(page, f"01_already_logged_in_{attempt}")
            return True

        try:
            page.wait_for_selector(
                'input[placeholder="Email or Username"], input[name="user"]',
                timeout=10000,
            )
        except Exception:
            cur_url = page.url
            log.warning(f"找不到用户名输入框，当前 URL: {cur_url}")
            take_screenshot(page, f"01_no_form_{attempt}")
            # ★ 修复3：page 内容检测，不只看 URL
            if is_logged_in_page(page):
                log.info("✅ 已在登录后页面（URL或内容），视为登录成功")
                return True
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

        # ★ 修复4：用 wait_for_url 替代轮询，更快响应
        #   同时加 page 内容检测（Credits/Dashboard），兜底处理 URL 未变但已登录的情况
        log.info("等待登录跳转（最多 30s）...")
        success_url = False
        try:
            page.wait_for_url(
                lambda url: any(k in url for k in ("/home", "/dashboard", "/servers")),
                timeout=30000,
            )
            success_url = True
        except Exception:
            # wait_for_url 超时——再用内容检测兜底
            if is_logged_in_page(page):
                log.info("[登录检测] wait_for_url 超时但页面内容确认已登录")
                success_url = True

        if success_url:
            log.info(f"✅ 登录成功，当前 URL: {page.url}")
            take_screenshot(page, "02_login_success")
            return True

        log.warning(f"登录后未跳转（30s），当前 URL: {page.url}，重试")
        take_screenshot(page, f"02_login_fail_{attempt}")

    return False

# ── 读取服务器信息 ─────────────────────────────────────────
def get_servers_info(page) -> list[dict]:
    if not navigate(page, SERVERS_URL):
        log.warning("进入服务器页 CF 失败")
        return []

    time.sleep(3)

    # 滚动到底部确保所有卡片（含续期按钮）渲染出来
    log.info("[服务器页] 滚动页面加载全部内容...")
    js_eval(page, "(() => { window.scrollTo(0, document.body.scrollHeight); })()")
    time.sleep(1)
    js_eval(page, "(() => { window.scrollTo(0, 0); })()")
    time.sleep(1)

    take_screenshot(page, "03_servers_page")

    # ★ 修复：page.evaluate 不能裸写 return，必须用箭头函数
    html = js_eval(page, "() => document.body.innerHTML") or ""
    log.info(f"[诊断] innerHTML 长度: {len(html)} 字符")

    if "handleServerRenew" in html:
        idx = html.index("handleServerRenew")
        log.info(f"[诊断] handleServerRenew 片段: ...{html[max(0,idx-30):idx+80]}...")
    else:
        log.warning("[诊断] innerHTML 中未找到 handleServerRenew，打印前 500 字符：")
        log.warning(f"  {html[:500]!r}")
        try:
            body_text = page.inner_text("body")
            log.info(f"[诊断] body inner_text 前 300 字: {body_text[:300]!r}")
        except Exception as e:
            log.warning(f"[诊断] inner_text 失败: {e}")

    server_ids = re.findall(r"handleServerRenew\(['\"]([^\'\"]+)[\'\"]\)", html)
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

    result = js_eval(page, f"() => {{ handleServerRenew('{server_id}'); return 'called'; }}")
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
