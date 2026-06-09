# 美股估值分析器

这是一个 FastAPI + 单页前端的美股估值分析服务。当前版本统一使用 FMP 作为数据源，避免不同来源之间口径不一致。

## 核心能力

- 抓取公司基础资料、最新价格、估值倍数和历史价格
- 抓取 FMP 季度财报、分析师预期、历史财报事件和 EPS surprise
- 展示历史 P/E、Forward P/E、P/S、EV/EBITDA 曲线与分位
- 基于 FMP peers 生成同行候选，并按行业、板块、市值排序
- 用价格模拟和财报达成率辅助输出估值结论
- 提供网页仪表盘、普通 JSON API 和流式日志 API

## 技术结构

- `providers/`: FMP 数据抓取层
- `services/`: 估值、同行比较、财报达成率和价格模拟逻辑
- `api.py`: FastAPI 接口与 token 校验
- `web/`: 单页前端仪表盘

## 配置方式

服务默认只读取进程环境变量，不会自动读取 `.env`。如果要从文件读取配置，请显式使用 `--env-file`。

可用配置：

```bash
APP_NAME=US Equity Valuation Analyzer
APP_ENV=local
HOST=127.0.0.1
PORT=8000
FMP_API_KEY=your_fmp_api_key
FMP_BASE_URL=https://financialmodelingprep.com/stable
CACHE_DIR=.cache
FMP_CACHE_ENABLED=false
FMP_CACHE_TTL_SECONDS=43200
FMP_PEERS_CACHE_TTL_SECONDS=86400
APP_ACCESS_TOKEN=
```

`FMP_API_KEY` 是获取财务和估值数据所需的 key。`APP_ACCESS_TOKEN` 为空时不启用访问控制；如果要放到公网，建议设置一个足够长的随机 token。

本地缓存默认关闭。若设置 `FMP_CACHE_ENABLED=true`，FMP 响应会缓存到 `.cache/fmp_api/`。

## 本地启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
export FMP_API_KEY=your_fmp_api_key
valuation-analysis --reload
```

如果想用配置文件：

```bash
cp env.example .env
valuation-analysis --env-file .env --reload
```

也可以继续直接使用 uvicorn，但这种方式只读取系统环境变量：

```bash
uvicorn --app-dir src valuation_analysis.api:app --reload
```

启动后访问：

- `GET /`
- `GET /health`
- `GET /analyze/AAPL`
- `GET /analyze-stream/AAPL`
- `GET /analyze/MSFT?peer_count=6`

`/analyze-stream/{symbol}` 会返回 Server-Sent Events，前端的运行日志就是通过这个接口显示的。

## Docker 部署

默认从宿主机环境变量读取配置：

```bash
export FMP_API_KEY=your_fmp_api_key
export APP_ACCESS_TOKEN=change_me_to_a_long_random_token
docker compose up --build -d
```

如果要使用环境变量文件，请用 Docker Compose 的 `--env-file` 参数：

```bash
cp env.example .env
docker compose --env-file .env up --build -d
```

如果 token 包含 `$` 这类 shell/Compose 会解释的字符，请在 `.env` 里用单引号包住，例如：

```bash
APP_ACCESS_TOKEN='abc$xyz'
```

不用 Compose 时：

```bash
docker build -t valuation-analysis .
docker run \
  --env-file .env \
  -p 8000:8000 \
  -v "$(pwd)/.cache:/app/.cache" \
  valuation-analysis
```

容器内服务监听 `0.0.0.0:8000`，访问地址为：

```bash
http://localhost:8000
```

## API Token

设置 `APP_ACCESS_TOKEN` 后，`/analyze/{symbol}` 和 `/analyze-stream/{symbol}` 都需要 token。

后端支持两种传参方式：

- `Authorization: Bearer <token>`
- `?access_token=<token>`

网页仪表盘的“访问 Token”输入框会自动把 token 传给后端。

## 输出内容

网页仪表盘当前展示：

- 公司画像：名称、行业、板块、市值
- 价格模拟：上次、近 1 年、近 2 年、近 5 年、历史中位估值对应目标价
- 估值评分：估值 / 执行 / 成长三个维度
- 市场快照：价格、5 日均价、30 日均价、P/E、PEG、P/S、EV/EBITDA、52 周区间
- 历史估值曲线：Trailing P/E、Forward P/E、P/S、EV/EBITDA
- 未来预期：本年/明年 EPS 与收入、增长率
- 财报达成率：beat rate、平均 surprise、近期财报事件
- 同行估值横截面与季度财务摘要

## 同行策略

当前版本只使用 FMP `stock-peers` 返回的候选，不再依赖本地股票池或离线索引。

同行筛选流程：

- 先拉取 FMP peers 候选代码
- 再逐个获取候选公司的 FMP 公司画像
- 优先保留行业完全匹配的公司
- 行业不足时使用同板块公司补足
- 最终按市值从大到小展示

同行估值仍然只是参考信号。对于 AAPL、TSM、ASML 这类业务边界很特殊的公司，后续更适合增加“自定义可比公司篮子”。

## 财务与财报达成率

当前版本统一使用 FMP：

- `income-statement`: 历史季度财报
- `earnings`: 历史财报事件与 EPS surprise
- `analyst-estimates`: 未来 EPS 与收入预期
- `historical-price-eod/full`: 历史价格与均价
- `ratios-ttm` / `key-metrics-ttm`: 估值倍数补充

EPS surprise 轨迹默认追踪最近 10 次有 estimate 和 actual 的财报事件。

## 价值评估逻辑

估值维度主要参考价格模拟：

- 上次、近 1 年、近 2 年、近 5 年、历史中位估值对应目标价
- 若 4 个或以上目标价高于当前价格 20% 以上，判为极度低估
- 若 3 个目标价高于当前价格 20% 以上，判为低估
- 若 1 个或 0 个目标价高于当前价格，判为高估

成长维度主要参考未来 EPS 增速和收入增速；执行维度主要参考历史 EPS beat rate 与平均 surprise。

## 后续方向

- 增加自定义可比公司篮子，解决特殊行业或垄断型公司的同行不准问题
- 增加 DCF、分部估值和 Bull/Base/Bear 情景分析
- 增加历史分析结果存储，用来追踪模型判断变化
- 增加任务调度，定时刷新重点股票
