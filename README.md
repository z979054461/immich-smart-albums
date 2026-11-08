# Immich Smart Albums · 自用 Docker 版

基于 [tahnau/immich-smart-albums](https://github.com/tahnau/immich-smart-albums)，新增单容器规则服务与内置定时任务。保留上游 GPL-3.0 许可、原 CLI 和 [上游说明](README.upstream.md)。

目标：配置 **Immich URL + API key + 相册规则** 后，定期把符合条件的已入库照片加入相册。无需再在 NAS 上维护 Python、pip 或外部计划任务。

- 人脸：任一人 / 所有人同框；按姓名或 UUID 选择，重名时拒绝猜测。
- 多任务、多目标相册，支持元数据、AI 语义搜索、JSONPath/正则以及排除规则。
- 内置 cron、北京时间、启动即跑、手动运行、防重入锁、JSON 日志和运行状态。
- **默认预览，永远只追加。** 不删除照片，不移出相册，不修改人脸，不下载原图，不创建或分享相册。
- API key 从只读密钥文件或环境变量读取；不写入镜像，不写入日志。
- 无网页界面，无需开放端口。第一版通过 YAML 配置；修改后重启服务。

## 当前验证范围

面向 **Immich 3.3.5** 的 DTO/路由源码实现并做离线模拟测试。没有连接真实 NAS 或用户图库，因此不宣称已完成 3.3.5 实机集成测试。新服务使用结构化人脸过滤/游标分页，也接受明确列出的旧式元数据查询。

容器定义支持 `linux/amd64`（华芸 AS6704T 等 x86 NAS）和 `linux/arm64`。GitHub Actions 可测试并构建双架构镜像。实际构建/发布是否成功，以本仓库 Actions 和 Packages 的结果为准；仓库内的 Dockerfile 并不等于已经发布镜像。

## 1. 准备配置与密钥

下载/克隆本仓库后：

```bash
mkdir -p config
cp config/config.example.yaml config/config.yaml
```

编辑 `config/config.yaml`：

```yaml
server_url: http://你的NAS地址:2283
api_key_file: /run/secrets/immich_api_key
timezone: Asia/Shanghai
schedule: "0 */6 * * *"
run_on_startup: true
dry_run: true
jobs:
  - name: family
    album: 家人
    include:
      people_any: [爸爸, 妈妈]
```

在 Immich 中先创建目标相册，完成人脸识别并给人脸命名。在 `config/api-key.txt` 中只填写自己的 API key，单行保存，不加引号。**不要把 key 发给别人、提交到 Git，或贴进命令参数/日志。**

最小 API key 权限：`album.read`、`person.read`、`asset.read`，正式写入再加 `albumAsset.create`。无需管理员权限，也不要启用删除/分享权限。具体权限名称以当前 Immich 页面为准。

Compose 的密钥文件由宿主机提供，需能被容器 UID/GID `1000:1000` 读取。例如在 Linux NAS 上将其归属设置为 `1000:1000`、权限 `600`，或设置等效 ACL。不要为方便把 key 文件设为所有人可读。配置目录也要允许 UID 1000 遍历。

URL 是容器可访问的地址，不要用 `localhost` 指向 NAS。服务默认不开代理、不读取宿主 `.netrc`；HTTPS 必须使用有效证书。HTTP 会明文传输 API key，仅适合你信任的局域网。反向代理跳转不会自动跟随，请填写最终地址。

## 2. 构建、检查与预览

```bash
docker compose build
docker compose run --rm smart-albums check
docker compose run --rm smart-albums list-people
docker compose run --rm smart-albums list-albums
docker compose run --rm smart-albums once --dry-run
```

`check` 只校验配置，不联网、不要求 key 文件有效；其余命令需要 URL/key 可用。预览只报告匹配数，不写相册，也不会把照片路径/EXIF 打印进日志。首次建议使用一个测试相册或少量照片进行验证。

如匹配数正确，把配置中的 `dry_run` 改为 `false` 后：

```bash
docker compose run --rm smart-albums once
docker compose up -d
docker compose logs -f --tail=100 smart-albums
```

不需要 NAS 额外计划任务。启动会立即运行一次，之后按 cron 执行。`0 */6 * * *` 表示北京时间每天 00:00、06:00、12:00、18:00。每天凌晨两点用 `0 2 * * *`，每小时用 `0 * * * *`。时区由配置 `timezone` 决定，环境变量 `TZ` 不会覆盖它。

修改配置之后：

```bash
docker compose restart smart-albums
```

只手动执行一条规则：

```bash
docker compose exec smart-albums python -m smart_albums once --job family --dry-run
```

`docker compose exec` 不使用镜像 ENTRYPOINT，需写出完整 Python 入口。

## 3. 规则语义

每个任务配置一个已有相册。`album` 可以用完整 UUID，避免重名歧义。默认仅追加；规则变化后不再符合的旧照片仍留在相册中，需自行处理。

`include` 和 `exclude` 可用字段：

| 字段 | 含义 |
| --- | --- |
| `people_any` / `people_all` | 姓名或 UUID 列表；任一人 / 所有人同框 |
| `metadata_any` / `metadata_all` | 元数据查询列表；任一查询 / 所有查询 |
| `smart_any` / `smart_all` | `{query: 文本, limit: 200}` 列表；任一 / 所有结果集合 |
| `local_any` / `local_all` | `{path: JSONPath, regex: 正则}` 列表；任一 / 所有本地条件 |

同一字段内 `any` 做并集、`all` 做交集；不同 `include` 字段之间做交集。不同 `exclude` 字段匹配的集合合并后从最终结果排除。

- 只用本地筛选或排除条件时，必须显式写 `include: {all_assets: true}`，防止拼写错误意外选择整个图库。
- 未找到人物/相册、重名、未知配置字段、搜索失败/分页不完整：该任务不写入，不把失败当零结果。
- `max_matches` 默认 100000。超过会整条规则拒绝写入，不随机截断。
- `smart_*` 是 Immich 语义搜索的前 N 个相似结果，支持 1–1000；不是“精确识别所有狗狗”。
- 人脸以 Immich 当前识别结果为准，新照片需等人脸识别完成。定时重查可补上历史照片。
- 相册去重由 Immich API 判断，日志区分 `added` / `duplicate` / `failed`；HTTP 200 不代表每张都成功。

结构化元数据示例（推荐 3.3.5）：

```yaml
include:
  people_all: [爸爸, 妈妈]
  metadata_all:
    - filter:
        type: {eq: IMAGE}
        takenAt: {gte: "2026-01-01T00:00:00+08:00"}
exclude:
  metadata_any:
    - filter:
        visibility: {eq: archive}
```

支持常用类型、日期、相机、城市、人物/相册/标签 ID 等字段。具体受支持字段在 `smart_albums/config.py` 中明确列出，未知字段拒绝执行。旧式 `{type: IMAGE, city: Shanghai}` 查询仍可用，但 Immich 已标为 deprecated，优先使用 `filter`。不支持把任意 CLI 参数或 shell 命令放进 YAML。

原项目 `.sh`/JSON 规则不会自动迁移。原 CLI 文件保留供参考，但 Docker 镜像仅包含新服务，不包含旧入口；旧 CLI 的 Python 安装仍需单独使用 `requirements.txt`。

## 4. 日志与故障处理

服务输出每个任务的开始/结束、匹配数、追加数、重复数、失败数和下次时间。持久卷 `/data` 保存：

- `last-run.json`：最近一次运行摘要。
- `heartbeat.json`：调度器心跳。
- `run.lock`：同一数据卷内的运行锁，避免计划与手动执行重叠。

只运行一个服务副本，所有手动运行共用同一数据卷。没有排队补跑风暴：任务执行期间错过的时刻合并，重启默认只立即跑一次。修改配置/升级前先确认没有正在进行的写入。

健康检查要求最近 90 秒有心跳且最近一次运行未失败。失败时 Docker 可显示 `unhealthy`，**Docker Compose 不会仅因 unhealthy 自动重启容器**，请检查日志。远程 API 读请求最多三次、单次连接超时 10 秒、读取默认 60 秒；写请求不自动重试。网络在写入途中中断时，结果可能不确定，应先检查相册；再次运行是追加并由服务端去重，不会删除已存在照片。

运行中途失败可能已有前面的任务/批次成功，不保证多任务事务回滚。容器停止也可能中断正在进行的请求。

## 5. 发布预构建镜像 / Portainer

工作流 `.github/workflows/container.yml` 在 push/PR 时运行离线测试，并构建 amd64/arm64；不自动发布。手动运行 **Test and build container**，设置 `publish=true`、版本如 `v0.1.0`，通过后发布：

```text
ghcr.io/<你的GitHub账号>/<仓库名>:v0.1.0
```

同时打 `latest` 与完整 commit SHA 标签。请优先部署版本/SHA 标签，不把 `latest` 误认为固定版本。工作流使用短期 `GITHUB_TOKEN`，不用创建 PAT。GHCR 包可能默认私有；拉取需相应登录权限，或由你决定是否公开包。不要将真实 URL/key/家庭人名提交到仓库，公开源码只保留样例。

Portainer 使用预构建镜像后可移除 `build` 字段，把 `image` 换为已验证存在的版本标签。必须把配置和 key 文件放在 **Docker 主机**，并将 Compose 中相对路径替换成 NAS 上真实绝对路径。Portainer 界面的目录不是自动等于你的 shell 当前目录。保持 state 命名卷、非 root 用户、只读配置与 key、无需端口映射。

## 开发与测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 -m smart_albums --help
```

测试不读取真实 NAS 或照片，使用模拟 API/临时状态目录。旧 `test.sh` 是上游依赖真实 Immich 数据的测试脚本，请不要误当新服务的离线测试执行。

## 许可与来源

GPL-3.0，完整许可证见 [LICENSE](LICENSE)。上游 README 末尾写 MIT，但其实际 LICENSE 文件是 GPL-3.0；本分支保守遵循该 LICENSE，不按 README 的 MIT 文字放宽许可。上游提交基线 `3b7d3dcb82dbc04160751e53c280e6d600d69e61`。2026-11-08 新增容器调度服务、严格配置验证、API 错误处理和离线测试。分发修改后的镜像时应同时提供相应源码及许可。
