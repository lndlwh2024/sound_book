# -*- coding: utf-8 -*-
"""
抖音自动化上传发布与归档引擎模块 (Douyin Uploader & Publishing Pipeline)
        实现：注入文件 -> 表单就绪 -> 独立标题填写 -> 封面设置 -> 合集关联 -> 发布确认 -> 归档与报告。
"""
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Callable
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, BrowserContext, Page

from ..browser_workflow import BrowserPublishWorkflow
from ..archive import archive_completed_file
from ..titles import resolve_title, resolve_series
from .models import DouyinAccountConfig, PublishLedgerRecord
from .browser import DouyinBrowserManager, DOUYIN_UPLOAD_URL

logger = logging.getLogger(__name__)

TITLE_SELECTOR = 'input[placeholder="填写作品标题，为作品获得更多流量"]'
DURATION_LIMIT_NOTICE = re.compile(r'(?:视频|时长)[^\n]{0,80}(?:不能|不得|不可|超过|超出)[^\n]{0,30}(?:60\s*分钟|六十分钟|1\s*小时|一小时)')


class VideoDurationRejected(Exception):
    """Permanent file rejection: continue with the next video without retry."""


def probe_video_duration(video_path: Path):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                 '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path.resolve())],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', check=True, timeout=5,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        duration = float(result.stdout.strip())
        return duration if math.isfinite(duration) and duration >= 0 else None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        logger.warning('读取视频时长失败，将继续检查平台提示：%s', type(error).__name__)
        return None


class PublishCancelled(Exception):
    """Cooperative stop, raised only on the Playwright owning thread."""


def compute_file_md5(file_path: Path) -> str:
    """计算文件的 MD5 指纹"""
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def parse_video_title_and_episode(file_name: str) -> Tuple[str, str, Optional[int]]:
    """
    从标准生产视频文件名中智能解析书籍名、标题与集数。
    例如: "巴菲特致股东的信_第26集_38m_[f2ffe8f].mp4"
    返回: (clean_book_name, clean_title, episode_num)
    """
    stem = Path(file_name).stem
    # 移除 commit hash 标记 [f2ffe8f]
    clean_stem = re.sub(r'\[[a-f0-9]{7,8}\]', '', stem).strip('_ ')

    ep_match = re.search(r'第\s*(\d+)\s*(?:集|问)', clean_stem)
    episode_num = int(ep_match.group(1)) if ep_match else None

    # 书名提取：以下划线分隔的第一部分或全名
    parts = clean_stem.split('_')
    book_name = parts[0].strip() if parts else clean_stem
    # 清洗书名号
    clean_book_name = book_name.replace("《", "").replace("》", "").strip()

    return clean_book_name, clean_stem, episode_num


def resolve_publish_series(file_name: str) -> str:
    """Strip the product prefix, date and episode from the displayed series title."""
    return resolve_series(file_name)


class DouyinUploader(BrowserPublishWorkflow):
    """
    抖音创作者中心自动化上传器。
    """
    stage_interruptions = (PublishCancelled, VideoDurationRejected)

    def __init__(self, account: DouyinAccountConfig, headless: bool = False,
                 stop_requested: Optional[Callable[[], bool]] = None,
                 progress_callback: Optional[Callable[[str], None]] = None):
        self.account = account
        self.headless = headless
        self.profile_path = Path(account.profile_dir).resolve()
        self.profile_path.mkdir(parents=True, exist_ok=True)
        self._stop_requested = stop_requested or (lambda: False)
        self._progress_callback = progress_callback
        self.last_outcome = "FAILED"
        self.before_upload = lambda: None
        self.supports_upload_boundary = True
        self._network_events = []
        self.account_limit_reason = None
        self._video_selected = False
        self._upload_activity_seen = False
        self._publish_armed = False
        self._publish_result = None

    def _attach_publish_diagnostics(self, page):
        def on_response(response):
            url = urlsplit(response.url)
            endpoint = url.scheme + '://' + url.netloc + url.path
            publish_api = ('douyin.com' in url.netloc and response.request.method == 'POST'
                           and any(word in url.path.lower() for word in ('publish', 'aweme/create')))
            if not publish_api and response.status < 400:
                return
            event = {'kind': 'response', 'url': endpoint, 'http_status': response.status}
            if publish_api:
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        if self._publish_armed and re.search(r'/aweme/create(?:_v\d+)?/?$', url.path):
                            code = body.get('status_code', body.get('code'))
                            if code is not None or response.status >= 400:
                                if response.status >= 400:
                                    code = f'HTTP {response.status}'
                                self._publish_result = {'code': str(code), 'message': str(body.get('status_msg') or body.get('message') or '')[:300]}
                        message = str(body.get('status_msg') or body.get('message') or '')
                        if str(body.get('status_code')) == '2158' or re.search(r'(今日|今天).*(投稿|发布).*(上限|限额)', message):
                            self.account_limit_reason = message or '今日投稿已达到上限（业务码2158）'
                        event['result'] = {key: str(body[key])[:160] for key in
                                           ('status_code', 'status_msg', 'code', 'message', 'error_msg')
                                           if key in body and isinstance(body[key], (str, int, float, bool))}
                except Exception:
                    pass
            if len(self._network_events) >= 40 and not self.account_limit_reason:
                return
            if len(self._network_events) >= 40:
                self._network_events.pop(0)
            self._network_events.append(event)
            self._progress(f"[接口诊断] HTTP {response.status} {endpoint}；{event.get('result', {})}")

        def on_failed(request):
            if len(self._network_events) < 40:
                url = urlsplit(request.url)
                if url.scheme not in ('http', 'https'):
                    return
                event = {'kind': 'requestfailed', 'url': url.scheme + '://' + url.netloc + url.path,
                         'error': request.failure}
                self._network_events.append(event)
                self._progress(f"[网络诊断] {event['url']}：{request.failure}")
        page.on('response', on_response)
        page.on('requestfailed', on_failed)

    def _navigate_upload_page(self, page):
        self._progress('正在打开上传页：主文档响应最多45秒，实际上传控件最多90秒')
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        try:
            page.goto(DOUYIN_UPLOAD_URL, wait_until='commit', timeout=45000)
        except PlaywrightTimeoutError:
            self._progress('[页面导航缓慢] 主文档响应超时，检查当前页面是否已进入创作者中心')
        from urllib.parse import urlsplit
        if urlsplit(page.url).hostname != 'creator.douyin.com':
            raise RuntimeError('45秒内未进入抖音创作者中心，未选择文件；请检查网络或重新登录')
        def upload_ready():
            if 'login' in page.url.lower():
                self.account.status='EXPIRED'
                self.last_outcome='AUTH_REQUIRED'
                raise RuntimeError('抖音登录已失效，未选择文件，请重新登录')
            from ..page_structure import file_input_selector
            inputs=page.locator(file_input_selector('douyin'))
            return any('image' not in (inputs.nth(i).get_attribute('accept') or '').lower()
                       for i in range(inputs.count()))
        self._wait_until(page, '等待抖音视频上传控件就绪', upload_ready, seconds=90)

    def _check_stop(self):
        if self._stop_requested():
            raise PublishCancelled()

    def _progress(self, message: str):
        logger.info(message)
        if self._progress_callback:
            self._progress_callback(message)

    def _check_duration_notice(self, page):
        notices = page.get_by_text(DURATION_LIMIT_NOTICE)
        for index in range(notices.count()):
            notice = notices.nth(index)
            if notice.is_visible():
                raise VideoDurationRejected(notice.inner_text()[:300])

    def _wait_for_title(self, page: Page, timeout_secs: float = 1800):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        title_input = page.locator(TITLE_SELECTOR + ':visible')
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            self._check_stop()
            self._check_duration_notice(page)
            try:
                count = title_input.count()
                if count > 1:
                    raise RuntimeError("存在多个可见作品标题输入框，停止发布以避免填写错误")
                if count == 1:
                    title_input.wait_for(state="visible", timeout=300)
                    if title_input.is_enabled() and title_input.is_editable():
                        self._check_stop()
                        return title_input
            except PlaywrightTimeoutError:
                pass
            self._pause(page, 100)
        raise RuntimeError("等待唯一可见且可编辑的作品标题输入框超时，请查看诊断报告")

    def _fill_title(self, title_input, title: str, timeout_secs: float = 30):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        self._check_stop()
        maxlength = title_input.get_attribute("maxlength")
        limit = int(maxlength) if maxlength and maxlength.isdigit() else 30
        value = title[:limit]
        if value != title:
            self._progress(f"作品标题超过 {limit} 字，已截取前 {limit} 字")
        deadline = time.monotonic() + timeout_secs
        last_error = None
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                # Short calls keep stop responsive; the overall budget tolerates SPA re-rendering.
                budget = max(1, min(300, int((deadline - time.monotonic()) * 1000)))
                title_input.fill(value, timeout=budget)
                self._check_stop()
                if title_input.input_value(timeout=budget) != value:
                    raise RuntimeError("作品标题回读不一致，停止发布")
                return
            except PlaywrightTimeoutError as error:
                last_error = error
        self._check_stop()
        raise RuntimeError(f"作品标题在 {timeout_secs:g} 秒内未能完成填写：{last_error}")

    def _prepare_title(self, page: Page, title: str):
        title_editor = self._wait_for_title(page)
        self._progress("正在填写作品标题；正文和话题不自动填写")
        self._fill_title(title_editor, title)

    def upload_single_video(
        self,
        video_path: Path,
        cover_path: Optional[Path] = None,
        custom_tags: Optional[List[str]] = None
    ) -> Tuple[bool, str, Optional[PublishLedgerRecord]]:
        """
        执行单个视频的完整自动化上传与发布流程。
        """
        if not video_path.exists():
            return False, f"视频文件不存在: {video_path}", None

        book_name, title, episode_num = parse_video_title_and_episode(video_path.name)
        book_name = resolve_publish_series(video_path.name)
        try:
            title = resolve_title(self.account, video_path.name, 'douyin')
        except ValueError as error:
            self.last_outcome = 'CONFIG_REQUIRED'
            return False, str(error), None
        # 当前发布模式只填写独立标题；正文与话题保持空白。
        publish_submitted = False
        self.last_outcome = "FAILED"
        self._network_events = []
        self.account_limit_reason = None
        self._video_selected = False
        self._upload_activity_seen = False
        self._publish_armed = False
        self._publish_result = None

        logger.info(f"开始上传视频: {video_path.name} | 归属书名: 《{book_name}》 | 集数: 第{episode_num}集")

        try:
            self._check_stop()
            duration = probe_video_duration(video_path)
            self._check_stop()
            if duration is not None:
                self._progress(f'[时长检查] {video_path.name}：{duration:.2f} 秒（上限 3600 秒）')
                if duration > 3600:
                    raise VideoDurationRejected(f'视频时长 {duration / 60:.2f} 分钟，超过60分钟；跳过且不重试')
            else:
                self._progress('[时长检查] 本地时长读取失败，将检查平台提示')
            self._progress("正在启动浏览器（启动调用最长等待 10 秒）")
            with sync_playwright() as p:
                context, page = None, None
                try:
                    context: BrowserContext = DouyinBrowserManager.create_persistent_context(
                        p, self.profile_path, headless=self.headless, viewport={"width": 1440, "height": 900},
                        timeout=10000
                    )
                    self._check_stop()
                    context.set_default_timeout(2000)
                    page = context.new_page() if not context.pages else context.pages[0]
                    DouyinBrowserManager.inject_stealth(page)
                    self._attach_publish_diagnostics(page)

                    logger.info(f"导航至抖音创作者中心上传页: {DOUYIN_UPLOAD_URL}")
                    self._navigate_upload_page(page)
                    # 设计说明：使用 page.wait_for_timeout() 替代 time.sleep()
                    # Playwright 同步模式依赖调用驱动底层 Node.js 管道。
                    # 严禁在持有 page 时调用 time.sleep，否则会导致上传 Web Worker 挂起死锁。
                    self._pause(page, 3000)

                    # 检查是否处于登录状态
                    if "login" in page.url.lower():
                        self.account.status = "EXPIRED"
                        return False, "账号登录态已失效，请重新扫码登录", None

                    # 步骤 1: 注入视频文件
                    logger.info("【步骤 1/5】正在注入视频文件...")
                    self._check_stop()
                    file_input = page.locator('input[type="file"][accept*="video"], input[type="file"][accept*=".mp4"]').first
                    if file_input.count() == 0:
                        return False, "未找到文件上传 Input 元素，请先使用 DOM 探针校准页面", None

                    self.before_upload()
                    file_input.set_input_files(str(video_path.resolve()))
                    self._video_selected = True
                    self._progress("视频文件已选择，正在等待作品标题表单")
                    logger.info("视频文件注入成功，正在等待页面加载与转码处理...")

                    # 步骤 2: 等待视频上传并进入编辑页
                    self._pause(page, 3000)
                    # 等待标题输入框出现（标志着上传成功进入作品发布表单）
                    collection_name = self.account.collection_name or book_name
                    publish_btn = self.prepare_publish_form(page,
                        details=("等待并填写作品标题", lambda: self._prepare_title(page, title)),
                        cover=("选择当前视频画面作为封面", lambda: self._select_video_cover(page)),
                        collection=(f"选择合集：{collection_name}",
                                    lambda: self._handle_collection_assignment(page, collection_name, episode_num)),
                        upload=('等待视频上传与处理完成（最多30分钟）', lambda: self._wait_for_upload_ready(page)),
                        submit=("校验发布按钮", lambda: self._get_publish_button(page)),
                        details_pause_ms=2000, options_pause_ms=1000)

                    self._check_stop()
                    # 点击超时也可能已提交，先记下边界，避免误报取消成功。
                    publish_submitted = True
                    self._publish_armed = True
                    publish_btn.click()
                    self._progress("已点击发布，等待实际投稿接口确认（最长10分钟）；此时停止且未确认成功将记为失败，保留文件")

                    # Only this attempt's actual create API acknowledgement confirms submission.
                    wait_start = time.time()
                    result = self._wait_for_confirmation(page, lambda: self._publish_confirmation(page))
                    success, err_detail = result if result else (False, None)

                    if not success:
                        self.last_outcome = ('ACCOUNT_LIMIT' if self.account_limit_reason else
                                             'FAILED')
                        reason = f"平台返回错误: {err_detail}" if err_detail else "点击发布后未能在规定时间内捕获成功提示或页面跳转"
                        self._progress(f"[发布核对] 等待 {time.time() - wait_start:.1f} 秒；页面：{urlsplit(page.url).path}；结果未确认")
                        self._save_diagnostics(page, "发布未确认", reason)
                        return False, reason, None

                    self._close_publish_context(context,page,publish_submitted)

                    logger.info(f"作品 [{title}] 在抖音平台发布成功！")

                    # 步骤 7: 物理归档与日账本生成
                    record = self._finalize_publish_archive_and_ledger(video_path, collection_name, episode_num)
                    self.last_outcome = "SUCCESS"
                    return True, "发布成功并已归档", record
                finally:
                    if context and (page is None or not page.is_closed()):
                        if not (self._publish_result and str(self._publish_result.get('code')) == '0'):
                            self._capture_failure_page(page, '抖音上传退出前未确认成功')
                        try:
                            self._close_publish_context(context, page, publish_submitted)
                        except Exception:
                            pass
        except VideoDurationRejected as error:
            self.last_outcome = 'DURATION_LIMIT'
            self._progress(f'[不可重试] {error}；保留原文件，继续下一个视频')
            return False, str(error), None
        except PublishCancelled:
            self.last_outcome = "FAILED" if self._video_selected else "CANCELLED"
            msg = ("已停止：已点击发布，平台结果未确认，按失败处理；本地文件未归档"
                   if publish_submitted else "已停止：当前任务已取消，未点击发布，本地文件未归档")
            self._progress(msg)
            return False, msg, None
        except Exception as e:
            if self.account_limit_reason:
                self.last_outcome = 'ACCOUNT_LIMIT'
            elif publish_submitted:
                self.last_outcome = "FAILED"
            err_msg = (f"账号今日投稿达到上限: {self.account_limit_reason}" if self.account_limit_reason
                       else f"自动化上传流程异常: {e}")
            logger.error(err_msg)
            return False, err_msg, None

    def _publish_confirmation(self, page):
        if self._publish_result is not None:
            if self._publish_result['code'] == '0':
                self._progress('实际投稿接口返回业务码0，平台已确认接收作品')
                return True, None
            return False, f"投稿接口业务码{self._publish_result['code']}：{self._publish_result['message']}"
        if self.account_limit_reason:
            return False, self.account_limit_reason
        quota_notice = page.get_by_text(re.compile(r'(今日|今天).*(投稿|发布).*(上限|限额)'))
        for index in range(quota_notice.count()):
            if quota_notice.nth(index).is_visible():
                self.account_limit_reason = quota_notice.nth(index).inner_text()
                return False, self.account_limit_reason
        try:
            fail_msg = page.get_by_text(re.compile(r"发布失败|包含违规|敏感词|视频格式不支持|请填写作品标题")).first
            if fail_msg.count() > 0 and fail_msg.is_visible():
                err_detail = fail_msg.inner_text()
                logger.error(f"捕获到平台发布拒绝提示: {err_detail}")
                return False, err_detail
        except Exception:
            pass
        return None

    def _get_publish_button(self, page: Page):
        self._check_stop()
        button = page.get_by_role("button", name="发布", exact=True)
        if button.count() != 1:
            raise RuntimeError("未找到唯一的【发布】提交按钮，拒绝使用【作品发布】侧栏入口")
        if not button.is_visible() or not button.is_enabled():
            raise RuntimeError("发布按钮不可用，视频处理或表单校验尚未完成")
        return button

    def _read_upload_state(self, page):
        # Read only: unrelated permission prompts and preview tips are left intact.
        state = page.evaluate(r'''() => {
                const visible = el => {const r=el.getBoundingClientRect();
                    return r.width>0 && r.height>0 && getComputedStyle(el).visibility!=='hidden';};
                const texts = Array.from(document.querySelectorAll('span,p,div'))
                    .filter(el=>el.children.length===0 && visible(el))
                    .map(el=>(el.innerText||'').trim()).filter(t=>t.length<160);
                const pending = texts.filter(t=>/正在上传|上传中|转码中|正在转码|视频处理中|视频处理尚未完成/.test(t));
                const ready = texts.some(t=>/^(视频)?上传(成功|完成)[！!。]?$/.test(t));
                const progress = Array.from(document.querySelectorAll('[role="progressbar"],progress,[class*="upload-progress"]'))
                    .filter(visible).map(el=>({text:el.innerText||'', value:Number(el.getAttribute('aria-valuenow')??el.value??((el.innerText||'').match(/(\d+(?:\.\d+)?)\s*%/)||[])[1]??-1),
                        max:Number(el.getAttribute('aria-valuemax')||el.max||100),
                        upload:/upload/i.test(el.className||'') || /上传/.test(el.getAttribute('aria-label')||'')}));
                const complete = progress.some(p=>p.upload && p.value>=p.max && p.max>0);
                const incomplete = progress.some(p=>p.upload && p.value>=0 && p.value<p.max);
                const serverVideo = Array.from(document.querySelectorAll('video')).some(el=>visible(el)
                    && /^https?:/.test(el.currentSrc||el.src||'') && el.readyState>=1);
                const failed = texts.filter(t=>/上传失败|上传错误|转码失败|视频处理失败/.test(t));
                const reupload = texts.some(t=>/^重新上传$/.test(t));
                const form = Array.from(document.querySelectorAll('input')).some(el=>visible(el)
                    && el.placeholder==='填写作品标题，为作品获得更多流量' && !el.disabled);
                const preview = Array.from(document.querySelectorAll('video,canvas,img')).some(el=>visible(el)
                    && el.getBoundingClientRect().width>=60 && el.getBoundingClientRect().height>=60
                    && (el.tagName==='CANVAS' || (el.tagName==='IMG' && el.complete && el.naturalWidth>0)
                        || (el.tagName==='VIDEO' && el.readyState>=1)));
                return {pending, ready:ready||complete||serverVideo, incomplete, progress, failed,
                    reupload, form, preview};
            }''')
        if isinstance(state, dict) and (state['pending'] or any(p['upload'] for p in state['progress'])):
            self._upload_activity_seen = True
        return state

    def _save_diagnostics(self, page: Page, stage: str, reason: str):
        from .probe import DouyinDOMProbe
        output = Path(self.account.target_dir or self.profile_path.parent) / "reports"
        try:
            report, snapshot = DouyinDOMProbe(output_dir=output).capture_dom_report(
                page, diagnostic={"stage": stage, "reason": reason, "network_events": self._network_events})
            self._progress(f"[{stage}] {reason}；诊断报告：{report}；截图：{snapshot}")
        except Exception as error:
            self._progress(f"[{stage}] {reason}；保存诊断失败：{error}")

    def _select_video_cover(self, page: Page):
        # 第一处是竖封面入口；在弹窗中确认当前视频帧，不上传外部图片。
        entry = page.get_by_text("选择封面", exact=True).first
        entry.wait_for(state="visible", timeout=2000)
        entry.click()
        dialog = page.locator('[role="dialog"]:visible, .semi-modal:visible').last
        dialog.wait_for(state="visible", timeout=2000)
        original_dialog = dialog.element_handle()
        if dialog.locator('video, canvas').count() == 0:
            raise RuntimeError("封面弹窗中未检测到视频或帧画布，无法确认视频画面来源，需采集弹窗 DOM")
        confirm = dialog.get_by_role("button", name=re.compile(r"^(确定|完成|保存)$"))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self._check_stop()
            available = [confirm.nth(i) for i in range(confirm.count())
                         if confirm.nth(i).is_visible() and confirm.nth(i).is_enabled()]
            if len(available) == 1:
                available[0].click(timeout=5000)
                self._progress("封面已确认，等待封面处理完成和弹窗关闭（最长 30 秒）")
                self._wait_cover_closed(page, original_dialog)
                self._progress("已确认当前视频封面画面")
                return
            self._pause(page, 300)
        raise RuntimeError("封面弹窗未出现唯一可用的确定/完成/保存按钮，需采集该弹窗 DOM")

    def _wait_cover_closed(self, page, original_dialog, timeout_secs=30):
        deadline = time.monotonic() + timeout_secs
        horizontal_requested = False
        horizontal_confirmed = False
        while time.monotonic() < deadline:
            self._check_stop()
            if not original_dialog.is_visible():
                return
            if self._handle_horizontal_cover_prompt(page, allow_click=not horizontal_requested):
                horizontal_requested = True
                deadline = time.monotonic() + timeout_secs
                self._progress('已选择设置横封面，等待当前视频横封面预览并确认完成')
            if horizontal_requested and not horizontal_confirmed:
                if '横封面预览' in original_dialog.inner_text():
                    if original_dialog.query_selector('video, canvas') is None:
                        raise RuntimeError('横封面编辑器未检测到当前视频或帧画布，停止确认')
                    buttons = [button for button in original_dialog.query_selector_all('button')
                               if re.fullmatch(r'确定|完成|保存', button.inner_text().strip())
                               and button.is_visible() and button.is_enabled()]
                    if len(buttons) == 1:
                        self._check_stop()
                        buttons[0].click(timeout=2000)
                        horizontal_confirmed = True
                        self._progress('已确认当前视频横封面，等待封面窗口关闭')
            warning = page.get_by_text(re.compile(r"操作频繁|发布频繁|达到.*上限|请稍后再试|封面.*失败"))
            for index in range(warning.count()):
                if warning.nth(index).is_visible():
                    raise RuntimeError(warning.nth(index).inner_text())
            self._pause(page, 300)
        raise RuntimeError("封面确认后弹窗 30 秒未关闭；可能是处理延迟或平台校验，需查看诊断截图")

    def _handle_horizontal_cover_prompt(self, page, allow_click=True):
        headings = page.get_by_text(re.compile(r'^设置横封面获(?:得)?更多流量$'))
        for index in range(headings.count()):
            heading = headings.nth(index)
            if not heading.is_visible():
                continue
            if not allow_click:
                raise RuntimeError('横封面引导重复出现，需查看诊断报告')
            # The nearest container with both choices excludes the editor's same-name button.
            prompt = heading.locator('xpath=ancestor::*[.//button[normalize-space(.)="暂不设置"]'
                                     ' and .//button[normalize-space(.)="设置横封面"]][1]')
            choice = prompt.get_by_role('button', name='设置横封面', exact=True)
            if choice.count() != 1 or not choice.is_visible() or not choice.is_enabled():
                raise RuntimeError('横封面引导未找到唯一可用的设置横封面按钮')
            self._check_stop()
            choice.click(timeout=2000)
            return True
        return False

    def _handle_collection_assignment(self, page: Page, book_name: str, episode_num: Optional[int]) -> None:
        # 实际 DOM 区分 select-mix-type（合集类型）和 select-collection（合集名称）。
        self._check_stop()
        control = page.locator('div.semi-select[class*="select-collection-"]')
        if control.count() != 1:
            raise RuntimeError("未找到唯一的合集名称选择框，需重新采集 DOM")
        current = control.inner_text().strip().splitlines()
        if current and current[0] == book_name:
            self._progress(f"已确认目标合集：{book_name}")
            return
        control.click()
        dropdown = page.locator('.semi-select-dropdown:visible, [role="listbox"]:visible').last
        dropdown.wait_for(state="visible", timeout=2000)
        option = dropdown.get_by_text(book_name, exact=True)
        if option.count() != 1:
            raise RuntimeError(f"下拉列表未找到唯一合集【{book_name}】，请先创建该合集或校准弹窗 DOM")
        option.click()
        self._pause(page, 300)
        current = control.inner_text().strip().splitlines()
        if not current or current[0] != book_name:
            raise RuntimeError(f"合集选择回读不一致，预期【{book_name}】")
        # 平台的集数是另一个 Semi Select，不把文件中的“第N问”写入无关数字框。
        self._progress(f"已确认目标合集：{book_name}；合集内集数使用平台默认顺序")
    def _finalize_publish_archive_and_ledger(
        self,
        video_path: Path,
        book_name: str,
        episode_num: Optional[int]
    ) -> PublishLedgerRecord:
        """
        复用统一归档规则写入 publiced/，并按日记录发布账本。
        """
        target_dir = Path(self.account.target_dir) if self.account.target_dir else video_path.parent
        reports_dir = target_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        file_hash = compute_file_md5(video_path)
        archived_path = None

        # 1. 物理归档挪动
        if self.account.auto_archive:
            info = video_path.stat()
            signature = info.st_size, info.st_mtime_ns
            archived_video = archive_completed_file(video_path, signature, self._progress, archive_root=target_dir)
            archived_path = str(archived_video)
            logger.info(f"已将视频物理归档至: {archived_video}")

        # 2. 按日生成带时间戳流水账本: publish_ledger_YYYYMMDD.json
        day_str = time.strftime("%Y%m%d")
        ledger_file = reports_dir / f"publish_ledger_{day_str}.json"

        record = PublishLedgerRecord(
            record_id=f"rec_{int(time.time()*1000)}",
            account_id=self.account.account_id,
            file_path=str(video_path),
            file_name=video_path.name,
            file_hash=file_hash,
            media_type="video",
            collection_name=book_name,
            episode_num=episode_num,
            published_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            status="SUCCESS",
            archived_path=archived_path
        )

        ledger_data = []
        if ledger_file.exists():
            try:
                with open(ledger_file, "r", encoding="utf-8-sig") as f:
                    ledger_data = json.load(f)
                if not isinstance(ledger_data,list) or any(not isinstance(r,dict) for r in ledger_data):
                    raise ValueError('账本结构应为记录列表')
            except (OSError,ValueError) as error:
                raise RuntimeError(f'已有账本读取失败，保留原文件：{ledger_file}；{error}') from error

        ledger_data.append(record.to_dict())
        temporary = ledger_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(ledger_data,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(ledger_file)

        logger.info(f"已记录当日账本: {ledger_file.name}；发布详情由任务汇总报告记录")
        return record
