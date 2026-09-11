from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional
from log_util import log

# playwright 只在浏览器路径用得到，且默认关闭（browser_listener=false）。
# 放到函数内部 import，免得没装 playwright 的部署（比如服务器）连轮询都起不来。

TAG = "ext-web"


class SessionManager:
    def __init__(self, session_dir: str, domain: str = "larksuite.com"):
        self.session_dir = Path(os.path.expanduser(session_dir))
        self.domain = domain
        self.state_file = self.session_dir / "storage_state.json"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def has_session(self) -> bool:
        """检查是否存在已保存的会话状态"""
        return self.state_file.exists() and self.state_file.stat().st_size > 10

    async def login_interactive(self, headless: bool = False) -> bool:
        """启动浏览器进行首次登录并持久化保存 StorageState"""
        from playwright.async_api import async_playwright

        # 直接访问租户主页，由官方前端自动引导至带完整 context 的扫码登录页
        login_url = f"https://c8ytzah4el.sg.{self.domain}/messenger"
        log(TAG, "session", "info", f"启动浏览器进行登录引导: {login_url}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            await page.goto(login_url)
            log(TAG, "session", "info", "请在弹出的浏览器窗口中扫码登录...")

            # 轮询检测：只要捕获到 session 相关 cookie 或者页面进入 messenger/drive，即判定登录成功
            success = False
            for _ in range(300):  # 最多等待 5 分钟
                await asyncio.sleep(1)
                try:
                    cookies = await context.cookies()
                    cookie_names = {c.get("name") for c in cookies}
                    current_url = page.url
                    # 关键登录态 cookie 存在，且不在登录引导/accounts 页
                    has_auth_cookie = any(k in cookie_names for k in ["session", "bear-session", "passport_session"])
                    is_in_app = ("messenger" in current_url or "drive" in current_url or "suite" in current_url) and "accounts" not in current_url and "login" not in current_url

                    if has_auth_cookie and is_in_app:
                        log(TAG, "session", "info", f"检测到登录成功！当前页面: {current_url}")
                        success = True
                        break
                except Exception:
                    pass

            if success:
                log(TAG, "session", "info", "正在导出并保存 session 状态...")
                await context.storage_state(path=str(self.state_file))
                log(TAG, "session", "info", f"会话状态已成功保存至: {self.state_file}")
                await browser.close()
                return True
            else:
                log(TAG, "session", "error", "登录超时或未检测到有效登录态")
                await browser.close()
                return False


    async def create_authenticated_context(self, playwright_instance, headless: bool = True):
        """基于持久化的 session 状态创建已认证的 BrowserContext"""
        if not self.has_session():
            raise FileNotFoundError(f"未找到登录会话文件: {self.state_file}，请先执行登录")
        
        browser = await playwright_instance.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = await browser.new_context(
            storage_state=str(self.state_file),
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
        return context
