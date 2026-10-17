# 书声待处理问题与功能清单

## GOOGLE-PUBLIC-001：Google Drive 面向公众的统一授权

- 登记日期：2026-10-17
- 状态：待开发、待 Google 数据访问审核；个人连接测试独立推进。
- 现状：品牌验证已通过，项目处于外部正式版；客户端申请 `drive.readonly`，授权页仍提示应用未验证。品牌通过不等于受限权限通过。
- 目标：普通用户安装书声后点击连接即可授权自己的 Google 账号，无需自行注册 Google Cloud 项目或手工导入客户端 JSON。
- 实施事项：
  1. 审查现有目录选择、当日新增文件发现与下载需求的最小权限；评估 `drive.file` 是否覆盖其他程序写入的日期目录/媒体，不能直接替换常量后认定兼容。
  2. 使用统一的正式桌面 OAuth 客户端配置，保留系统浏览器、PKCE、state、本机回环和每用户 DPAPI 令牌存储；不得将用户令牌或浏览器会话放入发行包、Git 或官网。
  3. 在 Google Auth Platform 的数据访问与验证中心申报实际权限、必要性，提交真实授权和目录轮询、下载、发布的演示视频。
  4. 如实说明 Drive 文件下载后向用户选择的发布平台上传的数据流；不能因桌面端运行就自行宣称免于安全评估。依据 Google 审核要求确定受限权限审核、第三方安全评估及后续复审责任。
  5. 检查多用户授权隔离、撤销、重连、失效处理、使用额度和发布前的错误提示，保持官网隐私政策与实现一致。
- 验收：新的普通用户无需导入 JSON 即可授权；正式请求的权限已完成适用审核，不再展示当前未验证警告；原有当日轮询、账号隔离、版本跳过、下载校验、内容去重和本地归档回归通过。
- 边界：继续使用现有独立 Google 项目；不退回测试版；官网为 Vercel 静态项目，不引入生产服务器依赖。个人测试成功不代表公开服务验收通过。
- 相关说明：[云盘接入与发布](GOOGLE_DRIVE_PUBLISHING.md)。
- 官方依据：[受限权限审核](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)、[Drive 权限](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)。
