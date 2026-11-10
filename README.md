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

使用 `latest` 滚动标签；需要固定版本时可改为 `v0.1.1`。在包含 `config` 目录的位置执行：

```bash
docker run -d --pull always \
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
  ghcr.io/z979054461/immich-smart-albums:latest
```

## Docker Compose

保存为 `compose.yaml`，与 `config` 目录放在一起。与 Docker Run 二选一。

```yaml
services:
  smart-albums:
    image: ghcr.io/z979054461/immich-smart-albums:latest
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
docker compose pull
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
| `max_pages` | `10000` | 单次搜索分页上限，1–100000 |
| `jobs` | 必填 | 任务列表 |
| `jobs[].name` | 必填 | 唯一任务名 |
| `jobs[].album` | 必填 | 已有相册名称或 UUID；重名时使用 UUID |
| `jobs[].enabled` | `true` | 是否启用 |
| `jobs[].dry_run` | 继承全局 | 单任务预览开关 |
| `jobs[].max_matches` | `100000` | 匹配数上限，1–10000000；超出则拒绝写入 |
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

每个 `jobs` 项写入一个已有相册；可配置多个任务，同一相册也可被多个任务追加。`include` 必填，`exclude` 可省略。

| 字段 | 每项怎么写 | 含义 |
| --- | --- | --- |
| `people_any` / `people_all` | `[人物A, 人物B]` | 任一人物 / 所有人同框；也可填人物 UUID |
| `metadata_any` / `metadata_all` | `[{filter: {isFavorite: {eq: true}}}]` | 任一 / 所有元数据查询 |
| `smart_any` / `smart_all` | `[{query: "a dog", limit: 200}]` | 任一 / 所有语义搜索结果集 |
| `local_any` / `local_all` | `[{path: "$.exifInfo.make", regex: "Canon"}]` | 任一 / 所有本地元数据条件 |
| `all_assets` | `true` | 仅用于 `include`，显式以 API 可见资产为候选集 |

### 组合方式

- 同一字段内：`any` 是“或”，`all` 是“且”。不同 `include` 字段之间也是“且”。例如人物A或B + 收藏 = “出现A或B，并且已收藏”。
- 不同 `exclude` 字段之间是“或”，合并后从候选结果中扣除；每个字段内部仍按 `any` / `all` 计算。
- 只做本地筛选或排除时，写 `include: {all_assets: true}`。与其他包含条件一起使用时，它不会取消那些条件。
- `people_*` / `album` 支持名称或 UUID，名称必须唯一且精确匹配；元数据里的 `personIds`、`albumIds`、`tagIds` 只能用 UUID。
- `smart_*` 只接受 `query` 和可选 `limit`（默认 200，范围 1–1000）。取前 N 个相似结果后再与其他条件组合，不保证找全，也不会筛掉后继续补足 N 个。
- `local_*` 只检查 API 返回的元数据，不读取原图。`path` 为 JSONPath，`regex` 是忽略大小写的 Python 正则，按搜索匹配；省略正则时仅检查路径存在。缺少路径不匹配。
- 未使用的字段直接省略；规则列表不能是空列表，`include` 不能是空对象。未知字段、人物/相册不存在或重名、查询失败或分页不完整时，该任务不写入。
- 规则变化不会移除相册里已有内容。`jobs[].dry_run` 可覆盖全局值；首次试运行用 `once --dry-run` 强制所有任务只读。

<details>
<summary>完整元数据字段与操作符</summary>

推荐写法：`metadata_any` / `metadata_all` 下每一项使用 `filter`。同一 `filter` 内多个字段、同一字段内多个操作符均为“且”。

| 字段 | 可用操作符 | 值 |
| --- | --- | --- |
| `type` | `eq`、`ne`、`in`、`notIn` | `IMAGE`、`VIDEO`、`AUDIO`、`OTHER` |
| `visibility` | `eq`、`ne`、`in`、`notIn` | `timeline`、`hidden`、`archive`、`locked` |
| `city`、`state`、`country`、`make`、`model`、`lensModel` | `eq`、`ne`、`in`、`notIn` | 字符串 |
| `checksum`、`encodedVideoPath` | `eq`、`ne`、`in`、`notIn` | 字符串 |
| `description`、`originalFileName`、`originalPath` | `eq`、`ne`、`in`、`notIn`、`like`、`notLike`、`startsWith`、`endsWith` | 字符串 |
| `ocr` | `matches` | OCR 查询文本，不是正则 |
| `isFavorite`、`isMotion`、`isOffline`、`isEncoded`、`hasAlbums`、`hasPeople`、`hasTags` | `eq` | `true` / `false` |
| `id`、`libraryId` | `eq`、`ne` | UUID |
| `personIds`、`albumIds`、`tagIds` | `any`、`all`、`none` | 非空 UUID 列表 |
| `takenAt`、`createdAt`、`updatedAt`、`trashedAt` | `eq`、`ne`、`gt`、`gte`、`lt`、`lte` | 带时区的日期时间字符串，用引号包裹 |
| `rating`、`fileSizeInBytes` | `eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`in`、`notIn` | 数值；评分通常 1–5，文件大小单位为字节 |

- `eq` / `ne`：等于 / 不等于；`gt` / `gte` / `lt` / `lte`：大于 / 大于等于 / 小于 / 小于等于。
- `in` / `notIn`：属于 / 不属于非空列表；`any` / `all` / `none`：关联任一 / 全部 / 不关联这些 ID。
- `like` / `notLike`：包含 / 不包含文本；`startsWith` / `endsWith`：以文本开头 / 结尾，均由 Immich 处理，不是 Python 正则。
- `eq: null` / `ne: null` 可用于可空字段：城市/地区/国家、相机品牌/型号/镜头、描述/文件名/路径、`libraryId`、`rating`、`trashedAt`；不要把 `null` 用在范围比较或列表中。
- `filter.or` 是非空分支列表，分支之间为“或”，分支内部为“且”，再与同级其他字段取“且”；不支持嵌套 `or`，`filter` 和各分支均不能为空。
- 每个查询还可在 `filter` 同级设置 `withPeople`、`withStacked`（布尔值）和 `orderBy: {field: fileCreatedAt, direction: desc}`（`direction` 可为 `asc` / `desc`）。当前服务与 Immich 3.3.5 共同支持的排序字段为 `fileCreatedAt`，通常无需设置排序。
- 不要手写 `page`、`cursor`、`size`、`withExif`、`resultLimit` 或元数据查询的 `limit`，服务负责分页。

旧式扁平查询仍兼容，例如 `metadata_any: [{type: IMAGE, isFavorite: true}]`，但不要与 `filter` / `orderBy` 混写。其支持：

- 字符串：`city`、`state`、`country`、`make`、`model`、`lensModel`、`description`、`originalFileName`、`originalPath`、`checksum`、`encodedVideoPath`、`ocr`、`id`、`libraryId`。
- 布尔值：`isFavorite`、`isMotion`、`isOffline`、`isEncoded`、`isNotInAlbum`。
- UUID 列表：`personIds`、`albumIds`、`tagIds`；枚举：`type`、`visibility`；评分：`rating`（1–5 或 `null`）。
- 日期字符串：`takenBefore`、`takenAfter`、`createdBefore`、`createdAfter`、`updatedBefore`、`updatedAfter`、`trashedBefore`、`trashedAfter`。

新配置优先使用结构化 `filter`。字段以本服务的 [配置校验](smart_albums/config.py) 为准，不等于支持 Immich 的所有搜索参数。

</details>

<details>
<summary>四个可直接修改的任务示例</summary>

用下面内容替换配置中的 `jobs`，按需保留任务。人物、城市、相机和相册均为示例占位，替换为自己的值；全局继续保留 `dry_run: true`。

```yaml
jobs:
  # 人物A与B同框，拍摄于 2025 年，排除归档照片
  - name: together
    album: 示例合照
    include:
      people_all: [人物A, 人物B]
      metadata_all:
        - filter:
            type: {eq: IMAGE}
            takenAt:
              gte: "2025-01-01T00:00:00+08:00"
              lt: "2026-01-01T00:00:00+08:00"
    exclude:
      metadata_any:
        - filter:
            visibility: {eq: archive}

  # 照片，并且满足“城市A且收藏”或“城市B且评分至少4”
  - name: selected_photos
    album: 示例精选
    include:
      metadata_any:
        - filter:
            type: {eq: IMAGE}
            or:
              - city: {eq: 城市A}
                isFavorite: {eq: true}
              - city: {eq: 城市B}
                rating: {gte: 4}

  # 两组语义搜索结果取并集，再限定为照片，排除人物C
  - name: animals
    album: 示例动物
    include:
      smart_any:
        - {query: "a dog", limit: 200}
        - {query: "a cat", limit: 200}
      metadata_all:
        - filter:
            type: {eq: IMAGE}
    exclude:
      people_any: [人物C]

  # 仅使用本地条件：相机型号以示例文本开头
  - name: camera
    album: 示例相机
    include:
      all_assets: true
      local_all:
        - path: $.exifInfo.model
          regex: '^Example Camera'
```

修改后先校验，再强制预览单个任务：

```bash
docker compose run --rm smart-albums check
docker compose run --rm smart-albums once --job together --dry-run
```

`check` 只校验格式和字段，不验证服务器连接、人物/相册是否存在；`once --dry-run` 才会实际查询且不写入。

</details>

## 用大模型生成配置

复制下面提示词，填写末尾需求即可。不提供真实 API key、服务器地址、人物姓名或照片；生成后在本地替换占位符。

<details>
<summary>展开并复制提示词</summary>

```text
请为 Immich Smart Albums 生成 config.yaml，把我的自然语言需求转换为有效规则。
项目：https://github.com/z979054461/immich-smart-albums

只使用以下配置规范，不要套用其他 Immich 插件的字段：
1. 根字段仅有 server_url、api_key_file、timezone、schedule、run_on_startup、dry_run、request_timeout、max_pages、jobs。
   默认使用 https://immich.example.com、/run/secrets/immich_api_key、Asia/Shanghai、"0 */6 * * *"、run_on_startup: true、dry_run: true。
   request_timeout 默认60（1–300），max_pages 默认10000（1–100000），不需要时省略。
2. jobs 必须是非空列表；任务仅允许 name、album、enabled、dry_run、max_matches、include、exclude。
   name 必须唯一，album 是已有相册名称或 UUID；enabled 默认true，max_matches 默认100000（1–10000000）。不要生成 dry_run: false。
3. include/exclude 仅有 people_any/people_all、metadata_any/metadata_all、smart_any/smart_all、local_any/local_all，值均为非空列表。
   people 的每项是人物名称或 UUID；smart 的每项只有 query 和可选 limit（默认200，1–1000）；local 的每项只有 JSONPath path 和可选 Python regex。
   仅 include 还允许 all_assets: true；没有 people/metadata/smart 包含条件而只用本地筛选或排除时必须设置它。
4. 同一字段的 any 取并集、all 取交集；不同 include 字段取交集；不同 exclude 字段取并集后从包含结果扣除。
   任意跨类型的“或”不能写成两个 include 字段；可拆成写入同一相册的多个任务。all_assets 不会取消其他包含条件。
5. metadata 每项写为 {filter: {...}}，同一 filter 的字段及操作符取交集。允许一层 or 分支，分支取并集后与同级字段取交集，不允许嵌套 or；filter 与各分支不能是空对象。
   可用字段与操作符：
   - type、visibility、city、state、country、make、model、lensModel、checksum、encodedVideoPath：eq/ne/in/notIn。
   - description、originalFileName、originalPath：eq/ne/in/notIn/like/notLike/startsWith/endsWith。
   - ocr：matches，值为查询文本，不是正则。
   - isFavorite、isMotion、isOffline、isEncoded、hasAlbums、hasPeople、hasTags：eq，值为布尔值。
   - id、libraryId：eq/ne，值为 UUID。
   - personIds、albumIds、tagIds：any/all/none，值为非空 UUID 列表，不能填名称。
   - takenAt、createdAt、updatedAt、trashedAt：eq/ne/gt/gte/lt/lte，值为带时区且加引号的 ISO 日期时间。
   - rating、fileSizeInBytes：eq/ne/gt/gte/lt/lte/in/notIn，值为数值，大小单位为字节。
   in/notIn 的值为非空列表，其余按字段类型填写。type 仅为 IMAGE/VIDEO/AUDIO/OTHER；visibility 仅为 timeline/hidden/archive/locked。
   可空字段的 eq/ne 可用 null：city/state/country/make/model/lensModel/description/originalFileName/originalPath/libraryId/rating/trashedAt。
   查询还可有 withPeople、withStacked（布尔值）及 orderBy，但优先省略；如需排序只用 field: fileCreatedAt 和 direction: asc/desc。
   禁止 page/cursor/size/withExif/resultLimit；limit 只能用于 smart 条目。不要生成旧式扁平查询、and/not、tags、people 或自造字段。
6. 语义搜索只是前 N 个相似结果，不是穷尽识别；再与其他条件组合后不会补足 N 个。本地规则只检查 API 返回的元数据，regex 忽略大小写，省略 regex 仅检查路径存在。
7. 不索要或输出真实 API key，不要求上传照片或私有元数据。只用示例域名、人物A/人物B、示例相册、城市A等占位符；需要 UUID 时明确标记待本地替换，不编造真实 ID。
8. 如需求有歧义，先问最少的必要问题；无法表达的规则明确说明。先给完整 YAML，再用几句话解释集合逻辑、需替换的占位符和限制，最后给出：
   docker compose run --rm smart-albums check
   docker compose run --rm smart-albums once --dry-run
   不声称已经连接服务器、验证人物相册或执行通过。服务只追加，不自动创建相册，也不移出已有照片。

我的需求（使用占位符描述）：
- 目标相册：
- 要包含的人物/类型/日期/地点/收藏/评分/语义条件：
- 要排除的条件：
- 条件之间是“且”还是“或”：
- 执行频率与时区（不填则用默认值）：
```

生成结果仍需执行 `check` 和强制预览，确认后再自行开启写入。

</details>

## 许可

基于 [tahnau/immich-smart-albums](https://github.com/tahnau/immich-smart-albums)，保留 [上游说明](README.upstream.md)，遵循 [GPL-3.0](LICENSE)。分发修改版镜像时需提供相应源码及许可。
