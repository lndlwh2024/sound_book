# -*- coding: utf-8 -*-
"""
Playwright 浏览器会话与扫码登录管理模块 (Douyin Browser & Session Manager)
"""
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

from playwright.sync_api import sync_playwright, BrowserContext, Page

from .models import DouyinAccountConfig

logger = logging.getLogger(__name__)

DOUYIN_CREATOR_URL = "https://creator.douyin.com/"
DOUYIN_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"


class DouyinBrowserManager:
    """
    抖音浏览器环境管理器。
    负责通过 Playwright 管理各账号的持久化浏览器上下文（Persistent Context），
    注入反检测脚本，提供可视化扫码登录引导与就绪检查。
    """
    def __init__(self):
        pass

    @staticmethod
    def get_anti_detection_args() -> list:
        """获取专业反自动化检测与浏览器伪装参数"""
        return [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--window-size=1280,800"
        ]

    @staticmethod
    def inject_stealth(page: Page) -> None:
        """向页面注入反爬伪装脚本，抹除 Playwright 特征"""
        stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        window.chrome = {
            runtime: {}
        };
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['zh-CN', 'zh', 'en']
        });
        """
        page.add_init_script(stealth_js)

    def launch_interactive_login(self, account: DouyinAccountConfig, timeout_secs: int = 180) -> Tuple[bool, str]:
        """
        以可视化窗口启动浏览器，引导用户进行抖音扫码登录。
        自动循环探测是否登录成功（URL 跳转或出现创作者中心主页）。
        """
        profile_path = Path(account.profile_dir).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"正在为账号 [{account.account_name}] 启动扫码登录浏览器: {profile_path}")

        try:
            with sync_playwright() as p:
                context: BrowserContext = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=False,
                    args=self.get_anti_detection_args(),
                    viewport={"width": 1280, "height": 800}
                )
                page = context.new_page() if not context.pages else context.pages[0]
                self.inject_stealth(page)

                logger.info(f"导航至抖音创作者中心: {DOUYIN_CREATOR_URL}")
                page.goto(DOUYIN_CREATOR_URL, wait_until="domcontentloaded", timeout=60000)

                start_time = time.time()
                is_logged_in = False

                while time.time() - start_time < timeout_secs:
                    # 检查是否关闭了浏览器窗口
                    if page.is_closed():
                        logger.warning("用户主动关闭了登录浏览器窗口")
                        break

                    cur_url = page.url.lower()
                    # 登录成功的特征：URL 包含 creator-micro 或出现上传入口/创作者头像
                    if "creator-micro" in cur_url or "content/upload" in cur_url:
                        is_logged_in = True
                        break

                    # 探测页面关键已登录特征元素
                    try:
                        if page.locator("text=发布视频").count() > 0 or page.locator("text=上传视频").count() > 0:
                            is_logged_in = True
                            break
                    except Exception:
                        pass

                    time.sleep(2)

                if is_logged_in:
                    account.status = "AUTHORIZED"
                    account.last_auth_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    logger.info(f"账号 [{account.account_name}] 扫码登录认证成功！")
                    # 等待 Cookie 充分持久化落盘
                    time.sleep(3)
                    context.close()
                    return True, "登录授权成功"
                else:
                    context.close()
                    return False, "登录超时或未完成扫码"

        except Exception as e:
            err_msg = f"启动或操作登录浏览器异常: {e}"
            logger.error(err_msg)
            return False, err_msg
