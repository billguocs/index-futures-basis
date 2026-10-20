# 接口契约与协作规范（API_CONTRACT）

> 本文件是三个模块的**唯一契约**。任何人改动数据形状都必须先改本文件，并在汇报里说明。
> 项目根目录：`D:\Code\Future-Stock-Combine`；Python 解释器：
> `C:\Users\billg\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`（下称 `$PY`，已含 numpy 2.3.5 / pandas 3.0.1）。

## 0. 产品目标

抓取中金所（CFFEX）股指期货**成交持仓排名**，跟踪**中信期货**在 IF/IH/IC/IM 四个品种上的
多空持仓，与沪深300 / 上证50 / 中证500 / 中证1000 指数做统计拟合，输出**次日行情**的概率性参考。

产品必须诚实：所有胜率、相关系数、回测都给出样本量 n 与显著性 p，结果不显著就明说不显著。

## 1. 目录与文件归属（禁止越界写别人的文件）

| 负责人 | 可写文件 |
| --- | --- |
| Lead | `config.py`、`backend/db.py`、`backend/api.py`、`app.py`、`README.md`、`run.ps1`、`contract/*`、`tests/test_api.py`、`tests/test_e2e.py` |
| data-engine | `backend/http_util.py`、`backend/sources/__init__.py`、`backend/sources/cffex.py`、`backend/sources/index_data.py`、`backend/sync.py`、`tests/test_sync.py`、`reports/data_sync.md`、`data/` |
| analytics-engine | `backend/analytics.py`、`backend/signal.py`、`tests/test_analytics.py`、`reports/analytics.md` |
| frontend-engine | `frontend/**` |

公共底座（已由 Lead 写好，**直接 import，不要重写**）：
`config.py`（全部常量/映射/URL）、`backend/db.py`（`connect()`、`session()`、`upsert_many()`、`SCHEMA` 全部表）。

## 2. 已实测确认的数据源事实（不要重新试错）

### 2.1 中金所成交持仓排名（核心数据）
```
http://www.cffex.com.cn/sj/ccpm/{YYYYMM}/{DD}/{IF|IH|IC|IM}_1.csv
例：http://www.cffex.com.cn/sj/ccpm/202610/20/IF_1.csv
```
- 编码 **GB2312/GBK**，用 `decode("gbk", "ignore")`。
- **缺失文件返回 HTTP 200 + 2132 字节的错误页**（"网页错误"），必须用长度或内容判定，不能只看状态码。
- 结构：2 行表头 + 每合约 20 行；12 列：
  `交易日, 合约, 排名, [成交量: 会员简称,成交量,增减], [持买单量: 会员简称,持买单量,增减], [持卖单量: 会员简称,持卖单量,增减]`
- 实测坑位：
  - 合约列**存在右补空格**（`"IF2401                        "`），必须 `strip()`。
  - 会员名 2024-05 之后带后缀 `(代客)`，之前是裸名（如 `中信期货`）；存在全/半角括号两种写法。
    归一化：用 `config.MEMBER_SUFFIX_PATTERN` 去掉尾部括号后缀 → `backend.sources.cffex.normalize_member()`。
  - 2020 年前后文件里有 `非期货公司` 的 0 值行、9 列的残行 → 跳过 `rank` 非 1..20 的整数行。
  - 每个品种每日通常 3 个合约（合计 60 行），偶尔 80 行（4 个合约）。
- 已验证覆盖：`202610/20`（最新）、`202207/22`、`202401/02`；IM 自 2022-07-22 上市。

### 2.2 中金所日行情（成交量/持仓量/结算价）
```
http://www.cffex.com.cn/sj/hqsj/rtj/{YYYYMM}/{DD}/{YYYYMMDD}_1.csv     # GBK CSV，全品种（含期权）
http://www.cffex.com.cn/sj/hqsj/rtj/{YYYYMM}/{DD}/index.xml            # UTF-8 XML，备用
```
CSV 列：`合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,今收盘,今结算,前结算,涨跌1,涨跌2,Delta`
（实测 `202401/02` 可用，65761 字节）。只需筛选合约代码以 IF/IH/IC/IM 开头的行。

### 2.3 指数日线
- **Sina（已实测稳定）**，一次最多 1023 根：
  `https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh000300&scale=240&ma=no&datalen=1023`
  返回 `[{"day":"2026-10-20","open":..,"high":..,"low":..,"close":..,"volume":..}]`，UTF-8，需 `Referer: https://finance.sina.com.cn/`。
  四个代码：`sh000300` `sh000016` `sh000905` `sh000852`（均已验证）。
- **腾讯（已实测，可按日期段翻页取长历史）**：
  `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000300,day,2010-01-01,2026-12-31,640,qfq`
  单次最多 640 根，返回 `data.sh000300.day`（或 `qfqday`）数组 `["日期","开","收","高","低","量"]`，需 `Referer: https://gu.qq.com/`。
- **东方财富（不稳定，IP 级 403/断连，仅作最后兜底）**：URL 见 `config.EASTMONEY_KLINE_URL`。
- ifeng 接口域名已失效，不要用。

**要求**：`index_data.fetch_history(code)` 必须按「腾讯（翻页拼长历史）→ 新浪（1023 根）→ 东财」降级，
返回 `[(date, open, high, low, close, volume, amount_or_None), ...]` 并与库中已有数据**合并去重**后 upsert。

### 2.4 交易日历
不要硬编码。取「指数日线日期集合 ∪ 中金所已成功抓取的日期」作为日历；
指数长历史足够（2010 起），因此 `config.SYNC_START` 可放心使用。

## 3. 数据库表

表结构在 `backend/db.py` 的 `SCHEMA` 里，**不要改表结构**；如确需加列，先告知 Lead。
写入一律用 `db.upsert_many(conn, table, columns, rows, pk_cols)`。

关键表：
- `index_daily(code, date, open, high, low, close, volume, amount, source)`
- `fut_daily(date, variety, contract, open, high, low, close, settle, prev_settle, volume, amount, open_interest, oi_change, is_main)`
- `member_rank(date, variety, contract, rank_type, rank, member, member_raw, position, change)`
  `rank_type ∈ {vol, long, short}`；`member` = 归一化名；`position` = 成交量/持买单量/持卖单量
- `member_daily(date, variety, member, long_pos, short_pos, volume, net_pos)`
  由 `member_rank` 按 (date, variety, 归一化member) 聚合：`long_pos = Σ short榜单position` 对应 `long` 榜… 即：
  `long_pos = Σ(member_rank where rank_type='long').position`，`short_pos` 同理，`volume = Σ(rank_type='vol')`，`net_pos = long_pos - short_pos`。
  注意：标题里的"持买单量排名"= 多头持仓，"持卖单量排名"= 空头持仓。
- `variety_daily(date, variety, main_contract, main_close, volume, open_interest, top20_long, top20_short, top20_net, citic_long, citic_short, citic_net, basis, basis_pct)`
  - `main_contract`：当日该品种 `fut_daily.open_interest` 最大的合约，`is_main=1`。
  - `open_interest`：该品种**全部合约**持仓量之和（来自 fut_daily）。
  - **命名映射（易错）**：库里的 `variety_daily.open_interest` 在 API 输出中叫 `market_oi`；
    `citic_net_pct = citic_net / open_interest * 100` 是**派生值，不落库**，算的时候现算。
  - `top20_*`：该品种当日所有合约 20 个名次 position 的合计。
  - `citic_*`：`config.DEFAULT_MEMBER`（中信期货）按合约求和后的多/空/净持仓。
  - `basis = main_close - index_close`，`basis_pct = basis / index_close * 100`。

## 4. 模块接口（必须完全按签名实现）

### 4.1 `backend/sync.py`（data-engine）
```python
def sync_all(start: str | None = None, end: str | None = None,
             varieties: list[str] | None = None, progress=None) -> dict:
    """增量同步：指数日线 -> 中金所日行情 -> 持仓排名 -> 聚合 member_daily/variety_daily。
    返回 {"ok": bool, "index_rows": int, "fut_rows": int, "rank_days": int,
          "variety_rows": int, "failed": [{"date":..., "variety":..., "reason":...}],
          "range": {"start":..., "end":...}, "elapsed": float}"""

def rebuild_aggregates(conn=None) -> dict:   # 纯 SQL/内存重算 member_daily + variety_daily，幂等
```
要求：并发抓取（`config.REQUEST_THREADS`）；单日失败不影响整体；幂等可重跑；
完成后写 `kv["last_sync_at"]`、`kv["member_naming_break_date"]`（首次出现带括号后缀的交易日）。

### 4.2 `backend/analytics.py`（analytics-engine）
```python
def compute_dashboard(conn, variety: str, lookback: int = 720,
                      member: str = config.DEFAULT_MEMBER) -> dict
def compute_members(conn, variety: str, lookback: int = 720,
                    members: list[str] | None = None) -> list[dict]
def compute_ranks(conn, date: str, variety: str,
                  highlight: str = config.DEFAULT_MEMBER) -> dict
```
`compute_dashboard` 返回第 5 节定义的完整字典（所有键必须存在，无法计算时给 `None` 或空列表，**不要抛异常**）。
数据不足时返回 `{"ok": False, "error": "数据不足", "empty": True}`。

### 4.3 `backend/signal.py`（analytics-engine）
```python
def build_guidance(df, variety: str, member: str) -> dict     # 返回第 5 节 guidance 块
```
由 `analytics.compute_dashboard` 内部调用，也可单独单测。

## 5. HTTP 接口（Lead 在 `backend/api.py` 实现；frontend-engine 按此开发）

统一：`GET`，`Content-Type: application/json; charset=utf-8`，HTTP 恒 200，
响应体恒含 `"ok": true|false` 与 `"error": null|"说明"`。

| 路径 | 说明 |
| --- | --- |
| `/api/health` | `{"ok":true,"db":true,"rows":{...}}` |
| `/api/meta` | 见 5.1 |
| `/api/dashboard?variety=IF&lookback=720&member=中信期货` | 见 5.2（核心） |
| `/api/ranks?date=2026-10-20&variety=IF&member=中信期货` | 见 5.3 |
| `/api/members?variety=IF&lookback=720` | 见 5.4 |
| `/api/sync/status` | `{"ok":true,"running":bool,"progress":{"done":n,"total":n,"label":str},"last":{"at":str,"result":{...}}}` |
| `POST /api/sync` body `{"start":"2022-01-03"}` | 启动后台同步，立即返回 `{"ok":true,"started":true}` |
| `/api/export?variety=IF&lookback=720` | `text/csv` 附件下载：date,index_close,index_ret_pct,citic_net,citic_net_pct,dnet,market_oi |

### 5.1 `/api/meta`
```json
{"ok":true,"error":null,
 "generated_at":"2026-10-20T22:40:00+08:00",
 "latest_trade_date":"2026-10-20",
 "data":{"index_daily":{"start":"2022-07-28","end":"2026-10-20","rows":1030},
         "variety_daily":{...},"member_rank":{...},
         "by_variety":{"IF":{"start":"...","end":"...","rows":n}, ...},
         "last_sync":"2026-10-20T22:31:00"},
 "varieties":[{"code":"IF","name":"沪深300股指期货","index_code":"000300","index_name":"沪深300","multiplier":300,"color":"#4d7cfe"}],
 "members":["中信期货","国泰君安",...],
 "default_member":"中信期货",
 "default_lookback":720,
 "lookback_options":[240,480,720,0],
 "disclaimer":"..."}
```

### 5.2 `/api/dashboard`
```json
{"ok":true,"error":null,"empty":false,
 "variety":"IF","variety_name":"沪深300股指期货","index_code":"000300","index_name":"沪深300",
 "member":"中信期货","lookback":720,"as_of":"2026-10-20",
 "window":{"start":"2023-08-01","end":"2026-10-20","n":720},

 "snapshot":{"date":"2026-10-20","index_close":4152.11,"index_chg_pct":-0.54,
   "main_contract":"IF2612","main_close":4148.2,"basis":-3.9,"basis_pct":-0.094,
   "citic_long":71661,"citic_short":169667,"citic_net":-98006,
   "citic_net_pct":-12.3,"citic_net_z":-1.42,"citic_net_pctile":12.4,
   "dnet":-3000,"dnet_pct_of_oi":-0.38,"dnet_z":-1.83,"dnet_pctile":8.5,
   "top20_long":600000,"top20_short":660000,"top20_net":-60000,
   "market_oi":790000,"citic_share_of_top20":-163.0},

 "series":{"date":["..."],"index_close":[...],"index_ret_pct":[...],
   "citic_net":[...],"citic_net_pct":[...],"citic_long":[...],"citic_short":[...],
   "market_oi":[...],"main_close":[...],"basis_pct":[...],
   "dnet_pct_of_oi":[...],"dnet_z":[...],"net_pctile":[...]},

 "fit":{"n":720,"sample":{"start":"...","end":"..."},
   "corr_contemporaneous":-0.31,
   "corr_predictive":-0.071,
   "models":[{"key":"dnet_z","name":"净空单日变化(标准化)","n":681,"corr":-0.071,
              "slope":-0.043,"intercept":0.008,"r2":0.005,"t_stat":-1.86,"p_value":0.063,
              "significant":false},
             {"key":"net_z","name":"净持仓水平(标准化)","n":681, ...},
             {"key":"net_pctile","name":"净持仓历史分位","n":681, ...}],
   "primary":"dnet_z",
   "lag_corr":[{"lag":-3,"corr":0.01,"n":700},{"lag":-2,...},{"lag":-1,...},{"lag":0,...},{"lag":1,...},{"lag":2,...},{"lag":3,...}],
   "rolling_corr":{"date":["..."],"contemporaneous":[...],"predictive":[...]},
   "stability":{"years":[{"year":2023,"corr_predictive":-0.12,"n":242},...]},
   "scatter":{"x":[...],"y":[...],"line":{"x":[-2.5,2.5],"y":[-0.3,0.3]}},
   "x_label":"净空单日变化(滚动252日标准化)","y_label":"次日指数对数收益(%)"},

 "buckets":{"x_label":"净空单日变化分位组","n_buckets":5,"ic":-0.068,"ic_p":0.06,
   "rows":[{"bucket":1,"label":"Q1 净空单大幅增加","lo":null,"hi":-1.28,"n":136,
            "mean_ret_pct":-0.09,"median_ret_pct":-0.07,"win_rate":45.6,
            "t_stat":-1.4,"p_value":0.16},...]},

 "backtest":{"rule":"滚动252日分位：≤20% 做空、≥80% 做多、其余空仓，次日收盘结算",
   "cost_bp":0.5,
   "equity":{"date":["..."],"nav":[...],"nav_bh":[...]},
   "metrics":{"cum_return":0.31,"annual_return":0.09,"max_drawdown":-0.17,
              "sharpe":0.62,"hit_rate":52.1,"trades":214,"exposure":0.57,
              "benchmark_cum":0.42,"n":681}},

 "guidance":{...见 5.2.1...},

 "ranks":{...见 5.3...},
 "warnings":["2024-05-06 起会员持仓披露改为代客口径，跨期比较需谨慎"]}
```

#### 5.2.1 `guidance` 块
```json
{"date":"2026-10-20","for_date":"2026-10-21",
 "direction":"偏空","score":-0.42,"confidence":"中",
 "up_prob":45.6,"expected_ret_pct":-0.09,"sample_n":136,
 "drivers":[{"key":"dnet_z","name":"净空单日变化(标准化)","value":-1.83,
             "impact":"利空","weight":0.6,"desc":"今日净空单较昨日增加，位于近一年 8% 分位"}],
 "evidence":["净空单日变化处于滚动 252 日 8.5% 分位，历史上该分位组次日上涨概率 45.6%（n=136）",
             "近 60 日「净空单变化→次日收益」滚动相关系数 -0.11，方向与全样本一致"],
 "caveats":["单因子解释力弱（R²=0.005），仅作概率参考，不可作为交易依据"],
 "similar":{"n":136,"mean_ret_pct":-0.09,"win_rate":45.6,
            "up_prob_ci":[38.6,52.8],"last_examples":[{"date":"2026-09-12","next_ret_pct":-0.42}]}}
```
`direction ∈ {"偏多","中性","偏空"}`；`confidence ∈ {"低","中","高"}`。

### 5.3 `ranks` 块（也是 `/api/ranks` 的返回体）
```json
{"ok":true,"date":"2026-10-20","variety":"IF",
 "long":[{"rank":1,"member":"国泰君安","position":8566,"change":-207}, ...20 条...],
 "short":[...],"volume":[...],
 "highlight":"中信期货",
 "highlight_row":{"member":"中信期货","long":71661,"short":169667,"net":-98006},
 "contracts":["IF2611","IF2612","IF2703"]}
```

### 5.4 `/api/members`
```json
{"ok":true,"variety":"IF","lookback":720,"member":"中信期货",
 "rows":[{"member":"中信期货","net_latest":-98006,"net_pct_latest":-12.3,
          "net_avg20":-91000,"net_change20":-7006,"pred_corr":-0.071,
          "n":681,"rank":1}, ...]}
```

## 6. 统计口径（硬性要求，违反即返工）

1. **严禁未来函数**。第 t 日的信号只能用 ≤ t 的数据：
   - 预测目标 `y_t = ln(close_{t+1}/close_t) * 100`（次日对数收益，%）。收盘价来自指数，不来自期货。
   - 标准化 `z`、分位 `pctile` 一律用**滚动**窗口（252 日，`min_periods=60`），不得用全样本均值/标准差。
   - 描述性统计（`fit.models`、`buckets`）可用窗口内全样本，但必须在 `caveats` 里注明"该分位基于全窗口，含后验信息，仅供描述"。
2. **显著性**：OLS 的 `t_stat` 用经典 OLS 标准误（`se(b) = sqrt(sigma2 / Sxx)`，
   `sigma2 = RSS/(n-2)`），`p_value` 用正态近似 `2*(1-Phi(|t|))`（可自行用 `math.erf` 实现，不要引入 scipy）。
   `significant = p_value < 0.05`。
3. **胜率**：`win_rate = 100 * mean(y > 0)`，用百分数（0–100）。
4. **分位分组**：`Q1 = 最小的一段`（净空单日变化最负 = 空头加仓最多），`Q5 = 最大的一段`；
   `label` 用中文，前两组描述"净空单增加"，后两组"净空单减少"。
5. **回测**：滚动分位阈值（只用 ≤t 的 252 日样本）；换仓才计成本 `cost_bp`；
   `annual_return = (1+cum_return)^(252/n) - 1`；`max_drawdown` 用净值序列；`sharpe = mean(daily)*sqrt(252)/std(daily)`（std 为 0 时给 None）。
6. 所有浮点保留 2–4 位小数即可，**不要输出 NaN / Infinity**（用 None 代替，JSON 里必须是 `null`）。
7. `numpy`/`pandas` 的计算要处理 `datetime` 排序与缺失对齐；`date` 字段统一 `YYYY-MM-DD` 字符串。

## 7. 前端要求（frontend-engine）

- 单页应用，**中文界面**，深色专业风（背景 `#0d1117` 系，卡片 `#161b22`，涨红跌绿的 A 股习惯：**涨用红 `#f6465d`，跌用绿 `#0ecb81`**）。
- 图表库用 **ECharts 5.5.1**，本地文件 `frontend/vendor/echarts.min.js`（从 `https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js` 下载，约 1MB，必须本地化，不能依赖外网 CDN）。
- 数据全部走同源 `/api/*`，页面加载时先 `/api/meta`，再 `/api/dashboard`；品种切换（IF/IH/IC/IM）与窗口切换（1年/2年/3年/全部）重新拉数据。
- 顶部：标题 + 数据截止日 + 品种 Tabs + 窗口选择 + "立即更新数据"按钮（POST /api/sync 后轮询 /api/sync/status）。
- 页面区块（建议 5 个 Tab 或一页滚动分区，自选，但必须有）：
  1. **次日研判卡**：direction 大字、score、up_prob、expected_ret、confidence、证据/风险要点、历史相似日统计。醒目的颜色区分偏多（红）/偏空（绿）/中性（灰）。
  2. **持仓总览**：中信期货多/空/净持仓（分品种横向对比 4 个卡片）、净持仓占市场持仓比、今日分位、日变化。
  3. **拟合分析**：净持仓与指数双轴折线；预测因子 vs 次日收益散点 + 回归线；滚动相关（60日）；领先滞后柱状图；年度稳定性表。
  4. **分位胜率**：5 个分位组的分组柱状图（次日平均收益 + 胜率双轴），含 n 和 p 值表格。
  5. **回测**：净值曲线 vs 指数买入持有、指标卡（年化/最大回撤/夏普/胜率/交易次数/仓位暴露）、及免责声明。
  6. **会员龙虎榜**：最新交易日前20多空持仓表，中信期货高亮；`/api/members` 的会员对比表。
- 所有数字格式化（持仓用千分位，百分比 2 位，None 显示 "—"）。
- 无数据时显示友好空状态提示（"请先点击「立即更新数据」"）。
- 响应式：≥1200px 三列，≤900px 单列。
- 代码结构：`index.html` + `styles.css` + `app.js`（可用 `frontend/js/*.js` 拆多个模块，但不要用打包器）。禁止 CDN 依赖。

## 8. 自测与交付

- 每个模块自带 `tests/test_*.py`，用 `$PY -m unittest` 或直接 `python tests/test_x.py` 可跑，**不依赖网络**（用 fake/内存 DB）。
- data-engine 交付前必须真正跑完一次全量同步，并在 `reports/data_sync.md` 写：日期范围、各表行数、失败日期清单、中信期货最新一日四品种数字（便于交叉核对）。
- analytics-engine 交付前必须在真实库上跑通四品种 × 三个窗口，`reports/analytics.md` 写清结论：哪个因子有预测力、IC 多少、是否显著、回测表现。
- 报告里所有结论都要带数字，不要写"效果较好"这种空话。
