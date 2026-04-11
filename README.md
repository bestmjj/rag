[toc]

# Lightweight RAG for OpenWebUI + Kodbox

一个面向 VPS 的轻量本地知识库方案：

- `OpenWebUI` 作为用户入口
- `rag-api` 作为薄 RAG 服务
- `Qdrant` 作为向量库
- `Kodbox` 文件目录通过宿主机挂载给 `rag-api`，不走上传流程

这个项目的目标不是做一个“大而全”的平台，而是用尽量少的组件完成可维护、可调优的本地 RAG。

## 特性

- Docker Compose 部署
- 直接读取本地路径，不上传文件
- 支持常见文本、代码、表格和 Office 文档
- 基于 `mtime + sha256` 的增量索引
- 文件删除后可自动从 Qdrant 清理旧向量
- 对 OpenWebUI 暴露 OpenAI-compatible 接口
- 支持本地 embedding 或远程 OpenAI-compatible embeddings
- 返回答案时附带来源文件路径

## 架构

组件说明：

- `open-webui`
  - 纯聊天入口
  - 不负责扫描本地文件
  - 通过 `http://rag-api:8000/v1` 调用 `rag-api`

- `rag-api`
  - 扫描宿主机挂载进来的知识库目录
  - 解析文档、切块、生成向量、写入 Qdrant
  - 查询时先检索，再调用下游 chat 模型生成答案
  - 对外暴露 OpenAI-compatible `chat completions` 接口

- `qdrant`
  - 存储 chunk 向量和元数据
  - 元数据包含来源文件路径、chunk 编号、文件 hash、mtime 等

- `openai-compatible model`
  - 用于聊天生成答案
  - 可与 embedding 服务相同，也可不同

- `embedding backend`
  - `local`：本地 `sentence-transformers`
  - `openai`：远程兼容 OpenAI 的 `/embeddings`

## 工作流

### 1. 建库 / 增量索引

调用：

```bash
curl -X POST http://127.0.0.1:8000/index
```

流程：

1. `rag-api` 扫描 `KB_ROOT` 下符合扩展名的文件
2. 根据 `KB_EXCLUDE_PATTERNS` 排除不需要的路径
3. 对每个文件读取文本内容
4. 计算文件 `sha256` 和 `mtime`
5. 如果文件未变化，跳过
6. 如果文件新增或内容变化：
   - 删除旧 chunk
   - 重新切块
   - 生成 embedding
   - 批量写入 Qdrant
7. 如果某个文件已从磁盘删除：
   - 自动从 Qdrant 删除对应 chunk

### 2. 查询 / 对话

OpenWebUI 或客户端调用：

```bash
POST /v1/chat/completions
```

流程：

1. `rag-api` 取最后一条用户问题
2. 将问题转成 query embedding
3. 到 Qdrant 检索相似 chunk
4. 根据分数过滤结果
5. 将有限数量的 chunk 拼成上下文
6. 调用下游 chat 模型生成答案
7. 将来源路径附加到回答后返回给 OpenWebUI

## 目录结构

```text
.
├── docker-compose.yml
├── .env.example
├── README.md
└── rag-api/
    ├── Dockerfile
    ├── requirements.txt
    └── app.py
```

## 快速开始

### 1. 准备 `.env`

复制模板：

```bash
cp .env.example .env
```

最少需要修改：

```env
KB_HOST_PATH=/your/kodbox/storage/path
OPENAI_BASE_URL=https://your-openai-compatible-api/v1
OPENAI_API_KEY=replace-with-your-api-key
CHAT_MODEL=replace-with-your-chat-model
```

如果你使用远程 embeddings，再补上：

```env
EMBEDDING_PROVIDER=openai
EMBEDDING_BASE_URL=https://your-openai-compatible-api/v1
EMBEDDING_API_KEY=replace-with-your-api-key
EMBEDDING_MODEL_REMOTE=text-embedding-3-small
```

### 2. 启动服务

```bash
docker compose up -d --build
```

### 3. 手动触发索引

```bash
curl -X POST http://127.0.0.1:8000/index
```

### 4. 在 OpenWebUI 中接入

OpenWebUI 中新增一个 OpenAI 兼容连接：

```text
Base URL: http://rag-api:8000/v1
API Key: any-non-empty-string
```

如果是从宿主机浏览器访问：

```text
http://<your-host>:8000/v1
```

说明：当前 `rag-api` 不校验上游 API Key，OpenWebUI 中填任意非空字符串即可。

## API

- `GET /health`
  - 健康检查

- `POST /index`
  - 触发一次全量扫描 + 增量更新
  - 同时处理新增、修改、删除

- `GET /v1/models`
  - 返回一个供 OpenWebUI 识别的模型列表

- `POST /v1/chat/completions`
  - OpenAI-compatible 聊天接口
  - 支持普通返回和 `stream=true`

## `.env` 参数说明

下面按用途说明各参数的作用、影响和建议值。

### OpenWebUI

- `OPEN_WEBUI_IMAGE`
  - OpenWebUI 镜像

- `OPEN_WEBUI_CONTAINER_NAME`
  - 容器名

- `OPEN_WEBUI_BIND_HOST`
  - 宿主机绑定地址
  - 示例：`172.17.0.1`、`127.0.0.1`、`0.0.0.0`

- `OPEN_WEBUI_PORT`
  - OpenWebUI 暴露端口

- `OPEN_WEBUI_DATA_PATH`
  - OpenWebUI 数据目录

- `WEBUI_AUTH`
  - 是否启用 OpenWebUI 登录认证

### Qdrant

- `QDRANT_IMAGE`
  - Qdrant 镜像

- `QDRANT_CONTAINER_NAME`
  - 容器名

- `QDRANT_DATA_PATH`
  - Qdrant 数据持久化目录

- `QDRANT_URL`
  - `rag-api` 访问 Qdrant 的地址
  - 默认容器内走 `http://qdrant:6333`

- `QDRANT_COLLECTION`
  - 向量集合名

### 路径与文件过滤

- `KB_HOST_PATH`
  - 宿主机上的 Kodbox 实际文件目录
  - 这个目录会被只读挂载到容器内部

- `KB_ROOT`
  - 容器内部知识库根目录
  - 一般保持 `/kb`

- `KB_EXTENSIONS`
  - 允许索引的文件扩展名列表
  - 默认包含：
    - 纯文本和配置：`txt`、`text`、`md`、`markdown`、`rst`、`log`、`ini`、`cfg`、`conf`、`yaml`、`yml`、`json`、`xml`
    - 网页和代码：`html`、`htm`、`css`、`js`、`ts`、`tsx`、`jsx`、`py`、`go`、`java`、`c`、`cc`、`cpp`、`h`、`hpp`、`rs`、`sh`、`sql`
    - 表格：`csv`、`tsv`、`xls`、`xlsx`
    - Office/PDF：`pdf`、`doc`、`docx`

- `KB_EXCLUDE_PATTERNS`
  - 需要排除的路径关键字
  - 示例：`/recycle/,/cache/,/thumb/`
  - 对大目录非常重要，能显著减少扫描和索引开销

### Indexing and chunking

- `INDEX_BATCH_SIZE`
  - 每次批量写入 Qdrant 的 chunk 数
  - 更大通常更快，但占用更多内存
  - 建议：`16-64`

- `EMBEDDING_BATCH_SIZE`
  - 本地 embedding 模型每批编码的文本数量
  - 更大通常更快，但也更吃内存
  - 建议：`64-128`

- `CHUNK_SIZE`
  - 每个 chunk 的文本长度
  - 太小会导致 chunk 数暴涨，索引变慢
  - 太大则检索可能不够精确
  - 中文技术文档常用：`600-900`

- `CHUNK_OVERLAP`
  - 相邻 chunk 的重叠长度
  - 用于降低切块导致的语义断裂
  - 建议：`50-120`

### Retrieval and answer length

- `RETRIEVAL_LIMIT`
  - 每次检索返回的 chunk 数上限
  - 越大上下文越全，但模型越慢
  - 日常问答建议：`2-4`

- `MIN_RETRIEVAL_SCORE`
  - 最低相似度阈值
  - 低于这个分数的 chunk 会被丢弃
  - 常用范围：`0.30-0.40`

- `MAX_CONTEXT_CHARS`
  - 送给下游模型的检索上下文最大字符数
  - 控制 prompt 大小，防止上下文过长导致速度下降
  - 建议：`1200-2200`

- `MAX_OUTPUT_TOKENS`
  - 下游模型最大回答长度
  - 太大回答会更慢
  - 简洁型问答建议：`256-512`

### 聊天模型

- `OPENAI_BASE_URL`
  - 下游 chat model 的 OpenAI-compatible 接口地址

- `OPENAI_API_KEY`
  - 下游 chat model API Key

- `CHAT_MODEL`
  - 下游 chat model 名称

### Embeddings

- `EMBEDDING_PROVIDER`
  - `local` 或 `openai`

- `EMBEDDING_MODEL`
  - 本地 embedding 模型名称
  - 默认：`BAAI/bge-small-zh-v1.5`

- `EMBEDDING_BASE_URL`
  - 远程 embedding 接口地址

- `EMBEDDING_API_KEY`
  - 远程 embedding 接口密钥

- `EMBEDDING_MODEL_REMOTE`
  - 远程 embedding 模型名称

### PDF 提取

- `PDF_EXTRACTOR`
  - `pymupdf` 或 `pypdf`
  - 一般优先推荐 `pymupdf`
  - `pymupdf` 速度通常更快
  - `pypdf` 更适合作为轻量兜底

## 当前支持的文件类型

### 文本文档

- `txt`
- `text`
- `md`
- `markdown`
- `rst`
- `log`

### 配置与结构化文本

- `ini`
- `cfg`
- `conf`
- `yaml`
- `yml`
- `json`
- `xml`

### 网页与代码文本

- `html`
- `htm`
- `css`
- `js`
- `ts`
- `tsx`
- `jsx`
- `py`
- `go`
- `java`
- `c`
- `cc`
- `cpp`
- `h`
- `hpp`
- `rs`
- `sh`
- `sql`

### 表格

- `csv`
- `tsv`
- `xls`
- `xlsx`

说明：

- `csv` / `tsv` 会按行读取，并将列拼成文本
- `xlsx` 使用 `openpyxl` 读取
- `xls` 使用 `xlrd` 读取
- 表格内容会按 sheet 和行展开成普通文本，再参与切块和索引
- 可通过 `EXCEL_MAX_ROWS_PER_SHEET` 限制单个 sheet 读取的最大行数，避免超大表格拖慢索引

### Office / PDF

- `pdf`
- `doc`
- `docx`

说明：

- `pdf` 支持 `pymupdf` 或 `pypdf`
- `docx` 使用 `python-docx`
- `doc` 优先使用 `antiword`，失败时回退到 `catdoc`

### 暂不建议纳入默认索引的类型

- 图片类：`png`、`jpg`、`jpeg`、`webp`
- 二进制文件：`exe`、`bin`、`so`
- 压缩包：`zip`、`rar`、`7z`
- 多媒体：`mp3`、`mp4`、`mkv`

这些格式通常需要 OCR、转码或专门解析器，不适合当前这套轻量实现。

### 其他

- `RAG_API_BUILD_CONTEXT`
  - `rag-api` 镜像构建上下文目录

- `RAG_API_CONTAINER_NAME`
  - `rag-api` 容器名

- `RESTART_POLICY`
  - Docker 重启策略

- `EXCEL_MAX_ROWS_PER_SHEET`
  - 单个 Excel sheet 最大读取行数
  - 超出后会截断，并写入一条 `[Truncated]` 标记
  - 用于避免超大表格显著拖慢索引

## 推荐配置

### 1. 轻量优先

适合 VPS 资源较小、追求响应速度：

```env
INDEX_BATCH_SIZE=32
EMBEDDING_BATCH_SIZE=64
CHUNK_SIZE=700
CHUNK_OVERLAP=80
RETRIEVAL_LIMIT=2
MIN_RETRIEVAL_SCORE=0.35
MAX_CONTEXT_CHARS=1200
MAX_OUTPUT_TOKENS=256
PDF_EXTRACTOR=pymupdf
```

### 2. 平衡推荐

适合中文技术文档、PDF、教程类文档混合场景：

```env
INDEX_BATCH_SIZE=32
EMBEDDING_BATCH_SIZE=128
CHUNK_SIZE=700
CHUNK_OVERLAP=80
RETRIEVAL_LIMIT=3
MIN_RETRIEVAL_SCORE=0.35
MAX_CONTEXT_CHARS=1800
MAX_OUTPUT_TOKENS=384
PDF_EXTRACTOR=pymupdf
```

### 3. 质量优先

适合更重视答案完整性，不太在意速度：

```env
INDEX_BATCH_SIZE=32
EMBEDDING_BATCH_SIZE=128
CHUNK_SIZE=900
CHUNK_OVERLAP=120
RETRIEVAL_LIMIT=4
MIN_RETRIEVAL_SCORE=0.30
MAX_CONTEXT_CHARS=2800
MAX_OUTPUT_TOKENS=512
PDF_EXTRACTOR=pymupdf
```

## 最佳实践

### 索引与切块

- 不要把 `CHUNK_SIZE` 调得过小
  - 过小会造成 chunk 数暴涨，索引明显变慢
  - 对大 PDF，`700-800` 往往比 `500` 更合适

- `CHUNK_OVERLAP` 不要太高
  - 太高会带来大量重复 chunk
  - 一般保持在 `CHUNK_SIZE` 的 `10%-20%`

- 优先排除无关目录
  - 回收站
  - 缓存目录
  - 缩略图目录
  - 不需要的资料区

- 大量 PDF 时优先使用 `pymupdf`
  - PDF 文本提取速度差异很大
  - 它通常是影响索引耗时的第一因素

- 如果索引非常慢，优先排查 chunk 数
  - 一次索引返回 `indexed_chunks` 如果达到几百上千
  - 优先考虑调大 `CHUNK_SIZE` 或缩小索引范围

### 检索与回答

- 对聊天速度敏感时，把 `RETRIEVAL_LIMIT` 控制在 `2-3`
- 给 `MAX_CONTEXT_CHARS` 设置上限，防止 prompt 过大
- 给 `MAX_OUTPUT_TOKENS` 设置上限，避免模型回答过长
- 如果问题是基础常识或闲聊，RAG 不一定比直连模型更合适

### Embeddings

- `local`
  - 部署简单
  - 不依赖远程 embedding 接口
  - 对 VPS CPU 有压力

- `openai`
  - 更省本地 CPU
  - 大文件索引时通常更快
  - 依赖远程接口稳定性

如果你的远程兼容 OpenAI 服务同时支持 `/embeddings`，而且速度稳定，通常值得优先考虑远程 embeddings。

## 增量更新与删除同步

当前通过同一个接口完成：

```bash
curl -X POST http://127.0.0.1:8000/index
```

行为说明：

- 新文件：自动入库
- 已修改文件：自动删除旧 chunk 并重建
- 已删除文件：自动从 Qdrant 删除对应向量
- 未变化文件：跳过

所以这里没有单独的“增量接口”，重复调用 `/index` 就是增量同步。

## 故障影响

### 如果下游 chat model 挂了

- 聊天接口会失败
- 但如果 embedding 是本地的，索引通常还能继续

### 如果 embedding 也走远程接口，并且远程服务挂了

- 索引会失败
- 查询检索也会失败

### 如果 Qdrant 不可用

- 无法索引
- 无法检索

## 常见问题

### 1. 为什么索引很慢？

常见原因：

- 大 PDF 文本提取慢
- chunk 数过多
- 本地 embedding 批量太小
- VPS CPU 性能有限

优先优化顺序：

1. 使用 `PDF_EXTRACTOR=pymupdf`
2. 提高 `CHUNK_SIZE` 到 `700-800`
3. 提高 `EMBEDDING_BATCH_SIZE`
4. 改用远程 embeddings
5. 缩小知识库目录范围

### 2. 为什么问一个简单问题也会很慢？

因为当前实现是“先检索，再生成”。

如果问题本身不依赖知识库，RAG 会比纯聊天模型更慢。可以通过降低：

- `RETRIEVAL_LIMIT`
- `MAX_CONTEXT_CHARS`
- `MAX_OUTPUT_TOKENS`

来减轻延迟。

### 3. 为什么回答会带很多来源？

因为 `rag-api` 会把命中的来源路径附加到最终回答里，方便回溯原文。

## 各格式处理限制

| 格式 | 当前处理方式 | 已知限制 | 建议 |
| --- | --- | --- | --- |
| `txt` `md` `rst` `log` | 直接按文本读取 | 编码异常时会忽略坏字符 | 适合默认开启 |
| `json` `xml` `yaml` `ini` | 按纯文本读取 | 不做结构化语义解析 | 适合配置检索 |
| `html` `css` `js` `ts` `py` 等代码 | 按源码文本读取 | 不做 AST 级分析，注释和代码会混在一起 | 适合代码片段检索，不适合精确语义理解 |
| `csv` `tsv` | 按行读取，列用 ` | ` 拼接 | 超大文件仍可能产生很多 chunk | 建议只索引需要的表 |
| `xlsx` | 使用 `openpyxl` 逐 sheet 读取 | 复杂公式、图表、批注不会完整保留 | 通过 `EXCEL_MAX_ROWS_PER_SHEET` 控制规模 |
| `xls` | 使用 `xlrd` 读取 | 老格式兼容性取决于文件本身，复杂样式会丢失 | 尽量转成 `xlsx` 更稳 |
| `docx` | 使用 `python-docx` 提取段落文本 | 图片、浮动对象、复杂表格会丢失部分信息 | 适合普通文档 |
| `doc` | 先用 `antiword`，失败后尝试 `catdoc` | 扫描件、复杂排版、嵌入对象效果有限 | 能转 `docx` 时优先转 `docx` |
| `pdf` | `pymupdf` 或 `pypdf` 提取文本 | 扫描版 PDF、双栏排版、公式文档可能抽取质量一般 | 文本型 PDF 推荐 `pymupdf`，扫描件建议 OCR 后再入库 |

## 当前实现的局限

- 还没有“是否需要检索”的智能门控
- `/index` 是同步接口，大目录下会阻塞较久
- 还没有后台任务队列或索引状态查询接口
- 还没有多知识库、多租户隔离能力

## 后续可优化方向

- 增加“普通闲聊直连模型，知识问题才检索”的门控
- 增加后台异步索引任务
- 增加索引 manifest，减少每次扫描时的额外开销
- 针对不同文件类型使用不同 chunk 策略
- 增加备用下游模型地址，降低单点故障影响
