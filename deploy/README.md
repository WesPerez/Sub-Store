# 自托管

公开入口是 `https://weesai.com/substore/`。网页及管理 API 使用 Nginx Basic Auth，凭据仅保存在宿主 `/etc/sub-store/`。客户端使用网页「分享」生成的 `/substore/share/...?...` 链接，分享 token 可单独撤销，不需要网页密码。

使用步骤：在「订阅」添加原订阅，建立「组合订阅」并选择这些来源；需要 Clash/Mihomo 完整配置时在「文件」选择组合订阅和模板；最后为组合订阅或文件生成分享链接。流量和到期信息按每条原订阅查看，数据取决于上游提供的信息。共享订阅不转发日常代理流量。

## 运行布局

- `/root/sub-store-repo`：源码，`mine` 分支。
- `/opt/sub-store/docker-compose.yml` 和 `.env`：宿主部署配置；`.env` 只存当前 CI 验证的镜像 digest。
- `/var/lib/sub-store/data`：应用数据，容器中为 `/opt/app/data`；禁止加入 Git、CI 或普通缓存清理。
- `/etc/sub-store/htpasswd`：网页登录口令散列；`admin-credentials.json` 为首次交付凭据，root 专用。
- `/etc/sub-store/auto-update.json`：只含本机更新器配置，0600。
- `/var/lib/sub-store-auto-update`：更新证据、事务和最近一组恢复点；敏感状态按 root 专用权限存储。
- `/etc/nginx/snippets/substore.conf`：入口配置，容器仅映射回环地址 `127.0.0.1:13001`。

首次部署先通过本仓库 GitHub-hosted CI，验证成功的完整 SHA、运行编号及 `image-metadata` 产物，按其中 digest 拉取并首启 Compose。首次创建 `/var/lib/sub-store/data` 时须设为 UID/GID `1000:1000`，供镜像内的 `node` 用户写入。启用 Nginx 前创建 `/var/log/sub-store` 和 `/etc/sub-store/htpasswd`，使 Nginx worker 能读取认证文件。口令文件不进入仓库。

再安装同一 CI 的自动更新器：

```sh
python3 -B deploy/auto-update/install.py --sha FULL_SHA --run-id RUN_ID --config /etc/sub-store/auto-update.json --enable
sub-store-auto-update check
sub-store-auto-update poll
```

后续推送 `mine` 会触发 CI；timer 只接受完整通过的构建、核验发布的镜像 digest，检查健康状态和原挂载/端口。升级前停服生成一致的目录数据恢复副本，健康失败只回滚镜像及部署配置，保留最新业务数据。任意数据格式迁移无法通过自动镜像回滚逆转，需要协调时提交 `deploy/auto-deploy.json`，内容 `{"version":1,"automatic":false}`。

## 前端与验证

前端来自固定的官方提交，并由 `scripts/frontend.py` 适配 `/substore/`、默认 API、流量加载和 PWA 范围。所有安装、类型检查、构建、镜像打包以及 Chromium 验收只在 GitHub-hosted Actions 运行。

CI 使用独立合成订阅验证逐源流量、组合下载、完整 Mihomo 文件、签名分享与错误 token，使用真实 Nginx 验证管理认证、子路径页面和静态资源。不会读取生产订阅或凭据。

GitHub 发布证据使用与 Watchdeck 相同的 run-bound 草稿 Release ZIP，避免占用有限的 Actions artifact 存储。CI 公共辅助文件来源记录在 `ci-baseline.json`。后台只拉取已验证镜像，绝不在宿主机安装依赖或构建。
