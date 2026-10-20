# -*- coding: utf-8 -*-
"""全局配置：数据源、品种映射、路径与服务参数。

所有模块通过 ``import config`` 读取本文件，避免魔法数字散落各处。
修改抓取区间 / 端口 / 跟踪会员只需改这里。
"""
from pathlib import Path

# ---------------------------------------------------------------- 路径
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "market.db"
REPORT_DIR = ROOT / "reports"
FRONTEND_DIR = ROOT / "frontend"
CACHE_DIR = DATA_DIR / "raw_cache"

# ---------------------------------------------------------------- 跟踪对象
# 主分析席位
DEFAULT_MEMBER = "中信期货"
# 页面"会员龙虎榜"里展示的重点会员（按需要增删）
TRACK_MEMBERS = [
    "中信期货", "国泰君安", "海通期货", "东证期货", "华泰期货",
    "银河期货", "中信建投", "南华期货", "永安期货", "国投期货",
]
# 会员名称归一化：去掉券商/交易所附加后缀，例如 "中信期货(代客)" / "中信期货（自营）"
MEMBER_SUFFIX_PATTERN = r"[（(][^）)]*[）)]$"

# ---------------------------------------------------------------- 品种与指数映射
VARIETIES = ["IF", "IH", "IC", "IM"]

VARIETY_INFO = {
    "IF": {
        "name": "沪深300股指期货", "index_code": "000300", "index_name": "沪深300",
        "multiplier": 300, "color": "#4d7cfe", "launch": "2010-04-16",
    },
    "IH": {
        "name": "上证50股指期货", "index_code": "000016", "index_name": "上证50",
        "multiplier": 300, "color": "#f2994a", "launch": "2015-04-16",
    },
    "IC": {
        "name": "中证500股指期货", "index_code": "000905", "index_name": "中证500",
        "multiplier": 200, "color": "#27ae60", "launch": "2015-04-16",
    },
    "IM": {
        "name": "中证1000股指期货", "index_code": "000852", "index_name": "中证1000",
        "multiplier": 200, "color": "#bb6bd9", "launch": "2022-07-22",
    },
}

INDEX_CODES = {
    "000300": {"name": "沪深300", "variety": "IF", "secid_em": "1.000300", "sina": "sh000300"},
    "000016": {"name": "上证50", "variety": "IH", "secid_em": "1.000016", "sina": "sh000016"},
    "000905": {"name": "中证500", "variety": "IC", "secid_em": "1.000905", "sina": "sh000905"},
    "000852": {"name": "中证1000", "variety": "IM", "secid_em": "1.000852", "sina": "sh000852"},
}

# ---------------------------------------------------------------- 数据源
# 中金所《成交持仓排名》每日分品种 CSV（GB2312/GBK 编码）
CFFEX_CCPM_URL = "http://www.cffex.com.cn/sj/ccpm/{yyyymm}/{dd}/{variety}_1.csv"
# 中金所日行情 CSV（GB2312/GBK 编码）：包含全部合约成交量、持仓量、结算价
CFFEX_DAILY_CSV_URL = "http://www.cffex.com.cn/sj/hqsj/rtj/{yyyymm}/{dd}/{yyyymmdd}_1.csv"
# 中金所日行情 XML（UTF-8）：备用
CFFEX_DAILY_XML_URL = "http://www.cffex.com.cn/sj/hqsj/rtj/{yyyymm}/{dd}/index.xml"

# 指数日线（备用源，按顺序降级）
EASTMONEY_KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&ut=fa5fd1943c7b386f172d6893dbfba10b"
    "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
    "&klt=101&fqt=0&beg=0&end=20500101&lmt=100000"
)
SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    "?symbol={sina}&scale=240&ma=no&datalen={n}"
)
SINA_KLINE_MAX = 1023
IFENG_KLINE_URL = "http://api.finance.ifeng.com/akdaily/?code={sina}&type=last"

# 抓取行为
SYNC_START = "2022-01-03"      # 抓取起始交易日（IM 于 2022-07 上市，留出对齐余量）
REQUEST_THREADS = 6            # 并发线程数（对中金所保持克制）
REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 4
REQUEST_SLEEP = 0.15           # 单请求最小间隔（秒）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 分析参数
DEFAULT_LOOKBACK = 720         # 默认分析窗口（交易日，约 3 年）
LOOKBACK_OPTIONS = [240, 480, 720, 0]   # 0 = 全部可用数据
ROLLING_CORR_WINDOW = 60       # 滚动相关性窗口
MAX_LAG = 3                    # 领先/滞后检验的最大阶数
N_BUCKETS = 5                  # 分位数组数
BACKTEST_WARMUP = 252          # 回测分位阈值所需的预热窗口
MAIN_CONTRACT_OI_RATIO = 0.0   # 主力合约判定：日内持仓量最大者（无需比例阈值）

# ---------------------------------------------------------------- 服务
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8788

# ---------------------------------------------------------------- 免责声明
DISCLAIMER = (
    "本网站仅基于公开的中金所持仓排名与指数行情做统计研究，展示的胜率、相关系数与回测结果"
    "均为历史统计特征，不构成任何投资建议。期货交易杠杆高、风险大，请独立判断并自负盈亏。"
)
