# YouTube 登录差异与共用处理：2026.10.18.8

## 已核实的差异

2026-10-18本机只读检查：完整版启动脚本使用envs/main，Python3.12.11、Playwright1.63.0、Chromium153.0.8010.12。YouTube账号配置显示AUTHORIZED，上次身份确认时间2026-10-11 20:12:43，账号专用profile存在，Last Version为153.0.8010.12；没有读取Cookie内容。用户确认Win11沿用之前登录，并非本次重新输入账号完成登录。

部署包使用Python3.12.11、Playwright1.56.0、Chromium141.0.7390.37。用户提供的Win10截图为accounts.google.com/v3/signin/rejected，且浏览器显示自动化控制提示。两版原来的YouTube登录代码都是ChannelProbeWorker调用Playwright持久化浏览器；部署版没有另外实现一套Google认证协议。

因此已确认的是运行环境和会话历史不同。不能把Win11复用旧登录视为“Win11全新登录一定可行”的对照，也不能只凭版本差异认定浏览器版本是唯一原因。新设备、网络和Google账号风控是否参与本次拒绝，尚无进一步证据。

Google官方[使用支持的浏览器登录](https://support.google.com/accounts/answer/7675428?hl=zh-Hans&co=GENIE.Platform%3DDesktop)说明可能阻止受软件自动控制的浏览器；Chrome和Edge均列在支持的浏览器中。Google Drive桌面OAuth和YouTube Studio网页会话独立，云盘连接成功不会自动提供YouTube网页登录。

## 两版共用方案

- 保留原有有效登录，不删除旧账号目录，不强制Win11账号重新输入密码。
- YouTube登录/诊断窗口增加显式“手工登录 Google”按钮。先安全关闭本账号的Playwright会话，再通过已安装Chrome或Edge的正常进程打开专用目录，由本人登录。该阶段不连接调试端口、不控制页面、不代填账密，也不改写自动化标识、指纹或安全设置。
- 手工窗口关闭后，才恢复Playwright并读取当前实际频道。窗口关闭或浏览器启动均不标记AUTHORIZED；Google拒绝会显示明确原因和手工登录操作说明，部署前检查停止，不进入上传。
- 浏览器选择保存在原账号目录下youtube-browser.json，仅保存schema和channel。专用浏览器数据位于该目录下youtube-system-browser/chrome或msedge；不触碰用户日常浏览器的默认目录。原有内置浏览器目录保持原值。
- 登录、部署前检查、实际上传和中断结果核验统一调用youtube_browser.py；所选浏览器不可用时停止并提示恢复安装，不回退其他浏览器或其他账号目录。日志记录实际浏览器及Playwright版本，方便对比两端。
- 手工窗口开启时继续占用该账号登录会话；请求退出时等待用户关闭其窗口，不强杀浏览器。若程序异常退出而手工浏览器仍开着，先关闭该专用窗口再重新启动书声，避免profile占用。

不直接把完整版降级为Chromium141，也不在未经Win10验证时把部署包升级为开发机的Chromium153；当前Playwright官方[系统要求](https://playwright.dev/python/docs/intro#system-requirements)为Windows11及以上。两版采用相同业务处理和同一浏览器选择规则，原有平台运行依赖暂保持。使用已安装浏览器的能力见[Playwright官方说明](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge)。[Microsoft说明](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-supported-operating-systems)确认Win10 22H2的Edge继续获得更新；这不代表已完成X230上的Playwright组合验证。

## 用户操作与验证边界

退出旧书声，将整个新部署包解压到新目录并启动。账号及历史保留在LOCALAPPDATA/ShuShengPublisher，无需清理或重建抖音、快手账号。YouTube进入账号登录窗口，点击手工登录按钮，在专用正常浏览器完成登录和频道选择后关闭该浏览器。书声应读取正确频道，再保存配置和启动部署。无需重新采集所有平台DOM。

本轮使用临时profile、模拟进程及全部拦截的.invalid本地测试页验证，不触碰真实Google登录或投稿。Google最终是否允许该账号在X230登录，需要目标机现场确认；若正常浏览器仍拒绝，保留提示并按Google账号流程处理，不能声称本地测试已消除此类限制。
