"""Verified Huatai consolidated period financial fields; percentages are points."""
import re
from datetime import date

from app.data_providers.base import ProviderRequestError

FIELDS = {
    "income": ("利润表", {"营业收入": "revenue", "减:营业成本": "operating_cost",
        "净利润(含少数股东损益)": "net_income", "净利润(不含少数股东损益)": "net_income_attributable",
        "营业利润": "operating_profit", "利润总额": "total_profit", "所得税": "income_tax",
        "基本每股收益": "basic_eps", "减:管理费用": "admin_expense", "减:销售费用": "selling_expense",
        "减:财务费用": "financial_expense"}),
    "balance_sheet": ("资产负债表", {"资产总计": "total_assets", "负债合计": "total_liabilities",
        "股东权益合计(含少数股东权益)": "total_equity", "流动资产合计": "total_current_assets",
        "非流动资产合计": "total_non_current_assets", "货币资金": "cash_and_equivalents",
        "应收账款": "accounts_receivable"}),
    "cash_flow": ("现金流量表", {"经营活动产生的现金流量净额": "net_operating_cash_flow",
        "投资活动产生的现金流量净额": "net_investing_cash_flow",
        "筹资活动产生的现金流量净额": "net_financing_cash_flow",
        "现金及现金等价物净增加额": "net_cash_change",
        "购建固定资产、无形资产和其他长期资产支付的现金": "capex"}),
}
METRICS = {
    "盈利能力": {"加权平均净资产收益率": ("roe", "%"), "总资产净利润": ("roa", "%"),
        "销售毛利率": ("gross_margin", "%"), "销售净利率": ("net_margin", "%")},
    "偿债能力": {"资产负债率": ("debt_to_asset_ratio", "%")},
    "成长能力": {"营业收入同比增长率": ("revenue_yoy", "%"),
        "同比增长率-归属母公司股东的净利润": ("net_income_yoy", "%")},
    "每股指标": {"基本每股收益": ("eps_basic", "元/股"), "每股净资产": ("bps", "元/股")},
    "营运能力": {"存货周转率": ("inventory_turnover", "次")},
}


def _number(value, unit):
    if value is None or value in ("", "--"):
        return None
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)" + re.escape(unit), str(value))
    if not match:
        raise ProviderRequestError("华泰财务单位与已验证契约不符")
    return float(match[1])


def normalize_financial(symbol, table, payload):
    data = payload if table == "metrics" else payload.get("data", {})
    try:
        period = date.fromisoformat(data["报告期"])
        announced = date.fromisoformat(data["发布日期"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderRequestError("华泰财务缺少有效报告期或公告日期") from exc
    if announced < period:
        raise ProviderRequestError("华泰财务公告日期早于报告期")
    row = {"symbol": symbol, "period_end": period.isoformat(), "announce_date": announced.isoformat()}
    if table == "metrics":
        if data.get("报表范围") != "合并报表" or data.get("指标计算类型") != "报告期累计":
            raise ProviderRequestError("华泰指标不是合并报表累计口径")
        for group, fields in METRICS.items():
            for label, (field, unit) in fields.items():
                row[field] = _number(data.get(group, {}).get(label), unit)
    else:
        group, fields = FIELDS[table]
        records = data.get(group, [])
        if len(records) != 1:
            raise ProviderRequestError("华泰财务报表记录数不符合契约")
        for label, field in fields.items():
            row[field] = _number(records[0].get(label), "元")
    return row
