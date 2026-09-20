# 书声（ShuSheng）v2.0 详细设计与实现规范

---

## 1. 文档目的与适用范围

本文档是“书声（ShuSheng）v2.0”工程落地与代码实现的最终技术基线。全面规定了系统目录结构、GUI 交互字段、配置数据结构、状态机状态转移、文本处理与切分算法、持久化 Worker 协议、Manifest 存储规范、字幕时间轴生成算法、Audio Mixer 混音滤镜链、Video Layout Engine 坐标系统以及测试验收矩阵。

---

## 2. 运行环境与目录架构规范

### 2.1 硬件与系统环境
- **操作系统**：Windows 11 64-bit
- **CPU**：多核 x86_64 处理器
- **内存**：>= 16GB（开发机约 40GB RAM）
- **显卡（目标机）**：NVIDIA Quadro T1000 (Max-Q, 4GB VRAM, 35W TDP, 768 CUDA Cores)
- **底层依赖**：FFmpeg (带 libx264, h264_nvenc, aac 支持，在系统 PATH 中可直接调用)

### 2.2 隔离 Python 环境规划
- **主环境 (`envs/main/`)**：Python 3.10，承载 PySide6、PyMuPDF、EbookLib、FFmpeg 适配器、任务状态机及 Azure SDK；
- **F5-TTS 独立环境 (`envs/f5/`)**：Python 3.10，承载 PyTorch (CUDA 11.8/12.x)、F5-TTS、Vocos、transformers 及依赖；
- **Kokoro 独立环境 (`envs/kokoro/`)**：Python 3.10，承载 Kokoro-ONNX / kokoro-tts 轻量推理运行时。

### 2.3 统一项目目录树
```text
soundbook/
├── app.py                     # 主启动入口（启动 PySide6 GUI）
├── cli.py                     # 开发者与诊断 CLI 入口
├── config.yaml                # 全局主配置文件
├── requirements.txt           # 主环境 Python 依赖清单
├── .env.example               # 环境变量模板（Azure Key 等）
├── run.bat                    # 用户双击启动脚本（自动调用 main 环境启动 app.py）
├── setup.bat                  # 一键环境检查与初始化脚本
│
├── docs/                      # 核心设计文档
│   ├── 01_PRD.md
│   ├── 02_ARCHITECTURE.md
│   └── 03_DETAILED_DESIGN.md
│
├── src/                       # 核心业务源码 (运行于 main 环境)
│   ├── app/                   # PySide6 GUI 视图与控制器
│   │   ├── main_window.py     # 主窗口与组件排版
│   │   ├── view_models.py     # 数据绑定与 UI 状态
│   │   └── widgets/           # 封面预览、波形图、计划面板等自定义组件
│   ├── core/                  # 应用与任务编排
│   │   ├── task_manager.py    # 任务状态机调度器
│   │   ├── engine_manager.py  # 引擎资源与硬件状态管理
│   │   └── episode_planner.py # 两阶段分集规划器
│   ├── parser/                # 电子书解析 (PDF/EPUB)
│   ├── cleaner/               # 确定性正文清洗
│   ├── validator/             # 字符与完整性校验
│   ├── chunker/               # SpeechUnit 与 Chunk 构建器
│   ├── tts/                   # TTS 调度层
│   │   ├── router.py          # 引擎路由
│   │   ├── voice_manager.py   # VoiceProfile 管理
│   │   └── backends/          # F5, Kokoro, Azure 适配器
│   ├── audio/                 # 音频处理
│   │   ├── audio_qc.py        # 完整性与 NaN/静音检查
│   │   ├── audio_mixer.py     # 增益与侧链压缩闪避
│   │   └── ffmpeg_adapter.py  # FFmpeg 进程封装
│   ├── video/                 # 视频生产
│   │   ├── layout_engine.py   # Video Layout Engine (9:16 / 16:9 坐标布局)
│   │   └── video_composer.py  # 画面与字幕合成渲染
│   └── state/                 # 持久化与状态管理
│       ├── manifest.py        # Manifest 读写与原子写入
│       └── fingerprint.py     # 缓存指纹算法
│
├── workers/                   # 独立环境的工作进程脚本
│   ├── f5_worker.py           # F5-TTS 持久化 Worker (运行于 envs/f5)
│   └── kokoro_worker.py       # Kokoro 持久化 Worker (运行于 envs/kokoro)
│
├── models/                    # 本地预训练模型与声音预设
│   ├── f5_tts/
│   │   ├── F5TTS_v1_Base/     # 模型权重与 vocab.txt
│   │   └── presets/           # 参考音频预设 (如 preset_male_d1_elite.wav)
│   └── kokoro/
│
├── books/                     # 单书生产工作区
│   └── <book_id>/             # 依据文件 SHA256 截取前 16 位命名的工作目录
│       ├── source/            # 原始 PDF/EPUB 归档
│       ├── parsed/            # 结构化正文与大纲 JSON
│       ├── cleaned/           # 清洗正文与清洗审计日志
│       ├── manifests/         # 任务与分块清单
│       │   ├── task_manifest.json
│       │   ├── tts_manifest.json
│       │   └── episode_manifest.json
│       ├── audio_chunks/      # 逐段生成的 WAV 文件
│       ├── episodes/          # 分集临时音视频
│       └── logs/              # 单任务执行日志
│
└── output/                    # 最终成品输出目录
    └── <book_title>/
        ├── Episode_01.mp4
        ├── Episode_01.srt
        ├── ...
        └── audiobooks/
            ├── chapter_001.mp3
            └── book.m4b
```

---

## 3. GUI 字段定义与 PySide6 线程模型

### 3.1 GUI 输入与展示字段规格
| 控件标识 (Object Name) | UI 呈现形式 | 选项值 / 默认值 | 校验与联动规则 |
| :--- | :--- | :--- | :--- |
| `input_book_file` | 文件选择框 | 支持 `.pdf`, `.epub` | 选择后触发异步解析，自动填充 `input_book_title` 与起始页。 |
| `input_book_title` | 单行文本框 | 默认取文件名，允许编辑 | 过滤 Windows 非法文件名字符（`\ / : * ? " < > \|`）。 |
| `input_start_pos` | 整数输入框 / 下拉框 | PDF 默认第 1 页；EPUB 默认第一章 | 用于剔除前言目录，只在新建时生效，Resume 时置灰。 |
| `combo_tts_engine` | 下拉选择框 | `F5-TTS` (默认) / `Kokoro` / `Azure` | 变更后联动更新音色列表，并触发硬件状态检查。 |
| `combo_voice_profile`| 下拉选择框 | 读取匹配引擎的 `VoiceProfile` | 默认选择 `男声/商业精英 (D1)`。 |
| `combo_video_layout` | 单选 / 下拉框 | `竖屏 9:16` (默认) / `横屏 16:9` | 联动更新右侧预览区的宽高比框线。 |
| `combo_target_duration`| 下拉选择框 | 15 / 30 / 45 / 60 分钟 (默认 30) | 决定 Episode Planner 的分组目标。 |
| `combo_run_mode` | 下拉选择框 | `全书连续` / `仅下一集` (默认) / `运行上限` | 决定 Task Manager 到达分集边界时的停机策略。 |
| `input_bgm_file` | 文件选择框 | 支持 `.mp3`, `.wav`, `.m4a` | 可为空；选择后激活混音试听与闪避。 |
| `input_cover_file` | 文件选择框 | 支持 `.jpg`, `.png`, `.webp` | 必填项；作为 Video Layout Engine 的核心视觉元素。 |
| `input_main_title` | 单行文本框 | 默认取 `input_book_title` | 用于合成在视频上方的固定主标题。 |
| `slider_voice_vol` | 滑动条 + 标签 | 0% ~ 200%，默认 100% | 调整旁白人声增益。 |
| `slider_bgm_vol` | 滑动条 + 标签 | 0% ~ 100%，默认 15% | 调整背景音乐基准音量。 |
| `btn_preview_mix` | 按钮 | 点击播放 15s 混音试听 | 触发后台临时合成短片段并在 UI 播放器中播放。 |

### 3.2 跨线程通信：Qt Signal & Slot 规范
GUI 视图层（`MainWindow`）持有 `TaskManagerBridge` 对象，通过纯信号进行非阻塞交互：

```python
from PySide6.QtCore import QObject, Signal

class TaskManagerBridge(QObject):
    # GUI -> 后台任务指令
    sig_start_task = Signal(dict)       # 携带任务启动配置字典
    sig_request_pause = Signal()        # 请求安全暂停
    sig_resume_task = Signal(str)       # 恢复指定 book_id 任务
    sig_request_preview = Signal(dict)  # 请求生成混音试听

    # 后台任务 -> GUI 状态通知
    sig_status_changed = Signal(str)           # 当前状态字符串 (如 "TTS_GENERATING")
    sig_progress_updated = Signal(float, str)  # 进度百分比 (0~100), 提示文案
    sig_preview_ready = Signal(str)            # 试听音频临时文件路径
    sig_hardware_alert = Signal(str, str)      # 级别 (WARNING/ERROR), 消息内容
    sig_task_finished = Signal(str)            # 完成输出目录路径
    sig_task_paused = Signal()                 # 任务已确认安全暂停
```

---

## 4. 配置体系与核心数据结构

### 4.1 全局配置 schema (`config.yaml`)
```yaml
app:
  version: "2.0.0"
  language: "zh-CN"
  log_level: "INFO"

hardware:
  force_float32: true              # 针对 4GB VRAM 强制启用 float32，避免 NaN
  cooling_pause_seconds: 30        # 连续单段合成后的呼吸冷却时间（秒）
  gpu_temp_limit: 78               # 达到该温度时强制延长休眠

tts:
  default_engine: "f5_tts"
  default_voice: "preset_male_d1_elite"
  f5_tts:
    python_env: "envs/f5/Scripts/python.exe"
    worker_script: "workers/f5_worker.py"
    nfe_step: 16                   # 扩散模型推理步数 (16 步在 T1000 上兼具音质与速度)
    device: "cuda"
  kokoro:
    python_env: "envs/kokoro/Scripts/python.exe"
    worker_script: "workers/kokoro_worker.py"
    device: "auto"
  azure:
    region_env_key: "AZURE_SPEECH_REGION"
    secret_env_key: "AZURE_SPEECH_KEY"

speech_unit:
  max_chars: 120                   # SpeechUnit 软上限字符数
  max_sentences: 2                 # 单个 SpeechUnit 最大自然句数
  min_chars: 10                    # 小于该字符数的短句优先合并

video:
  default_layout: "portrait_9_16"  # portrait_9_16 (1080x1920) | landscape_16_9 (1920x1080)
  fps: 30
  bg_blur_sigma: 20                # 封面高斯模糊强度
  font_family: "Microsoft YaHei"
  title_font_size: 48
  subtitle_font_size: 38
```

### 4.2 VoiceProfile 数据模型
```python
from dataclasses import dataclass
from typing import Optional, Dict, Any

@dataclass
class VoiceProfile:
    profile_id: str                # 如 "preset_male_d1_elite"
    display_name: str              # 如 "男声 / 商业精英 (D1)"
    engine: str                    # "f5_tts" | "kokoro" | "azure"
    speed: float = 1.0             # 语速倍率
    ref_audio: Optional[str] = None # 参考音频相对路径
    ref_text: Optional[str] = None  # 参考文本
    extra_options: Optional[Dict[str, Any]] = None # 特定引擎透传参数
```

---

## 5. 任务状态机与生命周期设计

### 5.1 状态转移矩阵
系统定义 16 种严格互斥的任务状态：

```text
[CREATED] ──(点击开始)──> [INGESTING] ──> [PARSED] ──> [CLEANED]
                                                            │
                     [NEEDS_REVIEW] <──(字符损失 > 15%)─────┤
                           │                                │ (正常通过)
                           ▼                                ▼
                     (人工确认通过) ───────────────────> [VALIDATED]
                                                            │
[TTS_GENERATING] <──(用户确认计划)── [PLANNED] <────────────┘
       │
       ├──(请求安全暂停)──> [PAUSING] ──(完成当前微段)──> [PAUSED]
       │                                                    │ (点击继续)
       │                                                    ▼
       │ <──────────────────────────────────────────────────┘
       │
       ├──(单段失败重试超限)──> [PARTIAL_FAILED]
       │
       ▼ (全段生成完毕并通过 Audio QC)
[AUDIO_QC] ──> [ALIGNING_SUBTITLES] ──> [AUDIO_MIXING] ──> [VIDEO_RENDERING]
                                                                  │
[FAILED] <──────────────────(不可恢复渲染异常)────────────────────┤
                                                                  ▼
                                                             [COMPLETED]
```

---

## 6. 文本解析、清洗与 SpeechUnit 构建规范

### 6.1 确定性正文清洗算法
`TextCleaner` 执行纯正则管道，禁止引入任何破坏原文字义的改写：
1. **页码消除**：匹配页尾或独占一行的阿拉伯数字 `^\s*\d{1,4}\s*$`；
2. **页眉/页脚消除**：统计全书出现频率超过 5 次的重复短文本行（如“人类简史 · 第一部分”）；
3. **连字与断行修复**：修复 PDF 换行硬回车造成的汉字中间断开：
   - 模式：`([一-龥])\n([一-龥])` 替换为 `\1\2`；
   - 英文单词换行连字符：`([a-zA-Z]+)-\n([a-zA-Z]+)` 替换为 `\1\2`。
4. **清洗验证公式**：
   $$\text{Loss Ratio} = 1.0 - \frac{\text{Cleaned Characters}}{\text{Raw Characters}}$$
   当 $\text{Loss Ratio} > 0.15$（字符减少超 15%）时，强制转入 `NEEDS_REVIEW` 挂起任务。

### 6.2 SpeechUnit 聚合构建规则
`SpeechUnitBuilder` 将章节内容按照以下规则聚合为 SpeechUnit（即 Chunk）：
1. 采用标点符号 `[。！？!?；;\n]` 将段落切分为有序自然句列表；
2. 设定贪心合并滑动窗口：
   - 当前正在组装的 Unit 字符数累计小于 `speech_unit_max_chars`（默认 120 字）且自然句不超过 2 句时，吸纳下一句；
   - 一旦加入下一句会导致总字数超过上限，则立即封包当前 Unit；
   - 若单句自身长度即超过上限，则以逗号 `[，,]` 作为次级切分点断开。
3. **1:1 映射持久化**：
   - 每个 SpeechUnit 赋予稳定 ID：`{book_id}_ch{chapter_idx:03d}_unit{unit_idx:04d}`；
   - 同时生成该 Unit 的 `Chunk` 实体，属性一一对应。

---

## 7. 缓存判定与 Fingerprint 计算

### 7.1 SHA256 指纹定义
每个 Chunk 的 `fingerprint` 是其唯一缓存鉴别凭证：
$$\text{Fingerprint} = \text{SHA256}(\text{text\_clean} + \text{engine} + \text{profile\_id} + \text{speed} + \text{nfe\_step})$$

### 7.2 缓存命中与跳过逻辑
当任务启动或 Resume 时，对清单中的每个 Chunk 进行判定：
1. 检查 `tts_manifest.json` 中该 Chunk 的 `fingerprint` 是否与当前计算一致；
2. 检查其对应的输出文件 `output_path`（如 `.wav`）在磁盘上是否存在；
3. 执行轻量 `Audio QC`（文件大小 > 1KB，时长 > 0.1s）；
4. 上述条件全部满足，状态直接置为 `SUCCESS` 并跳过 TTS 计算；任一条件失效，则状态标为 `PENDING` 重新排队生成。

---

## 8. Manifest 数据 Schema 与原子写入

### 8.1 `tts_manifest.json` 完整定义
```json
{
  "book_id": "ce5b60764f246928",
  "book_title": "人类简史",
  "created_at": "2026-09-16T12:00:00Z",
  "updated_at": "2026-09-16T12:20:00Z",
  "total_units": 850,
  "completed_units": 350,
  "units": [
    {
      "unit_id": "ce5b_ch001_unit0001",
      "chapter_index": 1,
      "order": 1,
      "text": "认知革命是智人历史上的一大飞跃。",
      "text_hash": "a1b2c3d4...",
      "fingerprint": "e5f6g7h8...",
      "engine": "f5_tts",
      "profile_id": "preset_male_d1_elite",
      "status": "SUCCESS",
      "output_file": "audio_chunks/ce5b_ch001_unit0001.wav",
      "audio_duration": 4.52,
      "retry_count": 0,
      "error_msg": null
    }
  ]
}
```

### 8.2 原子写入实现（Atomic Write Implementation）
```python
import os
import json
import tempfile

def atomic_save_json(target_path: str, data: dict):
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    
    # 1. 写入同目录临时文件
    with tempfile.NamedTemporaryFile('w', dir=target_dir, delete=False, encoding='utf-8') as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tf.flush()
        os.fsync(tf.fileno()) # 强制硬件落盘
        temp_name = tf.name
        
    # 2. 原子替换目标文件
    os.replace(temp_name, target_path)
```

---

## 9. Task-scoped Persistent Worker 与 IPC 协议

### 9.1 通信链路与环境隔离
主应用通过 `subprocess.Popen` 启动特定环境的 Worker，利用标准输入输出（`stdin/stdout`）传递 JSON Lines 报文：
- Worker 启动时在 `f5_worker.py` 入口执行：
  ```python
  # 将第三方库的杂质打印重定向到 stderr，保护 IPC 纯度
  _ipc_stdout = sys.stdout
  sys.stdout = sys.stderr
  ```

### 9.2 交互协议规范
1. **就绪握手（Handshake）**：
   - Worker 加载模型完毕后，发送：
     `{"status": "READY", "device": "cuda", "vram_allocated_mb": 1820}`
2. **合成任务请求（Task Request）**：
   - 主程序通过 `stdin` 写入单行 JSON：
     ```json
     {"cmd": "tts", "unit_id": "ce5b_ch001_unit0001", "text": "认知革命是智人历史上的一大飞跃。", "output_path": "H:/.../unit0001.wav", "ref_audio": "models/f5_tts/presets/preset_male_d1_elite.wav", "nfe_step": 16}
     ```
3. **合成结果返回（Task Response）**：
   - Worker 处理完毕后，通过 `_ipc_stdout` 发送单行 JSON：
     ```json
     {"success": true, "unit_id": "ce5b_ch001_unit0001", "output_path": "H:/.../unit0001.wav", "audio_duration": 4.52, "error": null}
     ```
4. **停机指令（Stop Request）**：
   - 主程序发送：`{"cmd": "stop"}`，Worker 安全释放显存并返回退出码 0。

### 9.3 F5-TTS 生产基线与音色工程规范 (Production Baseline)
经过深度单变量专项实验与严格人工听验验收，F5-TTS 生产基线固化如下规范：
1. **稳定推理超参数**：
   - `nfe_step = 16`（在生成质量与 GPU 推理耗时之间达到最优平衡）；
   - `speed = 1.0`；
   - `seed` 策略：`42 + sent_idx`（保证单句间声学自然微扰动，同时实现确定性可复现）；
   - 保持 F5 官方原生流匹配截断逻辑 `ref_audio_len`，严禁修改官方底层。
2. **Reference Pair 强约束**：
   - 参考音频与参考文本必须 **100% 逐字严格对应**，严禁开头或结尾存在未发音或多出的文字；
   - 参考音频必须在**自然完整语义和静音低谷处闭合**（如 `models/f5_tts/presets/preset_male_e1_narrator.wav` 截断于 5.30 秒）；
   - 严禁在“与此同时、但是、如果、因此”等未完结连接词处截断参考音频，防止扩散模型将未完成从句作为前置语境导致生成句首多字。
3. **文本切分规范（全面废弃 26 字硬切）**：
   - 生产管线全面采用自然句切分 `split_natural_sentences`，仅在自然标点（`。！？!?；;\n`）处切断；
   - 严禁使用旧版 `split_chinese_sentences(max_len=26)`，该硬切算法会破坏语义并严重放大句首丢词。
4. **最小回归测试集门禁（Minimal Regression Suite）**：
   - 测试脚本位于 `tests/regression/test_f5_minimal_regression.py`，包含 6 个核心测试单元（重点覆盖“如果”、“就算”、“反之”等易丢词句首）；
   - **门禁标准**：6 单元全部生成成功、0 漏词、0 多词、语调自然；
   - 凡修改文本切分、F5 Worker、音色资产、Seed/NFE 等模块，必须强制重新执行回归测试。

---

## 10. 字幕时间累计算法与 ASS 生成规范

### 10.1 TTS 原生时间轴累计算法
由于 `1 SpeechUnit = 1 WAV`，字幕时间区间依据物理时长精准累加：

```python
def generate_episode_subtitles(units: list[dict], start_offset: float = 0.0) -> list[dict]:
    timeline = []
    current_time = start_offset
    
    for u in units:
        dur = u["audio_duration"]
        sub_item = {
            "index": u["order"],
            "start_time": current_time,
            "end_time": current_time + dur,
            "text": u["text"] # 100% 原始文本
        }
        timeline.append(sub_item)
        current_time += dur
        
    return timeline
```

### 10.2 ASS 字幕排版模板
生成的 `.ass` 字幕采用带阴影和深色半透明底条的专业样式，保证任何封面背景下文字清晰可见：
```text
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,38,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,1,2,60,60,280,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:04.52,Default,,0,0,0,,认知革命是智人历史上的一大飞跃。
```

---

## 11. Audio Mixer 混音实现与 FFmpeg 滤镜链

### 11.1 增益分贝换算函数
```python
import math

def percent_to_db(percent: float) -> float:
    if percent <= 0:
        return -999.0
    # 100% -> 0dB; 50% -> -6.02dB; 15% -> -16.48dB; 200% -> +6.02dB
    return 20.0 * math.log10(percent / 100.0)
```

### 11.2 FFmpeg 侧链压缩闪避滤镜链
旁白人声（输入 `[0:a]`）控制背景音乐（输入 `[1:a]`）的闪避：
```bash
ffmpeg -y -i voice_concatenated.wav -stream_loop -1 -i bgm.mp3 \
-filter_complex "\
[0:a]volume=0dB[v_norm];\
[1:a]volume=-16.5dB[bgm_norm];\
[bgm_norm][v_norm]sidechaincompress=threshold=0.08:ratio=4:attack=20:release=350[bgm_ducked];\
[bgm_ducked]afade=t=out:st=1795:d=5[bgm_faded];\
[v_norm][bgm_faded]amix=inputs=2:duration=first:dropout_transition=2[aout]" \
-map "[aout]" -c:a aac -b:a 192k mixed_episode_01.m4a
```
- `threshold=0.08`：灵敏捕获旁白起音；
- `attack=20ms`：人声开口 20ms 内背景音乐迅速避让；
- `release=350ms`：人声停顿后 BGM 柔和回弹；
- `afade=t=out:st=1795:d=5`：片尾前 5 秒平滑淡出。

---

## 12. Video Layout Engine 坐标系统与 FFmpeg 合成

### 12.1 竖屏 9:16 (1080×1920) 几何计算
- **画布尺寸**：$W = 1080, H = 1920$
- **背景图层**：封面等比缩放填满画布后施加 `boxblur=20:20`；
- **封面居中层 (Contain)**：
  - 设原始封面尺寸为 $w \times h$；
  - 缩放比率 $s = \min(920 / w, 1100 / h)$；
  - 封面目标宽高 $W_{cov} = w \cdot s, H_{cov} = h \cdot s$；
  - 居中放置坐标：$X_{cov} = (1080 - W_{cov}) / 2, Y_{cov} = 320 + (1100 - H_{cov}) / 2$。
- **主标题安全区**：$Y \in [120, 240]$，水平居中；
- **副标题安全区**：$Y \in [245, 300]$，水平居中；
- **字幕安全区**：$Y \in [1500, 1680]$，底部留白 240px 避开手机手势导航条。

### 12.2 一次性合成视频 FFmpeg 命令
```bash
ffmpeg -y -loop 1 -i cover.jpg -i mixed_episode_01.m4a \
-filter_complex "\
[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];\
[0:v]scale=920:1100:force_original_aspect_ratio=decrease[fg];\
[bg][fg]overlay=(W-w)/2:360[comp1];\
[comp1]drawtext=fontfile='msyh.ttc':text='《人类简史》精选':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=160:shadowcolor=black@0.6:shadowx=2:shadowy=2[comp2];\
[comp2]drawtext=fontfile='msyh.ttc':text='第01集 · 认知革命':fontcolor=gold:fontsize=32:x=(w-text_w)/2:y=240[comp3];\
[comp3]subtitles=episode_01.ass[vout]" \
-map "[vout]" -map 1:a \
-c:v h264_nvenc -preset p4 -b:v 3500k -pix_fmt yuv420p \
-c:a copy -shortest output/Episode_01.mp4
```
*注：若 `h264_nvenc` 硬件加速不可用，自动平滑替换为 `-c:v libx264 -preset medium`。*

---

## 13. 分集规划两阶段算法实现

### 13.1 阶段一：TTS 前贪心动态规划
```python
def plan_initial_episodes(chapters: list[dict], target_mins: float = 30.0, speed_chars_per_min: float = 300.0) -> list[dict]:
    target_chars = target_mins * speed_chars_per_min # 默认 30分钟约 9000 字
    episodes = []
    current_ep_chapters = []
    current_chars = 0
    
    for ch in chapters:
        ch_chars = ch["char_count"]
        # 若单章超过 1.5 倍单集时长，强制该章节独立成集
        if ch_chars >= target_chars * 1.5:
            if current_ep_chapters:
                episodes.append(current_ep_chapters)
                current_ep_chapters = []
                current_chars = 0
            episodes.append([ch])
            continue
            
        # 尝试合并
        if current_chars + ch_chars > target_chars * 1.25 and current_ep_chapters:
            episodes.append(current_ep_chapters)
            current_ep_chapters = [ch]
            current_chars = ch_chars
        else:
            current_ep_chapters.append(ch)
            current_chars += ch_chars
            
    if current_ep_chapters:
        episodes.append(current_ep_chapters)
        
    return episodes
```

### 13.2 阶段二：TTS 后物理时长重整
在所有 Chunk 均成功生成且获得物理时长后，以各章节累计的物理秒数 $T_{ch}$ 替换预估字数，重新运行上述算法，锁定最终生成的 MP4 分集元数据。

---

## 14. T1000 硬件专项优化与安全控制

1. **EMA 权重强制 Float32**：
   在 `f5_worker.py` 加载模型后，强制执行：
   ```python
   f5_model.ema_model.to(torch.float32)
   ```
   彻底消除 16 步 ODE 求解器在 float16 下因数值下溢或上溢产生的 NaN 污染。
2. **串行执行与呼吸降温**：
   - 调度器坚持单句串行流水线（`batch_size=1`）；
   - 每个 Chunk 合成完毕后，调用 `time.sleep(cooling_pause_seconds)`（默认 30 秒）；
   - 读取 NVML 接口监控 GPU 温度，若温度超过 78°C，休眠延长至 60 秒直至回落至 65°C 以下，确保笔记本长期挂机安全。

---

## 15. 全量自动化测试矩阵

| 测试套件 | 测试用例 | 验证要点与预期结果 |
| :--- | :--- | :--- |
| **GUI & ViewModel** | `test_gui_input_validation` | 空书名、非法路径、负数字符串正确拦截，错误提示显式可见。 |
| | `test_gui_thread_isolation` | 启动合成任务后，GUI 主线程心跳定时器响应延迟 < 50ms，无未响应。 |
| **Engine & Model** | `test_hardware_checker` | T1000 准确识别为 `TIER_ENTRY_GPU`，强制生效 float32 与 16 步配置。 |
| | `test_model_smoke_test` | F5 与 Kokoro 在就绪前生成 3 秒测试音频，通过 Audio QC 判定为真。 |
| **SpeechUnit & Text** | `test_speech_unit_builder` | 自然句完整保留，无一处在成词中间断开；字数处于安全窗口内。 |
| | `test_unit_continuity` | 连续播放生成的 5 个 Unit，听感无明显音色跳变与音量突兀。 |
| **Worker & Resilience**| `test_worker_crash_recovery` | 人为 SIGKILL 杀死 Worker，主程序捕获后重启 Worker 并从断点 Chunk 继续。 |
| | `test_safe_pause_atomic` | 触发暂停时，当前在跑 Chunk 完整落盘，Manifest 状态进入 PAUSED。 |
| **Subtitle & Video** | `test_timeline_accumulation`| 抽查第 10 个字幕起止时间，与前 9 个 WAV 文件累加物理时长误差 < 1ms。 |
| | `test_video_layout_contain` | 极高（1:3）或极扁（3:1）封面合成后，均保持原始长宽比且位于安全区。 |
| **Audio QC & Mix** | `test_audio_nan_guard` | 模拟注入含 NaN 数组，Audio QC 准确拦截并抛出 `AUDIO_NAN_DETECTED`。 |
| | `test_mixer_preview_match` | 试听片段与最终视频中背景音乐的有效 RMS 能量比对误差 < 0.5dB。 |

---

## 16. 统一错误代码对照表

| 错误代码 (Error Code) | 归属类别 | 触发根因与排查建议 |
| :--- | :--- | :--- |
| `INVALID_INPUT_FILE` | 输入类 | 指定的 PDF/EPUB 文件不存在或无法访问。 |
| `SCAN_PDF_NOT_SUPPORTED`| 解析类 | PDF 无文本层（纯扫描图片），提示用户使用 OCR 版本或更换书籍。 |
| `CONTENT_LOSS_EXCESSIVE`| 验证类 | 清洗后字数缩减超过阈值，任务挂起转入 `NEEDS_REVIEW`。 |
| `HARDWARE_INSUFFICIENT` | 引擎类 | 显存不足且不支持 CPU 回退，提示硬件受限。 |
| `MODEL_WEIGHT_MISSING` | 引擎类 | 本地权重未找到且下载失败。 |
| `WORKER_IPC_TIMEOUT` | 执行类 | Persistent Worker 响应超时或子进程异常退出。 |
| `AUDIO_NAN_DETECTED` | 质量类 | F5 扩散解算溢出，触发自动重试或参数降级。 |
| `FFMPEG_RENDER_ERROR` | 合成类 | FFmpeg 滤镜参数错误或编码器不可用。 |
| `MANIFEST_SAVE_FAILED` | 存储类 | 磁盘空间不足或权限受限，原子写入失败。 |
