"""
国家统计局（NBS）数据爬虫 —— 新版 API

新版 Base URL: https://data.stats.gov.cn/dg/website/publicrelease/web/external
无需 Cookie / Token，纯 urllib 实现（无 requests 依赖）。

A. 宏观指标（月度，NBS_INDICATORS，5 项）：
  1. industrial_yoy    — 规上工业增加值同比增长 (%)
  2. manufacturing_pmi — 制造业采购经理指数 (%)
  3. non_mfg_pmi       — 非制造业商务活动指数 (%)
  4. composite_pmi     — 综合PMI产出指数 (%)

B. 工业企业按行业营业利润累计增长（月度，INDUSTRY_PROFIT_CID，45 项）：
  全行业汇总 + 采矿业 + 各细分行业（共 45 个 indic_id，同一 cid 批量请求）

公开接口：
  NBSCrawler.fetch_latest()                → dict  最新一期数据
  NBSCrawler.fetch_range(start, end)       → list[dict]  历史区间数据
  NBSCrawler.fetch_profit_range(start,end) → list[dict]  行业利润历史区间数据
  NBSCrawler.fetch_and_store(engine, ...)  → int   写入 PostgreSQL（含行业利润）
  NBSCrawler.build_prompt_context(data)    → str   LLM Prompt 上下文
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API 基础配置
# ---------------------------------------------------------------------------

BASE_URL   = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
ROOT_MONTH = "fc982599aa684be7969d7b90b1bd0e84"   # 月度数据根节点 ID

NBS_INDICATORS: list[dict] = [
    {
        "key":   "industrial_yoy",
        "label": "规上工业增加值同比增长(%)",
        "cid":   "3f2e14f0542348ed9fe02476eca3450b",
        "indic": "ef1b1765960d45a29b4d7c4ca91be916",
        "unit":  "%",
    },
    {
        "key":   "manufacturing_pmi",
        "label": "制造业采购经理指数(%)",
        "cid":   "93ffbb1aa85740d3aa2618371508b606",
        "indic": "a09aa989bdcf4cffa2021795722eb916",
        "unit":  "%",
    },
    {
        "key":   "non_mfg_pmi",
        "label": "非制造业商务活动指数(%)",
        "cid":   "7a64a6e25aec4a8e9dde044ecd9e2cce",
        "indic": "88a150208f6e4a1db8babe41ae700f66",
        "unit":  "%",
    },
    {
        "key":   "composite_pmi",
        "label": "综合PMI产出指数(%)",
        "cid":   "455378e1c3264a32875768a35ba5de76",
        "indic": "55cdc89fa122446aa263912bdf14a540",
        "unit":  "%",
    },
    # CPI：NBS 按时间段分 cid，2021-2025 和 2026+ 各一个桶，相同 key 合并
    # 值为"上年同月=100"，同比增长 = value - 100 (%)
    {
        "key":   "cpi_yoy",
        "label": "居民消费价格指数(上年同月=100)",
        "cid":   "809d2522b0fe4be89142650341b19083",   # 2021-2025 桶
        "indic": "4ae9047687934a6390984c21d6ddab96",
        "unit":  "上年同月=100",
    },
    {
        "key":   "cpi_yoy",
        "label": "居民消费价格指数(上年同月=100)",
        "cid":   "5c7452825c7c4dcba391db5ca7f335c5",   # 2026- 桶
        "indic": "53180dfb9c14411ba4b762307c85920c",
        "unit":  "上年同月=100",
    },
    # PPI：工业生产者出厂价格指数（上年同月=100）
    # 同比增长 = value - 100 (%)
    {
        "key":   "ppi_yoy",
        "label": "工业生产者出厂价格指数(上年同月=100)",
        "cid":   "60e8b361f11c4a878c652a6487a25561",
        "indic": "150633e52b9a470a9a9fd1b296dd6c5b",
        "unit":  "上年同月=100",
    },
]

# ---------------------------------------------------------------------------
# 工业企业按行业营业利润累计增长（45 个行业，同一 cid 批量请求）
# cid 来自：按行业分工业企业主要经济指标 (2018-至今) → 工业企业营业利润
# ---------------------------------------------------------------------------

INDUSTRY_PROFIT_CID = "bccae2664cb84c518064089feeaebc15"

# (key, 中文标签, indic_id)
INDUSTRY_PROFIT_INDICS: list[tuple[str, str, str]] = [
    ("profit_total",           "工业企业营业利润累计增长(%)",                         "871e7e2a97394ddfadad8f4a3a0e9bb3"),
    ("profit_mining",          "采矿业营业利润累计增长(%)",                           "85151f54aa4342a8931a737956e7f271"),
    ("profit_coal",            "煤炭开采和洗选业营业利润累计增长(%)",                      "1f0e2e6497074accb449440af80d7c51"),
    ("profit_oil_gas",         "石油和天然气开采业营业利润累计增长(%)",                     "862edcb0f4ed47859b57b62f3718a9aa"),
    ("profit_ferrous_mining",  "黑色金属矿采选业营业利润累计增长(%)",                      "1dadf959fea64822af32352767b1f91a"),
    ("profit_nonferrous_mining","有色金属矿采选业营业利润累计增长(%)",                     "65c185982d4048128698ea97b7dd8db3"),
    ("profit_nonmetal_mining", "非金属矿采选业营业利润累计增长(%)",                       "f89fa3f16f5e43e79b285a751b8be4d5"),
    ("profit_mining_support",  "开采专业及辅助性活动营业利润累计增长(%)",                    "4806169574a24526a5e3a1c341f80e1c"),
    ("profit_other_mining",    "其他采矿业营业利润累计增长(%)",                          "6d7c741849a040e5a19551f589c02f7b"),
    ("profit_manufacturing",   "制造业营业利润累计增长(%)",                            "3bf97371ed9c4b7d8258fb92e35b8f5f"),
    ("profit_agri_food",       "农副食品加工业营业利润累计增长(%)",                       "62bb2e138bc24baf869ee2f5f91f81e1"),
    ("profit_food_mfg",        "食品制造业营业利润累计增长(%)",                          "94d4112db74f4fcbaba4078915c5913c"),
    ("profit_beverage",        "酒、饮料和精制茶制造业营业利润累计增长(%)",                   "fc9182bc6d3b40ad83619d646d82be23"),
    ("profit_tobacco",         "烟草制品业营业利润累计增长(%)",                          "b65886494bfd44d99f63328872c26193"),
    ("profit_textile",         "纺织业营业利润累计增长(%)",                            "cfe15f08fb394fdf8d05b53254cf08e8"),
    ("profit_apparel",         "纺织服装、服饰业营业利润累计增长(%)",                      "a8e56d02e0fb424086c70b082754e1b7"),
    ("profit_leather",         "皮革、毛皮、羽毛及其制品和制鞋业营业利润累计增长(%)",             "360558252c044db190ce444547ea3e20"),
    ("profit_wood",            "木材加工和木、竹、藤、棕、草制品业营业利润累计增长(%)",           "9e7dd5495cb2410d8d73a13504b7d952"),
    ("profit_furniture",       "家具制造业营业利润累计增长(%)",                          "98d6d51ef92a454ba45cecf9501836a1"),
    ("profit_paper",           "造纸和纸制品业营业利润累计增长(%)",                       "77a66677a5cc412dabfd40ab96a0782e"),
    ("profit_printing",        "印刷和记录媒介复制业营业利润累计增长(%)",                    "7436dfe4f7ec44b5beabcd91f211b93f"),
    ("profit_culture_sports",  "文教、工美、体育和娱乐用品制造业营业利润累计增长(%)",             "89ad8028e0344adda01f3a5c38237591"),
    ("profit_petro_coal",      "石油、煤炭及其他燃料加工业营业利润累计增长(%)",                 "4a7486a7c7044f3f81c6c59e6bf1308c"),
    ("profit_chemicals",       "化学原料和化学制品制造业营业利润累计增长(%)",                  "ee34ccc9ff99482daa4bb351af1bb9e5"),
    ("profit_pharma",          "医药制造业营业利润累计增长(%)",                          "b201c25e92634363ab05942fed923ba4"),
    ("profit_chem_fiber",      "化学纤维制造业营业利润累计增长(%)",                        "6b1d5659ff4d44ec8ba56c99ccddb0b1"),
    ("profit_rubber_plastic",  "橡胶和塑料制品业营业利润累计增长(%)",                      "c4f7fbea1c8144cab42030c820b1d24b"),
    ("profit_nonmetal_prod",   "非金属矿物制品业营业利润累计增长(%)",                      "de9f441f3da64be78830531e1f875bef"),
    ("profit_ferrous_metal",   "黑色金属冶炼和压延加工业营业利润累计增长(%)",                  "873d1c2145744a398aa2032b4cc40b32"),
    ("profit_nonferrous_metal","有色金属冶炼和压延加工业营业利润累计增长(%)",                  "a5bb205fb2f0468b9309177782865c1c"),
    ("profit_metal_prod",      "金属制品业营业利润累计增长(%)",                          "8a466794c6f74c419025309afc8b37f4"),
    ("profit_general_equip",   "通用设备制造业营业利润累计增长(%)",                        "7acdf33f9d704f14b566fab3243c5a6d"),
    ("profit_special_equip",   "专用设备制造业营业利润累计增长(%)",                        "4e161ad15446446788a3823fb1c85a61"),
    ("profit_auto",            "汽车制造业营业利润累计增长(%)",                          "c77fa101b2984e7baa3e84a1adb8274d"),
    ("profit_transport_equip", "铁路、船舶、航空航天和其他运输设备制造业营业利润累计增长(%)",        "f0cf37b034b34c5e9296597066058fd2"),
    ("profit_elec_mach",       "电气机械和器材制造业营业利润累计增长(%)",                    "e02196621307454a8c2583f3aca313df"),
    ("profit_computer_elec",   "计算机、通信和其他电子设备制造业营业利润累计增长(%)",             "515f6629a0244bd5bbbb8d1c3570f8b5"),
    ("profit_instruments",     "仪器仪表制造业营业利润累计增长(%)",                        "60ffcf1290c2417484193c1545302335"),
    ("profit_other_mfg",       "其他制造业营业利润累计增长(%)",                          "47e7304c028548db903f4868c9ccf9c2"),
    ("profit_recycling",       "废弃资源综合利用业营业利润累计增长(%)",                     "1bce37a5a7454d179fcc4346e33425b4"),
    ("profit_metal_repair",    "金属制品、机械和设备修理业营业利润累计增长(%)",                 "8fd8f21558c4478c93c42c32c796596a"),
    ("profit_elec_gas_water",  "电力、热力、燃气及水生产和供应业营业利润累计增长(%)",             "7094578e4ef649b8afe8bb1b966afde3"),
    ("profit_elec_heat",       "电力、热力生产和供应业营业利润累计增长(%)",                   "fff72d4a32b34a3aa4e9555d5c09f097"),
    ("profit_gas",             "燃气生产和供应业营业利润累计增长(%)",                      "7d651fb336314703aabdc28d9da649ec"),
    ("profit_water",           "水的生产和供应业营业利润累计增长(%)",                      "bc0ea0b0cdbe4e7b89caed9427992116"),
]

# 快速查找映射：indic_id → (key, label)
_PROFIT_INDIC_MAP: dict[str, tuple[str, str]] = {
    iid: (key, label) for key, label, iid in INDUSTRY_PROFIT_INDICS
}

# ---------------------------------------------------------------------------
# 低层 API 工具
# ---------------------------------------------------------------------------

def _api_post(body: dict, timeout: int = 20, retry: int = 3) -> dict:
    """调用 stream/esData POST 接口，带重试"""
    url  = f"{BASE_URL}/stream/esData"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    for attempt in range(1, retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                if raw.strip():
                    return json.loads(raw)
            return {}
        except Exception as e:
            logger.debug("NBS API 第 %d 次重试 (%s): %s", attempt, url, e)
            if attempt < retry:
                time.sleep(2 * attempt)
    raise RuntimeError(f"NBS API 连续 {retry} 次失败")


def _build_dts(start_ym: str, end_ym: str) -> list[str]:
    """
    生成 dts 时间区间列表（每次最多跨 24 个月，避免接口超时）。
    start_ym / end_ym 格式：YYYYMM
    """
    start = int(start_ym)
    end   = int(end_ym)

    def add_month(ym: int, n: int) -> int:
        y, m = divmod(ym, 100)
        m += n
        while m > 12:
            m -= 12
            y += 1
        return y * 100 + m

    chunks: list[str] = []
    cur = start
    while cur <= end:
        chunk_end = min(add_month(cur, 23), end)
        chunks.append(f"{cur}MM-{chunk_end}MM")
        cur = add_month(chunk_end, 1)
    return chunks


def _parse_periods(periods: list[dict], indicator: dict) -> list[dict]:
    """将 stream/esData 返回的 periods 列表解析为标准记录列表"""
    results: list[dict] = []
    for p in periods:
        code   = p.get("code", "")     # e.g. "202501MM"
        name   = p.get("name", "")     # e.g. "2025年1月"
        values = p.get("values", [])
        if not values:
            continue
        raw_val = values[0].get("value", "")
        if raw_val == "" or raw_val is None:
            continue
        try:
            val = float(raw_val)
        except (ValueError, TypeError):
            val = None

        # 解析年月
        pure = code.replace("MM", "").replace("SS", "").replace("YY", "")
        year  = int(pure[:4]) if len(pure) >= 4 else 0
        month = int(pure[4:6]) if len(pure) >= 6 else 0

        results.append({
            "indicator_key":  indicator["key"],
            "indicator_name": indicator["label"],
            "stat_code":      code,
            "stat_period":    name,
            "stat_year":      year,
            "stat_month":     month,
            "value":          val,
            "unit":           indicator["unit"],
        })
    return results


def _fetch_indicator_range(
    indicator: dict,
    start_ym: str,
    end_ym:   str | None,
    interval: float = 0.3,
) -> list[dict]:
    """获取单个指标在 [start_ym, end_ym] 区间的全部数据"""
    if end_ym is None:
        end_ym = date.today().strftime("%Y%m")
    all_records: list[dict] = []
    for dts_chunk in _build_dts(start_ym, end_ym):
        body = {
            "cid":          indicator["cid"],
            "indicatorIds": [indicator["indic"]],
            "das":          [{"text": "全国", "value": "000000000000"}],
            "dts":          [dts_chunk],
            "showType":     "1",
            "rootId":       ROOT_MONTH,
        }
        result   = _api_post(body)
        periods  = result.get("data", [])
        records  = _parse_periods(periods, indicator)
        all_records.extend(records)
        time.sleep(interval)
    return all_records


def _fetch_industry_profit_range(
    start_ym:  str,
    end_ym:    str,
    interval:  float = 0.5,
    batch_size: int  = 20,
) -> list[dict]:
    """
    批量获取所有行业营业利润累计增长数据（45 个 indic_id，同一 cid）。
    每次请求最多 batch_size 个 indic_id，避免超时。
    """
    all_ids    = [iid for _, _, iid in INDUSTRY_PROFIT_INDICS]
    all_records: list[dict] = []

    for dts_chunk in _build_dts(start_ym, end_ym):
        # 分批请求
        for i in range(0, len(all_ids), batch_size):
            batch_ids = all_ids[i: i + batch_size]
            body = {
                "cid":          INDUSTRY_PROFIT_CID,
                "indicatorIds": batch_ids,
                "das":          [{"text": "全国", "value": "000000000000"}],
                "dts":          [dts_chunk],
                "showType":     "1",
                "rootId":       ROOT_MONTH,
            }
            result  = _api_post(body)
            periods = result.get("data", [])

            # 解析：每个 period 下有多个 values（每个 value 对应一个 indic_id）
            for p in periods:
                code = p.get("code", "")
                name = p.get("name", "")
                pure = code.replace("MM", "").replace("SS", "").replace("YY", "")
                year  = int(pure[:4]) if len(pure) >= 4 else 0
                month = int(pure[4:6]) if len(pure) >= 6 else 0

                for val in p.get("values", []):
                    iid     = val.get("_id", "")
                    raw_val = val.get("value", "")
                    if raw_val == "" or raw_val is None:
                        continue
                    key_label = _PROFIT_INDIC_MAP.get(iid)
                    if key_label is None:
                        continue
                    key, label = key_label
                    try:
                        v = float(raw_val)
                    except (ValueError, TypeError):
                        v = None
                    all_records.append({
                        "indicator_key":  key,
                        "indicator_name": label,
                        "stat_code":      code,
                        "stat_period":    name,
                        "stat_year":      year,
                        "stat_month":     month,
                        "value":          v,
                        "unit":           "%",
                    })

            time.sleep(interval)

    logger.info(
        "[NBSCrawler] 行业利润 fetch_range(%s~%s): %d 条",
        start_ym, end_ym, len(all_records),
    )
    return all_records


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class NBSCrawler:
    """
    国家统计局宏观数据爬虫（板块研究平台专用）

    用法示例：
        crawler = NBSCrawler()

        # 获取最新数据（最近 6 期）
        data = crawler.fetch_latest(n=6)

        # 获取历史区间（回填用）
        rows = crawler.fetch_range("202501", "202506")

        # 获取并写入 PostgreSQL
        from sector_heat.db import get_engine
        n = crawler.fetch_and_store(get_engine(), "202501", "202506")

        # 构建 LLM Prompt 上下文
        ctx = crawler.build_prompt_context(data)
    """

    def __init__(
        self,
        timeout:  int   = 20,
        interval: float = 0.5,
        retry:    int   = 3,
    ):
        self.timeout  = timeout
        self.interval = interval
        self.retry    = retry

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def fetch_latest(self, n: int = 6) -> dict[str, Any]:
        """
        获取所有指标的最新 n 期数据。
        返回：
          {
            "macro_indicators": {key: [{"stat_period": ..., "value": ..., ...}]},
            "fetch_time": "YYYY-MM-DD HH:MM:SS",
            "errors": [...],
          }
        """
        # 取近 n+2 个月的数据（保证至少有 n 期）
        today    = date.today()
        end_ym   = today.strftime("%Y%m")
        # 往前推 n+3 个月
        y, m = divmod(today.month - (n + 3), 12)
        start_y  = today.year + y
        start_m  = m + 1 if m >= 0 else m + 13
        start_ym = f"{start_y:04d}{start_m:02d}"

        rows = self.fetch_range(start_ym, end_ym)
        return self._rows_to_result(rows, n)

    def fetch_range(
        self,
        start_ym: str,
        end_ym:   str | None = None,
    ) -> list[dict]:
        """
        获取宏观指标（NBS_INDICATORS，不含行业利润明细）在 [start_ym, end_ym] 区间的数据。
        start_ym / end_ym 格式：YYYYMM，end_ym 默认为当前月。
        返回 list[dict]，每条包含标准字段。
        如需同时获取行业利润，请调用 fetch_all_range() 或 fetch_profit_range()。
        """
        if end_ym is None:
            end_ym = date.today().strftime("%Y%m")

        all_records: list[dict] = []
        for ind in NBS_INDICATORS:
            try:
                records = _fetch_indicator_range(
                    ind, start_ym, end_ym, interval=self.interval
                )
                all_records.extend(records)
                logger.info(
                    "[NBSCrawler] %s: 获取 %d 条 (%s~%s)",
                    ind["key"], len(records), start_ym, end_ym,
                )
            except Exception as e:
                logger.warning("[NBSCrawler] %s 失败: %s", ind["key"], e)
            time.sleep(self.interval)

        return all_records

    def fetch_profit_range(
        self,
        start_ym: str,
        end_ym:   str | None = None,
    ) -> list[dict]:
        """
        获取所有行业营业利润累计增长（45 项）在 [start_ym, end_ym] 区间的数据。
        start_ym / end_ym 格式：YYYYMM，end_ym 默认为当前月。
        """
        if end_ym is None:
            end_ym = date.today().strftime("%Y%m")
        return _fetch_industry_profit_range(
            start_ym, end_ym, interval=self.interval
        )

    def fetch_all_range(
        self,
        start_ym: str,
        end_ym:   str | None = None,
    ) -> list[dict]:
        """
        获取宏观指标 + 行业利润，合并返回。
        """
        macro_rows  = self.fetch_range(start_ym, end_ym)
        profit_rows = self.fetch_profit_range(start_ym, end_ym)
        return macro_rows + profit_rows

    def fetch_and_store(
        self,
        engine,
        start_ym:       str,
        end_ym:         str | None = None,
        include_profit: bool = True,
    ) -> int:
        """
        获取数据并写入 PostgreSQL（使用 macro_db.py 中的 upsert）。
        include_profit=True 时同时获取行业利润明细（45 项）。
        返回写入条数。
        """
        from skills.macro_db import init_macro_table, upsert_nbs_records

        init_macro_table(engine)

        if include_profit:
            rows = self.fetch_all_range(start_ym, end_ym)
        else:
            rows = self.fetch_range(start_ym, end_ym)

        if not rows:
            logger.warning("[NBSCrawler] 未获取到任何数据")
            return 0

        n = upsert_nbs_records(engine, rows)
        logger.info("[NBSCrawler] 写入 %d 条宏观数据（含行业利润=%s）", n, include_profit)
        return n

    def fetch_task(
        self,
        task_name:     str,
        schedules_cfg: dict,
        engine        = None,
        store:         bool = True,
    ) -> tuple[list[dict], str]:
        """
        按 config/schedules.yaml 中 nbs_tasks.<task_name> 的定义拉取数据。

        参数：
            task_name:     schedules.yaml nbs_tasks 中的任务名，如 "cpi_ppi"
            schedules_cfg: 已加载的 schedules.yaml 内容字典
            engine:        SQLAlchemy Engine，store=True 时必传
            store:         True = 同时写入 macro_nbs_data 表

        返回：
            (records, data_version) — records 是拉取到的所有记录列表，
            data_version 是写入时间戳字符串（用于触发 MacroAgent）
        """
        task_cfg = schedules_cfg.get("nbs_tasks", {}).get(task_name, {})
        if not task_cfg:
            raise ValueError(f"schedules.yaml 中未找到 nbs_tasks.{task_name}")

        end_ym   = date.today().strftime("%Y%m")
        # 默认从当月往前推 3 个月（通常够用；可根据需要扩展为配置项）
        today    = date.today()
        y, m     = divmod(today.month - 4, 12)
        start_ym = f"{today.year + y:04d}{m + 1:02d}"

        all_rows: list[dict] = []

        # 判断任务类型
        if task_cfg.get("indicators_all_profit"):
            # 全部行业利润
            rows = _fetch_industry_profit_range(start_ym, end_ym, interval=self.interval)
            all_rows.extend(rows)
            logger.info("[NBSCrawler.fetch_task] %s (all_profit): %d 条", task_name, len(rows))
        else:
            # 按 indicators 列表拉取
            indicator_keys = task_cfg.get("indicators", [])
            target_inds    = [ind for ind in NBS_INDICATORS if ind["key"] in indicator_keys]
            for ind in target_inds:
                try:
                    rows = _fetch_indicator_range(ind, start_ym, end_ym, interval=self.interval)
                    all_rows.extend(rows)
                    logger.info("[NBSCrawler.fetch_task] %s/%s: %d 条", task_name, ind["key"], len(rows))
                except Exception as e:
                    logger.warning("[NBSCrawler.fetch_task] %s/%s 失败: %s", task_name, ind["key"], e)
                time.sleep(self.interval)

        # 写入 DB
        data_version = ""
        if store and engine is not None and all_rows:
            from skills.macro_db import init_macro_table, upsert_nbs_records
            from datetime import datetime as _dt
            init_macro_table(engine)
            n            = upsert_nbs_records(engine, all_rows)
            data_version = _dt.now().isoformat()
            logger.info("[NBSCrawler.fetch_task] %s 写入 %d 条，data_version=%s", task_name, n, data_version[:19])

        return all_rows, data_version

    def build_prompt_context(self, data: dict[str, Any], n_periods: int = 3) -> str:
        """
        将 fetch_latest() / fetch_range() 的结果格式化为 LLM Prompt 上下文。
        data 格式兼容 fetch_latest() 返回值，也兼容旧的 macro_indicators 结构。
        """
        macro = data.get("macro_indicators", {})
        if not macro:
            # 也兼容直接传入 list[dict] 的情况
            rows = data if isinstance(data, list) else []
            if rows:
                macro = self._rows_to_result(rows).get("macro_indicators", {})

        fetch_time = data.get("fetch_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        lines: list[str] = [f"=== 宏观数据快照（{fetch_time}）==="]

        lines.append("\n【统计局核心指标（最近3期）】")
        for ind in NBS_INDICATORS:
            key    = ind["key"]
            label  = ind["label"]
            series = macro.get(key, [])
            if series:
                recent = series[:n_periods]
                parts  = "  /  ".join(
                    f"{r.get('stat_period', r.get('period', '?'))}={r.get('value', '?')}"
                    for r in recent
                )
                lines.append(f"  {label}：{parts}")
            else:
                lines.append(f"  {label}：暂无数据")

        # 市场指数（可选，由调用方注入）
        indices = data.get("market_indices", {})
        if indices:
            lines.append("\n【A 股主要指数（最新收盘）】")
            for name, info in indices.items():
                close   = info.get("close",   "N/A")
                pct_chg = info.get("pct_chg", "N/A")
                lines.append(f"  {name}：收盘 {close}  涨跌幅 {pct_chg}%")

        # 宏观新闻（可选，由调用方注入）
        news = data.get("macro_news", [])
        if news:
            lines.append("\n【近期宏观新闻摘要（前10条）】")
            for item in news[:10]:
                lines.append(f"  [{item.get('date', '')}] {item.get('title', '')}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # A 股指数（保留原有功能，不影响 MacroAgent）
    # ------------------------------------------------------------------

    def fetch_market_indices(self) -> dict[str, dict]:
        """用 AkShare 获取主要 A 股指数最新收盘价和涨跌幅"""
        try:
            import akshare as ak
        except ImportError:
            return {}

        index_map = {
            "上证指数": "sh000001",
            "深证成指": "sz399001",
            "创业板指": "sz399006",
            "沪深300":  "sh000300",
        }
        result: dict[str, dict] = {}
        for name, code in index_map.items():
            try:
                df = ak.stock_zh_index_daily(symbol=code)
                if df is not None and not df.empty:
                    last = df.iloc[-1]
                    prev = df.iloc[-2] if len(df) > 1 else last
                    pct  = round((float(last["close"]) / float(prev["close"]) - 1) * 100, 2)
                    result[name] = {
                        "close":   round(float(last["close"]), 2),
                        "pct_chg": pct,
                        "date":    str(last.name)[:10],
                    }
            except Exception as e:
                logger.debug("指数 %s 获取失败: %s", name, e)
        return result

    def fetch_macro_news(self, n: int = 15) -> list[dict]:
        """用 AkShare 获取宏观相关新闻（过滤财经/宏观关键词）"""
        try:
            import akshare as ak
        except ImportError:
            return []

        keywords = [
            "GDP", "PMI", "CPI", "PPI", "货币", "财政", "央行", "利率",
            "宏观", "政策", "经济", "贸易", "通胀", "就业", "降准", "降息",
        ]
        try:
            df = ak.stock_info_global_em()
            if df is None or df.empty:
                return []
            title_col = next(
                (c for c in df.columns if "标题" in c or "title" in c.lower()), None
            )
            date_col = next(
                (c for c in df.columns if "时间" in c or "date" in c.lower()), None
            )
            if title_col is None:
                return []

            results: list[dict] = []
            for _, row in df.iterrows():
                title = str(row.get(title_col, ""))
                if any(kw in title for kw in keywords):
                    results.append({
                        "title": title,
                        "date":  str(row.get(date_col, ""))[:10] if date_col else "",
                    })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.debug("宏观新闻获取失败: %s", e)
            return []

    def fetch_all(self) -> dict[str, Any]:
        """
        一次性获取宏观快照（兼容旧接口，不含行业利润明细）。
        返回供 MacroAgent 直接使用的 macro_indicators 结构。
        若需行业利润请调用 fetch_profit_range()。
        """
        result = self.fetch_latest(n=8)

        # 追加市场指数
        try:
            result["market_indices"] = self.fetch_market_indices()
        except Exception as e:
            logger.warning("市场指数获取失败: %s", e)
            result.setdefault("market_indices", {})

        # 追加宏观新闻
        try:
            result["macro_news"] = self.fetch_macro_news()
        except Exception as e:
            logger.warning("宏观新闻获取失败: %s", e)
            result.setdefault("macro_news", [])

        ok = sum(1 for v in result["macro_indicators"].values() if v)
        logger.info("NBS 数据采集完成：%d/%d 项成功", ok, len(NBS_INDICATORS))
        return result

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _rows_to_result(
        rows: list[dict],
        n: int = 8,
    ) -> dict[str, Any]:
        """将 list[dict] 转换为 macro_indicators 结构"""
        macro: dict[str, list[dict]] = {}
        for r in rows:
            key = r["indicator_key"]
            macro.setdefault(key, []).append(r)

        # 每个指标按 stat_code 降序，取前 n 期
        for key in macro:
            macro[key].sort(key=lambda x: x.get("stat_code", ""), reverse=True)
            macro[key] = macro[key][:n]

        return {
            "macro_indicators": macro,
            "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "errors": [],
            "market_indices": {},
            "macro_news": [],
        }
