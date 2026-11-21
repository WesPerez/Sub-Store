# 分支与发布

上游为 `sub-store-org/Sub-Store`。本 fork 的 `master` 每天只做 fast-forward 同步，出现非上游历史时停止；不会覆盖或强推用户修改。`mine` 是 GitHub 默认分支，也是唯一生产来源。上游升级以冻结的最新上游为基线重建仍有效的职责，不会自动把未审查的上游更新投入生产。

`mine` 的 push 和手动工作流均执行后端测试、锁文件安装、前端类型检查与构建、容器及真实 Nginx/浏览器验收，随后发布 GHCR 镜像和与完整提交绑定的 CI 证据。服务器独立的 `sub-store-auto-update.timer` 大约每两分钟检查成功构建，按证据中的不可变 digest 更新。

后端来自本仓库源码；前端来自 `deploy/frontend.json` 中固定的官方提交，CI 应用有界子路径适配。镜像同时记录前后端版本与来源。不在启动时下载上游 latest，也不在生产服务器安装依赖或构建镜像。

## 职责重建与源码同步

上游升级时先保存旧 `mine` 恢复引用，在隔离工作树以最新 `upstream/master` 为基线，按仍有效的职责重放 CI/部署、网页/分享、维护规则。优先采用上游实现，逐项审查共享文件并保留旧职责到新提交的映射；修复折入所属职责，不按历史提交数量拆分。`master` 必须保持上游净镜像，`mine` 才承载个性化和部署文件。

改动从本地规范源码分支提交并推送；隔离工作树可承担审查和重建，最终必须同步规范目录。上游同步后先更新并推送本地 `master`，再以精确 `--force-with-lease` 推送已审查的最终 `mine`，等待其原始 CI 请求，按现有更新器部署 exact digest。任务结束时确保 `/root/sub-store-repo` 检出干净的 `mine`，其完整 SHA 等于 `origin/mine` 和实时远端；同时单独报告生产镜像 revision/digest。遇到并行写者或未提交内容时先保全和协调，禁止强制重置覆盖。

重建打断旧 `mine` 的祖先关系时，提交 `deploy/upstream-transition.json`，绑定唯一旧完整 revision、旧上游与新上游。更新器只有在仓库/分支一致、旧 revision 精确匹配、旧/新基线同属上游历史且新基线仍可从 `sub-store-org/Sub-Store:master` 到达时才认可这次来源迁移；完整 CI、digest、锁、数据备份和健康验收照常执行。旧安装版不认识迁移合同的首次跳转，先从最终 SHA 的完整 CI 证据运行同版本 `deploy/auto-update/install.py`，只更新 updater；随后正式 timer 执行业务部署。不能伪造 `deployed.json` 或加入只为通过祖先门禁的 merge commit。

本次职责对应旧 `mine` 的最终有效树：CI/部署包含自托管、证据路由、安装证据、runner、轻量健康与真实备份容量；网页/分享包含固定前端、子路径、流量、分享复制及浏览器验收；维护规则包含 fork 指引、分支约定与本次重建来源清单。历史修复折入各自职责，不按历史提交数量拆分。新上游为 `12968bf66e995e9a9cbce40da6202f842abded22`（后端 2.44.25），并包含 Mieru URI 与 QX Hysteria 2 Gecko 支持。

旧提交到新职责的对照：

| 旧提交 | 新职责 |
| --- | --- |
| 原部署、CI、更新器、备份容量与验收提交 | `73a73fc6`：CI/部署职责 |
| 原固定前端、子路径和客户端分享提交 | `4de8c4ce`：前端/分享职责 |
| 原 README、AGENTS 与分支规则提交 | 本提交：维护规则职责，按新上游内容重审 |

README fork 指引、main/star-history 上游仓库限制和忽略规则继续由当前 upstream 实现；旧轻量 `/api/health` 端点与本项目的本地健康探针共同承担部署健康检查职责，不再作为独立定制主题保留。最终职责提交 SHA 在下表维护。

每次交付后的规范源码路径为 `/root/sub-store-repo`。结束前核实其干净检出 `mine`，完整 SHA 与 `origin/mine` 和实时远端头一致；隔离候选或生产镜像状态不能替代源码同步。

部署细节见 [deploy/README.md](deploy/README.md)。
