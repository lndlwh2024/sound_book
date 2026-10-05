# -*- coding: utf-8 -*-
"""
抖音页面 DOM 智能探针模块 (Douyin DOM Probe & Inspector)
实现方案 A：在可视化浏览器中智能提取关键表单、合集弹窗、按钮与输入框结构，生成轻量 JSON 报表与页面全景快照。
"""
import json
import logging
import time
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

from playwright.sync_api import sync_playwright, BrowserContext, Page

from .models import DouyinAccountConfig
from .browser import DouyinBrowserManager, DOUYIN_UPLOAD_URL

logger = logging.getLogger(__name__)


class DouyinDOMProbe:
    """
    抖音页面 DOM 智能探针。
    用于在用户操作页面时（如选择视频、点开合集下拉框后），一键抓取结构化 DOM 报表与快照。
    """
    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or (Path(__file__).resolve().parent.parent.parent.parent / "data")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def extract_dom_elements(page: Page) -> Dict[str, Any]:
        """
        通过执行浏览器端 JavaScript 脚本，智能提取页面核心可交互节点。
        """
        probe_js = """
        () => {
            const getXPath = (el) => {
                if (!el) return '';
                if (el.id) return `//*[@id="${el.id}"]`;
                const parts = [];
                while (el && el.nodeType === Node.ELEMENT_NODE) {
                    let index = 1;
                    let sibling = el.previousSibling;
                    while (sibling) {
                        if (sibling.nodeType === Node.ELEMENT_NODE && sibling.tagName === el.tagName) {
                            index++;
                        }
                        sibling = sibling.previousSibling;
                    }
                    parts.unshift(`${el.tagName.toLowerCase()}[${index}]`);
                    el = el.parentNode;
                }
                return `/${parts.join('/')}`;
            };

            const safeClass = (el) => {
                if (!el || !el.className) return '';
                if (typeof el.className === 'string') return el.className;
                if (el.className.baseVal) return el.className.baseVal;
                return String(el.className);
            };

            const safeText = (el, maxLen = 100) => {
                if (!el) return '';
                const raw = ((el.innerText || el.textContent || '') + '').trim();
                return maxLen ? raw.slice(0, maxLen) : raw;
            };

            const inputs = Array.from(document.querySelectorAll('input')).map(el => ({
                tag: 'input',
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                className: safeClass(el),
                accept: el.accept || '',
                xpath: getXPath(el)
            }));

            const buttons = Array.from(document.querySelectorAll('button, [role="button"]')).map(el => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || '',
                innerText: safeText(el, 100),
                className: safeClass(el),
                type: el.type || '',
                xpath: getXPath(el)
            }));

            const textareas = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]')).map(el => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || '',
                placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '',
                className: safeClass(el),
                xpath: getXPath(el)
            }));

            const modals = Array.from(document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="dialog"], [class*="dropdown"], [class*="popover"], [class*="select"]')).map(el => ({
                tag: el.tagName.toLowerCase(),
                className: safeClass(el),
                innerText: safeText(el, 100),
                xpath: getXPath(el)
            }));

            return {
                url: window.location.href,
                title: document.title,
                timestamp: new Date().toISOString(),
                inputs: inputs,
                buttons: buttons,
                textareas: textareas,
                modals: modals
            };
        }
        """
        return page.evaluate(probe_js)

    def capture_dom_report(self, page: Page) -> Tuple[Path, Path]:
        """抓取并落盘 DOM 报告 JSON 与高保真全屏截图"""
        report_data = self.extract_dom_elements(page)
        time_tag = time.strftime("%Y%m%d_%H%M%S")
        report_json_path = self.output_dir / f"douyin_dom_report_{time_tag}.json"
        snapshot_png_path = self.output_dir / f"douyin_page_snapshot_{time_tag}.png"

        # 写入 JSON 报告
        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)

        # 截取全屏快照
        page.screenshot(path=str(snapshot_png_path), full_page=True)

        logger.info(f"DOM 结构报告已导出: {report_json_path}")
        logger.info(f"页面全景截图已保存: {snapshot_png_path}")
        return report_json_path, snapshot_png_path

    def run_interactive_probe(self, account: DouyinAccountConfig, timeout_secs: int = 300) -> Tuple[bool, str]:
        """
        打开可视化上传页，等待用户操作并在窗口关闭时（或手动触发）自动完成 DOM 采集。
        """
        profile_path = Path(account.profile_dir).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"正在为账号 [{account.account_name}] 打开 DOM 探针采集窗口...")

        try:
            with sync_playwright() as p:
                context: BrowserContext = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=False,
                    args=DouyinBrowserManager.get_anti_detection_args(),
                    viewport={"width": 1440, "height": 900}
                )
                page = context.new_page() if not context.pages else context.pages[0]
                DouyinBrowserManager.inject_stealth(page)

                page.goto(DOUYIN_UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                logger.info("已打开抖音创作者上传页。请上传一个测试视频，并展开合集下拉框，随后保持页面！")

                start_time = time.time()
                captured = False
                json_p, img_p = None, None

                # 探测用户是否触发了上传或点开了合集
                while time.time() - start_time < timeout_secs:
                    if page.is_closed():
                        break

                    # 当检测到页面有文本输入框或出现“合集”字样时，自动周期性记录最佳快照
                    try:
                        has_editor = page.locator("[contenteditable='true']").count() > 0 or page.locator("textarea").count() > 0
                        has_collection = page.locator("text=合集").count() > 0
                        if has_editor or has_collection:
                            # 抓取当前快照
                            json_p, img_p = self.capture_dom_report(page)
                            captured = True
                            logger.info(f"已成功捕获当前表单与合集 DOM 报告！")
                            break
                    except Exception:
                        pass

                    time.sleep(3)

                if not captured and not page.is_closed():
                    json_p, img_p = self.capture_dom_report(page)
                    captured = True

                context.close()
                if captured:
                    return True, f"成功抓取 DOM 报告: {json_p.name}"
                else:
                    return False, "用户过早关闭窗口，未采集到完整 DOM"

        except Exception as e:
            err_msg = f"DOM 探针运行异常: {e}"
            logger.error(err_msg)
            return False, err_msg
