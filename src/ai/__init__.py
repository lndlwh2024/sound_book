# -*- coding: utf-8 -*-
"""
AI 视觉生成与提示词工程模块 (AI Visual & Prompt Engineering)
提供场景提示词提取、艺术风格注入、插画生成管理与本地模型调度服务。
"""
from .prompt_generator import PromptGenerator
from .illustration_manager import IllustrationManager

__all__ = ["PromptGenerator", "IllustrationManager"]
