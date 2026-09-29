# -*- coding: utf-8 -*-
"""
场景分段器模块 (Scene Splitter)
负责将电子书分集的 SpeechUnit 朗读单元聚类为"小人书场景" (ScenePlan)。

【为什么这样设计】
1. 音画同步根基：依赖阶段一 TTS 生成的物理 WAV 真实时长累加时间轴，将连续的语音流划分为离散的视觉分镜；
2. 自然语意边界：以自然段落或完整自然句为最小切分颗粒度，杜绝在单句话中间机械切图；
3. 严格无缝衔接：前一场景的 end_time 严格等于后一场景的 start_time，从数据结构上杜绝音画漂移和黑屏缝隙。
"""
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Union

from ..state.models import SpeechUnit

logger = logging.getLogger(__name__)


@dataclass
class ScenePlan:
    """
    小人书分镜场景数据模型。
    对应一张配图及其生命周期内的所有语音与字幕。
    """
    scene_index: int
    scene_id: str
    start_unit_index: int
    end_unit_index: int
    start_time: float
    end_time: float
    duration: float
    full_text: str
    unit_ids: List[str] = field(default_factory=list)
    prompt: str = ""
    image_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可持久化字典，供分集清单 Manifest 存档"""
        return {
            "scene_index": self.scene_index,
            "scene_id": self.scene_id,
            "start_unit_index": self.start_unit_index,
            "end_unit_index": self.end_unit_index,
            "start_time": round(self.start_time, 3),
            "end_time": round(self.end_time, 3),
            "duration": round(self.duration, 3),
            "full_text": self.full_text,
            "unit_ids": self.unit_ids,
            "prompt": self.prompt,
            "image_path": self.image_path,
        }


class SceneSplitter:
    """
    场景切分与编排引擎：
    按设定的段落数或目标时长将单集音频聚类为连续的分镜场景。
    """
    def __init__(
        self,
        paragraphs_per_scene: int = 5,
        min_duration_seconds: float = 12.0,
        max_duration_seconds: float = 45.0
    ):
        """
        :param paragraphs_per_scene: 每个场景容纳的目标自然段落数（默认5段）
        :param min_duration_seconds: 每个场景的最短显示时长，防止换图过频闪烁（默认12秒）
        :param max_duration_seconds: 每个场景的最长显示时长，防止单图静止过久视觉疲劳（默认45秒）
        """
        self.paragraphs_per_scene = max(1, paragraphs_per_scene)
        self.min_duration_seconds = max(3.0, min_duration_seconds)
        self.max_duration_seconds = max(self.min_duration_seconds + 5.0, max_duration_seconds)

    def split(
        self,
        units: List[Union[SpeechUnit, Dict[str, Any]]],
        paragraphs_per_scene: Optional[int] = None
    ) -> List[ScenePlan]:
        """
        将输入的 SpeechUnit 列表聚类为场景列表。

        【为什么这样设计】
        1. 优先遵循段落归属标记（如 SpeechUnit 内部携带的 paragraph_id 或 natural segment）；
        2. 若无段落归属标记，则将每 N 个连续的自然句作为一个场景；
        3. 双重时长熔断保护：累计时间达到 max_duration_seconds 时主动换镜，累计时间未达 min_duration_seconds 且还有后续句子时延迟换镜。
        """
        if not units:
            logger.warning("输入的 SpeechUnit 列表为空，无法进行场景切分")
            return []

        target_paras = max(1, paragraphs_per_scene or self.paragraphs_per_scene)
        scenes: List[ScenePlan] = []

        current_units: List[Any] = []
        current_texts: List[str] = []
        current_unit_ids: List[str] = []
        scene_start_time = 0.0
        current_time_cursor = 0.0
        scene_idx = 1
        scene_start_unit_idx = 0
        current_para_count = 0
        last_seen_para_id = None

        for idx, u in enumerate(units):
            # 兼容对象属性与字典键值
            if isinstance(u, dict):
                text = u.get("text", "")
                dur = float(u.get("audio_duration", 0.0))
                u_id = u.get("unit_id", f"unit_{idx:04d}")
                para_id = u.get("paragraph_id", None)
            else:
                text = getattr(u, "text", "")
                dur = float(getattr(u, "audio_duration", 0.0))
                u_id = getattr(u, "unit_id", f"unit_{idx:04d}")
                para_id = getattr(u, "paragraph_id", None)

            current_units.append(u)
            current_texts.append(text.strip())
            current_unit_ids.append(u_id)

            # 统计段落跨度
            if para_id is not None:
                if para_id != last_seen_para_id:
                    current_para_count += 1
                    last_seen_para_id = para_id
            else:
                current_para_count += 1

            current_time_cursor += dur
            current_scene_duration = current_time_cursor - scene_start_time

            # 判定是否触发换镜条件：
            # 1. 达到目标段落数且满足最短时长；
            # 2. 或者达到单图停留时长上限；
            # 3. 并且不是最后一个元素
            is_last_unit = (idx == len(units) - 1)
            hit_para_limit = (current_para_count >= target_paras and current_scene_duration >= self.min_duration_seconds)
            hit_time_limit = (current_scene_duration >= self.max_duration_seconds)

            if (hit_para_limit or hit_time_limit) and not is_last_unit:
                # 结算当前分镜场景
                scene_plan = ScenePlan(
                    scene_index=scene_idx,
                    scene_id=f"scene_{scene_idx:03d}",
                    start_unit_index=scene_start_unit_idx,
                    end_unit_index=idx,
                    start_time=round(scene_start_time, 3),
                    end_time=round(current_time_cursor, 3),
                    duration=round(current_scene_duration, 3),
                    full_text=" ".join(current_texts),
                    unit_ids=list(current_unit_ids)
                )
                scenes.append(scene_plan)

                # 重置光标进入下一个场景
                scene_idx += 1
                scene_start_unit_idx = idx + 1
                scene_start_time = current_time_cursor
                current_units = []
                current_texts = []
                current_unit_ids = []
                current_para_count = 0
                last_seen_para_id = None

        # 结算最后一个残留场景（保底）
        if current_units:
            scene_duration = current_time_cursor - scene_start_time
            # 若末尾残留时长极短且已有前面场景，可考虑合并，但为简单与绝对对应，独立成尾镜
            scene_plan = ScenePlan(
                scene_index=scene_idx,
                scene_id=f"scene_{scene_idx:03d}",
                start_unit_index=scene_start_unit_idx,
                end_unit_index=len(units) - 1,
                start_time=round(scene_start_time, 3),
                end_time=round(current_time_cursor, 3),
                duration=round(scene_duration, 3),
                full_text=" ".join(current_texts),
                unit_ids=list(current_unit_ids)
            )
            scenes.append(scene_plan)

        logger.info(
            f"场景切分完成：共将 {len(units)} 个语音切片聚类为 {len(scenes)} 个小人书分镜，"
            f"总时长约 {current_time_cursor:.2f} 秒，平均每镜 {current_time_cursor/max(1, len(scenes)):.1f} 秒"
        )
        return scenes
