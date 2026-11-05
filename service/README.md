# HTTP 服务（可选）

核心引擎仍然零依赖；只有跑 API / Docker 时才需要安装本目录的依赖。

## 本地运行

```bash
python -m pip install -r service/requirements.txt
uvicorn service.app:app --reload --port 8000
```

打开 http://127.0.0.1:8000/docs 看交互式文档。

## Docker

```bash
docker compose up --build
```

或：

```bash
docker build -t erp-cost-copilot .
docker run --rm -p 8000:8000 erp-cost-copilot
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查与版本 |
| POST | `/v1/rag` | 本地 BM25 检索 `{query, k}` |
| GET | `/v1/checkup?dataset=dirty` | 排雷清单（Markdown） |
| GET | `/v1/diagnose?dataset=demo&limit=5` | 强归因诊断 |
| POST | `/v1/challenge` | 反证复核 `{dataset, text}` |
| POST | `/v1/translate-config` | 分摊配置翻译 `{text}` |
| GET | `/v1/eval` | 运行评测集并返回汇总 |
| GET | `/v1/abc-check?file=sample` | ABC 动因数据前置校验 |

`dataset` 只允许 `demo` / `november` / `dirty` / `fixed`，不接受任意文件路径。

## 示例

```bash
curl -s http://127.0.0.1:8000/v1/rag \
  -H "Content-Type: application/json" \
  -d '{"query":"未分配制造费用怎么处理","k":2}'

curl -s "http://127.0.0.1:8000/v1/checkup?dataset=dirty"

curl -s http://127.0.0.1:8000/v1/challenge \
  -H "Content-Type: application/json" \
  -d '{"dataset":"demo","text":"车间说已经退料了，只是单据没录"}'

curl -s "http://127.0.0.1:8000/v1/abc-check?file=sample"
```

## 边界

- 只读：不写库、不生成凭证、不改账；
- 默认离线规则；要接 DeepSeek / Ollama，设置 `CC_LLM_PROVIDER` 与对应密钥；
- 本地 Demo 没有鉴权，不要直接暴露到公网；生产部署请加反向代理与鉴权。
