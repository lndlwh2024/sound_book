# BookAgent v0.1 概要设计与产品架构

## 1. 文档目的

本文档定义 BookAgent v0.1 的总体架构、模块划分、运行环境、TTS 接入方式、数据流、缓存机制、故障恢复机制和扩展原则。

本版本只实现：

**MODE_A_AUDIOBOOK：原文有声书**

暂不实现：

- 深度精讲
- OCR
- Web UI
- 视频生成
- 数字人
- LLM 总结或改写

---

# 2. 架构目标

BookAgent 采用：

**单应用 + 多 TTS Backend + 本地 Worker 环境隔离**

架构。

核心目标：

1. 用户只操作一个 BookAgent；
2. 用户运行时选择 TTS 引擎；
3. 用户不需要理解或操作多个 Python 环境；
4. Kokoro、F5-TTS、Azure AI Speech 对上层业务提供统一接口；
5. TTS 引擎之间互相隔离，不因依赖冲突影响整个应用；
6. 支持长书；
7. 支持缓存；
8. 支持断点续跑；
9. 支持失败重试；
10. 支持以后增加新的 TTS Backend，而无需修改核心 Book Pipeline。

---

# 3. 用户视角架构

对于最终用户，BookAgent 始终表现为一个应用。

启动方式：

    run.bat

或：

    python app.py

启动后：

    BookAgent v0.1

    请输入书籍路径：
    > D:\Books\book.pdf

    请选择 TTS：

    1. Kokoro
    2. F5-TTS
    3. Azure AI Speech

    > 1

用户不得手工执行：

    activate
    deactivate

也不得要求用户手动切换 Kokoro 或 F5 的 Python 环境。

所有环境选择和 Worker 调用均由 BookAgent 自动完成。

---

# 4. 总体产品架构

总体数据流：

    PDF / EPUB
          │
          ▼
      BookAgent
          │
          ▼
    Application Layer
          │
          ▼
      Task Manager
          │
          ▼
      Book Pipeline
          │
          ├───────────────┐
          │               │
          ▼               ▼
     Book Parser      State Manager
          │               │
          ▼               │
     Text Cleaner          │
          │               │
          ▼               │
      Validator            │
          │               │
          ▼               │
       Chunker             │
          │               │
          └───────┬───────┘
                  ▼
              TTS Router
                  │
       ┌──────────┼──────────┐
       │          │          │
       ▼          ▼          ▼
    Kokoro      F5-TTS      Azure
    Backend     Backend      Backend
       │          │          │
       ▼          ▼          ▼
    Kokoro      F5-TTS    Azure Speech
    Worker       Worker      API
       │          │          │
       └──────────┼──────────┘
                  ▼
               WAV Cache
                  │
                  ▼
              Audio QC
                  │
                  ▼
               FFmpeg
                  │
          ┌───────┴────────┐
          ▼                ▼
    Chapter MP3        Book M4B

---

# 5. 核心设计思想

## 5.1 单应用体验

BookAgent 对用户必须表现为一个完整应用。

虽然内部可能存在：

- 主 Python 环境；
- Kokoro Python 环境；
- F5-TTS Python 环境；

但这些实现细节必须对用户透明。

---

## 5.2 TTS Backend 解耦

业务 Pipeline 不允许直接依赖：

- Kokoro；
- F5-TTS；
- Azure SDK。

必须通过统一的 TTS Backend 接口调用。

因此：

Book Pipeline 只关心：

    text
    voice
    speed
    output_path

不关心底层到底是：

    Kokoro
    F5-TTS
    Azure
    未来其他 TTS

---

## 5.3 中间数据可恢复

整个项目必须保存关键中间结果，包括：

- 原始文本；
- 清洗文本；
- 书籍结构；
- TTS Manifest；
- Chunk 状态；
- Chunk WAV；
- Chapter MP3；
- 日志。

任何一个阶段失败后，都应该尽量从失败点继续，而不是重新处理整本书。

---

# 6. 核心模块划分

BookAgent v0.1 主要包括以下模块：

1. Application Layer
2. Task Manager
3. Book Parser
4. Text Cleaner
5. Validator
6. Chunker
7. State Manager
8. TTS Router
9. TTS Backend
10. TTS Worker
11. Audio Cache
12. Audio QC
13. Audio Assembler
14. Logging
15. Config Manager

---

# 7. Application Layer

职责：

- 程序入口；
- CLI 参数解析；
- 用户交互；
- 书籍路径输入；
- TTS 选择；
- Voice 选择；
- Speed 设置；
- 启动任务；
- 显示执行进度；
- 显示错误摘要。

主要文件建议：

    app.py

Application Layer 不应该实现：

- PDF 解析逻辑；
- TTS 具体逻辑；
- 音频拼接逻辑；
- 缓存判断逻辑。

它只负责协调各模块。

---

# 8. Task Manager

Task Manager 管理一本书从输入到最终输出的完整任务。

主要职责：

- 创建 book_id；
- 加载已有任务；
- 创建新任务；
- 保存当前任务状态；
- 控制各阶段执行顺序；
- 判断是否继续已有任务；
- 处理 FAILED；
- 处理 NEEDS_REVIEW；
- 控制 Resume。

任务状态：

    INGEST
      ↓
    PARSED
      ↓
    CLEANED
      ↓
    VALIDATED
      ↓
    TTS_READY
      ↓
    TTS_GENERATING
      ↓
    AUDIO_QC
      ↓
    ASSEMBLED
      ↓
    COMPLETED

异常状态：

    NEEDS_REVIEW

    FAILED

---

# 9. Book Parser

Book Parser 负责将原始书籍转换为结构化文本。

v0.1 支持：

- PDF
- EPUB

暂不支持：

- OCR；
- 扫描图片型 PDF；
- DOCX；
- 网页。

---

# 10. PDF Parser

推荐：

**PyMuPDF**

主要职责：

1. 打开 PDF；
2. 检测 PDF 是否存在有效文本层；
3. 按页提取文字；
4. 保存页码与文本的对应关系；
5. 获取 PDF metadata；
6. 尝试读取 PDF TOC；
7. 输出原始文本；
8. 输出候选章节结构。

如果文本密度过低：

返回：

    SCAN_PDF_NOT_SUPPORTED

不得默认启动 OCR。

---

# 11. EPUB Parser

主要职责：

- 读取 EPUB metadata；
- 读取导航结构；
- 读取 spine；
- 保留章节顺序；
- 提取 XHTML 正文；
- 去除 HTML tag；
- 去除 CSS；
- 去除 JavaScript；
- 保留标题；
- 保留段落。

输出格式应与 PDF Parser 尽量统一。

---

# 12. Book Structure

统一书籍结构建议：

    Book
    ├── Metadata
    ├── Front Matter
    ├── Chapter 001
    │   ├── Section
    │   └── Paragraphs
    ├── Chapter 002
    ├── ...
    ├── Appendix
    └── End Matter

程序不得根据猜测创造不存在的章节。

---

# 13. Text Cleaner

Text Cleaner 负责确定性文本清理。

允许处理：

- 页码；
- 重复页眉；
- 重复页脚；
- PDF 异常换行；
- 连续空格；
- 多余空行；
- 排版产生的错误换行；
- 明显排版噪声。

禁止：

- AI 改写；
- AI 润色；
- 自动总结；
- 改变作者措辞；
- 删除正常正文。

---

# 14. Cleaner 数据流

输入：

    raw_text.json

输出：

    cleaned_text.json

同时生成：

    cleaning_report.json

报告至少包含：

- before_chars；
- after_chars；
- removed_headers；
- removed_footers；
- removed_page_numbers；
- change_ratio。

---

# 15. Validator

Validator 用于防止 BookAgent 在文本已经损坏时继续生成几小时音频。

需要检查：

- 原始字符数量；
- 清洗后字符数量；
- 章节数量；
- 是否存在空章节；
- 是否存在异常字符损失；
- 是否存在明显解析失败。

正常：

    VALIDATED

异常：

    NEEDS_REVIEW

进入 NEEDS_REVIEW 后：

不得自动开始 TTS。

---

# 16. Chunker

Chunker 将章节正文切分为适合 TTS 的最小生成单元。

切分优先级：

    Chapter
        ↓
    Paragraph
        ↓
    Sentence
        ↓
    Chunk

禁止机械按照固定字符位置切断一句话。

只有单句本身超过 Backend 限制时，才允许进一步拆分。

---

# 17. TTS Chunk

每个 Chunk 必须拥有稳定 ID。

示例：

    chapter_001_chunk_0001
    chapter_001_chunk_0002
    chapter_002_chunk_0001

每个 Chunk 保存：

- chunk_id；
- chapter_id；
- order；
- text；
- text_hash；
- backend；
- voice；
- speed；
- fingerprint；
- status；
- output_file；
- retry_count；
- error。

---

# 18. TTS Manifest

所有 Chunk 状态统一存储于：

    tts_manifest.json

Manifest 是：

- 缓存；
- Resume；
- 重试；
- 音频拼接；

的核心依据。

每生成成功一个 Chunk：

必须立即保存状态。

禁止等整本书完成后才更新 Manifest。

---

# 19. State Manager

State Manager 负责：

- Book 状态；
- Chapter 状态；
- Chunk 状态；
- Resume；
- Atomic Save；
- Crash Recovery。

关键 JSON 文件建议采用：

    temp write
        ↓
    fsync
        ↓
    atomic replace

避免程序异常退出导致 Manifest 损坏。

---

# 20. TTS Router

TTS Router 是 Book Pipeline 与所有 TTS 引擎之间的唯一入口。

逻辑：

    Book Pipeline
          │
          ▼
      TTS Router
          │
          ├── KokoroBackend
          ├── F5Backend
          └── AzureBackend

Pipeline 不允许直接出现大量：

    if backend == kokoro
    elif backend == f5
    elif backend == azure

这种分支逻辑。

Backend 的选择统一集中到 Router / Factory。

---

# 21. 统一 TTS Backend 接口

三个 Backend 必须实现统一逻辑接口：

    synthesize(
        text,
        output_path,
        voice=None,
        speed=1.0,
        options=None
    )

建议同时提供：

    health_check()

统一返回：

    TTSResult

其中至少包含：

- success；
- output_path；
- duration；
- error_code；
- error_message。

---

# 22. TTS 环境架构

BookAgent 内部建议使用三个 Python 环境。

## 22.1 bookagent-main

负责：

- 主应用；
- Parser；
- Cleaner；
- Validator；
- Chunker；
- State；
- Router；
- FFmpeg；
- Azure SDK；
- 日志。

---

## 22.2 bookagent-kokoro

专门负责：

**Kokoro TTS**

不运行其他业务模块。

---

## 22.3 bookagent-f5

专门负责：

**F5-TTS**

不运行其他业务模块。

---

# 23. 为什么本地 TTS 独立环境

Kokoro 和 F5-TTS 可能拥有不同版本的：

- torch；
- torchaudio；
- transformers；
- numpy；
- tokenizer；
- vocoder；
- CUDA 相关依赖。

如果全部安装在一个 Python 环境里，可能产生依赖冲突。

因此：

用户看到：

    一个 BookAgent

内部实际上运行：

    bookagent-main
    bookagent-kokoro
    bookagent-f5

但环境切换由程序自动完成。

---

# 24. Worker 调用架构

主应用使用 subprocess 调用本地 Worker。

示例：

    BookAgent Main
          │
          ▼
      TTS Router
          │
          ▼
    KokoroBackend
          │
          ▼
      subprocess
          │
          ▼
    envs/kokoro/Scripts/python.exe
          │
          ▼
    workers/kokoro_worker.py
          │
          ▼
        WAV

F5 同理。

---

# 25. Worker 通信

v0.1 优先采用：

**subprocess + JSON**

不引入：

- Redis；
- RabbitMQ；
- Docker；
- HTTP 微服务；
- 消息队列。

输入示例：

    {
      "text": "BookAgent 测试文字",
      "voice": "...",
      "speed": 1.0,
      "output_path": "..."
    }

输出示例：

    {
      "success": true,
      "output_file": "...",
      "duration": 5.8,
      "error_code": null,
      "error_message": null
    }

---

# 26. Kokoro Backend

架构：

    Main Application
          │
          ▼
    KokoroBackend
          │
          ▼
    Kokoro Worker
          │
          ▼
    Kokoro Model
          │
          ▼
        WAV

要求：

- 支持 voice；
- 支持 speed；
- 支持 device=auto；
- 支持 CPU；
- 如 CUDA 可用可尝试 CUDA；
- 输出统一 WAV。

---

# 27. F5-TTS Backend

架构：

    Main Application
          │
          ▼
      F5Backend
          │
          ▼
      F5 Worker
          │
          ▼
      F5-TTS
          │
          ▼
        WAV

支持参数：

- text；
- ref_audio；
- ref_text；
- speed；
- device；
- output_path。

设备：

    auto
    cuda
    cpu

当前开发机器只有：

**4GB NVIDIA VRAM**

因此 F5-TTS 不保证 CUDA 一定能够运行。

如果 CUDA OOM：

返回：

    CUDA_OOM

不得无限重试。

是否允许 CPU fallback：

通过配置控制。

---

# 28. Azure AI Speech Backend

Azure 使用：

**Microsoft Azure AI Speech Text-to-Speech 官方 API**

不使用 edge-tts 替代。

运行于：

    bookagent-main

无需单独 Python 环境。

使用官方 Python SDK：

    azure-cognitiveservices-speech

支持：

- voice；
- rate；
- volume；
- SSML；
- WAV 输出。

---

# 29. Azure 配置安全

Azure 凭据从：

    .env

读取。

变量：

    AZURE_SPEECH_KEY

    AZURE_SPEECH_REGION

禁止：

- 写入代码；
- 写入 config.yaml；
- 提交 Git；
- 输出到日志。

项目提供：

    .env.example

---

# 30. Azure 错误处理

可以重试：

- timeout；
- 网络错误；
- HTTP 429；
- 服务端 5xx。

建议：

**有限次数指数退避重试**

认证失败：

    AZURE_AUTH_FAILED

应立即停止，不做无限重试。

---

# 31. Cache 架构

所有成功生成的 TTS Chunk 应缓存。

结构：

    audio_chunks/
        chapter_001_chunk_0001.wav
        chapter_001_chunk_0002.wav
        ...

缓存是否有效由：

**fingerprint**

判断。

---

# 32. Fingerprint

Fingerprint 至少包含：

- text_hash；
- backend；
- voice；
- speed；
- 影响语音生成结果的 Backend 参数。

例如：

    SHA256(
        text_hash
        + backend
        + voice
        + speed
        + relevant_options
    )

如果 fingerprint 一致：

且输出 WAV 有效：

    SKIP

如果 fingerprint 改变：

    REGENERATE

---

# 33. Resume 架构

例如一本书：

    Chunk 001 SUCCESS
    Chunk 002 SUCCESS
    ...
    Chunk 350 SUCCESS
    Chunk 351 FAILED
    Chunk 352 PENDING

程序重启后：

从 Chunk 351 继续。

不得重新生成 Chunk 001～350。

---

# 34. Retry 架构

单个 Chunk 默认最多：

    3 retries

超过最大次数：

    FAILED

但其他已成功 Chunk 不删除。

整书任务可进入：

    PARTIAL_FAILED

用户修复原因后：

再次 Resume。

---

# 35. Audio Cache

所有 Backend 最终统一提供：

    WAV

这样后续 Audio Pipeline 不需要关心原始 TTS 引擎。

数据流：

    TTS Backend
          ↓
        WAV
          ↓
    Audio QC
          ↓
    Chapter Assembly
          ↓
      MP3 / M4B

---

# 36. Audio QC

最低检查：

1. 文件是否存在；
2. 文件大小是否正常；
3. FFmpeg 是否可读取；
4. duration 是否大于 0；
5. 是否出现明显异常静音；
6. 是否缺失 Chunk。

Audio QC 不负责：

复杂语音语义检查。

v0.1 不引入额外语音识别模型进行逐字校验。

---

# 37. Audio Assembly

职责：

- Chunk WAV 合并；
- 章节生成；
- 整书生成；
- metadata；
- chapter timestamp。

使用：

**FFmpeg**

---

# 38. Chapter Assembly

只有当某章节的所有必须 Chunk：

    SUCCESS

才能生成：

    chapter_001.mp3

Chunk 排序必须严格依据：

Manifest 中的 order。

不能依赖文件系统默认排序。

---

# 39. Chapter Manifest

章节生成后保存：

    chapter_manifest.json

至少包含：

- chapter_id；
- title；
- order；
- audio_file；
- duration；
- start_time；
- end_time。

---

# 40. M4B Assembly

最终整书输出：

    book.m4b

M4B 应包含：

- Title；
- Author；
- Cover；
- Chapter Name；
- Chapter Start Time；
- Chapter End Time。

章节时间必须依据：

**真实音频时长**

不得依据文字长度估算。

---

# 41. 配置体系

非敏感配置放：

    config.yaml

建议结构：

    tts:
      default_backend: kokoro

      kokoro:
        voice: ""
        speed: 1.0
        device: auto

      f5:
        speed: 1.0
        device: auto
        ref_audio: ""
        ref_text: ""
        cpu_fallback: true

      azure:
        voice: zh-CN-XiaoxiaoNeural
        rate: 1.0
        volume: 1.0

    chunking:
      max_chars: 1000

    audio:
      sample_rate: 24000
      channels: 1
      mp3_bitrate: 128k

敏感信息只允许：

    .env

---

# 42. 推荐项目目录

    BookAgent/
    │
    ├── app.py
    ├── config.yaml
    ├── requirements.txt
    ├── .env.example
    ├── .gitignore
    ├── setup.bat
    ├── run.bat
    │
    ├── docs/
    │   ├── 01_PRD.md
    │   ├── 02_ARCHITECTURE.md
    │   └── 03_DETAILED_DESIGN.md
    │
    ├── src/
    │   ├── app/
    │   ├── parser/
    │   ├── cleaner/
    │   ├── validator/
    │   ├── chunker/
    │   ├── state/
    │   ├── tts/
    │   │   ├── base.py
    │   │   ├── router.py
    │   │   ├── kokoro_backend.py
    │   │   ├── f5_backend.py
    │   │   └── azure_backend.py
    │   │
    │   ├── audio/
    │   └── utils/
    │
    ├── workers/
    │   ├── kokoro_worker.py
    │   └── f5_worker.py
    │
    ├── envs/
    │   ├── kokoro/
    │   └── f5/
    │
    ├── books/
    ├── output/
    └── logs/

---

# 43. 单本书目录

每本书建立独立工作目录。

例如：

    books/
    └── <book_id>/
        │
        ├── source/
        │   └── book.pdf
        │
        ├── parsed/
        │   ├── metadata.json
        │   ├── structure.json
        │   └── raw_text.json
        │
        ├── cleaned/
        │   ├── cleaned_text.json
        │   └── cleaning_report.json
        │
        ├── manifests/
        │   ├── task_manifest.json
        │   └── tts_manifest.json
        │
        ├── audio_chunks/
        │
        ├── chapters/
        │
        ├── output/
        │   └── book.m4b
        │
        └── logs/

---

# 44. setup.bat 架构

提供：

    setup.bat

职责：

1. 检查 Python；
2. 检查 FFmpeg；
3. 创建主环境；
4. 安装主环境依赖；
5. 创建 Kokoro 环境；
6. 安装 Kokoro 依赖；
7. 创建 F5 环境；
8. 安装 F5 依赖；
9. 创建必要目录；
10. 创建或提示配置 .env；
11. 执行 smoke test。

如果某一个 TTS 安装失败：

必须明确指出：

    KOKORO_SETUP_FAILED

或：

    F5_SETUP_FAILED

不得把安装失败伪装为成功。

---

# 45. run.bat 架构

run.bat 的职责应该非常简单：

    启动 bookagent-main
        ↓
    执行 app.py

用户不需要知道 Python venv 的具体路径。

---

# 46. Smoke Test

三个 Backend 必须独立提供 smoke test。

## Kokoro

测试：

    “这是 BookAgent Kokoro 测试。”

成功标准：

生成有效 WAV。

---

## F5-TTS

使用：

最小合法 reference。

如果用户尚未提供：

    ref_audio

则返回：

    NOT_CONFIGURED

而不是伪造 reference。

---

## Azure

如果没有配置 Key：

    NOT_CONFIGURED

如果存在合法配置：

生成短 WAV。

---

# 47. Logging

日志级别：

- DEBUG
- INFO
- WARNING
- ERROR

日志内容至少记录：

- 当前阶段；
- 当前 Chapter；
- 当前 Chunk；
- Backend；
- Retry；
- Error Code；
- 总进度。

禁止记录：

- Azure API Key；
- Token；
- 完整敏感环境变量。

---

# 48. 错误体系

建议定义统一错误代码。

输入类：

    INVALID_INPUT
    UNSUPPORTED_FILE
    SCAN_PDF_NOT_SUPPORTED

解析类：

    PARSE_FAILED
    VALIDATION_FAILED

TTS 类：

    TTS_BACKEND_UNAVAILABLE
    TTS_FAILED
    CUDA_OOM
    AZURE_AUTH_FAILED
    AZURE_RATE_LIMITED

音频类：

    FFMPEG_NOT_FOUND
    AUDIO_INVALID

状态类：

    MANIFEST_CORRUPTED

统一错误代码有利于：

- 日志；
- UI；
- Resume；
- 后续自动处理。

---

# 49. Git 与数据管理

不得提交：

    .env
    envs/
    .venv/
    __pycache__/
    *.wav
    *.mp3
    *.m4b
    logs/
    下载的大模型文件

测试 fixture 可例外。

---

# 50. 当前硬件环境

目标开发机器：

- Windows 11
- NVIDIA Quadro T1000
- 4GB VRAM
- 约 40GB RAM

因此：

## Kokoro

要求：

优先保证可以稳定运行。

## F5-TTS

允许：

由于显存不足导致 CUDA 不可用。

但是：

不得因为 F5 失败导致 BookAgent 整体无法使用。

Azure 和 Kokoro 必须仍可正常工作。

---

# 51. 性能原则

BookAgent 第一版优先保证：

- 正确；
- 可恢复；
- 稳定；

其次才是速度。

禁止为了提升速度而牺牲：

- 原文完整性；
- Manifest；
- Cache；
- Resume；
- 音频正确顺序。

---

# 52. 模块依赖原则

依赖方向建议：

    Application
         ↓
    Task Manager
         ↓
    Book Pipeline
         ↓
    Parser / Cleaner / Validator / Chunker
         ↓
    TTS Router
         ↓
    Backend
         ↓
    Worker / Azure API

Audio 模块独立于：

具体 TTS Backend。

Parser 不得依赖：

Audio。

Cleaner 不得依赖：

TTS。

---

# 53. v0.1 禁止过度设计

当前版本不引入：

- 数据库服务器；
- Redis；
- Celery；
- RabbitMQ；
- Docker 集群；
- Kubernetes；
- 微服务架构；
- Web API Gateway；
- 多 Agent 自主协作。

本项目当前是：

**单机 Python 应用**

优先保持简单、可靠、可维护。

---

# 54. 未来 TTS 扩展

未来可能扩展为：

    TTSBackend
    ├── Kokoro
    ├── F5-TTS
    ├── Azure AI Speech
    ├── Edge TTS
    ├── OpenAI TTS
    └── Other

增加新 TTS 时：

只增加：

- Backend；
- 必要 Worker；
- Config。

不得要求修改核心 Book Pipeline。

---

# 55. 未来 Deep Dive 扩展

未来：

    MODE_B_DEEP_DIVE

将复用以下模块：

- Parser；
- Cleaner；
- Book Structure；
- Validator；
- State Manager；
- TTS；
- Audio Pipeline。

新增：

    Knowledge Extraction
            ↓
    Coverage Matrix
            ↓
    Lecture Structure
            ↓
    Lecture Script
            ↓
    Fact / Coverage Audit
            ↓
    Storyboard
            ↓
    Narration
            ↓
    Visual Production

因此 v0.1 的 Parser 和 Book Structure 应避免与“原文朗读”场景过度耦合。

---

# 56. v0.1 架构验收标准

概要架构通过标准：

1. 用户只运行一个 BookAgent；
2. 用户运行时可以选择 Kokoro / F5-TTS / Azure；
3. 用户无需手工切换 Python 环境；
4. Kokoro 和 F5 依赖互相隔离；
5. 三个 TTS 使用统一 Backend 接口；
6. PDF / EPUB Pipeline 与 TTS Backend 解耦；
7. 所有 TTS Chunk 有 Manifest；
8. 已完成 Chunk 可以缓存；
9. 任务支持 Resume；
10. 单个 Chunk 失败不导致整本书从头开始；
11. F5 CUDA 失败不影响 Kokoro 和 Azure；
12. Azure Key 不进入源码或 Git；
13. FFmpeg 统一负责最终音频处理；
14. 可以输出章节 MP3；
15. 可以输出整书 M4B；
16. 第二本书无需修改程序；
17. 后续增加第四个 TTS 不需要重构核心 Pipeline。

---

# 57. 最终架构原则总结

BookAgent v0.1 的核心架构原则为：

**一个应用**

用户只看到一个 BookAgent。

**三个 TTS Backend**

Kokoro、F5-TTS、Azure AI Speech。

**两个隔离本地 Worker**

Kokoro 和 F5-TTS 独立运行环境。

**一个统一 Pipeline**

书籍解析、清洗、验证、Chunk、TTS、音频合并。

**全过程可恢复**

Manifest、Cache、Retry、Resume。

**原文优先**

不使用 LLM 自动改写书籍正文。

**简单优先**

第一版拒绝不必要的微服务、数据库和复杂基础设施。

**扩展优先**

未来新增 TTS 或 Deep Dive 模式时，不推翻现有 BookAgent 核心架构。