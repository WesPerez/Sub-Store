# 分支与发布

上游为 `sub-store-org/Sub-Store`。本 fork 的 `master` 每天只做 fast-forward 同步，出现非上游历史时停止；不会覆盖或强推用户修改。`mine` 是 GitHub 默认分支，也是唯一生产来源。上游升级需要把已审查的 `master` 合入 `mine`，不会自动把未审查的上游更新投入生产。

`mine` 的 push 和手动工作流均执行后端测试、锁文件安装、前端类型检查与构建、容器及真实 Nginx/浏览器验收，随后发布 GHCR 镜像和与完整提交绑定的 CI 证据。服务器独立的 `sub-store-auto-update.timer` 大约每两分钟检查成功构建，按证据中的不可变 digest 更新。

后端来自本仓库源码；前端来自 `deploy/frontend.json` 中固定的官方提交，CI 应用有界子路径适配。镜像同时记录前后端版本与来源。不在启动时下载上游 latest，也不在生产服务器安装依赖或构建镜像。

部署细节见 [deploy/README.md](deploy/README.md)。
