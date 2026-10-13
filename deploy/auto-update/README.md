# Sub-Store 自动部署

本目录只负责 `WesPerez/Sub-Store:mine`。GitHub-hosted CI 测试并发布镜像及更新器，宿主 `sub-store-auto-update.timer` 约每两分钟检查一次，独立于其他项目运行。

`project.json` 固定仓库、工作流及必需 job；`config.example.json` 是宿主配置模板。完整 CI 必须成功，镜像 digest 必须来自同一提交、同一构建的 `image-metadata` 证据。只有已验证分支上向前推进的提交才可自动部署。失败回滚镜像与 `.env`，不倒写订阅、分享或其他业务数据。

宿主只需现有 Docker Compose、Python 3.11+、git、curl、systemd 和已登录的 gh，不安装项目依赖、不编译或构建。

首次启动 CI 验证的镜像后运行：

```sh
python3 -B deploy/auto-update/install.py --sha FULL_SHA --run-id RUN_ID --config /etc/sub-store/auto-update.json --enable
sub-store-auto-update check
sub-store-auto-update poll
sub-store-auto-update status
```

安装位置为 `/usr/local/lib/sub-store-auto-update/`，入口为 `/usr/local/sbin/sub-store-auto-update`；宿主配置为 `/etc/sub-store/auto-update.json`，状态与最近一组恢复点保存在 `/var/lib/sub-store-auto-update/sub-store/`。宿主配置、恢复状态和数据只允许 root 管理。

升级前停止 Sub-Store 并复制普通目录数据，拒绝符号链接和特殊文件；恢复副本保留逐文件校验和。验收通过后替代上一组由本更新器创建的恢复点，不清理手工备份、活动数据或其他项目状态。涉及不可逆格式迁移时，用 `deploy/auto-deploy.json` 的 `{"version":1,"automatic":false}` 暂停该提交的自动部署，由运维安排迁移。

公共更新器和 CI 证据基线来自 Watchdeck；来源及本项目适配记录在仓库根的 `ci-baseline.json`。主分支选择、镜像证据、目录恢复和身份配置是本项目适配，其余交付协议保留基线行为。
