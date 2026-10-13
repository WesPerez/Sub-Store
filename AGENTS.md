# Sub-Store 自托管约定

- `master` 是 `sub-store-org/Sub-Store:master` 的净镜像；`mine` 是默认分支和生产发布来源。个性化部署文件只进入 `mine`。
- 本生产服务器只编辑、审查、推送源码和运行已验证的产物。依赖安装、构建、编译型测试、浏览器依赖安装与镜像构建全部在 GitHub-hosted Actions 完成；本机禁止 npm/pnpm install、docker build 或等价操作。
- 推送 `mine` → 完整 CI 成功 → 核对完整 Git SHA、构建编号和镜像 digest → 项目独立 timer 部署 → 健康验收，失败回滚代码；不倒写业务数据。
- 前端固定在 `deploy/frontend.json` 的上游提交，子路径适配集中在 `scripts/frontend.py`。升级前端必须同时通过页面、逐源流量、分享链接和 PWA 范围验收。
- 生产只监听 `127.0.0.1:13001`，入口 `/substore/`。管理 API 与网页使用 Nginx 认证，只有原生签名 `/share/` 下载入口免网页登录。
- `/etc/sub-store/`、`/var/lib/sub-store/`、运行配置、订阅、口令和恢复副本禁止进入 Git 或 CI。测试仅使用合成节点和独立临时数据。
- Docker 日志使用原生有界轮转；专用 Nginx 日志保留 3 天并限制大小。不得删除活动日志、订阅数据或唯一恢复副本。
- 修改自动更新器时保留精确 CI 校验、禁止降级、部署互斥和回滚测试。它的公共基线来自 Watchdeck；不得修改其他项目的更新器或定时器。
