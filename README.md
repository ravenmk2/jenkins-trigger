# jenkins-trigger

Jenkins Job 自动化执行工具: 支持 cron 定时与 GitLab Push Webhook 两种触发模式, 按 stage 分层编排触发 Jenkins Job, 构建完成后钉钉汇总通知。

## 快速开始

```bash
uv sync
cp -r example config  # 示例配置复制为运行时配置, 按需修改
uv run python -m jenkins_trigger
```

`config/` 是默认配置目录(已被 gitignore, 不会误提交密钥), 示例配置在 `example/`。
运行数据(分支 commit 记录)默认写入 `data/`(同样已 gitignore, 可用 `JENKINS_TRIGGER_DATA` 改路径)。

## Docker

```bash
docker build -t jenkins-trigger .
docker run -d \
  -p 8080:8080 -p 8081:8081 \
  -v /path/to/config:/app/config:ro \
  -v /path/to/data:/app/data \
  ghcr.io/ravenmk2/jenkins-trigger:dev
```

配置(含密钥)不打入镜像, 运行时只读挂载到 `/app/config`。
`/app/data` 为镜像声明的 VOLUME, 存放运行数据(commit 记录), 建议显式挂载; 未挂载时由 docker 自动创建匿名卷。

> **注意**: 容器以非 root 用户运行 (uid/gid `10001`)。宿主机上的配置文件
> 需要对该用户可读——默认 umask (`022`) 创建的文件即可; 若权限收紧过
> (如 `600`), 执行 `chmod -R a+rX /path/to/config` 或
> `chown -R 10001:10001 /path/to/config`。

## 触发模式

- **cron 定时**: 任务配置 `crons = ["0 8 * * *", ...]`(服务器本地时区), 到点立即执行, 可配多个。
- **GitLab Push**: 匹配仓库 + 分支后延迟 `delay` 秒执行, 期间再次 Push 刷新计时(去抖)。

两种触发都走统一流程: 生成执行计划 → 钉钉"开始"通知 → 按计划调用 Jenkins → 钉钉"结束"通知。
计划内容按三步确定: Jenkins 上次构建失败(FAILURE/ABORTED)重跑(Retry) →
GitLab 分支 commit 与记录不一致(Changed, 需实例配置 `api_token`) →
Push 事件标记的仓库(Pushed); 同一执行项只保留最高优先级原因。

## GitLab 侧配置

在仓库或 Group 的 Webhook 设置中:

- URL: `http://<host>:8081/webhook/gitlab/<instance-id>` (对应 `config/gitlab.toml` 中实例的 `id`)
- Secret Token: 与该实例的 `webhook_token` 一致
- 触发器: 勾选 **Push events**

也兼容管理后台的 **System Hook**——按 payload 的 `object_kind` 判断事件类型(所有事件都带此字段), MR 合并产生的推送同样生效。

Changed 检测需要实例配置 `api_token`(GitLab Personal Access Token, 需 `read_api` 权限);
未配置则跳过 Changed 检测与启动基线补齐, Push 与 Retry 不受影响。

## 任务配置

每个任务一个文件 `config/jobs/<job-id>.toml`, 示例见 `example/jobs/example.toml`。
每个任务包含多个执行项 `[[items]]`(`repo` 仓库 + `job` Jenkins Job 的绑定):

- `name` — 执行项显示名(必填), 钉钉通知使用
- `crons` — cron 触发表达式列表, 到点立即执行
- `delay` — 收到 Push 后延迟多少秒执行, 期间再次 Push 会刷新计时(去抖)
- `stage` — 执行分层(轻量 DAG), 值越小越早执行, 同 stage 并行, 默认 100
- `parameterized` — `true` 时走 `buildWithParameters` 传参, `false`(默认) 纯触发 `/build`
- `build_params` — 构建参数字典(仅 `parameterized = true` 可用); 分支需要时显式写, 如 `BRANCH = "master"`

启动时会为所有任务的所有仓库补齐 commit 基线记录(已有记录保留);
构建正常结束后回写记录, 构建异常则下轮计划以 Changed 自动重试。
同一仓库可绑定多个 Jenkins Job(写多个 `[[items]]`), 都会执行。

## API

| 端点 | 说明 |
| --- | --- |
| `POST :8081/webhook/gitlab/{instance-id}` | GitLab Webhook 入口 |
| `GET :8080/api/health` | 健康检查 |
| `GET :8080/api/status` | 任务状态与最近执行结果 |

## 开发

```bash
uv sync          # 安装依赖(含 dev)
uv run pytest    # 运行测试
```

更多设计细节见 [docs/architecture.md](docs/architecture.md)。

## CI

push 到 `master`/`develop` → 测试并推送 `dev` 镜像到 GitHub Packages;
push `v*` tag → 推送对应版本镜像。
