"""Shared browser workflow extracted from the proven Douyin uploader.

Platform adapters supply DOM reading, configuration and confirmation evidence.
Douyin defaults retain its original event-pump, stages and readiness behaviour.
"""
import time
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


class BrowserPublishWorkflow:
    _video_selected = False
    _publish_armed = False
    stage_interruptions = ()

    def _capture_failure_page(self, page, reason='', role='last'):
        callback = getattr(self, 'failure_capture', None)
        if callable(callback):
            try:
                callback(page, getattr(self, 'stage', '浏览器检查'), str(reason), role=role)
            except Exception as error:
                self._progress('[失败诊断保存异常] '+str(error))

    def _close_publish_context(self, context, page, submitted=False):
        """Keep a submitted page alive while its final browser events settle."""
        if submitted and page and not page.is_closed():
            self._progress('[关闭延迟] 已点击发布，页面保留5秒后关闭')
            try:
                # Do not re-run upload/rejection checks or cancel confirmed success.
                page.wait_for_timeout(5000)
            except Exception as error:
                self._progress('[关闭延迟] 页面已退出或连接断开：'+str(error)[:150])
        context.close()

    def _save_diagnostics(self, page, stage, reason):
        self._progress(f'[阶段失败] {stage}：{reason}')

    def _wait_until(self, page, label, condition, seconds=60):
        self.stage = label
        self._progress(label)
        deadline, last_log = time.monotonic() + seconds, 0
        while time.monotonic() < deadline:
            self._check_stop()
            self._check_duration_notice(page)
            try:
                result = condition()
                if result:
                    return result
            except PlaywrightTimeoutError:
                pass
            if time.monotonic() - last_log >= 30:
                self._progress(f'[等待] {label}；浏览器保持打开')
                last_log = time.monotonic()
            self._pause(page, 300)
        raise RuntimeError(f'{label}超时；原文件保留')

    def _wait_for_confirmation(self, page, probe, timeout_secs=600, poll_ms=1000):
        """Douyin's acknowledgement loop; the adapter supplies its own proof.

        A falsey result is pending, a truthy result is terminal (including an
        explicit rejection). Timeout is unknown, never publication success.
        """
        deadline = time.monotonic() + timeout_secs
        last_log = time.monotonic()
        while time.monotonic() < deadline:
            self._check_stop()
            result = probe()
            if result:
                return result
            if time.monotonic() - last_log >= 30:
                self._progress('[发布等待] 平台结果尚未确认；浏览器保持打开，原文件保留')
                last_log = time.monotonic()
            self._pause(page, poll_ms)
        return None

    def prepare_publish_form(self, page, *, details, cover, collection, upload, submit,
                             prepare_submit=None, uploaded_before_options=False,
                             details_pause_ms=0, options_pause_ms=0,
                             collection_before_cover=False):
        """Douyin's details → cover → collection → readiness → submit sequence.

        Some sites expose usable options only after transfer, and YouTube has
        a visibility wizard. These are explicit adapter capabilities, not forks.
        """
        self._required_stage(page, *details)
        self._pause(page, details_pause_ms)
        if uploaded_before_options:
            self._required_stage(page, *upload)
        options = (collection, cover) if collection_before_cover else (cover, collection)
        for option in options:
            self._required_stage(page, *option)
        self._pause(page, options_pause_ms)
        if not uploaded_before_options:
            self._required_stage(page, *upload)
        if prepare_submit:
            self._required_stage(page, *prepare_submit)
        return self._required_stage(page, *submit)

    def _pause(self, page: Page, milliseconds: int):
        # Keep pumping Playwright events while checking stop at most every 300 ms.
        remaining = milliseconds
        self._check_stop()
        while remaining > 0:
            step = min(300, remaining)
            page.wait_for_timeout(step)
            remaining -= step
            self._check_stop()
            if self._video_selected:
                self._check_duration_notice(page)
                if not self._publish_armed:
                    self._read_upload_state(page)

    def _wait_for_upload_ready(self, page, timeout_secs=1800, ready_stable_seconds=0):
        deadline = time.monotonic() + timeout_secs
        last_log = 0
        quiet_since = None
        ready_since = None
        while time.monotonic() < deadline:
            self._check_stop()
            self._check_duration_notice(page)
            state = self._read_upload_state(page)
            if state['failed']:
                raise RuntimeError('平台视频上传/处理失败：' + '；'.join(state['failed']))
            if state['ready'] and not state['pending'] and not state['incomplete']:
                ready_since = time.monotonic() if ready_since is None else ready_since
                if time.monotonic() - ready_since < ready_stable_seconds:
                    self._pause(page, 300)
                    continue
                self._progress('已确认视频上传完成且无正在上传/转码状态，允许提交发布')
                return
            ready_since = None
            # Douyin removes its progress panel on completion without keeping a 100% label.
            # Absence alone is insufficient: require this file's observed upload activity,
            # a usable editor/preview/reupload entry, and two seconds of stable quiet state.
            disappeared = (self._upload_activity_seen and not state['pending']
                           and not any(p['upload'] for p in state['progress'])
                           and state['form'] and state['preview'] and state['reupload'])
            if disappeared:
                if quiet_since is None:
                    quiet_since = time.monotonic()
                if time.monotonic() - quiet_since >= 2:
                    self._progress('已确认本次上传进度消失，视频预览与重新上传入口就绪，连续2秒无上传/转码状态，允许提交发布')
                    return
            else:
                quiet_since = None
            if time.monotonic() - last_log >= 30:
                self._progress(f"[上传等待] 尚未完成：{state['pending']}；进度：{state['progress']}；"
                               f"本次上传已观测={self._upload_activity_seen}；表单={state['form']}；"
                               f"预览={state['preview']}；重新上传={state['reupload']}；保持浏览器打开")
                last_log = time.monotonic()
            self._pause(page, 300)
        raise RuntimeError('等待视频上传与处理完成超时；未点击发布，请查看上传状态诊断')

    def _required_stage(self, page: Page, name: str, action):
        self.stage = name
        self._progress(name)
        self._check_stop()
        started = time.monotonic()
        try:
            return action()
        except self.stage_interruptions:
            raise
        except Exception as error:
            self._save_diagnostics(page, name, str(error))
            raise RuntimeError(f"{name}失败，未执行发布：{error}") from error
        finally:
            self._progress(f"[阶段耗时] {name}：{time.monotonic() - started:.1f} 秒")
