# 书声（ShuSheng）v2.0 系统架构与概要设计

---

## 1. 文档目的与架构愿景

### 1.1 文档定位
本文档定义“书声（ShuSheng）v2.0”的总体软件系统架构、分层设计、核心模块边界、数据流与控制流、线程与进程隔离模型、硬件自适应策略以及故障恢复机制。

本文档回答的核心问题是：
> “书声内部由哪些子系统与模块组成，它们如何协同完成一本长篇电子书的高可靠自动化有声视频生产。”

### 1.2 核心架构原则
1. **单应用一体化体验（Single App Surface）**：用户操作的是一个统一的 PySide6 图形应用。内部复杂的 Python 多环境隔离、后台 Worker 进程管理、FFmpeg 命令调用对用户完全透明。
2. **GUI 线程绝对隔离（Non-blocking GUI）**：GUI 主线程仅负责交互响应、界面渲染与命令分发，绝不直接执行 TTS 推理、FFmpeg 音视频渲染、模型下载或大文件解析，杜绝 Windows 系统“未响应”现象。
3. **确定性管道与原文忠实（Deterministic Pipeline）**：从电子书解析到字幕生成，全程基于确定性规则与数学累加，正文字幕 100% 取自原文，严禁引入 LLM 临场修改。
4. **全过程可恢复性（Crash-Resilient State）**：以 Manifest 和 Fingerprint 为核心，所有中间资产均可幂等复用。支持任务安全暂停（Safe Pause）、断点续跑（Resume）与进程崩溃自愈。
5. **引擎与音色正交解耦（Engine-Voice Decoupling）**：TTS 引擎抽象为执行管道，音色抽象为独立配置（VoiceProfile），上层业务流水线不与具体 TTS 库硬编码绑定。
6. **字幕与 TTS 单元对齐（SpeechUnit Convergence）**：V2.0 创新性地将字幕时间单位与 TTS 调度单元统一定义为 `SpeechUnit`，默认实现 1:1 映射，无需额外挂载沉重 ASR 模型即可获得高精度时间轴。

---

## 2. 总体系统分层架构

书声采用清晰的垂直分层与横向解耦架构，从上至下分为五大层次：

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        1. 表现层 (GUI Presentation)                     │
│    PySide6 MainWindow | ViewModels | Preview Widgets | Progress Panels │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Qt Signals & Slots (非阻塞跨线程)
┌───────────────────────────────────▼────────────────────────────────────┐
│                    2. 编排与应用层 (Application & Task)                  │
│    Application Core | Task Manager | Episode Planner | Config Manager │
└──────┬────────────────────────────┬─────────────────────────────┬──────┘
       │                            │                             │
┌──────▼──────────────────┐  ┌──────▼──────────────────┐  ┌───────▼──────┐
│  3.1 内容解析与验证子系统  │  │   3.2 引擎与资源管理系统  │  │ 3.3 音视频流水线│
│  - Book Parser (PDF/EPUB)│  │   - Engine Resource Mgr │  │ - Audio QC   │
│  - Text Cleaner          │  │   - Hardware Checker    │  │ - Audio Mixer│
│  - Content Validator     │  │   - Model Manager       │  │ - Video      │
│  - Chapter Detector      │  │   - VoiceProfile Manager│  │   Composer   │
│  - SpeechUnit Builder    │  │   - TTS Router          │  │ - Video      │
│                          │  │                         │  │   Layout     │
└──────┬───────────────────┘  └──────┬──────────────────┘  └───────┬──────┘
       │                             │                             │
┌──────▼─────────────────────────────▼─────────────────────────────▼──────┐
│                      4. 执行与适配层 (Execution & Workers)              │
│    TTS Backend Abstraction (F5Backend | KokoroBackend | AzureBackend)   │
│    Task-scoped Persistent Workers (IPC via JSON Lines over stdio)       │
│    FFmpeg Process Adapter (Filters, Mixing, Encoding, muxing)          │
│    Subtitle Formatter (TTS 原生时间轴生成器 / SubtitleAligner 扩展点)  │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
┌────────────────────────────────────▼────────────────────────────────────┐
│                      5. 存储与状态持久层 (Persistence & State)          │
│    Task Manifest | TTS Manifest | Chapter Manifest | Episode Manifest   │
│    Audio Chunks Cache | Atomic File Writer | Structured Logging         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. GUI 与业务层线程/进程通信架构

### 3.1 线程隔离原则
为了确保 Windows 桌面端界面的流畅度与稳定性：
- **GUI 主线程（Qt Main Thread）**：只执行 QEvents 分发、控件绘制、用户输入校验与轻量状态更新；
- **后台任务执行器（Task Manager ThreadPool / QThread）**：所有重型业务流水线在独立的工作线程中跑；
- **重型计算子进程（Isolated Subprocesses）**：F5-TTS 模型推理、Kokoro 语音合成、FFmpeg 滤镜转码等均在受控的子进程中执行。

### 3.2 信号与槽（Signals & Slots）通信链路
GUI 与后台 Task Manager 之间通过线程安全的信号槽双向交互：

```text
[用户交互] ──点击"开始生产"──> [GUI Main Thread]
                                   │
                                emit sig_start_task(config)
                                   ▼
                            [Task Manager (Worker Thread)]
                                   │
                                   ├── 1. 检查硬件与模型状态
                                   ├── 2. 解析正文 & 规划 SpeechUnit
                                   ├── 3. 调度 TTS Worker 逐段生成
                                   ├── 4. 状态变迁 & 原子存盘
                                   │
       [GUI Main Thread] <── emit sig_progress_updated(pct, msg) ──┤
               │                   │
        更新进度条与状态文案       ├── 5. 触发 Audio Mixer & Video Composer
                                   │
       [GUI Main Thread] <── emit sig_task_completed(output_dir) ──┘
```

---

## 4. 核心系统模块划分与职责

系统划分为以下 18 个高内聚、低耦合的核心模块：

| 模块名称 | 归属子系统 | 核心职责 |
| :--- | :--- | :--- |
| **GUI Layer** | 表现层 | 负责用户参数输入、封面/混音/计划预览展示、控制按钮与进度反馈。 |
| **Task Manager** | 应用编排 | 管理任务全生命周期，协调各子阶段流转，处理安全暂停、错误与断点恢复。 |
| **Engine Resource Mgr** | 引擎管理 | 硬件能力诊断（CUDA/显存/TDP）、环境校验、模型一键自动下载与就绪检查。 |
| **Hardware Checker** | 引擎管理 | 检查显卡型号、VRAM 容量与可用性，决定运行步数与降温策略。 |
| **Model Manager** | 引擎管理 | 统一管理 HuggingFace/ModelScope 权重下载、完整性校验与本地缓存路径。 |
| **Book Ingest & Parser** | 文本解析 | 支持 PDF/EPUB 导入，提取文字层、目录与元数据；提供纯扫描件拦截保护。 |
| **Text Cleaner** | 文本解析 | 确定性清洗页眉、页脚、页码及格式乱码，绝对不改变正文字词。 |
| **Validator** | 文本解析 | 校验正文字符损失率、空章节与异常截断，超阈值自动触发 `NEEDS_REVIEW`。 |
| **Chapter Detector** | 文本解析 | 识别书籍大纲、章节标题与层级结构，构建统一的内部书籍大纲树。 |
| **SpeechUnit Builder** | 文本解析 | 将章节切分为兼具自然朗读体验与字幕展示适宜性的最小生成单元。 |
| **TTS Router** | 语音调度 | 统一调度 F5、Kokoro、Azure 后端，实现请求路由与参数转换。 |
| **VoiceProfile Manager** | 语音调度 | 集中管理音色配置资产，解耦音色名称与底层模型参数。 |
| **Persistent Worker** | 语音执行 | 跨环境 Python 工作进程，单次启动常驻显存，连续批处理，隔离运行依赖。 |
| **Subtitle Engine** | 视频字幕 | 基于 1:1 WAV 实际时长生成精准原文字幕，保留 SubtitleAligner 对齐接口。 |
| **Audio QC** | 音频流水线 | 检查生成的 WAV 文件完整性、静音异常、振幅削顶及 NaN 溢出。 |
| **Audio Mixer** | 音频流水线 | 旁白与 BGM 增益换算、基于侧链压缩的自动闪避（Ducking）、BGM 自动循环淡出。 |
| **Video Layout Engine** | 视频流水线 | 智能计算 9:16 / 16:9 画布布局，实现封面 Contain 等比缩放与高斯模糊填充。 |
| **Video Composer** | 视频流水线 | 调用 FFmpeg 将背景、标题、字幕、混音音轨一次性封装渲染为分集 MP4。 |

---

## 5. 引擎资源管理器 (Engine Resource Manager)

### 5.1 硬件适配与等级划分
为了让不同硬件（特别是类似 NVIDIA T1000 4GB 笔记本显卡）均能稳定跑通长任务：
- **硬件探针（Hardware Checker）**：
  - 检测 CUDA 是否可用、当前可用显存大小、GPU 核心数及功耗等级；
  - 判定等级：`TIER_ENTRY_GPU` (<=4GB VRAM，如 T1000)、`TIER_MID_GPU` (6~8GB)、`TIER_HIGH_GPU` (>=12GB)、`TIER_CPU_ONLY`。
- **动态运行策略匹配**：
  - T1000 等级：强制单句串行处理（batch_size=1）、强制 float32 精度（防止 ODE 扩散数值溢出）、默认采用 16 步轻量扩散、设置 30 秒呼吸冷却间隔防热降频；
  - 高配显卡：可提升步数至 32 步，缩短或取消冷却等待。

### 5.2 模型自动部署与初始化
- 用户首次选用某本地引擎（如 F5-TTS）时，Engine Resource Manager 自动检测模型文件完整性；
- 若缺失权重，弹出下载向导，后台启动多线程下载并显示百分比进度与校验码核对；
- 权重下载完成后，自动执行内置的轻量 Smoke Test（合成测试音频）；通过后状态置为 `READY`，方可允许启动生产任务。

---

## 6. 文本处理流水线与 SpeechUnit 体系

### 6.1 文本流水线流向
```text
PDF / EPUB 原始文件
       │
       ▼
[Book Parser] ──> 提取页面/章节文字与结构，检测文本层有效性
       │
       ▼
[Text Cleaner] ──> 消除重复页眉、页脚、页码，连接断行（纯确定性正则表达式）
       │
       ▼
[Validator] ──> 对比清洗前后字符变化率，异常时进入 NEEDS_REVIEW 阻断
       │
       ▼
[Chapter Detector] ──> 确定正式物理章节清单与标题
       │
       ▼
[SpeechUnit Builder] ──> 构建自然朗读最小单元
```

### 6.2 SpeechUnit 与 Chunk 的关系体系
为了从根本上解决“TTS 音频与字幕起止时间边界不确定”的问题，书声 v2.0 正式确立 **SpeechUnit** 核心概念：

```text
Chapter (章节)
   └── Paragraph (段落)
          └── Sentence (自然句)
                 └── SpeechUnit (自然朗读单元 / 字幕单元)
                        └── Chunk (TTS 任务调度与执行单元)
                               └── WAV (独立音频资产)
```

1. **核心职责划分**：
   - **SpeechUnit**：面向正文的自然朗读与字幕时间区间的最小单元。首要目标是保证朗读断句自然流畅，同时文字体量适合字幕行显示；
   - **Chunk**：面向底层系统的调度实体，承载 ID、文本哈希、引擎参数、Fingerprint、状态流转、重试（Retry）、缓存（Cache）与断点续跑（Resume）。
2. **V2.0 默认 1:1 极简映射**：
   - 当前版本默认：`1 SpeechUnit = 1 Chunk = 1 WAV 文件`；
   - 彻底避免在系统初期引入复杂的“多对一”或“一对多”双向映射树，保持系统精炼高可靠；
   - *架构保留点*：未来若接入流式输出 TTS 或段落级超大上下文 TTS 模型，可在 TTS Router 内部解耦，上层接口保持不变。
3. **切分策略约束**：
   - 优先保持自然句完整，在句号、问号、感叹号等边界处聚合；
   - 短句合理并入一个 SpeechUnit，避免把语音切得过碎导致发音断续；
   - 独立配置 `speech_unit_max_chars`（默认通过 F5 连贯性测试优化确定），不随意压缩底层单 Chunk 安全最大字符限制。

---

## 7. TTS 路由与持久化 Worker 架构

### 7.1 引擎与音色解耦体系 (VoiceProfile)
业务流水线仅与抽象的 `VoiceProfile` 交互：
- 一个 `VoiceProfile` 包含：
  - `profile_id`（全局唯一标识，如 `preset_male_d1_elite`）
  - `display_name`（面向用户的名称，如“男声 / 商业精英 (D1)”）
  - `engine`（绑定引擎：`f5_tts` / `kokoro` / `azure`）
  - `ref_audio_path` / `ref_text`（针对克隆引擎的参考声音）
  - `speed`（语速微调系数，默认 1.0）
  - `nfe_step`（推理步数，默认 16）
  - `cooling_seconds`（降温间隔，默认 30）

### 7.2 Task-scoped Persistent Worker 机制
对于本地模型（F5 / Kokoro），绝不在每个 Chunk 执行时重新启动 Python 进程和加载数 GB 模型权重。系统采用“任务级持久化 Worker”：

```text
[书声主进程 Main App (Python 3.10)]
       │
       │ 1. 启动任务: 孵化专用环境子进程
       ▼
[F5 Worker Subprocess (envs/f5/python.exe)]
       │
       │ 2. 模型仅在启动时加载一次至 GPU (EMA to Float32)
       │ 3. 向标准输出写入就绪信号: {"status": "READY"}
       ▼
[IPC 通信循环 (JSON Lines over stdio)]
   Main App ──发送 TTS 请求──> Worker stdin
   Main App <──接收生成完成── Worker stdout (专用通道，隔离三方输出)
       │
       │ 4. 本次运行结束 / 用户点击安全暂停 / 异常发生
       ▼
[Worker 优雅安全退出，释放显存]
```

- **三方日志干扰防御**：Worker 内部将 `sys.stdout` 强行重定向至 `sys.stderr`，仅开辟独立的 `_ipc_stdout` 句柄收发纯净 JSON Lines，彻底杜绝 PyTorch / tqdm / Vocos 的控制台字符破坏通信协议。
- **崩溃自动复原**：若 Worker 进程因意外 CUDA 错误退出，主程序捕获异常信号后，将当前 Chunk 标为待重试，重新唤醒 Worker 继续处理，已成功的历史 Chunk 绝不重做。

---

## 8. 字幕时间对齐架构 (Subtitle Architecture)

### 8.1 默认模式：TTS 原生时间轴
书声 v2.0 字幕系统的最大工程优势在于：**在已知权威原文的前提下，利用 TTS 真实音频时间建立字幕，不重复执行 ASR 识别。**

- **核心工程定论**：
  > “如果字幕单元与独立 TTS 音频单元一一对应（1 SpeechUnit = 1 WAV），则单元起止时间可以直接使用实际音频时长准确获得；如果一个音频单元内部包含多个字幕句，则不能靠字数比例宣称精确，需要进一步时间对齐。”

- **时间累计算法流程**：
  1. 每个 SpeechUnit 经 TTS 生成一个独立的 WAV 文件；
  2. Audio QC 验证通过后，读取其真实物理时长（`duration`，精度达毫秒级）；
  3. 维护当前分集的累加游标 `timeline_cursor`；
  4. 第 $i$ 个字幕区间的开始时间即为当前游标，结束时间为 `timeline_cursor + duration`，随后游标推进；
  5. 字幕内容直接填充该 SpeechUnit 对应的电子书原文字符串。

### 8.2 扩展架构：SubtitleAligner 接口预留
系统中保留 `SubtitleAligner` 抽象基类，但不作为 V2.0 默认依赖项，仅在未来满足以下条件时按需挂载：
- 单个 Chunk 包含多句且无法细拆；
- 需要词级（Word-level）逐字卡拉OK动效；
- 实际测试发现部分语段的前后静音造成字幕感知滞后。

---

## 9. 智能混音与音频处理流水线

### 9.1 音量增益模型与试听一致性
系统杜绝“试听与最终成片响度不一”的工程隐患，采用统一的线性百分比到分贝（dB）增益模型：
$$	ext{Gain (dB)} = 20 	imes \log_{10}\left(rac{	ext{Volume Percent}}{100}ight)$$
无论是 15 秒混音试听还是最终整集 MP4 合成，统一调用相同的 FFmpeg 滤镜链参数生成。

### 9.2 自动闪避 (Ducking / 侧链压缩)
为了保证旁白清晰听辨，系统采用侧链压缩（Sidechain Compression）实现智能动态压音：
- **旁白主轨（Master Voice Track）** 作为触发侧链；
- **背景音乐轨（BGM Track）** 作为受控对象；
- 当检测到旁白信号超过阈值时，BGM 自动快速平滑下潜指定的压降量（如 -14dB）；当旁白停顿或结束时，BGM 缓慢回弹至基准音量；
- 整集结束前 3~5 秒，BGM 自动触发线性淡出（Fade-out）。

---

## 10. 视频合成与自动排版引擎 (Video Layout Engine)

### 10.1 视频版面架构
Video Layout Engine 负责计算不同长宽比下的几何变换矩阵，杜绝图像拉伸与变形：

```text
       【竖屏 9:16 (1080×1920)】                       【横屏 16:9 (1920×1080)】
┌──────────────────────────────────────┐     ┌──────────────────────────────────────────┐
│  主标题区 (Safe Zone Top: 80~240px)  │     │ 顶部安全区                               │
│  第XX集·章节副标题                   │     ├──────────┬────────────────────┬──────────┤
│                                      │     │          │   主标题与副标题   │          │
│ ┌──────────────────────────────────┐ │     │ 封面等比 │                    │ 模糊背景 │
│ │                                  │ │     │ 完整居中 │   字幕显示安全区   │          │
│ │         封面图片 (Contain)       │ │     │ (Contain)│                    │          │
│ │       等比居中，保留原书名       │ │     │          │                    │          │
│ │                                  │ │     │          │                    │          │
│ └──────────────────────────────────┘ │     ├──────────┴────────────────────┴──────────┤
│ 动态高斯模糊背景填充 (Blur Sigma: 20) │     │ 底部留白安全区                           │
│                                      │     └──────────────────────────────────────────┘
│  高对比度原文字幕区 (Bottom: 1500px) │
└──────────────────────────────────────┘
```

### 10.2 合成流水线 (Video Pipeline)
1. **静态视觉图层构建**：生成高斯模糊背景流，并将封面原图等比叠加在中央；
2. **文本图层渲染**：利用 `drawtext` 滤镜渲染主标题与自动副标题，内置抗锯齿与阴影描边；
3. **字幕流挂载**：挂载由 SpeechUnit 累加生成的 ASS 格式字幕流；
4. **复合音轨注入**：注入经 Audio Mixer 处理完毕的高品质 AAC 混音音轨；
5. **硬件编码加速（NVENC / libx264）**：自动检测 GPU 硬件编码器（h264_nvenc），不可用时平滑回退至 libx264 软件编码，输出符合主流播放器规范的分集 MP4。

---

## 11. 分集规划架构 (Episode Planning)

分集规划采用 **两阶段规划机制**，兼顾预估确定性与成片精确度：

```text
阶段一：TTS 启动前 (预估规划)
输入: 全书各章节字符数 + 所选音色基准语速 (chars/min)
计算: 预估章节时长 -> 结合"单集目标时长"执行动态规划分组
产出: 生产计划预览表 (预计总集数、各集章节归属、预估耗时) -> 呈现给用户确认

阶段二：TTS 完成后 (真实规划)
输入: 各章节所有 SpeechUnit 生成的真实 WAV 物理时长汇总
计算: 累加精确物理时长 -> 在完整章节边界处划分正式 Episode
约束: 严禁将单个章节横切拆分到两集中；若单章真实时长已超目标，则单章独立成集
产出: 最终分集元数据清单 (Episode Manifest) -> 驱动后续混音与视频合成
```

---

## 12. 状态机、Manifest 与断点续跑体系

### 12.1 全局任务状态机 (Task State Machine)
任务状态在应用生命周期内严格单向流转，异常与暂停具备专门恢复路径：

```text
CREATED ──> INGESTING ──> PARSED ──> CLEANED ──> VALIDATED
                                                    │ (字符异常)
                                                    ▼
                                              NEEDS_REVIEW (需人工确认)
                                                    │ (确认继续)
                                                    ▼
AUDIO_QC <── TTS_GENERATING <── PLANNED <───────────┘
   │               │ (点击安全暂停)
   │               ▼
   │           PAUSING ──> PAUSED (保存断点，释放显存)
   │                         │ (点击继续)
   │                         ▼
   │                   TTS_GENERATING (接续未完 Chunk)
   ▼
ALIGNING_SUBTITLES ──> AUDIO_MIXING ──> VIDEO_RENDERING ──> COMPLETED
       │                                     │
       └────────── 异常中断或失败 ────────────┴──> PARTIAL_FAILED / FAILED
```

### 12.2 Manifest 存储与原子写入
1. **结构化清单**：
   - `tts_manifest.json`：持久化记录每一个 Chunk / SpeechUnit 的哈希、音色、时长、生成状态与物理路径；
   - `episode_manifest.json`：记录最终分集划分、包含的章节、合成的 MP4 与 SRT 路径；
2. **原子写入保障（Atomic Write Protocol）**：
   - 任何状态或清单的更新，严禁直接在原文件上流式覆盖；
   - 必须先写入 `.tmp` 临时文件，执行操作系统的 `flush` 与 `fsync` 确保落盘，再通过 `os.replace` 原子性替换原文件；
   - 彻底杜绝因断电、崩溃造成的 Manifest 文件半截截断损坏。

### 12.3 安全暂停 (Safe Pause) 控制流
用户点击“安全暂停”后：
1. GUI 向 Task Manager 发出 `sig_request_pause` 信号；
2. Task Manager 将内部原子标记 `pause_requested = True`；
3. TTS Worker 当前正在执行的微小 SpeechUnit（通常 5~10 秒）继续合成直至结束；
4. 结果落盘并更新 Manifest，状态置为 `PAUSED`；
5. 优雅终止 Worker 子进程，释放 GPU 显存；
6. GUI 状态栏提示“任务已安全暂停，所有进度已保存”。

---

## 13. 错误隔离与韧性原则

1. **引擎故障隔离**：本地 F5 模型发生 CUDA 异常，只导致当前 Chunk 重试或标记为局部错误，绝不导致主界面崩溃闪退，更不影响 Kokoro 或 Azure 引擎的独立运行。
2. **音频质量熔断（Audio QC Safeguard）**：任何单段音频一旦出现持续静音、时长为零、振幅溢出或 NaN，立即阻断合并流程，记录错误日志并触发有限次数重试。
3. **安全凭据防泄漏**：云端 API Key（如 Azure Speech Key）严禁写入源码、配置及日志文件，仅允许通过本地环境变量或加密配置加载，日志打印自动脱敏。
