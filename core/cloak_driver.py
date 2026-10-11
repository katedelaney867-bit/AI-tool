# -*- coding: utf-8 -*-
"""CloakBrowser 的 Selenium 风格适配层。

提炼自 turb-gpt-free-register/core/cloakbrowser_driver.py。改动：

* 去掉 config.cloakbrowser / config.browser / config.proxy 依赖，全部改为函数参数；
* 去掉项目自建的出口 IP 地理探测 —— CloakBrowser 自带 `geoip=True` 会按出口 IP
  自动匹配 timezone / locale / WebRTC，少一次外部 HTTP 请求，也少一个失败点；
* 保留类名 `CloakSeleniumDriver`：core/browser_flow.py 的日志前缀靠它识别。

页面流程是按 Selenium API 写的（`find_element(By.XPATH, ...)` /
`execute_script` / `send_keys`），这里把这套 API 映射到 Playwright。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PAGE_LOAD_TIMEOUT = 90

#: CloakBrowser 报「地理库不可用」时，错误文本里一定带这些词。
#: 实测原文（2026-10-11，内测第一台 Win10，没点「下载内核」直接开跑）：
#:     RuntimeError: GeoIP lookup failed for 23.237.50.35: [Errno 2]
#:     No such file or directory: b'...\\AI小工具\\browser\\geoip\\GeoLite2-City.mmdb'
_GEOIP_ERR = re.compile(r"geoip|geolite|mmdb", re.I)


def _launch_with_geoip_fallback(launch: Any, options: dict) -> Any:
    """geoip 失败必须**降级**，绝不能致命。

    ## 为什么这个坑一定会被踩到（不是偶发）

    交付包为了体积（84MB）**不带浏览器内核**（带上是 ~600MB），
    而地理库 `GeoLite2-City.mmdb` 是**随内核一起下发**的。
    所以**任何全新用户第一次开跑时，这个库一定不存在** ——
    只要没先点「下载内核」，`geoip=True` 就必然抛错。

    ## 旧行为造成的损失（比"报个错"严重得多）

    异常冒到 `registration` 的重试循环里被当成「这条连接连不上」→ 重试 8 次
    （同一个确定性错误，必然全败）→ 判死账号。代价有两笔：

    1. 白等约 24 秒 + 一次没意义的 30 秒首试；
    2. ★ **邮箱已经分配出去了，而号池对已 delivered 的邮箱单不退**
       （见 AGENTS.md「号池的退款规则」）—— 实测 ¥0.04/次，纯粹白烧。

    ## 新行为

    只在**错误文本确实像 geoip 问题**时降级（不误伤代理错误），
    关掉 geoip 再来一次。代价是 locale 退回系统语言、可能和出口 IP 不自洽
    （见 core/browser_flow.py 的说明），但**能跑** —— 总比整轮死掉强。
    真在意指纹一致，就先点界面上的「下载内核」把库补上。
    """
    if not options.get("geoip"):
        return launch(**options)
    try:
        return launch(**options)
    except Exception as exc:  # noqa: BLE001
        if not _GEOIP_ERR.search(str(exc)):
            raise  # 不是 geoip 的问题（比如代理连不上），原样抛出走重试
        logger.warning(
            "[Cloak] geoip 不可用，本次降级为 geoip=False 继续启动。"
            "原因通常是浏览器内核/地理库还没下载 —— 点界面上的「下载内核」可根治。"
            "注意：关掉 geoip 后 locale 会退回系统语言，与出口 IP 可能不一致。原始错误：%s",
            str(exc)[:200],
        )
        return launch(**dict(options, geoip=False))


@dataclass
class CloakOpenResult:
    profile_id: str = "cloakbrowser"
    raw: dict | None = None


class CloakElement:
    """Selenium WebElement 的最小子集。"""

    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        return self._handle().evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate(
                "el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)"
                " && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"
            ))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def select_option(self, value: str) -> None:
        """选中 <select> 里 value 对应的 option。

        ★ 为什么不用 JS：add-phone 的国家选择器是 React 受控组件，
        `select.value = 'CO'` 加派发 change，DOM 上看着切换了，React 自己的 state
        还停在默认值（美国）—— 号码框于是按 +1 格式化，`573012731422` 被吃成
        `(572) 217-2403`，一个号就这么废了（2026-10-03 实测）。
        Playwright 的 select_option 走真实选择流程，React 会当成用户操作。
        """
        if self.locator is not None:
            self.locator.select_option(value=value, timeout=10000)
        else:
            self.handle.select_option(value=value, timeout=10000)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
        except Exception:
            # 部分非 input 元素不支持 fill，回退键盘清空。
            self.click()
            self.page.keyboard.press("Meta+A")
            self.page.keyboard.press("Backspace")

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    @property
    def text(self) -> str:
        """Selenium WebElement.text 的对应物。

        适配层原来漏了这个属性，而 browser_flow 里有一处 `btn.text`
        （挑「重新发送验证码」按钮时用它做匹配）。结果是 AttributeError：
        'CloakElement' object has no attribute 'text' —— 实测在验证码被拒、
        需要重发的那条路径上直接炸掉，整个注册白跑。
        innerText / textContent / value 依次兜底，拿不到就回空串（调用方本来就会兜底）。
        """
        try:
            return str(self._eval(
                "el => (el.innerText || el.textContent || el.value || '').trim()"
            ) or "")
        except Exception:
            return ""

    def send_keys(self, *values: str) -> None:
        """兼容 `el.send_keys(Keys.COMMAND, 'a')` 与逐字符输入。

        普通文本必须保持**追加**语义：上层的人工输入会逐字符调用 send_keys，
        用 fill 会让输入框只剩最后一个字符。
        """
        text = "".join(str(v or "") for v in values)
        lower = text.lower()
        if "\ue03d" in text or "\ue009" in text or "command" in lower or "control" in lower:
            # Selenium Keys.CONTROL/COMMAND 的私有区编码，按全选处理。
            try:
                if self.locator is not None:
                    self.locator.press("ControlOrMeta+A", timeout=10000)
                else:
                    self.handle.press("ControlOrMeta+A", timeout=10000)
            except Exception:
                self.page.keyboard.press("Control+A")
            return
        special_keys = {
            "\ue003": "Backspace", "\ue004": "Tab", "\ue006": "Enter", "\ue007": "Enter",
            "\ue00c": "Escape", "\ue012": "ArrowLeft", "\ue013": "ArrowUp",
            "\ue014": "ArrowRight", "\ue015": "ArrowDown", "\ue017": "Delete",
        }
        if text in special_keys:
            try:
                if self.locator is not None:
                    self.locator.press(special_keys[text], timeout=10000)
                else:
                    self.handle.press(special_keys[text], timeout=10000)
            except Exception:
                self.page.keyboard.press(special_keys[text])
            return
        try:
            if self.locator is not None:
                self.locator.type(text, delay=12, timeout=10000)
            else:
                self.handle.type(text, delay=12, timeout=10000)
        except Exception:
            self.page.keyboard.insert_text(text)

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None


class _SwitchTo:
    def __init__(self, driver: "CloakSeleniumDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)


class CloakSeleniumDriver:
    """只实现页面流程实际用到的 WebDriver 子集。"""

    def __init__(self, browser: Any, context: Any | None, page: Any):
        self.browser = browser
        self.context = context
        self.page = page
        self._page_load_timeout_ms = DEFAULT_PAGE_LOAD_TIMEOUT * 1000
        self.switch_to = _SwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        return [str(i) for i in range(len(self._pages()))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages: list[Any] = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        self.page = pages[int(handle)]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    def get(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def quit(self) -> None:
        for closer in (
            lambda: self.context and self.context.close(),
            lambda: self.browser and self.browser.close(),
        ):
            try:
                closer()
            except Exception:
                pass

    def find_elements(self, by: Any, selector: str) -> list[CloakElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> CloakElement:
        elements = self.find_elements(by, selector)
        if not elements:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return elements[0]

    def _locator(self, by: Any, selector: str):
        by_text = str(by or "").lower()
        if "xpath" in by_text or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        try:
            context = self.context or self.page.context
            client = context.new_cdp_session(self.page)
            return client.send(cmd, params or {})
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[CloakElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list 里跨
        page.evaluate 传递；Selenium 脚本最常见的是 `arguments[0]` 为元素，
        所以只把第一个元素作为真实 DOM el 传入，其余保持 JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], CloakElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = [None if isinstance(item, CloakElement) else item for item in rest]
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return CloakElement(page, handle=element)
        try:
            return handle.json_value()
        except Exception as exc:
            message = str(exc)
            if "Execution context was destroyed" in message or "navigation" in message.lower():
                logger.info("[Cloak] JS 执行后页面发生跳转，忽略返回值读取失败：%s", message[:160])
                return {"ok": True, "reason": "navigation_after_script"}
            raise
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            if first_el is not None:
                result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
            else:
                result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本常以 `return ...` 结尾，用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        if first_el is not None:
            handle = first_el._eval_handle(element_wrapper, {"script": script, "args": serial_args})
        else:
            handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
        return self._unwrap_js_result(self.page, handle)


def build_cloak_driver(
    proxy: str | None = None,
    *,
    headless: bool = True,
    humanize: bool = True,
    geoip: bool = True,
    license_key: str = "",
    page_load_timeout: int = DEFAULT_PAGE_LOAD_TIMEOUT,
) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser，返回 Selenium 风格 driver。

    proxy="" 或 None 表示不走代理（直接用本机出口）。
    """
    try:
        from cloakbrowser import launch
    except ImportError as exc:
        raise RuntimeError("未安装 cloakbrowser，请执行：pip install \"cloakbrowser[geoip]\"") from exc

    proxy_url = str(proxy or "").strip().replace("socks5h://", "socks5://") or None
    options: dict[str, Any] = {
        "headless": bool(headless),
        "humanize": bool(humanize),
        "geoip": bool(geoip),
        # stealth_args 默认 True：CloakBrowser 自带指纹注入。
    }
    if proxy_url:
        options["proxy"] = proxy_url
    if license_key:
        options["license_key"] = license_key

    logger.info(
        "[Cloak] 启动：headless=%s humanize=%s geoip=%s proxy=%s",
        options["headless"], options["humanize"], options["geoip"], proxy_url or "无",
    )
    browser = _launch_with_geoip_fallback(launch, options)
    context = browser.new_context()
    page = context.new_page()
    driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
    driver._registration_log_prefix = "[注册]"

    # ★ 记录 openai/chatgpt 的**请求和响应**，失败现场才有得看。
    #   以前代码里到处 `getattr(driver, '_auth_network_events', [])`，
    #   但这个属性**从来没有人写过**，永远读到空列表 —— 排查时等于没有网络证据。
    #   「页面没动」到底是没发请求、还是请求被上游拒了，只能靠这个区分。
    #
    # ★★ 2026-10-04：只挂 response 不够用。
    #   卡在验证码页时日志里一条 openai 事件都没有，但这有两种截然不同的成因：
    #     ① 点击压根没触发提交 → 根本没有请求
    #     ② 请求发出去了，但上游把响应**挂着不回**（会话被风控时很常见）
    #   只监听 response 时，② 会因为「响应永远不来」而不留任何痕迹，
    #   和 ① 长得一模一样，没法查。所以同时挂 request，用 `kind` 区分：
    #     `req`  = 请求已发出（status 记成 '-'）
    #     `resp` = 响应已收到
    #   看到 `req` 找不到配对的 `resp`，就是 ②；一条 req 都没有才是 ①。
    try:
        events: list[dict] = []

        def _on_request(request: Any) -> None:
            try:
                url = str(getattr(request, "url", "") or "")
                if "openai.com" not in url and "chatgpt.com" not in url:
                    return
                entry = {
                    "kind": "req",
                    "method": str(getattr(request, "method", "") or ""),
                    "status": "-",
                    "url": url[:220],
                }
                # ★ POST 的请求体也要：通道到底是 sms 还是 whatsapp、号码是什么，
                #   只有请求体说得清（响应体只能说「400」）。
                if entry["method"].upper() in ("POST", "PUT", "PATCH"):
                    try:
                        post = getattr(request, "post_data", None)
                        if post:
                            entry["post"] = str(post)[:400]
                    except Exception:  # noqa: BLE001
                        pass
                events.append(entry)
                del events[:-40]
            except Exception:  # noqa: BLE001
                pass

        def _on_response(resp: Any) -> None:
            try:
                url = str(getattr(resp, "url", "") or "")
                if "openai.com" not in url and "chatgpt.com" not in url:
                    return
                request = getattr(resp, "request", None)
                status = int(getattr(resp, "status", 0) or 0)
                entry = {
                    "kind": "resp",
                    "method": str(getattr(request, "method", "") or ""),
                    "status": status,
                    "url": url[:220],
                }
                # ★ POST 的请求体也要：通道到底是 sms 还是 whatsapp、号码是什么，
                #   只有请求体说得清（响应体只能说「400」）。
                if entry["method"].upper() in ("POST", "PUT", "PATCH"):
                    try:
                        post = request.post_data
                        if post:
                            entry["post"] = str(post)[:400]
                    except Exception:  # noqa: BLE001
                        pass
                # ★ 失败响应一定要带上响应体：只看到 400 不知道原因，
                #   抓下来才能判断是「号码被拒」还是「参数不对」（2026-10-03 加）。
                if status >= 400:
                    try:
                        body = resp.text()
                        entry["body"] = str(body or "")[:400]
                    except Exception as exc:  # noqa: BLE001
                        entry["body"] = f"<读不到: {type(exc).__name__}>"
                events.append(entry)
                del events[:-40]
            except Exception:  # noqa: BLE001
                pass

        page.on("request", _on_request)
        page.on("response", _on_response)
        driver._auth_network_events = events
    except Exception:  # noqa: BLE001
        pass

    driver.set_page_load_timeout(int(page_load_timeout))
    return driver, CloakOpenResult(
        raw={"driver": "cloakbrowser", "proxy": proxy_url, "options": {k: v for k, v in options.items() if k != "license_key"}}
    )
