\# BookAgent v0.1 详细设计文档



\## 1. 文档目的



本文档定义 BookAgent v0.1 的详细实现方案，包括：



\- 程序启动流程；

\- CLI 参数；

\- 书籍解析；

\- 文本清洗；

\- 完整性校验；

\- TTS Chunk；

\- Manifest；

\- 缓存；

\- 断点续跑；

\- Kokoro；

\- F5-TTS；

\- Azure AI Speech；

\- Worker 隔离；

\- 音频 QC；

\- MP3 / M4B 生成；

\- 安装脚本；

\- 测试；

\- 日志；

\- 错误处理。



本版本只实现：



\*\*MODE\_A\_AUDIOBOOK：原文有声书\*\*



不实现：



\- OCR；

\- 深度精讲；

\- Web UI；

\- 视频；

\- 数字人；

\- LLM 总结或改写。



\---



\# 2. 运行环境



目标开发及运行环境：



\- Windows 11

\- Python 3.x 64-bit

\- NVIDIA Quadro T1000

\- 4GB VRAM

\- 约 40GB RAM

\- FFmpeg



本系统必须可以在没有高性能 GPU 的情况下运行主程序。



Kokoro：



\- 优先支持正常本地运行；

\- 支持 CPU；

\- 可选 CUDA。



F5-TTS：



\- 允许受 4GB VRAM 限制；

\- CUDA 不保证成功；

\- 必须能够明确报告 CUDA OOM；

\- 不得因为 F5-TTS 失败导致 BookAgent 主程序失效。



Azure：



\- 云端执行；

\- 不依赖本地 GPU。



\---



\# 3. 启动方式



用户支持两种启动方式。



方式一：



&#x20;   run.bat



方式二：



&#x20;   python app.py



\---



\# 4. 交互式启动流程



用户运行：



&#x20;   run.bat



程序显示：



&#x20;   BookAgent v0.1



&#x20;   请输入书籍路径：

&#x20;   > D:\\Books\\book.pdf



&#x20;   请选择 TTS：



&#x20;   1. Kokoro

&#x20;   2. F5-TTS

&#x20;   3. Azure AI Speech



&#x20;   > 1



&#x20;   请选择声音：

&#x20;   > default



&#x20;   语速：

&#x20;   > 1.0



&#x20;   开始处理...



程序后续自动完成：



&#x20;   INGEST

&#x20;     ↓

&#x20;   PARSE

&#x20;     ↓

&#x20;   CLEAN

&#x20;     ↓

&#x20;   VALIDATE

&#x20;     ↓

&#x20;   CHUNK

&#x20;     ↓

&#x20;   TTS

&#x20;     ↓

&#x20;   AUDIO\_QC

&#x20;     ↓

&#x20;   CHAPTER\_ASSEMBLY

&#x20;     ↓

&#x20;   BOOK\_ASSEMBLY



\---



\# 5. 非交互式启动



必须支持命令行调用。



示例：



&#x20;   python app.py "D:\\Books\\book.pdf" --tts kokoro



&#x20;   python app.py "D:\\Books\\book.epub" --tts azure



&#x20;   python app.py "D:\\Books\\book.pdf" --tts f5 --resume



建议支持参数：



\- book\_file

\- --tts

\- --voice

\- --speed

\- --resume

\- --force

\- --config

\- --device

\- --ref-audio

\- --ref-text



\---



\# 6. CLI 参数定义



\## 6.1 book\_file



必填。



支持：



\- PDF

\- EPUB



示例：



&#x20;   D:\\Books\\book.pdf



\---



\## 6.2 --tts



可选。



值：



&#x20;   kokoro

&#x20;   f5

&#x20;   azure



未提供时：



进入交互选择。



\---



\## 6.3 --voice



指定 TTS voice。



如果不提供：



使用 config.yaml 中 Backend 默认 voice。



\---



\## 6.4 --speed



默认：



&#x20;   1.0



影响语速。



\---



\## 6.5 --resume



继续已经存在的 Book 任务。



不得重新执行已经完成且缓存有效的 Chunk。



\---



\## 6.6 --force



强制重新执行对应生成步骤。



用于：



\- 调试；

\- Voice 修改；

\- 测试缓存。



默认不得开启。



\---



\## 6.7 --device



可选：



&#x20;   auto

&#x20;   cpu

&#x20;   cuda



主要用于：



\- Kokoro

\- F5-TTS



Azure 忽略该参数。



\---



\# 7. 项目目录



建议目录：



&#x20;   BookAgent/

&#x20;   │

&#x20;   ├── app.py

&#x20;   ├── config.yaml

&#x20;   ├── requirements.txt

&#x20;   ├── .env.example

&#x20;   ├── .gitignore

&#x20;   ├── setup.bat

&#x20;   ├── run.bat

&#x20;   │

&#x20;   ├── docs/

&#x20;   │   ├── 01\_PRD.md

&#x20;   │   ├── 02\_ARCHITECTURE.md

&#x20;   │   └── 03\_DETAILED\_DESIGN.md

&#x20;   │

&#x20;   ├── src/

&#x20;   │   ├── app/

&#x20;   │   ├── parser/

&#x20;   │   ├── cleaner/

&#x20;   │   ├── validator/

&#x20;   │   ├── chunker/

&#x20;   │   ├── state/

&#x20;   │   ├── tts/

&#x20;   │   │   ├── base.py

&#x20;   │   │   ├── router.py

&#x20;   │   │   ├── kokoro\_backend.py

&#x20;   │   │   ├── f5\_backend.py

&#x20;   │   │   └── azure\_backend.py

&#x20;   │   │

&#x20;   │   ├── audio/

&#x20;   │   └── utils/

&#x20;   │

&#x20;   ├── workers/

&#x20;   │   ├── kokoro\_worker.py

&#x20;   │   └── f5\_worker.py

&#x20;   │

&#x20;   ├── envs/

&#x20;   │   ├── kokoro/

&#x20;   │   └── f5/

&#x20;   │

&#x20;   ├── books/

&#x20;   ├── output/

&#x20;   ├── tests/

&#x20;   └── logs/



\---



\# 8. Book 工作目录



每本书建立独立工作目录。



结构：



&#x20;   books/

&#x20;   └── <book\_id>/

&#x20;       │

&#x20;       ├── source/

&#x20;       │   └── source.pdf

&#x20;       │

&#x20;       ├── parsed/

&#x20;       │   ├── metadata.json

&#x20;       │   ├── structure.json

&#x20;       │   └── raw\_text.json

&#x20;       │

&#x20;       ├── cleaned/

&#x20;       │   ├── cleaned\_text.json

&#x20;       │   └── cleaning\_report.json

&#x20;       │

&#x20;       ├── manifests/

&#x20;       │   ├── task\_manifest.json

&#x20;       │   ├── tts\_manifest.json

&#x20;       │   └── chapter\_manifest.json

&#x20;       │

&#x20;       ├── audio\_chunks/

&#x20;       │

&#x20;       ├── chapters/

&#x20;       │

&#x20;       ├── output/

&#x20;       │   └── book.m4b

&#x20;       │

&#x20;       └── logs/



\---



\# 9. Book ID 设计



book\_id 不得只使用文件名。



建议组成：



&#x20;   normalized\_title + source\_file\_hash



或：



&#x20;   SHA256(source\_file)



截取适合目录使用的前若干位。



目标：



\- 同名不同书不冲突；

\- 同一本书可以恢复历史任务；

\- 文件内容变化能够识别。



\---



\# 10. 文件输入检测



输入文件进入系统后：



检查：



1\. 是否存在；

2\. 是否可读；

3\. 文件扩展名；

4\. 文件大小；

5\. 是否为支持格式。



错误：



&#x20;   INVALID\_INPUT

&#x20;   UNSUPPORTED\_FILE



支持：



&#x20;   .pdf

&#x20;   .epub



\---



\# 11. PDF Parser



推荐使用：



\*\*PyMuPDF\*\*



模块职责：



1\. 打开 PDF；

2\. 读取 metadata；

3\. 读取页数；

4\. 尝试读取 PDF TOC；

5\. 按页提取文本；

6\. 保留 page\_number；

7\. 分析文本密度；

8\. 判断是否为扫描 PDF；

9\. 保存原始提取结果。



\---



\# 12. PDF 原始数据结构



示例：



&#x20;   {

&#x20;     "pages": \[

&#x20;       {

&#x20;         "page": 1,

&#x20;         "text": "..."

&#x20;       },

&#x20;       {

&#x20;         "page": 2,

&#x20;         "text": "..."

&#x20;       }

&#x20;     ]

&#x20;   }



原始提取文本必须保留。



不得只保留清洗结果。



\---



\# 13. 扫描 PDF 检测



v0.1 不实现 OCR。



如果：



\- 页面数量正常；

\- 但可提取字符极少；

\- 多数页面没有有效文本；



则判定为：



&#x20;   SCAN\_PDF\_NOT\_SUPPORTED



程序必须：



\- 明确提示用户；

\- 停止正文处理。



不得生成大量乱码后继续 TTS。



\---



\# 14. EPUB Parser



EPUB 处理步骤：



1\. 读取 EPUB；

2\. 获取 metadata；

3\. 获取 navigation；

4\. 获取 spine；

5\. 按正确顺序读取 XHTML；

6\. 提取标题；

7\. 提取正文；

8\. 去掉 CSS；

9\. 去掉 JavaScript；

10\. 去掉无意义 HTML 标签。



必须保持：



\- 原始章节顺序；

\- 段落顺序。



\---



\# 15. Metadata 数据模型



BookMetadata：



&#x20;   {

&#x20;     "book\_id": "",

&#x20;     "title": "",

&#x20;     "author": "",

&#x20;     "source\_type": "pdf",

&#x20;     "source\_path": "",

&#x20;     "source\_hash": "",

&#x20;     "total\_pages": 0

&#x20;   }



\---



\# 16. Chapter 数据模型



Chapter：



&#x20;   {

&#x20;     "chapter\_id": "chapter\_001",

&#x20;     "title": "",

&#x20;     "order": 1,

&#x20;     "paragraphs": \[]

&#x20;   }



如果存在 Section：



&#x20;   {

&#x20;     "section\_id": "",

&#x20;     "title": "",

&#x20;     "paragraphs": \[]

&#x20;   }



\---



\# 17. Chapter Detection



章节来源优先级：



1\. EPUB navigation；

2\. PDF TOC；

3\. 明确标题模式；

4\. 文本规则识别。



禁止：



通过 LLM 猜测章节。



常见标题规则可识别：



\- 第一章

\- 第1章

\- Chapter 1

\- CHAPTER ONE

\- 前言

\- 序

\- 后记

\- 附录



\---



\# 18. Text Cleaner



Text Cleaner 只做确定性文本清洗。



允许：



\- 删除重复页眉；

\- 删除重复页脚；

\- 删除页码；

\- 合并不合理换行；

\- 修复 PDF 单词断行；

\- 合并连续空白；

\- 去掉排版噪声。



禁止：



\- 改写；

\- 总结；

\- 润色；

\- 自动补写；

\- 改变作者句意。



\---



\# 19. 页眉页脚识别



建议采用：



跨页重复模式检测。



例如：



同一文本出现在多数页面页首：



&#x20;   Book Title



可能判定为页眉。



同一文本出现在多数页面页尾：



&#x20;   Publisher Name



可能判定为页脚。



删除动作必须记录到：



&#x20;   cleaning\_report.json



\---



\# 20. 页码识别



常见页码：



&#x20;   1

&#x20;   2

&#x20;   3



或：



&#x20;   - 15 -



或：



&#x20;   Page 15



只能删除高度可信的页码模式。



不确定内容应保留。



\---



\# 21. PDF 换行恢复



典型 PDF：



&#x20;   这是一个被 PDF

&#x20;   强制换行的句子。



清洗后：



&#x20;   这是一个被 PDF 强制换行的句子。



但以下情况不得错误合并：



&#x20;   第一段结束。



&#x20;   第二段开始。



因此必须保留段落边界。



\---



\# 22. Cleaning Report



至少记录：



&#x20;   {

&#x20;     "before\_chars": 100000,

&#x20;     "after\_chars": 96500,

&#x20;     "change\_ratio": 0.035,

&#x20;     "removed\_headers": 220,

&#x20;     "removed\_footers": 220,

&#x20;     "removed\_page\_numbers": 310

&#x20;   }



\---



\# 23. Validator



Validator 在开始 TTS 前运行。



检查：



\- 字符数变化；

\- 章节数；

\- 空章节；

\- 超短章节；

\- 大段正文消失；

\- 乱码比例；

\- 异常重复。



正常：



&#x20;   VALIDATED



异常：



&#x20;   NEEDS\_REVIEW



\---



\# 24. Validation 阈值



阈值必须放：



&#x20;   config.yaml



例如：



&#x20;   validation:

&#x20;     max\_text\_loss\_ratio: 0.15

&#x20;     min\_chars: 1000



不得在业务代码里散落硬编码。



\---



\# 25. TTS Chunking 原则



切分顺序：



&#x20;   Chapter

&#x20;     ↓

&#x20;   Section

&#x20;     ↓

&#x20;   Paragraph

&#x20;     ↓

&#x20;   Sentence

&#x20;     ↓

&#x20;   Chunk



优先保证语义完整。



禁止机械：



每 1000 个字符直接切一刀。



\---



\# 26. 中文句子边界



至少识别：



&#x20;   。

&#x20;   ！

&#x20;   ？

&#x20;   ；

&#x20;   ……



必要时结合：



&#x20;   ”

&#x20;   ’

&#x20;   ）



处理句尾标点。



\---



\# 27. 英文句子边界



至少识别：



&#x20;   .

&#x20;   !

&#x20;   ?

&#x20;   ;



避免常见缩写误切：



&#x20;   Mr.

&#x20;   Dr.

&#x20;   etc.



v0.1 不要求复杂 NLP，但必须避免明显错误。



\---



\# 28. Chunk 最大长度



不同 TTS Backend 可能有不同限制。



因此：



Chunker 需要获取：



&#x20;   backend\_max\_chars



或统一使用：



&#x20;   config.yaml



例如：



&#x20;   chunking:

&#x20;     default\_max\_chars: 1000



未来允许 Backend 自己提供建议长度。



\---



\# 29. TTSChunk 数据模型



&#x20;   {

&#x20;     "chunk\_id": "chapter\_001\_chunk\_0001",

&#x20;     "chapter\_id": "chapter\_001",

&#x20;     "order": 1,

&#x20;     "text": "",

&#x20;     "text\_hash": "",

&#x20;     "backend": "kokoro",

&#x20;     "voice": "",

&#x20;     "speed": 1.0,

&#x20;     "fingerprint": "",

&#x20;     "status": "PENDING",

&#x20;     "output\_file": "",

&#x20;     "retry\_count": 0,

&#x20;     "error\_code": null,

&#x20;     "error\_message": null

&#x20;   }



\---



\# 30. Chunk ID



格式建议：



&#x20;   chapter\_001\_chunk\_0001



要求：



\- 稳定；

\- 可排序；

\- 不因程序重启改变。



\---



\# 31. Text Hash



使用：



&#x20;   SHA256(text)



作用：



\- 检测正文是否发生变化；

\- 控制缓存失效。



\---



\# 32. Fingerprint



Fingerprint 必须包含所有影响音频结果的因素。



例如：



&#x20;   SHA256(

&#x20;       text\_hash

&#x20;       + backend

&#x20;       + voice

&#x20;       + speed

&#x20;       + backend\_options

&#x20;   )



F5 还应考虑：



\- ref\_audio hash；

\- ref\_text；

\- device 不一定影响结果，可根据实现决定是否加入。



Azure 应考虑：



\- voice；

\- rate；

\- volume；

\- SSML 参数。



\---



\# 33. Manifest



核心文件：



&#x20;   tts\_manifest.json



Manifest 保存：



\- Chunk 顺序；

\- Chunk 状态；

\- Fingerprint；

\- 输出路径；

\- 错误；

\- Retry。



Manifest 是 Resume 的核心。



\---



\# 34. Manifest 状态



Chunk Status：



&#x20;   PENDING

&#x20;   RUNNING

&#x20;   SUCCESS

&#x20;   FAILED

&#x20;   SKIPPED



启动时如果发现：



&#x20;   RUNNING



但程序上次异常退出，



应重新判断该 Chunk 输出文件是否有效。



无效：



回到：



&#x20;   PENDING



\---



\# 35. Manifest 持久化



每完成一个 Chunk：



立即保存 Manifest。



采用：



&#x20;   write temp file

&#x20;     ↓

&#x20;   flush

&#x20;     ↓

&#x20;   atomic replace



避免：



程序异常退出时 Manifest 半写入损坏。



\---



\# 36. Resume 逻辑



启动：



&#x20;   --resume



程序读取：



&#x20;   tts\_manifest.json



例如：



&#x20;   001 SUCCESS

&#x20;   002 SUCCESS

&#x20;   003 FAILED

&#x20;   004 PENDING



执行：



从 003 开始。



不得重新生成：



001、002。



\---



\# 37. Cache 判断



满足：



1\. status == SUCCESS；

2\. fingerprint 一致；

3\. output\_file 存在；

4\. WAV 可正常读取；



则：



&#x20;   SKIP



\---



\# 38. Force 模式



当：



&#x20;   --force



启用后：



忽略现有 TTS Cache。



重新生成目标 Chunk。



但不得自动删除：



\- 原始文本；

\- Parsed 数据；

\- Cleaned 数据。



\---



\# 39. TTS Backend 抽象



定义抽象基类。



逻辑接口：



&#x20;   class TTSBackend:



&#x20;       def health\_check(self):

&#x20;           pass



&#x20;       def synthesize(

&#x20;           self,

&#x20;           text,

&#x20;           output\_path,

&#x20;           voice=None,

&#x20;           speed=1.0,

&#x20;           options=None

&#x20;       ):

&#x20;           pass



\---



\# 40. TTSResult



统一返回：



&#x20;   {

&#x20;     "success": true,

&#x20;     "output\_path": "",

&#x20;     "duration": 0,

&#x20;     "error\_code": null,

&#x20;     "error\_message": null

&#x20;   }



业务层只处理统一结果。



不得直接依赖具体 Backend 的原始异常格式。



\---



\# 41. TTS Router



统一入口。



逻辑：



&#x20;   get\_backend("kokoro")

&#x20;   get\_backend("f5")

&#x20;   get\_backend("azure")



返回对应 Backend。



Backend 实例创建逻辑集中管理。



\---



\# 42. 禁止业务层散落 Backend 判断



禁止：



&#x20;   if backend == "kokoro":

&#x20;       ...



&#x20;   elif backend == "f5":

&#x20;       ...



在多个业务文件重复出现。



Backend 选择逻辑只能集中在：



\- Router；

\- Factory；

\- Backend 实现。



\---



\# 43. Python 环境设计



内部三个环境：



&#x20;   bookagent-main

&#x20;   bookagent-kokoro

&#x20;   bookagent-f5



\---



\# 44. bookagent-main



负责：



\- app.py；

\- PDF Parser；

\- EPUB Parser；

\- Cleaner；

\- Validator；

\- Chunker；

\- Manifest；

\- State Manager；

\- TTS Router；

\- Azure SDK；

\- FFmpeg；

\- MP3 / M4B；

\- Logging。



\---



\# 45. bookagent-kokoro



只负责：



Kokoro 推理。



运行：



&#x20;   workers/kokoro\_worker.py



主环境通过 subprocess 调用。



\---



\# 46. bookagent-f5



只负责：



F5-TTS 推理。



运行：



&#x20;   workers/f5\_worker.py



主环境通过 subprocess 调用。



\---



\# 47. 用户不得操作环境



禁止要求用户：



&#x20;   activate bookagent-kokoro



或：



&#x20;   activate bookagent-f5



BookAgent 自动调用：



&#x20;   envs\\kokoro\\Scripts\\python.exe



&#x20;   envs\\f5\\Scripts\\python.exe



\---



\# 48. Worker 通信方式



v0.1 使用：



\*\*subprocess + JSON\*\*



不引入：



\- HTTP Service；

\- Redis；

\- RabbitMQ；

\- Docker；

\- Message Queue。



\---



\# 49. Worker 输入



推荐通过：



临时 JSON 文件



或：



stdin JSON



传递。



示例：



&#x20;   {

&#x20;     "text": "这是测试内容。",

&#x20;     "voice": "xxx",

&#x20;     "speed": 1.0,

&#x20;     "output\_path": "D:\\\\...\\\\chunk.wav"

&#x20;   }



\---



\# 50. Worker 输出



stdout 返回 JSON：



&#x20;   {

&#x20;     "success": true,

&#x20;     "output\_path": "D:\\\\...\\\\chunk.wav",

&#x20;     "duration": 4.8,

&#x20;     "error\_code": null,

&#x20;     "error\_message": null

&#x20;   }



stderr：



允许输出调试信息。



主程序需要捕获。



\---



\# 51. Worker Timeout



每个 Worker 调用应允许设置超时。



防止：



模型进程永久卡死。



配置例如：



&#x20;   tts:

&#x20;     worker\_timeout\_seconds: 600



\---



\# 52. KokoroBackend



Kokoro Backend 负责：



\- 组装 Worker 参数；

\- 调用 Kokoro Worker；

\- 验证输出；

\- 将 Worker Error 转换成统一 TTSResult。



\---



\# 53. Kokoro Worker



职责：



1\. 启动 Kokoro；

2\. 加载模型；

3\. 选择 voice；

4\. 设置 speed；

5\. 合成音频；

6\. 输出 WAV。



设备：



&#x20;   auto

&#x20;   cpu

&#x20;   cuda



auto：



优先自动判断。



\---



\# 54. Kokoro 模型加载优化



如果每个 Chunk 都重新启动 Python：



会重复加载模型，效率很差。



因此 v0.1 可接受两种实现：



方案 A：



简单 subprocess，每 Chunk 启动一次。



优点：



简单。



缺点：



慢。



方案 B：



长驻 Worker。



优点：



模型只加载一次。



缺点：



实现稍复杂。



推荐：



如果简单模式模型加载开销明显，



应优先实现：



\*\*单任务期间长驻 Worker\*\*



但不要引入复杂分布式架构。



\---



\# 55. F5Backend



负责：



\- ref\_audio；

\- ref\_text；

\- speed；

\- device；

\- Worker 调用；

\- 错误转换。



\---



\# 56. F5 必需参数



如果当前 F5 模式需要参考音频：



而用户没有设置：



&#x20;   ref\_audio



则返回：



&#x20;   F5\_REFERENCE\_REQUIRED



不得随意伪造参考音频。



\---



\# 57. F5 Reference Audio



配置：



&#x20;   tts:

&#x20;     f5:

&#x20;       ref\_audio: ""

&#x20;       ref\_text: ""



如果 ref\_audio 设置：



必须检查：



\- 文件存在；

\- FFmpeg 可读取；

\- 音频时长有效。



\---



\# 58. F5 GPU 处理



device：



&#x20;   auto

&#x20;   cuda

&#x20;   cpu



当：



&#x20;   cuda



发生显存不足：



返回：



&#x20;   CUDA\_OOM



不得：



无限 Retry。



\---



\# 59. F5 CPU Fallback



配置：



&#x20;   tts:

&#x20;     f5:

&#x20;       cpu\_fallback: true



如果：



CUDA OOM



且：



cpu\_fallback = true



允许：



CUDA → CPU



仅自动回退一次。



必须在日志中记录。



\---



\# 60. AzureBackend



使用：



\*\*Microsoft Azure AI Speech 官方 Python SDK\*\*



运行：



&#x20;   bookagent-main



不使用：



&#x20;   edge-tts



替代 Azure。



\---



\# 61. Azure 凭据



环境变量：



&#x20;   AZURE\_SPEECH\_KEY



&#x20;   AZURE\_SPEECH\_REGION



通过：



&#x20;   .env



加载。



不得：



\- 写入 config.yaml；

\- 写入代码；

\- 打印到日志；

\- 提交 Git。



\---



\# 62. Azure 参数



支持：



\- voice；

\- rate；

\- volume；

\- output\_format；

\- SSML。



示例 config：



&#x20;   tts:

&#x20;     azure:

&#x20;       voice: zh-CN-XiaoxiaoNeural

&#x20;       rate: 1.0

&#x20;       volume: 1.0



\---



\# 63. Azure SSML



Azure Backend 可以内部生成 SSML。



但原始书籍正文不得被修改。



SSML 只允许增加：



\- voice；

\- prosody；

\- break；

\- pronunciation。



不得偷偷重写文本。



\---



\# 64. Azure Retry



以下错误允许 Retry：



\- timeout；

\- 网络异常；

\- 429；

\- 5xx。



采用：



有限次数指数退避。



例如：



&#x20;   1s

&#x20;   2s

&#x20;   4s



最大次数由 config 控制。



\---



\# 65. Azure 不应 Retry 的错误



例如：



&#x20;   AZURE\_AUTH\_FAILED



&#x20;   INVALID\_VOICE



&#x20;   INVALID\_CONFIG



应立即返回失败。



\---



\# 66. Retry 机制



默认：



&#x20;   retry\_count = 3



每次失败：



记录：



\- time；

\- backend；

\- error\_code；

\- message。



超过最大 Retry：



Chunk：



&#x20;   FAILED



\---



\# 67. 单 Chunk 失败原则



一个 Chunk 失败：



不得删除其他成功 Chunk。



整本书可以进入：



&#x20;   PARTIAL\_FAILED



后续 Resume。



\---



\# 68. WAV 统一格式



无论：



\- Kokoro；

\- F5；

\- Azure；



最终进入 Audio Pipeline 前都统一为：



&#x20;   WAV



优点：



\- 简化 QC；

\- 简化拼接；

\- 简化 Backend 差异。



\---



\# 69. WAV Cache



目录：



&#x20;   audio\_chunks/



示例：



&#x20;   chapter\_001\_chunk\_0001.wav



&#x20;   chapter\_001\_chunk\_0002.wav



\---



\# 70. Audio QC



每个 Chunk 完成后检查：



1\. 文件存在；

2\. 文件大小 > 最小阈值；

3\. FFmpeg 可以读取；

4\. Duration > 0；

5\. Sample Rate 有效；

6\. Channel 有效。



失败：



&#x20;   AUDIO\_INVALID



\---



\# 71. 静音检测



v0.1 可以实现简单静音检测。



例如：



如果整个 WAV RMS 极低：



标记异常。



不得把：



正常停顿



误判为失败。



因此仅检测：



“整段几乎无声”。



\---



\# 72. 音频格式标准化



进入章节拼接前：



使用 FFmpeg 统一：



\- sample\_rate；

\- channels；

\- codec。



config：



&#x20;   audio:

&#x20;     sample\_rate: 24000

&#x20;     channels: 1

&#x20;     mp3\_bitrate: 128k



\---



\# 73. Chapter Assembly



一个 Chapter 所有 Chunk：



&#x20;   SUCCESS



之后才允许生成：



&#x20;   chapter\_001.mp3



Chunk 顺序：



严格使用 Manifest order。



不得依赖：



文件系统自然排序。



\---



\# 74. Chunk 间停顿



不同 TTS Chunk 拼接时：



需要合理停顿。



建议：



\- 句间停顿由 TTS 自己处理；

\- Chunk 间仅增加很短停顿；

\- Chapter 间增加更明显停顿。



相关参数放：



&#x20;   config.yaml



\---



\# 75. Chapter Manifest



示例：



&#x20;   {

&#x20;     "chapter\_id": "chapter\_001",

&#x20;     "title": "第一章",

&#x20;     "order": 1,

&#x20;     "audio\_file": "chapter\_001.mp3",

&#x20;     "duration": 1234.5

&#x20;   }



\---



\# 76. Book Assembly



章节完成后：



按 Chapter order 拼接。



最终输出：



&#x20;   book.m4b



可选同时生成：



&#x20;   book.mp3



\---



\# 77. M4B Metadata



必须尽可能写入：



\- title；

\- author；

\- cover；

\- chapter name；

\- chapter start time；

\- chapter end time。



\---



\# 78. Chapter Timestamp



必须根据：



真实 Chapter Audio Duration



累计计算。



禁止：



根据文字数量估算时间。



\---



\# 79. 封面



如果 EPUB / PDF 可提取封面：



保存：



&#x20;   cover.jpg



M4B 写入。



如果没有：



允许无封面。



v0.1 不自动生成 AI 封面。



\---



\# 80. Task Manifest



保存整本书任务状态。



示例：



&#x20;   {

&#x20;     "book\_id": "",

&#x20;     "status": "TTS\_GENERATING",

&#x20;     "source\_hash": "",

&#x20;     "backend": "kokoro",

&#x20;     "created\_at": "",

&#x20;     "updated\_at": ""

&#x20;   }



\---



\# 81. Task 状态机



状态：



&#x20;   INGEST

&#x20;     ↓

&#x20;   PARSED

&#x20;     ↓

&#x20;   CLEANED

&#x20;     ↓

&#x20;   VALIDATED

&#x20;     ↓

&#x20;   TTS\_READY

&#x20;     ↓

&#x20;   TTS\_GENERATING

&#x20;     ↓

&#x20;   AUDIO\_QC

&#x20;     ↓

&#x20;   ASSEMBLED

&#x20;     ↓

&#x20;   COMPLETED



异常：



&#x20;   NEEDS\_REVIEW



&#x20;   FAILED



&#x20;   PARTIAL\_FAILED



\---



\# 82. State 恢复



应用启动时：



如果发现已有 Task Manifest：



询问：



&#x20;   发现未完成任务，是否继续？



CLI 使用：



&#x20;   --resume



可直接继续。



\---



\# 83. config.yaml



建议：



&#x20;   app:

&#x20;     log\_level: INFO



&#x20;   validation:

&#x20;     max\_text\_loss\_ratio: 0.15



&#x20;   chunking:

&#x20;     default\_max\_chars: 1000



&#x20;   tts:

&#x20;     default\_backend: kokoro

&#x20;     retry\_count: 3

&#x20;     worker\_timeout\_seconds: 600



&#x20;     kokoro:

&#x20;       voice: ""

&#x20;       speed: 1.0

&#x20;       device: auto



&#x20;     f5:

&#x20;       speed: 1.0

&#x20;       device: auto

&#x20;       ref\_audio: ""

&#x20;       ref\_text: ""

&#x20;       cpu\_fallback: true



&#x20;     azure:

&#x20;       voice: zh-CN-XiaoxiaoNeural

&#x20;       rate: 1.0

&#x20;       volume: 1.0



&#x20;   audio:

&#x20;     sample\_rate: 24000

&#x20;     channels: 1

&#x20;     mp3\_bitrate: 128k

&#x20;     chunk\_pause\_ms: 150

&#x20;     chapter\_pause\_ms: 1000



\---



\# 84. .env.example



内容：



&#x20;   AZURE\_SPEECH\_KEY=

&#x20;   AZURE\_SPEECH\_REGION=



用户复制为：



&#x20;   .env



再填写。



\---



\# 85. .gitignore



至少：



&#x20;   .env

&#x20;   envs/

&#x20;   .venv/

&#x20;   \_\_pycache\_\_/

&#x20;   \*.pyc

&#x20;   \*.wav

&#x20;   \*.mp3

&#x20;   \*.m4b

&#x20;   logs/

&#x20;   books/\*/audio\_chunks/

&#x20;   books/\*/output/



测试 fixture 例外。



\---



\# 86. Logging



日志级别：



&#x20;   DEBUG

&#x20;   INFO

&#x20;   WARNING

&#x20;   ERROR



日志必须记录：



\- Task；

\- Stage；

\- Chapter；

\- Chunk；

\- Backend；

\- Retry；

\- Duration；

\- Error。



\---



\# 87. 日志禁止内容



不得记录：



\- Azure Key；

\- Token；

\- 完整敏感环境变量。



\---



\# 88. 错误代码



统一错误：



输入：



&#x20;   INVALID\_INPUT

&#x20;   UNSUPPORTED\_FILE

&#x20;   SCAN\_PDF\_NOT\_SUPPORTED



解析：



&#x20;   PARSE\_FAILED

&#x20;   CLEAN\_FAILED

&#x20;   VALIDATION\_FAILED



TTS：



&#x20;   TTS\_BACKEND\_UNAVAILABLE

&#x20;   TTS\_FAILED

&#x20;   TTS\_TIMEOUT

&#x20;   CUDA\_OOM

&#x20;   F5\_REFERENCE\_REQUIRED

&#x20;   AZURE\_AUTH\_FAILED

&#x20;   AZURE\_RATE\_LIMITED



音频：



&#x20;   FFMPEG\_NOT\_FOUND

&#x20;   AUDIO\_INVALID

&#x20;   AUDIO\_ASSEMBLY\_FAILED



状态：



&#x20;   MANIFEST\_CORRUPTED

&#x20;   CACHE\_INVALID



\---



\# 89. 错误对象



建议：



&#x20;   {

&#x20;     "error\_code": "CUDA\_OOM",

&#x20;     "message": "F5-TTS CUDA memory allocation failed.",

&#x20;     "recoverable": true

&#x20;   }



\---



\# 90. FFmpeg 检测



BookAgent 启动时：



检查：



&#x20;   ffmpeg -version



如果不存在：



返回：



&#x20;   FFMPEG\_NOT\_FOUND



并告诉用户安装方法。



不得执行到最后一步才发现没有 FFmpeg。



\---



\# 91. setup.bat



职责：



1\. 检查 Python；

2\. 检查 Python 版本；

3\. 检查 FFmpeg；

4\. 创建主环境；

5\. 安装主依赖；

6\. 创建 Kokoro 环境；

7\. 安装 Kokoro；

8\. 创建 F5 环境；

9\. 安装 F5；

10\. 创建必要目录；

11\. 创建 .env.example；

12\. 执行 Smoke Test。



\---



\# 92. setup.bat 错误处理



如果 Kokoro 安装失败：



输出：



&#x20;   KOKORO\_SETUP\_FAILED



如果 F5 安装失败：



输出：



&#x20;   F5\_SETUP\_FAILED



不能：



明明失败却打印：



&#x20;   Setup completed successfully



这种很有 AI 乐观主义特色的行为禁止出现。



\---



\# 93. run.bat



run.bat 只负责：



1\. 定位主环境；

2\. 启动 app.py。



示意：



&#x20;   envs\\main\\Scripts\\python.exe app.py



用户无需：



activate。



\---



\# 94. Health Check



每个 TTS Backend 提供：



&#x20;   health\_check()



返回：



&#x20;   READY

&#x20;   NOT\_CONFIGURED

&#x20;   UNAVAILABLE



\---



\# 95. Kokoro Smoke Test



文本：



&#x20;   这是 BookAgent Kokoro 测试。



成功标准：



\- 模型正常加载；

\- 生成 WAV；

\- WAV 可播放；

\- Duration > 0。



\---



\# 96. F5 Smoke Test



如果存在合法 ref\_audio：



运行最小推理。



没有：



返回：



&#x20;   NOT\_CONFIGURED



不得随意选择其他参考声音骗过测试。



\---



\# 97. Azure Smoke Test



如果：



Azure Key 未设置：



返回：



&#x20;   NOT\_CONFIGURED



如果配置存在：



合成：



&#x20;   这是 BookAgent Azure Speech 测试。



输出有效 WAV。



\---



\# 98. Unit Test



至少覆盖：



\- PDF parser 基础逻辑；

\- EPUB parser 基础逻辑；

\- cleaner；

\- validator；

\- chunker；

\- fingerprint；

\- manifest；

\- router；

\- config loader。



\---



\# 99. Integration Test



使用小型测试文本。



流程：



&#x20;   Input

&#x20;     ↓

&#x20;   Parser

&#x20;     ↓

&#x20;   Cleaner

&#x20;     ↓

&#x20;   Chunker

&#x20;     ↓

&#x20;   Mock TTS

&#x20;     ↓

&#x20;   WAV

&#x20;     ↓

&#x20;   Chapter MP3



不要让正常测试每次调用 Azure 产生费用。



\---



\# 100. End-to-End Smoke Test



至少使用：



一个短 PDF 或 EPUB。



执行：



&#x20;   PDF

&#x20;     ↓

&#x20;   Parse

&#x20;     ↓

&#x20;   Clean

&#x20;     ↓

&#x20;   Validate

&#x20;     ↓

&#x20;   Chunk

&#x20;     ↓

&#x20;   实际 Backend

&#x20;     ↓

&#x20;   MP3

&#x20;     ↓

&#x20;   M4B



\---



\# 101. Resume Test



测试步骤：



1\. 准备 5 个 Chunk；

2\. 生成前 3 个；

3\. 人为终止程序；

4\. 重新启动；

5\. 使用 --resume。



验收：



只能生成：



Chunk 4、5。



不得重新生成：



1、2、3。



\---



\# 102. Fingerprint Test



第一次：



&#x20;   voice=A



生成。



第二次：



仍然：



&#x20;   voice=A



应：



SKIP。



第三次：



&#x20;   voice=B



应：



重新生成。



\---



\# 103. Retry Test



模拟：



第一次失败；

第二次成功。



最终：



Chunk SUCCESS。



retry\_count：



&#x20;   1



\---



\# 104. F5 OOM Test



如果可以模拟 CUDA OOM：



Backend 应返回：



&#x20;   CUDA\_OOM



如果：



cpu\_fallback = true



则：



自动尝试一次 CPU。



不得无限重试 CUDA。



\---



\# 105. Azure Rate Limit Test



Mock：



&#x20;   429



验证：



\- Retry；

\- Backoff；

\- 最大次数；

\- 最终错误。



\---



\# 106. 文本完整性原则



原文模式中：



BookAgent 不得：



\- 改写；

\- 摘要；

\- 重排段落；

\- 自动删除“看起来没用”的内容。



这是第一优先级。



\---



\# 107. Text Cleaning 审计



所有删除动作必须：



可追踪。



至少报告：



\- 删除类型；

\- 数量；

\- 字符变化比例。



未来可扩展：



差异文件。



\---



\# 108. 性能优化原则



优化优先级：



第一：



正确性。



第二：



Resume / Cache。



第三：



稳定性。



第四：



速度。



不得：



为了快而牺牲文本完整性。



\---



\# 109. 模型常驻优化



如果发现：



Kokoro / F5 每个 Chunk 都重新加载模型耗时严重，



可以增加：



Persistent Worker。



但接口仍保持：



TTSBackend



上层无需变化。



\---



\# 110. 并发策略



v0.1 默认：



串行 TTS。



原因：



\- 4GB VRAM；

\- 降低 OOM；

\- 简化稳定性。



Azure 后续可以增加并发。



但 v0.1 不作为必要功能。



\---



\# 111. 主程序与 Worker 解耦



主程序不得：



import F5 内部模型代码



也不得：



import Kokoro 大量依赖



进入 main 环境。



所有本地模型通过 Worker 隔离。



\---



\# 112. Worker Crash



如果 Worker 进程异常退出：



Backend 捕获：



\- return code；

\- stderr；

\- timeout。



返回统一：



&#x20;   TTS\_FAILED



或：



更明确错误。



主程序不得直接崩溃。



\---



\# 113. 文件路径



所有内部文件路径使用：



pathlib.Path



避免：



Windows 路径拼接问题。



不得硬编码：



&#x20;   C:\\Users\\xxx\\...



\---



\# 114. UTF-8



所有：



\- JSON；

\- YAML；

\- Log；

\- Text；



默认 UTF-8。



确保中文：



不乱码。



\---



\# 115. JSON 输出



JSON 写入：



&#x20;   ensure\_ascii = false



以便调试时直接查看中文。



\---



\# 116. 临时文件



临时文件统一目录：



&#x20;   books/<book\_id>/temp/



任务成功后：



可清理。



不得散落系统目录。



\---



\# 117. 敏感信息



敏感信息只有：



Azure Key 等。



不得存在：



\- config.yaml；

\- README 示例真实 Key；

\- Log；

\- Git。



\---



\# 118. v0.1 不使用数据库



数据状态使用：



\- JSON；

\- 文件目录；

\- Manifest。



当前规模没有必要引入：



\- SQLite；

\- PostgreSQL；

\- Redis。



未来如有必要再迁移。



\---



\# 119. v0.1 不使用 LLM



原文有声书流程：



不需要：



\- GPT；

\- Gemini；

\- Claude；

\- 本地 LLM。



只采用：



确定性解析 + TTS。



\---



\# 120. 发音词典预留



虽然 v0.1 可以简化实现，



但设计上应预留：



&#x20;   pronunciation\_dictionary.json



未来用于：



\- 人名；

\- 地名；

\- 英文缩写；

\- 专业术语；

\- 多音字。



不要让 TTSBackend 接口阻止未来扩展。



\---



\# 121. Future Backend 扩展



未来增加：



&#x20;   EdgeTTSBackend

&#x20;   OpenAITTSBackend

&#x20;   OtherBackend



只增加：



\- Backend；

\- Worker / API；

\- Config。



核心 Pipeline 不变。



\---



\# 122. Deep Dive 模式扩展



未来：



&#x20;   MODE\_B\_DEEP\_DIVE



复用：



\- Parser；

\- Cleaner；

\- Validator；

\- Book Structure；

\- State；

\- TTS；

\- Audio Pipeline。



新增：



&#x20;   Knowledge Extraction

&#x20;       ↓

&#x20;   Coverage Matrix

&#x20;       ↓

&#x20;   Lecture Structure

&#x20;       ↓

&#x20;   Lecture Script

&#x20;       ↓

&#x20;   Coverage Audit

&#x20;       ↓

&#x20;   Storyboard



因此当前设计中：



Parser 和 Cleaner 不得与“直接原文朗读”逻辑过度绑定。



\---



\# 123. 首版开发顺序



推荐按以下顺序开发。



Phase 1：



项目骨架：



\- config；

\- logging；

\- app；

\- state；

\- error。



Phase 2：



书籍：



\- PDF Parser；

\- EPUB Parser；

\- Cleaner；

\- Validator。



Phase 3：



TTS 数据：



\- Chunker；

\- Manifest；

\- Fingerprint；

\- Resume。



Phase 4：



TTS：



\- Base；

\- Router；

\- Kokoro；

\- F5；

\- Azure。



Phase 5：



Audio：



\- WAV QC；

\- Chapter MP3；

\- M4B。



Phase 6：



安装：



\- setup.bat；

\- run.bat。



Phase 7：



测试：



\- Unit；

\- Smoke；

\- Integration；

\- End-to-End。



\---



\# 124. 第一阶段最小可运行目标



最小目标：



&#x20;   一个正常文本 PDF

&#x20;     ↓

&#x20;   解析

&#x20;     ↓

&#x20;   清洗

&#x20;     ↓

&#x20;   Chunk

&#x20;     ↓

&#x20;   Kokoro

&#x20;     ↓

&#x20;   Chapter MP3



先跑通。



随后增加：



&#x20;   F5



再增加：



&#x20;   Azure



最后：



&#x20;   M4B + Resume + 完整测试



\---



\# 125. 开发过程中禁止事项



禁止：



\- 无需求的大规模重构；

\- 自行增加 Web UI；

\- 自行加入数据库；

\- 自行加入 LLM；

\- 自行加入 OCR；

\- 自行加入 Docker；

\- 自行改变目录架构核心原则；

\- 为解决一个问题修改无关模块；

\- 删除已有设计约束而不说明。



\---



\# 126. 实现冲突处理



如果开发过程中发现：



PRD、Architecture、Detailed Design 存在冲突，



必须：



1\. 停止相关实现；

2\. 明确指出冲突；

3\. 给出建议；

4\. 等待确认。



不得：



自行选择一个版本偷偷实施。



\---



\# 127. 完成输出要求



开发完成后，反重力应输出：



1\. 最终目录树；

2\. 新增文件；

3\. 修改文件；

4\. setup.bat 运行结果；

5\. run.bat 运行结果；

6\. Kokoro Smoke Test；

7\. F5 Smoke Test；

8\. Azure Smoke Test；

9\. PDF End-to-End Test；

10\. EPUB End-to-End Test；

11\. Resume Test；

12\. 已知问题；

13\. Git Diff 摘要。



\---



\# 128. v0.1 验收标准



必须满足：



1\. 用户只操作一个 BookAgent；

2\. 支持 PDF；

3\. 支持 EPUB；

4\. 能识别扫描 PDF 并停止；

5\. 能清理常见页眉页脚；

6\. 能保存原始文本；

7\. 能保存清洗文本；

8\. 能执行完整性校验；

9\. 能生成稳定 TTS Chunk；

10\. 每个 Chunk 有 Fingerprint；

11\. 支持 Manifest；

12\. 支持 Resume；

13\. 支持 Cache；

14\. 支持单 Chunk Retry；

15\. 支持 Kokoro；

16\. 支持 F5-TTS；

17\. 支持 Azure AI Speech；

18\. 用户无需切 Python 环境；

19\. Kokoro/F5 环境隔离；

20\. F5 OOM 不影响其他 Backend；

21\. Azure Key 不进入源码；

22\. 能生成 Chunk WAV；

23\. 能生成 Chapter MP3；

24\. 能生成 Book M4B；

25\. M4B 包含章节；

26\. 第二本书无需修改代码；

27\. 中途失败无需整书重跑；

28\. 不使用 LLM 改写原文；

29\. setup.bat 可用于安装；

30\. run.bat 可用于正常运行。



\---



\# 129. 最终技术原则



BookAgent v0.1 必须遵守以下核心原则：



\*\*原文完整性优先\*\*



有声书模式不得由 AI 改写原文。



\*\*一个应用\*\*



用户只看到一个 BookAgent。



\*\*环境隔离\*\*



Kokoro 与 F5 独立环境。



\*\*统一 Backend\*\*



所有 TTS 使用统一接口。



\*\*可恢复\*\*



Manifest + Cache + Resume + Retry。



\*\*可审计\*\*



保留原始文本、中间数据、日志和报告。



\*\*失败隔离\*\*



单个 Chunk 或单个 Backend 失败，不拖垮整本书或整个应用。



\*\*简单优先\*\*



第一版不引入不必要的服务器、数据库、微服务和 Agent 框架。



\*\*扩展优先\*\*



未来增加新 TTS、OCR 或 Deep Dive 模式时，不推翻当前核心架构。

