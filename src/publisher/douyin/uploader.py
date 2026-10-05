# -*- coding: utf-8 -*-
"""
抖音自动化上传发布与归档引擎模块 (Douyin Uploader & Publishing Pipeline)
实现：注入文件 -> 转码就绪 -> 标题与话题注入 -> 封面设置 -> 合集智能自判断(已有选中/新书书名自建) -> 点击发布 -> 物理归档 -> 按日带时间戳账本与报告。
"""
import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

from playwright.sync_api import sync_playwright, BrowserContext, Page

from .models import DouyinAccountConfig, PublishLedgerRecord
from .browser import DouyinBrowserManager, DOUYIN_UPLOAD_URL

logger = logging.getLogger(__name__)


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

    ep_match = re.search(r'第\s*(\d+)\s*集', clean_stem)
    episode_num = int(ep_match.group(1)) if ep_match else None

    # 书名提取：以下划线分隔的第一部分或全名
    parts = clean_stem.split('_')
    book_name = parts[0].strip() if parts else clean_stem
    # 清洗书名号
    clean_book_name = book_name.replace("《", "").replace("》", "").strip()

    return clean_book_name, clean_stem, episode_num


class DouyinUploader:
    """
    抖音创作者中心自动化上传器。
    """
    def __init__(self, account: DouyinAccountConfig, headless: bool = False):
        self.account = account
        self.headless = headless
        self.profile_path = Path(account.profile_dir).resolve()
        self.profile_path.mkdir(parents=True, exist_ok=True)

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
        tags = custom_tags or self.account.default_tags

        logger.info(f"开始上传视频: {video_path.name} | 归属书名: 《{book_name}》 | 集数: 第{episode_num}集")

        try:
            with sync_playwright() as p:
                context: BrowserContext = p.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_path),
                    headless=self.headless,
                    args=DouyinBrowserManager.get_anti_detection_args(),
                    viewport={"width": 1440, "height": 900}
                )
                page = context.new_page() if not context.pages else context.pages[0]
                DouyinBrowserManager.inject_stealth(page)

                logger.info(f"导航至抖音创作者中心上传页: {DOUYIN_UPLOAD_URL}")
                page.goto(DOUYIN_UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)

                # 检查是否处于登录状态
                if "login" in page.url.lower():
                    context.close()
                    self.account.status = "EXPIRED"
                    return False, "账号登录态已失效，请重新扫码登录", None

                # 步骤 1: 注入视频文件
                logger.info("【步骤 1/5】正在注入视频文件...")
                file_input = page.locator('input[type="file"]').first
                if file_input.count() == 0:
                    context.close()
                    return False, "未找到文件上传 Input 元素，请先使用 DOM 探针校准页面", None

                file_input.set_input_files(str(video_path.resolve()))
                logger.info("视频文件注入成功，正在等待页面加载与转码处理...")

                # 步骤 2: 等待视频上传并进入编辑页
                time.sleep(5)
                # 等待标题输入框出现（标志着上传成功进入作品发布表单）
                title_editor = page.locator('[contenteditable="true"], textarea, [data-placeholder*="作品"]').first
                try:
                    title_editor.wait_for(state="visible", timeout=120000)
                    logger.info("【步骤 2/5】视频已成功载入，进入作品信息编辑表单")
                except Exception as e:
                    context.close()
                    return False, f"等待视频转码与表单展示超时: {e}", None

                # 步骤 3: 填充作品描述与话题标签
                logger.info("【步骤 3/5】正在填充作品描述与话题标签...")
                tag_str = " " + " ".join(tags) if tags else ""
                full_desc = f"{title}{tag_str}"

                title_editor.click()
                title_editor.fill("")
                # 模拟逐字键入，以触发可能的平台话题联想
                page.keyboard.type(title)
                for tag in tags:
                    clean_tag = tag.strip('# ')
                    page.keyboard.type(f" #{clean_tag} ")
                    time.sleep(0.5)
                    page.keyboard.press("Enter")

                time.sleep(2)

                # 步骤 4: 封面设置（若存在本地配套封面）
                if cover_path and cover_path.exists():
                    logger.info(f"【步骤 4/5】检测到配套封面，正在上传: {cover_path.name}")
                    try:
                        # 查找选择/更换封面按钮
                        cover_btn = page.locator('text=选择封面, text=设置封面, text=更换封面').first
                        if cover_btn.count() > 0:
                            cover_btn.click()
                            time.sleep(2)
                            # 封面上传弹窗内的 file input
                            cover_input = page.locator('input[type="file"]').last
                            if cover_input.count() > 0:
                                cover_input.set_input_files(str(cover_path.resolve()))
                                time.sleep(3)
                                # 确定保存封面
                                confirm_btn = page.locator('button:has-text("确定"), button:has-text("完成")').last
                                if confirm_btn.count() > 0:
                                    confirm_btn.click()
                                    time.sleep(2)
                    except Exception as e:
                        logger.warning(f"设置封面出现轻微异常，回退使用默认抓帧封面: {e}")

                # 步骤 5: 合集智能自判断与归属
                logger.info(f"【步骤 5/5】正在进行合集自判断与归属: 《{book_name}》 (第{episode_num}集)")
                self._handle_collection_assignment(page, book_name, episode_num)

                time.sleep(3)

                # 步骤 6: 点击发布
                logger.info("正在执行最终发布...")
                publish_btn = page.locator('button:has-text("发布"), button[type="primary"]:has-text("发布")').first
                if publish_btn.count() == 0:
                    context.close()
                    return False, "未找到发布按钮，请检查页面结构", None

                publish_btn.click()
                logger.info("已点击发布按钮，正在监听发布成功状态...")

                # 监听结果
                success = False
                err_detail = None
                wait_start = time.time()
                while time.time() - wait_start < 45:
                    cur_url = page.url.lower()
                    if "content/manage" in cur_url or "creator-micro" in cur_url:
                        # 成功跳转至作品管理页
                        success = True
                        break

                    try:
                        toast = page.locator('text=发布成功, text=已发布, [class*="toast"]').first
                        if toast.count() > 0 and toast.is_visible():
                            success = True
                            break
                    except Exception:
                        pass
                    time.sleep(2)

                context.close()

                if not success:
                    return False, "点击发布后未能在规定时间内捕获成功提示或跳转", None

                logger.info(f"作品 [{title}] 在抖音平台发布成功！")

                # 步骤 7: 物理归档与日账本生成
                record = self._finalize_publish_archive_and_ledger(video_path, book_name, episode_num)
                return True, "发布成功并已归档", record

        except Exception as e:
            err_msg = f"自动化上传流程异常: {e}"
            logger.error(err_msg)
            return False, err_msg, None

    def _handle_collection_assignment(self, page: Page, book_name: str, episode_num: Optional[int]) -> None:
        """
        合集自判断核心逻辑：
        展开合集下拉框 -> 搜索匹配已有合集 -> 命中则选择并填入集数 -> 未命中则自动新建合集。
        """
        try:
            # 1. 尝试找到合集复选框或选择按钮
            coll_entry = page.locator('text=添加至合集, text=作品合集, [class*="collection"]').first
            if coll_entry.count() == 0:
                logger.info("页面未检测到合集入口，跳过合集操作")
                return

            coll_entry.click()
            time.sleep(2)

            # 2. 检查下拉列表中是否已有该书名合集
            target_coll_option = page.locator(f'text={book_name}').first
            if target_coll_option.count() > 0 and target_coll_option.is_visible():
                logger.info(f"命中已有合集: 《{book_name}》，执行直接关联！")
                target_coll_option.click()
                time.sleep(1)
            else:
                # 3. 未找到已有合集，执行新建合集
                create_btn = page.locator('text=新建合集, text=+ 新建合集, text=创建合集').first
                if create_btn.count() > 0:
                    logger.info(f"未匹配到已有合集，正在自动新建合集: 《{book_name}》...")
                    create_btn.click()
                    time.sleep(1)
                    # 填入合集名称
                    name_input = page.locator('input[placeholder*="合集名称"], input[placeholder*="名称"]').last
                    if name_input.count() > 0:
                        name_input.fill(book_name)
                        time.sleep(1)
                        # 点击确定
                        confirm_btn = page.locator('button:has-text("确定"), button:has-text("创建")').last
                        if confirm_btn.count() > 0:
                            confirm_btn.click()
                            time.sleep(2)
                            logger.info(f"合集 《{book_name}》 自动创建并绑定成功！")

            # 4. 如果有集数输入框，自动填入集数
            if episode_num is not None:
                ep_input = page.locator('input[placeholder*="集数"], input[type="number"]').first
                if ep_input.count() > 0 and ep_input.is_visible():
                    ep_input.fill(str(episode_num))
                    logger.info(f"已自动绑定合集集数: 第 {episode_num} 集")

        except Exception as e:
            logger.warning(f"合集自动化处理出现轻微异常，不中断主发布链路: {e}")

    def _finalize_publish_archive_and_ledger(
        self,
        video_path: Path,
        book_name: str,
        episode_num: Optional[int]
    ) -> PublishLedgerRecord:
        """
        物理平移归档至 published/ 目录，并按日生成带时间戳账本与报告。
        """
        target_dir = Path(self.account.target_dir) if self.account.target_dir else video_path.parent
        published_dir = target_dir / "published"
        reports_dir = target_dir / "reports"
        published_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)

        file_hash = compute_file_md5(video_path)
        archived_path = None

        # 1. 物理归档挪动
        if self.account.auto_archive:
            archived_video = published_dir / video_path.name
            shutil.move(str(video_path), str(archived_video))
            archived_path = str(archived_video)
            logger.info(f"已将视频物理归档至: {archived_video}")

            # 配套字幕同步归档
            srt_candidate = video_path.with_suffix(".srt")
            if srt_candidate.exists():
                shutil.move(str(srt_candidate), str(published_dir / srt_candidate.name))
                logger.info(f"已同步归档字幕: {srt_candidate.name}")

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
                with open(ledger_file, "r", encoding="utf-8") as f:
                    ledger_data = json.load(f)
            except Exception:
                ledger_data = []

        ledger_data.append(record.to_dict())
        with open(ledger_file, "w", encoding="utf-8") as f:
            json.dump(ledger_data, f, ensure_ascii=False, indent=2)

        # 3. 单次详细带时间戳审计报告: publish_report_YYYYMMDD_HHMMSS.txt
        time_tag = time.strftime("%Y%m%d_%H%M%S")
        report_txt = reports_dir / f"publish_report_{time_tag}.txt"
        with open(report_txt, "w", encoding="utf-8") as f:
            f.write(f"=====================================================\n")
            f.write(f"书声抖音自动发布审计报告 (Publish Audit Report)\n")
            f.write(f"时间戳: {record.published_at}\n")
            f.write(f"发布账号: {self.account.account_name} ({self.account.account_id})\n")
            f.write(f"视频文件: {record.file_name}\n")
            f.write(f"文件指纹 (MD5): {record.file_hash}\n")
            f.write(f"归属合集: 《{record.collection_name}》 (集数: 第{record.episode_num}集)\n")
            f.write(f"归档路径: {record.archived_path or '未启用物理归档'}\n")
            f.write(f"发布状态: 100% 成功已确认\n")
            f.write(f"=====================================================\n")

        logger.info(f"已记录当日账本: {ledger_file.name} 与审计报告: {report_txt.name}")
        return record
