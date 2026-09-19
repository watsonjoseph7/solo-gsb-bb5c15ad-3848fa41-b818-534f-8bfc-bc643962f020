# HTTP 语言协商解释 API

供国际化网关调用的语言协商微服务：接收客户端 `Accept-Language` 头、服务端支持的
BCP 47 语言标签列表与默认语言，依据 RFC 9110 权重规则与 RFC 4647 lookup 逐级截断
匹配，选出最终语言，并返回**每一个候选标签的接受 / 淘汰轨迹**，方便网关侧排障与
可观测。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · Uvicorn，依赖在容器构建时安装。

## 单条命令启动

```bash
docker compose up -d --build
```

服务随后在 **http://localhost:8000** 提供访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查（`GET`，返回 `{"status":"ok"}`） |
| http://localhost:8000/api/v1/negotiate | 语言协商接口（`POST`） |
| http://localhost:8000/docs | Swagger UI 交互式文档 |
| http://localhost:8000/openapi.json | OpenAPI 3 描述 |

停止：`docker compose down`

## 调用示例

```bash
curl -X POST http://localhost:8000/api/v1/negotiate \
  -H 'Content-Type: application/json' \
  -d '{
    "accept_language": "zh-TW;q=0.95,zh;q=0.9,en;q=0,*;q=0.5",
    "supported_languages": ["en-US", "zh-CN", "zh-TW", "ja-JP"],
    "default_language": "en-US"
  }'
```

响应（节选）：

```json
{
  "selected_language": "zh-TW",
  "selected_supported_index": 2,
  "fallback": false,
  "fallback_reason": null,
  "parsed_ranges": [
    {"range": "zh-tw", "q": 0.95, "position": 1},
    {"range": "zh", "q": 0.9, "position": 2},
    {"range": "en", "q": 0.0, "position": 3},
    {"range": "*", "q": 0.5, "position": 4}
  ],
  "ignored_duplicates": [],
  "candidates": [
    {
      "tag": "en-US",
      "supported_index": 0,
      "truncation_chain": ["en-us", "en"],
      "matched_range": "en",
      "q": 0.0,
      "status": "excluded",
      "detail": "匹配到的范围 'en' q=0，候选被显式排除",
      "events": [ {"truncated_to": "en-us", "outcome": "no-match"},
                  {"truncated_to": "en", "outcome": "match",
                   "matched_range": "en", "q": 0.0, "header_position": 3},
                  {"truncated_to": "*", "outcome": "skipped",
                   "detail": "存在更具体的语言范围匹配，通配符未参与"} ]
    }
  ]
}
```

### 请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `accept_language` | string \| null | 否 | 原始 `Accept-Language` 头；缺失、`null` 或空白串视为“无有效范围” |
| `supported_languages` | string[] | 是 | 服务端支持的 BCP 47 标签，至少 1 个、不可重复（大小写不敏感）；**顺序即稳定优先级** |
| `default_language` | string | 是 | 默认语言，必须包含在 `supported_languages` 中（大小写不敏感） |

### 响应要点

- `selected_language`：最终选中的服务端标签（保持服务端原始大小写）。
- `fallback` / `fallback_reason`：
  - `no-valid-range`：请求头缺失或没有任何有效范围 → 回退默认语言；
  - `no-acceptable-candidate`：头合法但所有候选都被 `q=0` 排除或未命中 → 回退默认语言。
- `parsed_ranges`：解析出的语言范围（小写、去重后的结果，含位置与 q 值）。
- `ignored_duplicates`：重复范围中被忽略的后续出现（保留第一次出现）。
- `candidates[]`：每个候选的完整轨迹：
  - `truncation_chain`：逐级截断链，如 `zh-Hant-TW → zh-Hant → zh`；
  - `events[]`：每一级截断的 `match` / `no-match`，以及通配符 `*` 的
    `match` / `skipped`（存在更具体匹配时通配符不参与）；
  - `matched_range`、`q`：最终命中的客户端范围及权重；
  - `status`：`selected` / `eligible` / `excluded`（q=0）/ `unmatched`（未命中）/
    `no-range`（头无有效范围）。

## 协商规则

1. **q 权重**：遵循 RFC 9110，缺省 `q=1`；合法写法为 `0`–`1`、最多 3 位小数
   （如 `0.9`、`1.000`）。非法 q 值、未知参数、非法 BCP 47 标签等语法错误一律返回
   **HTTP 422**。
2. **q=0 排除**：命中 `q=0` 范围的候选标记为 `excluded`，不参与选择。
3. **通配符 `*`**：只在候选没有任何具体范围命中时兜底；具体范围永远优先于 `*`。
4. **重复范围**：同一范围（大小写不敏感）出现多次时**保留第一次出现**及其 q 值，
   后续出现进入 `ignored_duplicates`。
5. **逐级截断匹配（RFC 4647 lookup）**：候选标签从右向左每次去掉一个子标签
   （`zh-Hant-TW → zh-Hant → zh`，单例子标签如扩展 `-u-`、私有标记 `-x-`
   不会作为链尾），最长前缀命中的范围决定候选权重；比较大小写不敏感。
6. **排序**：先按客户端权重从高到低；权重相同则按 `supported_languages` 中的先后
   顺序（稳定、可预期）。
7. **回退**：请求头缺失/空白/无有效范围，或没有任何可接受候选时，返回
   `default_language` 并置 `fallback=true`。若默认语言本身被 `q=0` 显式排除，
   `message` 中会额外说明。

### 422 错误示例

```bash
curl -i -X POST http://localhost:8000/api/v1/negotiate \
  -H 'Content-Type: application/json' \
  -d '{"accept_language":"en;q=2","supported_languages":["en"],"default_language":"en"}'
```

```json
{
  "detail": [
    {
      "loc": ["body", "accept_language"],
      "msg": "第 1 个语言范围的 q 值非法: '2'",
      "type": "accept_language.syntax_error"
    }
  ]
}
```

请求体自身的 Pydantic 校验错误（如默认语言不在支持列表、标签重复/非法、字段缺失）
同样返回标准 422 结构。

## 配置

所有配置均为可选项，默认值可直接本地使用：

| 配置 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 端口映射 | `docker-compose.yml` | `8000:8000` | 左侧改宿主机端口，如 `18000:8000` |
| `UVICORN_WORKERS` | compose 环境变量 | `2` | Uvicorn worker 进程数 |
| `LOG_LEVEL` | compose 环境变量 | `info` | 日志级别（`critical`/`error`/`warning`/`info`/`debug`/`trace`） |

容器内服务固定监听 `8000` 端口；镜像以非 root 用户运行，内置 `/health`
Docker healthcheck。

不使用 Compose 时等价的单容器启动方式：

```bash
docker build -t language-negotiator .
docker run --rm -p 8000:8000 -e UVICORN_WORKERS=2 language-negotiator
```

## 本地开发（不用容器）

需要 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -q          # 40 个单元/接口测试
```

## 项目结构

```
app/
  main.py          # FastAPI 路由、异常处理（422）、健康检查
  negotiation.py   # Accept-Language 解析与逐级截断协商核心
  schemas.py       # Pydantic v2 请求/响应模型
tests/             # pytest 单元测试与 TestClient 接口测试
Dockerfile         # python:3.12-slim，依赖构建期安装，非 root 运行
docker-compose.yml # 单服务编排，8000 端口 + healthcheck
requirements.txt   # 运行时固定版本依赖
```
