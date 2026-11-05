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

部署入口不设固定剩余磁盘空间门槛。应用产物先下载并校验，应用产物准备失败不会进入服务切换；既有备份、回滚、归档大小限制和完整性校验继续生效。CI 产物中转的全局 1 GiB 分配门槛属于 CI 控制器，不是本项目的部署条件。

升级前停止 Sub-Store 并复制普通目录数据，拒绝符号链接和特殊文件；备份位置按本次实际复制需求检查容量，恢复副本保留逐文件校验和。验收通过后替代上一组由本更新器创建的恢复点，不清理手工备份、活动数据或其他项目状态。涉及不可逆格式迁移时，用 `deploy/auto-deploy.json` 的 `{"version":1,"automatic":false}` 暂停该提交的自动部署，由运维安排迁移。

公共更新器和 CI 证据基线来自 Watchdeck；来源及本项目适配记录在仓库根的 `ci-baseline.json`。主分支选择、镜像证据、目录恢复和身份配置是本项目适配，其余交付协议保留基线行为。


### 备份容量与独立部署

备份容量统一采用随发布包携带的 `backup_capacity.py`（`actual-backup-v1`），不依赖本机 CI 控制器或中央配置。更新器在替换已有版本前制作恢复副本；停服/切换前预检，锁内实际复制前再次检查。无旧数据的首次安装不需要升级前恢复副本。

检查按本次复制对象的真实大小计算：普通文件按目标文件系统分配单元向上取整；SQLite 使用包含已提交 WAL 页的逻辑快照大小；目录、必要清单及 inode 按对象数量计算。不额外加固定 GiB、不乘任意倍数，也不把正在保留的旧备份算作将来可用的空间。备份失败不切换到新版本，原有事务恢复继续处理复制中磁盘耗尽等错误；容量预检是估算，不是空间预留。

成功备份作为最新完整恢复点保留；仅在替代关系和完整性确认后自动退役本更新器的旧恢复点。未完成、仍有引用、手工创建或归属不明的恢复对象不参与自动删除。新环境使用相同计算规则，备份路径仍由项目已有配置决定。CI 中转的 1 GiB 是另一个组件的暂存规则，不要求各应用也空出 1 GiB。
