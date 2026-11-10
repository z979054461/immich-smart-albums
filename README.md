# Immich Smart Albums

按人脸、元数据或 AI 语义搜索规则，定时将照片和视频加入已有 Immich 相册。单容器运行，无需开放端口。

**默认预览，只追加，不删除照片或移出相册。** 基于 Immich 3.3.5 API，支持 `amd64` / `arm64`。

## 配置

创建 `config/config.yaml`，替换示例地址、相册和人物：

```yaml
server_url: https://immich.example.com
api_key_file: /run/secrets/immich_api_key
timezone: Asia/Shanghai
schedule: "0 */6 * * *"
run_on_startup: true
dry_run: true
jobs:
  - name: example
    album: 示例相册
    include:
      people_any: [人物A, 人物B]
      metadata_all:
        - filter:
            type: {eq: IMAGE}
    exclude:
      metadata_any:
        - filter:
            visibility: {eq: archive}
```

在 Immich 中先创建目标相册；`config/api-key.txt` 只填写 API key。文件需允许容器用户 `1000:1000` 读取，勿公开或提交到 Git。API key 权限：`album.read`、`person.read`、`asset.read`；写入时增加 `albumAsset.create`。优先使用 HTTPS，HTTP 仅用于可信网络。

## Docker Run

在包含 `config` 目录的位置执行：

```bash
docker run -d \
  --name immich-smart-albums \
  --restart unless-stopped \
  --init \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount type=bind,src="$PWD/config/config.yaml",dst=/config/config.yaml,readonly \
  --mount type=bind,src="$PWD/config/api-key.txt",dst=/run/secrets/immich_api_key,readonly \
  --mount type=volume,src=smart-albums-data,dst=/data \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m,mode=1777 \
  ghcr.io/z979054461/immich-smart-albums:v0.1.1
```

## Docker Compose

保存为 `compose.yaml`，与 `config` 目录放在一起。与 Docker Run 二选一。

```yaml
services:
  smart-albums:
    image: ghcr.io/z979054461/immich-smart-albums:v0.1.1
    restart: unless-stopped
    init: true
    user: "1000:1000"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    volumes:
      - type: bind
        source: ./config/config.yaml
        target: /config/config.yaml
        read_only: true
        bind:
          create_host_path: false
      - smart-albums-data:/data
    secrets:
      - immich_api_key
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=16m,mode=1777

secrets:
  immich_api_key:
    file: ./config/api-key.txt

volumes:
  smart-albums-data:
```

```bash
docker compose up -d
```

首次保持 `dry_run: true` 检查匹配结果；确认后改为 `false` 并重启容器。后续修改配置也需重启。

## 参数

`config.yaml`：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `server_url` | 必填 | 容器可访问的 Immich 地址 |
| `api_key_file` | `/run/secrets/immich_api_key` | API key 文件 |
| `timezone` | `Asia/Shanghai` | 调度与日志时区；不受 `TZ` 覆盖 |
| `schedule` | `0 */6 * * *` | 五段 cron，默认每 6 小时 |
| `run_on_startup` | `true` | 启动时执行一次 |
| `dry_run` | `true` | 仅预览，不写入相册 |
| `request_timeout` | `60` | 读取超时秒数，1–300 |
| `max_pages` | `10000` | 单次搜索分页上限 |
| `jobs` | 必填 | 任务列表 |
| `jobs[].name` | 必填 | 唯一任务名 |
| `jobs[].album` | 必填 | 已有相册名称或 UUID；重名时使用 UUID |
| `jobs[].enabled` | `true` | 是否启用 |
| `jobs[].dry_run` | 继承全局 | 单任务预览开关 |
| `jobs[].max_matches` | `100000` | 匹配数上限，超出则拒绝写入 |
| `jobs[].include` / `exclude` | 必填 / 可选 | 包含 / 排除规则，见下表 |

可选环境变量：

| 变量 | 作用 |
| --- | --- |
| `IMMICH_SERVER_URL` | 覆盖 `server_url` |
| `IMMICH_API_KEY_FILE` | 覆盖 `api_key_file` |
| `IMMICH_API_KEY` | 直接提供 key，优先于密钥文件 |
| `SMART_ALBUMS_CONFIG` | 配置路径，默认 `/config/config.yaml` |
| `SMART_ALBUMS_STATE_DIR` | 状态目录，默认 `/data` |

## 规则

`include` / `exclude` 均支持：

| 字段 | 作用 | 示例值 |
| --- | --- | --- |
| `people_any` / `people_all` | 任一人物 / 所有人同框；姓名或 UUID | `[人物A, 人物B]` |
| `metadata_any` / `metadata_all` | 任一 / 所有元数据查询 | `[{filter: {isFavorite: {eq: true}}}]` |
| `smart_any` / `smart_all` | 任一 / 所有语义搜索结果集 | `[{query: "a dog", limit: 200}]` |
| `local_any` / `local_all` | 任一 / 所有 JSONPath 条件；正则可选 | `[{path: "$.exifInfo.make", regex: "Canon|Nikon"}]` |

同一字段内 `any` 取并集、`all` 取交集；不同 `include` 字段取交集，所有 `exclude` 匹配结果合并后排除。

- 只用本地筛选或排除条件时，需在 `include` 中设置 `all_assets: true`。
- 元数据支持类型、日期、城市、相机、收藏等条件，完整字段见 [配置校验](smart_albums/config.py)。更多示例见 [config.example.yaml](config/config.example.yaml)。
- 语义搜索 `limit` 为 1–1000，表示前 N 个相似结果，不保证穷尽匹配照片。
- 人物或相册不存在、重名、未知配置字段或查询失败时，不执行该任务的写入。

## 许可

基于 [tahnau/immich-smart-albums](https://github.com/tahnau/immich-smart-albums)，保留 [上游说明](README.upstream.md)，遵循 [GPL-3.0](LICENSE)。分发修改版镜像时需提供相应源码及许可。
