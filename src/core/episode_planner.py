# -*- coding: utf-8 -*-
"""
两阶段分集规划器 (Two-Stage Episode Planner)
负责根据单集目标时长对长篇电子书执行智能分集规划。

架构原则：
1. 严禁在章节中间机械切断音频或句子；
2. 阶段一（TTS前）：基于字符数和语速估算初始分集，供生产计划全景预览；
3. 阶段二（TTS后）：基于真实音频物理秒数精确划分最终 Episode 边界。
"""
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from ..state.models import Chapter, EpisodeManifest

logger = logging.getLogger(__name__)


@dataclass
class EpisodePreview:
    """阶段一生产计划预览中的单集摘要"""
    episode_order: int
    title: str
    subtitle: str
    chapter_ids: List[str]
    chapter_titles: List[str]
    total_chars: int
    estimated_duration_seconds: float

    @property
    def estimated_duration_minutes(self) -> float:
        return round(self.estimated_duration_seconds / 60.0, 1)

    @property
    def char_count(self) -> int:
        """兼容别名：避免调用方误用 char_count 而静默得到0"""
        return self.total_chars


@dataclass
class BookProductionPlan:
    """全书生产计划全景预览数据对象"""
    book_title: str
    total_chapters: int
    total_chars: int
    estimated_total_minutes: float
    target_duration_minutes: float
    total_episodes: int
    episodes: List[EpisodePreview]


class EpisodePlanner:
    """
    两阶段分集规划算法实现。
    """
    def __init__(self, target_duration_mins: float = 30.0, speed_chars_per_min: float = 300.0):
        self.target_duration_mins = target_duration_mins
        self.speed_chars_per_min = speed_chars_per_min
        # 单集目标字数
        self.target_chars = int(target_duration_mins * speed_chars_per_min)

    def plan_initial_episodes(self, book_title: str, chapters: List[Chapter], split_mode: str = "by_duration") -> BookProductionPlan:
        """
        阶段一：TTS 前进行预估分集规划。
        【为什么这样设计】
        1. split_mode == "by_chapter" (按自然章节切割)：
           严格遵循书籍原生章节结构，一章对应一集，不合并、不拆分，完全由章节自身长短决定；
        2. split_mode == "by_duration" (按目标时长切割)：
           根据用户指定的单集时长预算，利用贪心算法智能进行多短章合并与超长章隔离。
        """
        if not chapters:
            return BookProductionPlan(
                book_title=book_title,
                total_chapters=0,
                total_chars=0,
                estimated_total_minutes=0.0,
                target_duration_minutes=self.target_duration_mins,
                total_episodes=0,
                episodes=[]
            )

        episodes: List[EpisodePreview] = []
        total_book_chars = 0

        # 分支 1：按自然章节切割 (一章一集)
        if split_mode == "by_chapter":
            for idx, ch in enumerate(chapters, 1):
                ch_id = getattr(ch, "chapter_id", None) or getattr(ch, "id", f"chapter_{idx:03d}")
                ch_title = getattr(ch, "title", "").strip() or f"第{getattr(ch, 'order', idx)}章"
                ch_chars = len(ch.content) if hasattr(ch, "content") else sum(len(p) for p in getattr(ch, "paragraphs", []))
                total_book_chars += ch_chars
                episodes.append(self._create_episode_preview(
                    idx, [ch_id], [ch_title], ch_chars
                ))
            est_total_mins = round(total_book_chars / self.speed_chars_per_min, 1)
            logger.info(f"阶段一按自然章节规划完成：全书 {len(chapters)} 章，对应生成 {len(episodes)} 集")
            return BookProductionPlan(
                book_title=book_title,
                total_chapters=len(chapters),
                total_chars=total_book_chars,
                estimated_total_minutes=est_total_mins,
                target_duration_minutes=self.target_duration_mins,
                total_episodes=len(episodes),
                episodes=episodes
            )

        # 分支 2：按目标时长切割 (时长贪心与边界合并)
        current_ch_ids: List[str] = []
        current_ch_titles: List[str] = []
        current_chars = 0
        ep_order = 1

        for ch in chapters:
            ch_id = getattr(ch, "chapter_id", None) or getattr(ch, "id", "")
            ch_title = getattr(ch, "title", "") or f"第{ch.order}章"
            ch_chars = len(ch.content) if hasattr(ch, "content") else sum(len(p) for p in getattr(ch, "paragraphs", []))
            total_book_chars += ch_chars

            # 极端边界判断：若单章字数直接达到或超过 1.5 倍目标，强制该章节独立成集
            if ch_chars >= self.target_chars * 1.5:
                # 先收尾当前正在积累的分集
                if current_ch_ids:
                    episodes.append(self._create_episode_preview(
                        ep_order, current_ch_ids, current_ch_titles, current_chars
                    ))
                    ep_order += 1
                    current_ch_ids, current_ch_titles, current_chars = [], [], 0

                # 该超长章节独立成集
                episodes.append(self._create_episode_preview(
                    ep_order, [ch_id], [ch_title], ch_chars
                ))
                ep_order += 1
                continue

            # 贪心判断：若加入当前章节后严重超出目标字数（> 1.25倍），且当前已有积累，则在章节边界切集
            if (current_chars + ch_chars > self.target_chars * 1.25) and current_ch_ids:
                episodes.append(self._create_episode_preview(
                    ep_order, current_ch_ids, current_ch_titles, current_chars
                ))
                ep_order += 1
                current_ch_ids = [ch_id]
                current_ch_titles = [ch_title]
                current_chars = ch_chars
            else:
                current_ch_ids.append(ch_id)
                current_ch_titles.append(ch_title)
                current_chars += ch_chars

        # 处理末尾剩余章节
        if current_ch_ids:
            episodes.append(self._create_episode_preview(
                ep_order, current_ch_ids, current_ch_titles, current_chars
            ))

        est_total_mins = round(total_book_chars / self.speed_chars_per_min, 1)
        logger.info(f"阶段一分集规划完成：全书 {len(chapters)} 章，预估 {len(episodes)} 集，总时长约 {est_total_mins} 分钟")

        return BookProductionPlan(
            book_title=book_title,
            total_chapters=len(chapters),
            total_chars=total_book_chars,
            estimated_total_minutes=est_total_mins,
            target_duration_minutes=self.target_duration_mins,
            total_episodes=len(episodes),
            episodes=episodes
        )

    def plan_final_episodes(self, chapter_durations: Dict[str, float], chapter_titles: Dict[str, str]) -> List[EpisodeManifest]:
        """
        阶段二：TTS 后读取各章节实际生成的物理音频秒数，重新锁定最终分集。
        :param chapter_durations: 章节 ID 到实际音频物理秒数映射（如 {"chapter_001": 1240.5, ...}）
        :param chapter_titles: 章节 ID 到章节标题映射
        :return: EpisodeManifest 清单
        """
        target_seconds = self.target_duration_mins * 60.0
        final_manifests: List[EpisodeManifest] = []

        current_chapters: List[str] = []
        current_titles: List[str] = []
        current_duration = 0.0
        ep_order = 1

        for ch_id, dur in chapter_durations.items():
            title = chapter_titles.get(ch_id, ch_id)

            # 超长章节独立成集保护
            if dur >= target_seconds * 1.5:
                if current_chapters:
                    final_manifests.append(self._create_episode_manifest(
                        ep_order, current_chapters, current_titles, current_duration
                    ))
                    ep_order += 1
                    current_chapters, current_titles, current_duration = [], [], 0.0

                final_manifests.append(self._create_episode_manifest(
                    ep_order, [ch_id], [title], dur
                ))
                ep_order += 1
                continue

            # 若加入当前章节后时长严重超出单集上限（> 1.25倍），且当前已有积累章节，则换集
            if (current_duration + dur > target_seconds * 1.25) and current_chapters:
                final_manifests.append(self._create_episode_manifest(
                    ep_order, current_chapters, current_titles, current_duration
                ))
                ep_order += 1
                current_chapters = [ch_id]
                current_titles = [title]
                current_duration = dur
            else:
                current_chapters.append(ch_id)
                current_titles.append(title)
                current_duration += dur

        # 处理末尾剩余章节
        if current_chapters:
            final_manifests.append(self._create_episode_manifest(
                ep_order, current_chapters, current_titles, current_duration
            ))

        logger.info(f"阶段二精准分集重整完成，最终产出 {len(final_manifests)} 集")
        return final_manifests

    def _create_episode_preview(self, order: int, ch_ids: List[str], ch_titles: List[str], total_chars: int) -> EpisodePreview:
        """生成分集预览"""
        est_sec = total_chars / (self.speed_chars_per_min / 60.0)
        subtitle = self._generate_auto_subtitle(order, ch_titles)
        return EpisodePreview(
            episode_order=order,
            title=f"第{order:02d}集",
            subtitle=subtitle,
            chapter_ids=ch_ids,
            chapter_titles=ch_titles,
            total_chars=total_chars,
            estimated_duration_seconds=est_sec
        )

    def _create_episode_manifest(self, order: int, ch_ids: List[str], ch_titles: List[str], duration: float) -> EpisodeManifest:
        """生成正式 EpisodeManifest"""
        subtitle = self._generate_auto_subtitle(order, ch_titles)
        return EpisodeManifest(
            episode_id=f"episode_{order:02d}",
            order=order,
            title=f"第{order:02d}集",
            subtitle=subtitle,
            chapters=ch_ids,
            duration=round(duration, 2)
        )

    @staticmethod
    def _generate_auto_subtitle(order: int, ch_titles: List[str]) -> str:
        """自动生成优雅的分集副标题（如：第01集 · 认知革命 或 第02集 · 第三章至第五章）"""
        if not ch_titles:
            return f"第{order:02d}集"
        if len(ch_titles) == 1:
            clean_t = ch_titles[0].strip()
            return f"第{order:02d}集 · {clean_t}"
        else:
            first_t = ch_titles[0].strip()
            last_t = ch_titles[-1].strip()
            return f"第{order:02d}集 · {first_t} ~ {last_t}"
