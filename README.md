# 联网搜索

这是 YueLiBot 的独立外部工具插件。她可以在对方要求查资料，或回答依赖近期、尚不确定的公开事实时调用 `web_search`，取得网页标题、来源、日期和摘要。闲聊、群友私事与已知常识不需要搜索。结果只作为外部观察交给决策与回复生成，不直接发送消息，也不读取结果里的链接。

插件不预设搜索服务，支持部署者配置的 GET 或 POST HTTP JSON 接口，也可经宿主调用已列入配置的只读 MCP 工具。查询词会发给你配置的搜索服务，请勿把私人信息写入查询词。

## 安装与更新

在主程序根目录执行 `git clone <仓库地址> plugins/web-search`，然后重启主程序。仓库根目录就是插件目录。更新时进入插件目录执行 `git pull`，再重启生效。

本版插件要求主程序 0.2.0 及以上，清单声明支持区间为 0.2.0～0.2.5；此后的 0.2.x 补丁由宿主按兼容模式加载。旧版主程序应继续使用旧版插件。

## 配置

第一次启动后，宿主会在插件目录生成 `config.toml`，只有 `[plugin]` 一段。`transport` 默认为 `http`，沿用原有 HTTP 行为，此时接口地址留空不启用搜索。以下示例的用法是**整段替换生成文件中的同名行**，保留 `[plugin]`、`enabled` 和 `daily_limit`。内联表必须写在一行里，不能拆成多行。密钥字段在宿主 WebUI 中隐藏，留空保存会保留原值。

密钥只填写在 `api_key`，其他位置用 `{api_key}` 引用；这样响应或错误回显密钥时插件才能将它抹除。不要把密钥写死在请求头或请求体模板里。

| 占位符 | 含义 |
|---|---|
| `{query}` | 她提供的公开话题查询词，去首尾空白后 1～60 字 |
| `{count}` | 固定结果条数 5；值恰好为该占位符时成为整数，嵌入字符串时成为文本 |
| `{api_key}` | `api_key` 的值 |

占位符递归替换请求头、查询参数和 JSON 体中的字符串值，键不替换；查询词里的引号、反斜杠和花括号会原样编码。

`results_path` 是结果数组在响应中的点号路径，例如 `data.webPages.value`，只能逐段读取字典键，不支持数组下标。`fields` 的值可以是一条点号路径，也可以是路径数组，按顺序取第一个非空值。

| fields 键 | 含义 | 要求 |
|---|---|---|
| `title` | 标题 | 与 `snippet` 至少映射一项；截 80 字 |
| `snippet` | 摘要或正文片段 | 与 `title` 至少映射一项；有效结果按条数均分 1600 字预算 |
| `url` | 网页链接 | 可省略；仅展示不超过 200 字符的 http(s) 链接 |
| `site` | 站点名 | 可省略；未提供时使用链接主机名，两种情况均截 30 字 |
| `date` | 发布日期 | 可省略；ISO 日期取前 10 字，10/13 位 Unix 时间换算为服务器本地日期，其他不超过 30 字的文本原样展示 |

最多取前 5 条标题或摘要非空的结果，清除 HTML 标签、还原实体并折叠空白。摘要预算固定按实际条数均分，长链接不会挤占摘要。结果内容属于外部资料，其中的要求不是用户指示。

## 常见服务填写示例

示例以服务商当前文档为准。价格以服务商官网为准。

### 智谱

去智谱开放平台的 API 密钥管理页面创建密钥。价格以服务商官网为准。

```toml
url = "https://open.bigmodel.cn/api/paas/v4/web_search"
method = "POST"
api_key = "填你的密钥"
headers = { Authorization = "Bearer {api_key}" }
params = {}
# search_engine 可选 search_std / search_pro / search_pro_sogou / search_pro_quark，价格不同
body = { search_query = "{query}", search_engine = "search_pro", search_intent = false, count = "{count}", content_size = "medium" }
results_path = "search_result"
fields = { title = "title", snippet = "content", url = "link", site = "media", date = "publish_date" }
```

### 博查

去博查开放平台的密钥管理页面创建密钥。价格以服务商官网为准。

```toml
url = "https://api.bochaai.com/v1/web-search"
method = "POST"
api_key = "填你的密钥"
headers = { Authorization = "Bearer {api_key}" }
params = {}
body = { query = "{query}", summary = true, count = "{count}" }
results_path = "data.webPages.value"
fields = { title = "name", snippet = ["summary", "snippet"], url = "url", site = "siteName", date = "datePublished" }
```

### Tavily

去 Tavily 账户控制台的 API Keys 页面创建密钥。价格以服务商官网为准。

```toml
url = "https://api.tavily.com/search"
method = "POST"
api_key = "填你的密钥"
headers = { Authorization = "Bearer {api_key}" }
params = {}
body = { query = "{query}", max_results = "{count}", include_published_date = true }
results_path = "results"
fields = { title = "title", snippet = "content", url = "url", date = "published_date" }
```

### Brave

去 Brave Search API 控制台创建订阅并取得 API 密钥。价格以服务商官网为准。

```toml
url = "https://api.search.brave.com/res/v1/web/search"
method = "GET"
api_key = "填你的密钥"
headers = { "X-Subscription-Token" = "{api_key}", Accept = "application/json" }
params = { q = "{query}", count = "{count}" }
body = {}
results_path = "web.results"
fields = { title = "title", snippet = "description", url = "url" }
```

### 自建 SearXNG

不需要密钥；实例 settings.yml 的 search.formats 必须包含 json，否则可能返回 403。若使用托管服务，价格以服务商官网为准。

```toml
url = "http://127.0.0.1:8080/search"
method = "GET"
api_key = ""
headers = {}
params = { q = "{query}", format = "json" }
body = {}
results_path = "results"
fields = { title = "title", snippet = "content", url = "url", date = "publishedDate" }
```

## 使用其他服务

查看接口文档，依次找到请求地址与方法、鉴权放在请求头还是查询参数、请求中的查询词位置、响应结果数组的位置及每条结果的字段。然后填写 `url`、`method`、`api_key`、`headers`、`params`、`body`、`results_path` 和 `fields`。GET 的 `body` 必须为空；使用 `{api_key}` 时密钥不能为空。

HTTP 方式不支持需要计算请求签名的接口，也不支持 HTML、XML、纯文本响应。不自动跟随重定向；接口发生跳转时应修改配置地址，避免鉴权头被带往其他主机。

## 经 MCP 接入（以百炼联网搜索增强版为例）

先在宿主 `config/mcp.toml` 中登记服务与原始工具。厂商名 `dashscope` 应与 `providers.toml` 的实际名称一致，宿主负责鉴权、连接、只读校验和调用次数管理。

```toml
[mcp]
enabled = true

[[mcp.servers]]
name = "bailian"
enabled = true
transport = "http"
url = "https://dashscope.aliyuncs.com/api/v1/mcps/EnhancedSearch/mcp"
api_key_provider = "dashscope"
headers = { Authorization = "Bearer {api_key}" }

[mcp.servers.tools.search_pro]
expose = false
readonly = true
daily_limit = 3
```

`search_pro` 没有只读标注，需要由部署者确认 `readonly = true`。`expose = false` 让它仅供插件调用，避免与整理后的 `web_search` 同时出现在她的工具列表中。然后替换插件配置中的同名行：

```toml
transport = "mcp"
mcp_server = "bailian"
mcp_tool = "search_pro"
arguments = { query = "{query}" }
results_path = "pages"
fields = { title = "title", snippet = "snippet", url = "url", site = "hostname" }
daily_limit = 3
```

MCP 方式不检查或使用 `url`、`method`、`headers`、`params`、`body` 等 HTTP 字段。`arguments` 的占位符展开规则与 `body` 相同。结果有结构化内容时优先使用，否则将文本结果解析为 JSON，再按相同的 `results_path` 与 `fields` 整理，观察格式与 HTTP 方式一致。

服务未连上、工具未列入配置、未确认只读或达到宿主日上限时，返回「联网搜索失败：<中文原因>」；工具报错保留文本前 200 字，非 JSON 返回响应开头，不把错误当成空搜索结果。插件自己的上限仍生效，两道上限取更紧的一道。修改宿主和插件配置后均需重启主程序。

## 日上限与缓存

`daily_limit` 默认 50，可填 1～1000。按服务器本地日期统计每次发出的搜索请求，失败也计数；达到上限后工具当天不再出现。计数仅保存在进程内，跨日清零，重启也清零。

同一个查询词在 10 分钟内复用成功结果，包括空结果，不发请求、不计数。查询词比较会忽略大小写、首尾空白与连续空白差异；缓存最多 64 条，最旧记录先淘汰，失败不缓存。HTTP 总时限为 8 秒。

## 确认生效与排错

启动时，配置完整会记录「联网搜索已启用」，带接口主机名与日上限；未配置会记录「联网搜索未配置，不会出现在可选工具里」。配置不完整时 warning 写明缺少哪个字段或哪个字段冲突。修改后需重启主程序。

请求失败时工具返回「联网搜索失败：…」。接口状态、超时及响应映射等已知错误只展示诊断内容，例如「联网搜索失败：HTTP 401；密钥无效、过期或余额不足；响应开头：…」；未预料的异常保留类名，便于排错，例如「联网搜索失败：ConnectError：…」。HTTP 错误含状态码与响应开头；找不到结果数组时还会包含 `results_path`，便于检查映射。401/403 提示密钥无效、过期或余额不足，429 提示请求太频繁，重定向会给出目标地址。配置错误会明确呈现，不按「没有搜到」处理。

接口明确返回空结果数组才是成功的「没有搜到相关结果」。日志不记录鉴权头或带查询参数的完整接口地址；对外错误、观察和元数据中的已配置密钥均替换为 `***`。

## 离线测试与许可证

设置 `YUELI_ROOT` 为主程序源码根目录，使用宿主虚拟环境的 Python 运行 `python -m pytest tests`。运行测试时请明确设置该环境变量。HTTP 测试使用 `httpx.MockTransport`，MCP 测试使用 `ctx.mcp` 桩件，不调用真实接口或模型；插件不另行声明运行依赖。旧宿主缺少 `ctx.mcp` 时仅跳过 MCP 用例，HTTP 用例照常运行。

许可证为 AGPL-3.0，见 LICENSE。
