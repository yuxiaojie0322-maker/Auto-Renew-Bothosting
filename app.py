#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests, subprocess
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱,只用于通知使用，可随意填写
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or ""   # session token，默认登录方式,非必须
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token 备用登录方式, 失败时才使用,必须填写
GH_TOKEN      = os.environ.get("GH_TOKEN") or ""        # GitHub PAT token,用于自动更新session token,可选
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 

# 解析 DISCORD_TOKEN
DC_TOKEN = ""
if DISCORD_TOKEN:
    _parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = _parts[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 DISCORD_TOKEN,脚本终止。")
    sys.exit(1)

# 构造cookie
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

# 记录本次登录方式（用于通知）
_LOGIN_METHOD = "SESSION_TOKEN"

# 获取cookie到期时间
def get_cookie_info(sb, name):
    cookies = sb.get_cookies()
    for c in cookies:
        if c.get('name') == name:
            value = c.get('value')
            expiry_ts = c.get('expiry')
            expiry_dt = datetime.fromtimestamp(expiry_ts) if expiry_ts else None
            return value, expiry_dt
    return None, None

# 检查是否需要更新cookie
def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3):
    if new_value is None:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now()).total_seconds()
        if remaining < days_threshold * 24 * 3600:
            return True
    return False

# 更新cookie到secrets
def update_github_secret(secret_name, new_value):
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    masked = new_value[:4] + "..." + new_value[-4:] if len(new_value) > 8 else "***"
    print(f"🔄 更新 Secret: {secret_name} (新值: {masked})")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        cmd = ["gh", "secret", "set", secret_name, "--body", new_value]
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if repo:
            cmd.extend(["-R", repo])
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30, check=False,
            env=env
        )
        if proc.returncode == 0:
            return True
        else:
            print(f"❌ 更新失败: {proc.stderr.strip()}")
            return False
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False

# 发送tg通知
def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

# 通知格式
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 
    
    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# 深度检测 Turnstile 验证状态及弹窗续期按钮状态
def check_turnstile_status(sb):
    js_code = """
        const tokenInput = document.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], [name="cf-turnstile-response"]');
        const tokenVal = tokenInput ? (tokenInput.value || '').trim() : '';
        
        let tokenByApi = '';
        if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
            try { tokenByApi = (window.turnstile.getResponse() || '').trim(); } catch(e) {}
        }
        
        const hasToken = (tokenVal.length > 10) || (tokenByApi.length > 10);
        
        const buttons = Array.from(document.querySelectorAll('button'));
        const modalBtn = buttons.find(b => {
            const txt = (b.innerText || '').toLowerCase();
            return txt.includes('renew for 4') || txt.includes('renew for 4 days');
        });
        
        const btnEnabled = modalBtn ? (!modalBtn.disabled && !modalBtn.hasAttribute('disabled')) : false;
        
        return {
            hasToken: hasToken,
            tokenLen: Math.max(tokenVal.length, tokenByApi.length),
            btnFound: Boolean(modalBtn),
            btnEnabled: btnEnabled,
            btnText: modalBtn ? (modalBtn.innerText || '').trim() : ''
        };
    """
    try:
        res = sb.execute_script(js_code)
        if isinstance(res, dict):
            return res
    except Exception as e:
        print(f"⚠️ 执行 Turnstile 状态检测异常: {e}")
    return {"hasToken": False, "tokenLen": 0, "btnFound": False, "btnEnabled": False, "btnText": ""}

# 等待并执行 Turnstile 人机验证
def wait_for_turnstile_pass(sb, timeout=45):
    start = time.time()
    sb.sleep(3)
    while time.time() - start < timeout:
        status = check_turnstile_status(sb)
        if status["hasToken"] or status["btnEnabled"]:
            print(f"✅ Turnstile 人机验证已通过！(Token长度: {status['tokenLen']}, 续期按钮已就绪: {status['btnEnabled']})")
            return True

        # 尝试通过 SeleniumBase 内置方法点击验证码
        try:
            sb.uc_gui_handle_captcha()
        except Exception:
            pass

        sb.sleep(2)
        status = check_turnstile_status(sb)
        if status["hasToken"] or status["btnEnabled"]:
            print(f"✅ Turnstile 人机验证已通过！(Token长度: {status['tokenLen']}, 续期按钮已就绪: {status['btnEnabled']})")
            return True

        try:
            sb.uc_gui_click_captcha()
        except Exception:
            pass

        sb.sleep(2)
        status = check_turnstile_status(sb)
        if status["hasToken"] or status["btnEnabled"]:
            print(f"✅ Turnstile 人机验证已通过！(Token长度: {status['tokenLen']}, 续期按钮已就绪: {status['btnEnabled']})")
            return True

        # 尝试穿透进入 iframe 寻找点击复选框
        try:
            cf_frames = sb.find_elements('iframe[src*="cloudflare.com"], iframe[src*="challenges.cloudflare.com"]')
            for frame in cf_frames:
                try:
                    sb.switch_to_frame(frame)
                    for sel in ['input[type="checkbox"]', '#cf-stage input', '.cb-i', 'span.mark', '#challenge-stage input', 'body']:
                        if sb.is_element_visible(sel):
                            sb.click(sel)
                            break
                    sb.switch_to_default_content()
                except Exception:
                    sb.switch_to_default_content()
        except Exception:
            try:
                sb.switch_to_default_content()
            except Exception:
                pass

        sb.sleep(2)
        elapsed = int(time.time() - start)
        print(f"⏳ 正在等待 Turnstile 验证就绪... 已耗时 {elapsed}s")

    print(f"❌ Turnstile 验证超时（{timeout}s 未能生成有效 Token 或解除按钮禁用）")
    return False
    
# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except:
        return countdown_str

# 获取过期日期
def extract_expiry_date(page_source: str) -> str:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})",   # Expires 2026/07/07
        r"[Ee]xpires\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",   # Expires 07/07/2026 (MM/DD/YYYY)
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew",        # 2026/07/07 - renew
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew",        # 07/07/2026 - renew
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew manually to extend for 4 days", # 2026/07/07 - renew manually to extend for 4 days
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            date_str = match.group(1)
            # 如果是 MM/DD/YYYY 格式，转换为 YYYY/MM/DD
            if len(date_str.split('/')[-1]) == 4:  # 年份长度4
                parts = date_str.split('/')
                if len(parts[0]) == 2:  # 第一部分是2位（月）
                    # 修正：将 MM/DD/YYYY 转为 YYYY/MM/DD
                    return f"{parts[2]}/{parts[0]}/{parts[1]}"
            return date_str
    return None

#   Discord OAuth 登录（SESSION_TOKEN 失效时的备用方案）
DISCORD_CLIENT_ID   = "884382422530158623"
OAUTH_REDIRECT_URI  = "https://bot-hosting.net/login"
OAUTH_SCOPE         = "identify email guilds"
DISCORD_API         = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
STATE_RE = re.compile(r"[?&]state=([^&]+)")


def capture_discord_state(sb) -> str:
    """打开 /login/discord，从落地页 URL 里提取本次会话的 state"""
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=4)
    time.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 相关页面，当前 URL：{url}")
        return ""

    m = STATE_RE.search(url)
    if not m:
        print(f"❌ 未能从 URL 中解析出 state，当前 URL：{url}")
        return ""

    state = urllib.parse.unquote(m.group(1))
    print(f"✅ 已捕获 state（当前落地页：{urllib.parse.urlparse(url).path}）")
    return state


def discord_authorize(state: str) -> str:
    """用 DC_TOKEN 直接完成 Discord 侧授权，返回跳转回 bot-hosting.net 的 location"""
    query = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "scope":         OAUTH_SCOPE,
        "state":         state,
    })
    authorize_url = f"{DISCORD_API}?{query}"

    referer = (
        "https://discord.com/oauth2/authorize?" +
        urllib.parse.urlencode({
            "client_id":     DISCORD_CLIENT_ID,
            "redirect_uri":  OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope":         OAUTH_SCOPE,
            "state":         state,
        })
    )

    headers = {
        "accept":           "*/*",
        "authorization":    DC_TOKEN,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }

    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })

    # 如果配置了代理，Discord API 请求也走代理
    proxies = None
    _is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    _proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    if _is_proxy:
        proxies = {"http": _proxy_server, "https": _proxy_server}

    try:
        resp = requests.post(authorize_url, headers=headers, data=body, proxies=proxies, timeout=20)
        if resp.status_code != 200:
            print(f"❌ Discord OAuth2 授权失败: HTTP {resp.status_code} - {resp.text[:300]}")
            return ""
        resp_data = resp.json()
    except Exception as e:
        print(f"❌ Discord OAuth2 授权异常: {e}")
        return ""

    location = resp_data.get("location", "")
    if not location:
        print(f"❌ 授权响应中未找到 location 字段: {resp_data}")
        return ""

    masked = re.sub(r"code=[^&]+", "code=***", location)
    print(f"✅ 拿到回调 URL: {masked}")
    return location


def do_discord_login(sb) -> bool:
    """通过 Discord Token 走完整 OAuth 流程登录 bot-hosting.net"""
    print("\n🔑 通过 Discord Token 登录...")

    state = capture_discord_state(sb)
    if not state:
        sb.save_screenshot("login_no_state.png")
        return False

    location = discord_authorize(state)
    if not location:
        return False

    print("↩️ 携带授权码打开回调链接...")
    sb.uc_open_with_reconnect(location, reconnect_time=4)
    time.sleep(3)

    url = sb.get_current_url()

    if "/error/banned" in url:
        print("🚫 账号已被封禁")
        sb.save_screenshot("login_banned.png")
        return False

    if "bot-hosting.net" not in url:
        print(f"❌ 回调后未跳转至 bot-hosting.net，当前 URL：{url}")
        sb.save_screenshot("login_no_redirect.png")
        return False

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        sb.save_screenshot("login_fraud.png")
        return False

    for _ in range(30):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    try:
        body_text = sb.get_text("body")
        print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
    except Exception:
        pass
    sb.save_screenshot("login_timeout.png")
    return False


# 主流程
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    sb_kwargs = {"uc": True, "headless": HEADLESS}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    global _LOGIN_METHOD

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # 方式1: SESSION_TOKEN Cookie 登录（默认）
        if SESSION_TOKEN:
            print("🚀 启动浏览器...")
            sb.open("https://bot-hosting.net/")
            sb.wait_for_ready_state_complete()
            sb.sleep(2)

            print("📝 注入 Cookie...")
            for name, value in COOKIES.items():
                if value:
                    sb.add_cookie({"name": name, "value": value, "domain": "bot-hosting.net"})

            print("🌐 访问 https://bot-hosting.net/a/billings ...")
            sb.open("https://bot-hosting.net/a/billings")
            sb.wait_for_ready_state_complete()
            sb.sleep(3)
            current_url = sb.get_current_url()
            current_title = sb.get_title()
            print(f"📝 当前URL: {current_url}, Title: {current_title}")

            if "/a/billings" in current_url and "/login" not in current_url and "error=" not in current_url:
                login_ok = True
                print("✅ SESSION_TOKEN 登录成功, 当前已到达账单页")
            else:
                print(f"❌ SESSION_TOKEN 登录失败，当前URL: {current_url}, 当前标题: {current_title}")

        # 方式2: Discord OAuth 登录（备用）
        if not login_ok and DC_TOKEN:
            _LOGIN_METHOD = "Discord Token"
            print("\n🔄 SESSION_TOKEN 登录失败或未配置，尝试 Discord OAuth 登录...")
            if do_discord_login(sb):
                print("🌐 访问 https://bot-hosting.net/a/billings ...")
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                if "a/billings" in current_url:
                    login_ok = True
                    print("✅ Discord OAuth 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Discord OAuth 登录后仍未到达账单页，当前URL: {current_url}")
            else:
                print("❌ Discord OAuth 登录失败")

        if not login_ok:
            error_msg = "Cookie 已失效或页面异常"
            if not SESSION_TOKEN and DC_TOKEN:
                error_msg = "Discord OAuth 登录失败"
            elif SESSION_TOKEN and DC_TOKEN:
                error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg))
            return

        if _LOGIN_METHOD == "Discord Token":
            print("ℹ️ 本次使用 Discord OAuth 登录，新的 SESSION_TOKEN 将自动更新到 Secrets")

        # 提取当前到期日期
        sb.sleep(2)
        page_source = sb.get_page_source()
        current_expiry = extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        # 寻找外部续期按钮
        outer_renew_selector = None
        countdown_text = None
        possible_selectors = [
            'button:contains("Renew")',
            'button:contains("Renew free plan")',
            'a:contains("Renew")',
            '[class*="renew"]',
            '[class*="Renew"]',
        ]

        for selector in possible_selectors:
            try:
                if sb.is_element_visible(selector):
                    button_text = sb.get_text(selector)
                    if "Renew in" in button_text:
                        match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", button_text)
                        if match:
                            countdown_text = match.group(1)
                        break
                    elif "Renew" in button_text and "in" not in button_text.lower():
                        outer_renew_selector = selector
                        print(f"✅ 续期按钮可用: '{button_text}'")
                        break
            except Exception as e:
                pass

        # 点击外部续期按钮等待弹窗
        if outer_renew_selector:
            print("🔄 点击外部续期按钮，等待弹窗加载...")
            sb.save_screenshot("step1_before_outer_click.png")
            try:
                sb.sleep(2)
                sb.click(outer_renew_selector)
                print("✅ 外部续期按钮已点击")
                sb.sleep(8)  # 等待模态框及其内的 Turnstile 组件加载
                sb.save_screenshot("step2_modal_opened.png")
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                sb.save_screenshot("step2_outer_click_failed.png")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            # 处理弹窗中的 Turnstile
            print("🔒 检测弹窗中的 Turnstile 验证状态...")
            turnstile_passed = wait_for_turnstile_pass(sb, timeout=45)
            sb.save_screenshot("step3_turnstile_status.png")

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                send_telegram_message(format_notification("❌ 续期失败", error="Turnstile 人机验证未通过或超时"))
                return

            # 点击弹窗续期按钮
            print("⏳ 弹窗验证通过，准备点击 'Renew for 4 days' 确认按钮...")
            sb.sleep(2)
            sb.save_screenshot("step4_before_modal_click.png")

            modal_button_clicked = False
            modal_selectors = [
                'button:contains("Renew for 4 days")',
                'button:contains("Renew for 4 Days")',
                'button:contains("Renew for 4")',
            ]
            for m_sel in modal_selectors:
                try:
                    if sb.is_element_visible(m_sel):
                        sb.click(m_sel, timeout=5)
                        modal_button_clicked = True
                        print(f"✅ 已通过选择器点击弹窗确认按钮: {m_sel}")
                        break
                except Exception as e:
                    print(f"⚠️ 选择器点击弹窗按钮失败: {e}")

            if not modal_button_clicked:
                try:
                    js_click = """
                        const buttons = Array.from(document.querySelectorAll('button'));
                        const btn = buttons.find(b => {
                            const txt = (b.innerText || '').toLowerCase();
                            return txt.includes('renew for 4');
                        });
                        if (btn) {
                            btn.click();
                            return true;
                        }
                        return false;
                    """
                    clicked = sb.execute_script(js_click)
                    if clicked:
                        modal_button_clicked = True
                        print("✅ 已通过 JavaScript 触发弹窗续期按钮点击")
                except Exception as e:
                    print(f"❌ JS 点击弹窗按钮异常: {e}")

            if not modal_button_clicked:
                print("❌ 未能点击弹窗中的确认续期按钮！")
                sb.save_screenshot("step4_modal_click_failed.png")
                send_telegram_message(format_notification("❌ 续期失败", error="未能点击弹窗内的续期确认按钮"))
                return

            print("⏳ 已点击确认续期，等待服务端异步处理 (等待10秒)...")
            sb.sleep(10)
            sb.save_screenshot("step5_after_modal_click.png")

            # 先检查当前页面（SPA局部更新）
            page_text = sb.get_page_source()
            match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", page_text)
            new_expiry = extract_expiry_date(page_text)

            # 如果当前页面没有倒计时且到期日期未变，刷新账单页验证服务端持久化数据
            if not match and (not new_expiry or new_expiry == current_expiry):
                print("🔄 重新加载账单页以获取服务端最新状态...")
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(4)
                page_text = sb.get_page_source()
                match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", page_text)
                new_expiry = extract_expiry_date(page_text)

            sb.save_screenshot("step6_after_reload.png")

            # 检查页面是否仍然留有未续期的外部按钮
            still_has_renew_button = False
            try:
                for sel in ['button:contains("Renew free plan")', 'button:contains("Renew")']:
                    if sb.is_element_visible(sel):
                        txt = sb.get_text(sel)
                        if "Renew" in txt and "Renew in" not in txt:
                            still_has_renew_button = True
                            break
            except Exception:
                pass

            # 严格结果判定，绝不虚假上报成功
            if match:
                new_countdown = match.group(1)
                friendly_countdown = format_countdown(new_countdown)
                print(f"✅ 续期成功！下次可续期倒计时: {new_countdown} ({friendly_countdown})")
                if new_expiry:
                    print(f"📅 最新到期日期: {new_expiry}")
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=f"⏱️ 下次可续期时间: {friendly_countdown}后",
                        expiry_date=new_expiry or current_expiry or "（未获取到）"
                    )
                )
            elif new_expiry and new_expiry != current_expiry:
                print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=f"📅 到期日期已更新 ({current_expiry} ➔ {new_expiry})",
                        expiry_date=new_expiry
                    )
                )
            elif not still_has_renew_button and modal_button_clicked:
                # 续期按钮已消失（进入正常锁定状态）
                print(f"✅ 续期按钮已消失，续期成功生效！")
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra="可续期按钮已更新锁定",
                        expiry_date=new_expiry or current_expiry or "（未获取到）"
                    )
                )
            else:
                # 依然存在“Renew free plan”按钮且到期时间无变化 -> 确凿的续期未生效
                print(f"❌ 续期未生效：页面仍保留可续期按钮，到期时间仍为 {current_expiry}！")
                send_telegram_message(
                    format_notification(
                        "⚠️ 续期未成功生效",
                        extra=f"脚本已尝试点击，但服务端未接受续期（页面仍显示可续期，到期日仍为 {current_expiry}）。请手动在网页端续期，并可在 GitHub Actions 检查上传的截图制品排查原因。",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )

        else:
            if countdown_text:
                friendly = format_countdown(countdown_text)
                print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                send_telegram_message(
                    format_notification(
                        "⏳ 未到续期时间",
                        extra=f"⏱️ 可续期时间: {friendly}后",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )
            else:
                print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                send_telegram_message(
                    format_notification(
                        "ℹ️ 无需续期",
                        extra="当前状态未知，请手动检查",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )

        # 更新SESSION_TOKEN 
        print("🔄 检查 SESSION_TOKEN 是否需要更新")
        new_token, token_expiry = get_cookie_info(sb, "session_token")
        old_token = SESSION_TOKEN

        if should_update_cookie(new_token, old_token, token_expiry):
            print("🔄 SESSION_TOKEN 需要更新")
            if GH_TOKEN:
                if update_github_secret("SESSION_TOKEN", new_token):
                    print("✅ SESSION_TOKEN 更新成功")
                else:
                    print("⚠️ 更新失败，请检查 GH_TOKEN 权限")
            else:
                print("⚠️ 未设置 GH_TOKEN，无法自动更新")
                print(f"📋 请手动设置 SESSION_TOKEN = {new_token[:4]}...{new_token[-4:]}")
        else:
            print("✅ SESSION_TOKEN 无需更新")
        
        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()
