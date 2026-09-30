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
    小人书分镜场景数据模型 (SceneUnit)。
    【为什么这样设计】
    严格落实用户确立的核心原则：
    1. SpeechUnit 与 SceneUnit 分离：SpeechUnit 专职服务于 TTS 和 ASS 字幕；SceneUnit 专职服务于插画和镜头；
    2. 每 X 句决定图片分组：一个 SceneUnit 聚合 X 个连续的 SpeechUnit；
    3. 图片显示时间由真实 TTS 时间轴决定：
       Scene start = 该 Scene 第一条 SpeechUnit 的开始时间；
       Scene end = 该 Scene 最后一条 SpeechUnit 的结束时间；
       时长不搞平均分配，完全由语音物理时长自然决定。
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
    negative_prompt: str = ""
    image_path: Optional[str] = None
    first_sentence: str = ""

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
            "negative_prompt": self.negative_prompt,
            "image_path": self.image_path,
            "first_sentence": self.first_sentence,
        }


# 架构别名：明确区分 SpeechUnit (语音/字幕单元) 与 SceneUnit (插画分镜单元)
SceneUnit = ScenePlan


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
                    unit_ids=list(current_unit_ids),
                    first_sentence=current_texts[0] if current_texts else ""
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
                unit_ids=list(current_unit_ids),
                first_sentence=current_texts[0] if current_texts else ""
            )
            scenes.append(scene_plan)

        logger.info(
            f"场景切分完成：共将 {len(units)} 个语音切片聚类为 {len(scenes)} 个小人书分镜，"
            f"总时长约 {current_time_cursor:.2f} 秒，平均每镜 {current_time_cursor/max(1, len(scenes)):.1f} 秒"
        )
        return scenes

    def split_by_text(
        self,
        units: List[Union[SpeechUnit, Dict[str, Any]]],
        paragraphs_per_scene: Optional[int] = None
    ) -> List[ScenePlan]:
        """
        Step A 专用：基于自然文本对句子进行分镜场景聚类（不依赖任何音频物理时长）。

        【为什么这样设计】
        1. 落实两段式管线设计：在 TTS 启动前，直接按纯文本句子数聚类 SceneUnit，
           供 CPU 并发预先提炼 Prompt，此时无需等待 GPU 生成音频文件；
        2. 记录 first_sentence，专供前端状态栏展示当前分镜正在提炼的句首内容。
        """
        if not units:
            return []

        target_step = max(1, paragraphs_per_scene or self.paragraphs_per_scene)
        scenes: List[ScenePlan] = []
        scene_idx = 1
        total_units = len(units)

        for start_idx in range(0, total_units, target_step):
            end_idx = min(start_idx + target_step - 1, total_units - 1)
            group_units = units[start_idx:end_idx + 1]

            texts = []
            u_ids = []
            for idx_in_grp, u in enumerate(group_units):
                cur_idx = start_idx + idx_in_grp
                if isinstance(u, dict):
                    t = u.get("text", "")
                    uid = u.get("unit_id", f"unit_{cur_idx:04d}")
                else:
                    t = getattr(u, "text", "")
                    uid = getattr(u, "unit_id", f"unit_{cur_idx:04d}")
                texts.append(t.strip())
                u_ids.append(uid)

            first_sent = texts[0] if texts else ""
            full_txt = " ".join(texts)

            scene = ScenePlan(
                scene_index=scene_idx,
                scene_id=f"scene_{scene_idx:03d}",
                start_unit_index=start_idx,
                end_unit_index=end_idx,
                start_time=0.0,
                end_time=0.0,
                duration=0.0,
                full_text=full_txt,
                unit_ids=u_ids,
                first_sentence=first_sent
            )
            scenes.append(scene)
            scene_idx += 1

        logger.info(f"[Step A 文本分镜聚类] 已将 {total_units} 个文本切片预划分为 {len(scenes)} 个分镜场景")
        return scenes

    def bind_timestamps(
        self,
        scenes: List[ScenePlan],
        sub_items_or_units: List[Any]
    ) -> List[ScenePlan]:
        """
        Step B 专用：将 TTS 合成完成后的真实物理时间轴绑定至已提炼好 Prompt 的分镜场景。

        【为什么这样设计】
        1. 落实零开销原则：纯内存字段赋值，执行耗时 < 1 毫秒；
        2. 绝对不重新触发大模型 Prompt 生成；
        3. 保证 Scene start/end 严格等于对应首末 SpeechUnit 的起止时间，保持音画严格无缝衔接。
        """
        if not scenes or not sub_items_or_units:
            return scenes

        total_items = len(sub_items_or_units)
        for scene in scenes:
            s_idx = max(0, min(scene.start_unit_index, total_items - 1))
            e_idx = max(0, min(scene.end_unit_index, total_items - 1))

            first_item = sub_items_or_units[s_idx]
            last_item = sub_items_or_units[e_idx]

            if hasattr(first_item, "start_time"):
                start_t = float(first_item.start_time)
            elif isinstance(first_item, dict):
                start_t = float(first_item.get("start_time", 0.0))
            else:
                start_t = 0.0

            if hasattr(last_item, "end_time"):
                end_t = float(last_item.end_time)
            elif isinstance(last_item, dict):
                end_t = float(last_item.get("end_time", 0.0))
            else:
                end_t = start_t + 5.0

            scene.start_time = round(start_t, 3)
            scene.end_time = round(max(start_t + 0.1, end_t), 3)
            scene.duration = round(scene.end_time - scene.start_time, 3)

        logger.info(f"[Step B 时序绑定] 成功为 {len(scenes)} 个分镜场景绑定真实 TTS 时间轴 (总时长 {scenes[-1].end_time:.2f}s)")
        return scenes
