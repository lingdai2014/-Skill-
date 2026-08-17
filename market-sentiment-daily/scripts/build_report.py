# -*- coding: utf-8 -*-
"""
A股市场情绪日报生成器：龙虎榜 + 题材热点 + 行业轮动
数据来源：
  1. 东方财富 datacenter RPT_DAILYBILLBOARD_DETAILSNEW（全市场龙虎榜）
  2. 同花顺 zx.10jqka.com.cn/event/api/getharden/（强势股题材归因）
  3. 东方财富 push2 clist fs=m:90+t:2（行业板块排名）
输出：单文件 HTML（资源全内联，可离线打开，暗色盘面风，内联SVG图表）
用法：python build_report.py [--date YYYY-MM-DD] [--out output.html]
依赖：requests
"""
import argparse
import html as html_mod
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timedelta

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# ---------- 东财防封：统一节流入口 ----------
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
EM_MIN_INTERVAL = 1.0
_em_last_call = [0.0]


def em_get(url, params=None, headers=None, timeout=15, retries=3):
    """东财统一请求入口：节流 + 会话复用 + 失败重连重试"""
    last_err = None
    for attempt in range(retries):
        wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.5))
        try:
            return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            # 连接被重置等异常：换一个新 session 再试
            try:
                EM_SESSION.close()
            except Exception:
                pass
            globals()["EM_SESSION"] = requests.Session()
            EM_SESSION.headers.update({"User-Agent": UA})
            time.sleep(1.5 + attempt * 1.5 + random.uniform(0, 1))
        finally:
            _em_last_call[0] = time.time()
    raise last_err


def eastmoney_datacenter(report_name, filter_str="", page_size=500,
                         sort_columns="", sort_types="-1"):
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = em_get(DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ---------- 数据源 1：全市场龙虎榜 ----------
def fetch_dragon_tiger(trade_date):
    """返回 (stocks, actual_date)。stocks 按净买入降序。"""
    data = eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str="(TRADE_DATE>='%s')(TRADE_DATE<='%s')" % (trade_date, trade_date),
        page_size=500,
        sort_columns="BILLBOARD_NET_AMT", sort_types="-1",
    )
    if not data:
        return [], trade_date
    actual_date = str(data[0].get("TRADE_DATE", ""))[:10]
    stocks = []
    for row in data:
        stocks.append({
            "code": row.get("SECURITY_CODE", ""),
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "reason": row.get("EXPLANATION", "") or "",
            "close": row.get("CLOSE_PRICE") or 0,
            "change_pct": _f(row.get("CHANGE_RATE")),
            "net_buy_wan": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
            "buy_wan": round((row.get("BILLBOARD_BUY_AMT") or 0) / 10000, 1),
            "sell_wan": round((row.get("BILLBOARD_SELL_AMT") or 0) / 10000, 1),
            "turnover_pct": _f(row.get("TURNOVERRATE")),
        })
    return stocks, actual_date


def _f(v, default=0.0):
    if v is None or v == "" or v == "-":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


# ---------- 数据源 2：同花顺强势股题材归因 ----------
def fetch_ths_hot(date_str):
    """返回强势股列表。同花顺接口零鉴权。周末/节假日可能返回空。"""
    url = ("http://zx.10jqka.com.cn/event/api/getharden/"
           "date/%s/orderby/date/orderway/desc/charset/GBK/" % date_str)
    headers = {"User-Agent": UA}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
    except Exception as e:
        print("[WARN] 同花顺热点接口请求失败: %s" % e, file=sys.stderr)
        return []
    if data.get("errocode", 0) != 0:
        return []
    rows = data.get("data") or []
    out = []
    for row in rows:
        out.append({
            "code": row.get("code", ""),
            "name": row.get("name", ""),
            "reason": str(row.get("reason", "") or ""),
            "change_pct": _f(row.get("zhangfu")),
            "turnover_pct": _f(row.get("huanshou")),
        })
    return out


def theme_frequency(hot_stocks, top_n=None):
    """reason 按 + 拆词 → 词频统计 → [(tag, count)]，按词频降序全量返回"""
    tags = []
    for s in hot_stocks:
        for t in s["reason"].split("+"):
            t = t.strip()
            if t:
                tags.append(t)
    return Counter(tags).most_common(top_n)


# ---------- 数据源 3：东财行业板块排名 ----------
def fetch_industry():
    """返回行业列表，按涨跌幅降序。"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:90+t:2",
        "fid": "f3",
        "fields": "f2,f3,f4,f12,f14,f104,f105,f128,f136,f140",
    }
    r = em_get(url, params=params, headers={"User-Agent": UA}, timeout=15)
    d = r.json()
    items = (d.get("data") or {}).get("diff") or []
    if isinstance(items, dict):  # 东财有时返回 {"0": {...}, ...} 字典形式
        items = list(items.values())
    rows = []
    for item in items:
        rows.append({
            "name": item.get("f14", ""),
            "code": item.get("f12", ""),
            "change_pct": _f(item.get("f3")),
            "up_count": item.get("f104") or 0,
            "down_count": item.get("f105") or 0,
            "leader": item.get("f140", "") or "",
            "leader_change": _f(item.get("f136")),
        })
    return rows


# ---------- 交易日回溯 ----------
def resolve_trade_date(date_str):
    """从 date_str 向前最多回溯 5 天，找龙虎榜有数据的交易日。"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(6):
        cur = (d - timedelta(days=i)).strftime("%Y-%m-%d")
        stocks, actual = fetch_dragon_tiger(cur)
        if stocks:
            return stocks, actual
        print("[INFO] %s 龙虎榜无数据，向前回溯..." % cur, file=sys.stderr)
    return [], date_str


# ---------- 格式化 ----------
def fmt_money(wan):
    """万元 → ¥X亿 / ¥X万"""
    sign = "-" if wan < 0 else ""
    v = abs(wan)
    if v >= 10000:
        return "%s¥%.2f亿" % (sign, v / 10000)
    return "%s¥%.0f万" % (sign, v)


def esc(s):
    return html_mod.escape(str(s), quote=True)


# ---------- SVG 图表 ----------
def svg_bar_chart_theme(theme_freq, width=640, bar_h=24, gap=6):
    """题材热度条形图（横向，红色系，热度越深越亮）"""
    if not theme_freq:
        return "<p class='empty'>当日无题材数据</p>"
    max_v = max(c for _, c in theme_freq) or 1
    label_w, value_w = 150, 46
    chart_w = width - label_w - value_w - 16
    height = (bar_h + gap) * len(theme_freq) + 8
    parts = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" '
             'role="img" aria-label="题材热度条形图">' % (width, height)]
    for i, (tag, cnt) in enumerate(theme_freq):
        y = 4 + i * (bar_h + gap)
        bw = max(2, chart_w * cnt / max_v)
        alpha = 0.55 + 0.45 * (cnt / max_v)
        parts.append(
            '<text x="%d" y="%d" text-anchor="end" fill="#c9d1d9" '
            'font-size="13">%s</text>'
            '<rect x="%d" y="%d" width="%.1f" height="%d" rx="3" '
            'fill="#f0334b" fill-opacity="%.2f"/>'
            '<text x="%d" y="%d" fill="#e6edf3" font-size="13" '
            'font-weight="600">%d只</text>'
            % (label_w - 8, y + bar_h - 7, esc(tag),
               label_w, y, bw, bar_h, alpha,
               label_w + bw + 8, y + bar_h - 7, cnt))
    parts.append("</svg>")
    return "".join(parts)


def svg_bar_chart_lhb(stocks, top_n=20, width=640, bar_h=22, gap=5):
    """龙虎榜净买入 TOP N 横向条形图（正红负绿，双向）"""
    if not stocks:
        return "<p class='empty'>当日无龙虎榜数据</p>"
    sel = stocks[:top_n]
    max_v = max(abs(s["net_buy_wan"]) for s in sel) or 1
    label_w, value_w = 150, 86
    chart_w = width - label_w - value_w - 16
    height = (bar_h + gap) * len(sel) + 8
    parts = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" '
             'role="img" aria-label="龙虎榜净买入排名">' % (width, height)]
    for i, s in enumerate(sel):
        y = 4 + i * (bar_h + gap)
        v = s["net_buy_wan"]
        bw = max(2, chart_w * abs(v) / max_v)
        color = "#f0334b" if v >= 0 else "#00b578"
        parts.append(
            '<text x="%d" y="%d" text-anchor="end" fill="#c9d1d9" '
            'font-size="12.5">%s</text>'
            '<rect x="%d" y="%d" width="%.1f" height="%d" rx="3" fill="%s"/>'
            '<text x="%d" y="%d" fill="%s" font-size="12.5" '
            'font-weight="600">%s</text>'
            % (label_w - 8, y + bar_h - 6, esc(s["name"]),
               label_w, y, bw, bar_h, color,
               label_w + bw + 8, y + bar_h - 6, color, esc(fmt_money(v))))
    parts.append("</svg>")
    return "".join(parts)


def svg_bar_chart_industry(industries, top_n=10, width=640, bar_h=22, gap=5):
    """行业涨跌榜（前N涨 + 后N跌，红绿对称，中线零点）"""
    if not industries:
        return "<p class='empty'>当日无行业数据</p>"
    ups = industries[:top_n]
    downs = [r for r in industries[-top_n:] if r["change_pct"] < 0]
    downs.reverse()  # 跌得最少的排最上
    sel = ups + downs
    max_v = max(abs(r["change_pct"]) for r in sel) or 1
    label_w, value_w = 120, 70
    half = (width - label_w - value_w - 16) / 2.0
    zero_x = label_w + half
    height = (bar_h + gap) * len(sel) + 28
    parts = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" '
             'role="img" aria-label="行业涨跌榜">' % (width, height)]
    # 零轴
    parts.append('<line x1="%.1f" y1="4" x2="%.1f" y2="%d" stroke="#30363d" '
                 'stroke-width="1" stroke-dasharray="3,3"/>' % (zero_x, zero_x, height - 20))
    for i, r in enumerate(sel):
        y = 4 + i * (bar_h + gap)
        v = r["change_pct"]
        bw = max(1.5, half * abs(v) / max_v)
        if v >= 0:
            x, color = zero_x, "#f0334b"
            tx = zero_x + bw + 8
        else:
            x, color = zero_x - bw, "#00b578"
            tx = x + bw + 8
        parts.append(
            '<text x="%d" y="%d" text-anchor="end" fill="#c9d1d9" '
            'font-size="12.5">%s</text>'
            '<rect x="%.1f" y="%d" width="%.1f" height="%d" rx="3" fill="%s"/>'
            '<text x="%.1f" y="%d" fill="%s" font-size="12.5" '
            'font-weight="600">%+.2f%%</text>'
            % (label_w - 8, y + bar_h - 6, esc(r["name"]),
               x, y, bw, bar_h, color,
               tx, y + bar_h - 6, color, v))
    # 图例
    ly = height - 8
    parts.append(
        '<rect x="%d" y="%d" width="10" height="10" rx="2" fill="#f0334b"/>'
        '<text x="%d" y="%d" fill="#8b949e" font-size="11">涨幅 TOP%d</text>'
        '<rect x="%d" y="%d" width="10" height="10" rx="2" fill="#00b578"/>'
        '<text x="%d" y="%d" fill="#8b949e" font-size="11">跌幅 TOP%d</text>'
        % (label_w, ly - 9, label_w + 16, ly, top_n,
           label_w + 110, ly - 9, label_w + 126, ly, top_n))
    parts.append("</svg>")
    return "".join(parts)


# ---------- HTML 组装 ----------
CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0d1117; color:#e6edf3;
  font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",sans-serif;
  font-size:14px; line-height:1.6; }
.wrap { max-width:1200px; margin:0 auto; padding:28px 20px 48px; }
header.top { display:flex; align-items:baseline; justify-content:space-between;
  flex-wrap:wrap; gap:8px; border-bottom:2px solid #21262d; padding-bottom:14px; }
h1 { font-size:24px; font-weight:700; letter-spacing:1px; }
h1 .accent { color:#f0334b; }
.date-badge { background:#161b22; border:1px solid #30363d; border-radius:6px;
  padding:4px 12px; color:#8b949e; font-size:13px; }
.date-badge b { color:#e6edf3; font-size:15px; }
.subtitle { color:#8b949e; font-size:12.5px; margin-top:8px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:12px; margin:22px 0; }
.card { background:#161b22; border:1px solid #21262d; border-radius:10px;
  padding:16px 18px; position:relative; overflow:hidden; }
.card::before { content:""; position:absolute; left:0; top:0; bottom:0;
  width:3px; background:#f0334b; }
.card.green::before { background:#00b578; }
.card.gold::before { background:#d29922; }
.card.blue::before { background:#388bfd; }
.card .k { color:#8b949e; font-size:12.5px; }
.card .v { font-size:22px; font-weight:700; margin-top:4px; }
.card .v small { font-size:13px; font-weight:400; color:#8b949e; }
.card .sub { color:#8b949e; font-size:12px; margin-top:2px; }
section { background:#161b22; border:1px solid #21262d; border-radius:10px;
  padding:18px 20px; margin-top:18px; }
section h2 { font-size:17px; font-weight:700; margin-bottom:4px;
  display:flex; align-items:center; gap:8px; }
section h2 .dot { width:8px; height:8px; border-radius:2px;
  background:#f0334b; display:inline-block; }
section h2 .dot.g { background:#00b578; }
section h2 .dot.y { background:#d29922; }
section .desc { color:#8b949e; font-size:12.5px; margin-bottom:14px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
@media (max-width:900px) { .grid2 { grid-template-columns:1fr; } }
.chart-box { background:#0d1117; border:1px solid #21262d; border-radius:8px;
  padding:12px; overflow-x:auto; }
.chart-box svg { width:100%; height:auto; display:block; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { color:#8b949e; font-weight:500; text-align:left; padding:8px 10px;
  border-bottom:1px solid #30363d; white-space:nowrap;
  position:sticky; top:0; background:#161b22; }
td { padding:7px 10px; border-bottom:1px solid #21262d; }
tr:hover td { background:#1c2128; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.up { color:#f0334b; font-weight:600; }
.down { color:#00b578; font-weight:600; }
.tag { display:inline-block; background:#21262d; border-radius:4px;
  padding:1px 7px; margin:1px 3px 1px 0; font-size:12px; color:#c9d1d9; }
.tbl-scroll { max-height:520px; overflow-y:auto; border:1px solid #21262d;
  border-radius:8px; }
.tbl-scroll::-webkit-scrollbar { width:8px; height:8px; }
.tbl-scroll::-webkit-scrollbar-thumb { background:#30363d; border-radius:4px; }
.empty { color:#8b949e; padding:20px; text-align:center; }
footer { margin-top:28px; padding-top:16px; border-top:1px solid #21262d;
  color:#8b949e; font-size:12.5px; line-height:1.9; }
footer b { color:#c9d1d9; }
"""


def pct_span(v):
    cls = "up" if v >= 0 else "down"
    return '<span class="%s">%+.2f%%</span>' % (cls, v)


def build_lhb_table(stocks):
    rows = []
    for i, s in enumerate(stocks):
        reasons = "".join('<span class="tag">%s</span>' % esc(t)
                          for t in s["reason"].split("，") if t.strip())
        net_cls = "up" if s["net_buy_wan"] >= 0 else "down"
        rows.append(
            "<tr><td>%d</td><td>%s</td><td><b>%s</b></td>"
            "<td class='num'>%s</td><td class='num %s'>%s</td>"
            "<td class='num'>%s万</td><td class='num'>%s万</td>"
            "<td class='num'>%.2f%%</td><td>%s</td></tr>"
            % (i + 1, esc(s["code"]), esc(s["name"]),
               pct_span(s["change_pct"]),
               net_cls, esc(fmt_money(s["net_buy_wan"])),
               esc(fmt_money(s["buy_wan"]).replace("¥", "")),
               esc(fmt_money(s["sell_wan"]).replace("¥", "")),
               s["turnover_pct"], reasons))
    return ("<div class='tbl-scroll'><table><thead><tr>"
            "<th>#</th><th>代码</th><th>名称</th><th class='num'>涨跌幅</th>"
            "<th class='num'>净买入额</th><th class='num'>买入额</th>"
            "<th class='num'>卖出额</th><th class='num'>换手率</th>"
            "<th>上榜原因</th></tr></thead><tbody>%s</tbody></table></div>"
            % "".join(rows))


def build_industry_table(industries):
    rows = []
    for i, r in enumerate(industries):
        rows.append(
            "<tr><td>%d</td><td><b>%s</b></td><td class='num'>%s</td>"
            "<td class='num'>%d</td><td class='num'>%d</td>"
            "<td>%s <span class='num'>%s</span></td></tr>"
            % (i + 1, esc(r["name"]), pct_span(r["change_pct"]),
               r["up_count"], r["down_count"],
               esc(r["leader"]), pct_span(r["leader_change"])))
    return ("<div class='tbl-scroll'><table><thead><tr>"
            "<th>#</th><th>行业</th><th class='num'>涨跌幅</th>"
            "<th class='num'>上涨家数</th><th class='num'>下跌家数</th>"
            "<th>领涨股</th></tr></thead><tbody>%s</tbody></table></div>"
            % "".join(rows))


def build_hot_stock_table(hot_stocks, limit=40):
    rows = []
    for i, s in enumerate(hot_stocks[:limit]):
        tags = "".join('<span class="tag">%s</span>' % esc(t)
                       for t in s["reason"].split("+") if t.strip())
        rows.append(
            "<tr><td>%d</td><td>%s</td><td><b>%s</b></td>"
            "<td class='num'>%s</td><td class='num'>%.2f%%</td><td>%s</td></tr>"
            % (i + 1, esc(s["code"]), esc(s["name"]),
               pct_span(s["change_pct"]), s["turnover_pct"], tags))
    return ("<div class='tbl-scroll'><table><thead><tr>"
            "<th>#</th><th>代码</th><th>名称</th><th class='num'>涨幅</th>"
            "<th class='num'>换手率</th><th>题材归因</th></tr></thead>"
            "<tbody>%s</tbody></table></div>" % "".join(rows))


def render_html(trade_date, lhb, theme_freq, hot_stocks, industries, gen_time):
    total_net_wan = sum(s["net_buy_wan"] for s in lhb)
    total_net_yi = total_net_wan / 10000
    net_cls = "up" if total_net_wan >= 0 else "down"
    net_card = "" if total_net_wan >= 0 else "green"
    top_theme = theme_freq[0] if theme_freq else ("—", 0)
    top_theme2 = theme_freq[1] if len(theme_freq) > 1 else None
    top_theme3 = theme_freq[2] if len(theme_freq) > 2 else None
    lead = industries[0] if industries else None
    lag = industries[-1] if industries else None
    up_ind = sum(1 for r in industries if r["change_pct"] > 0)
    down_ind = sum(1 for r in industries if r["change_pct"] < 0)

    theme_sub = "、".join("%s(%d只)" % (t, c) for t, c in
                         [x for x in (top_theme2, top_theme3) if x])

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股市场情绪日报 · %(date)s</title>
<style>%(css)s</style>
</head>
<body>
<div class="wrap">
<header class="top">
  <div>
    <h1><span class="accent">A股</span>市场情绪日报</h1>
    <div class="subtitle">龙虎榜 · 题材热点 · 行业轮动 —— 短线情绪复盘</div>
  </div>
  <div class="date-badge">交易日 <b>%(date)s</b></div>
</header>

<div class="cards">
  <div class="card">
    <div class="k">龙虎榜上榜家数</div>
    <div class="v">%(n_lhb)d <small>只</small></div>
    <div class="sub">全市场当日上榜</div>
  </div>
  <div class="card %(net_card)s">
    <div class="k">龙虎榜净买入合计</div>
    <div class="v %(net_cls)s">%(total_net)s</div>
    <div class="sub">游资席位当日净动向</div>
  </div>
  <div class="card gold">
    <div class="k">最热题材</div>
    <div class="v" style="color:#d29922">%(top_theme)s <small>%(top_theme_n)d只</small></div>
    <div class="sub">%(theme_sub)s</div>
  </div>
  <div class="card blue">
    <div class="k">领涨行业</div>
    <div class="v" style="color:#388bfd">%(lead_name)s <small>%(lead_pct)s</small></div>
    <div class="sub">行业上涨 %(up_ind)d / 下跌 %(down_ind)d</div>
  </div>
</div>

<section>
  <h2><span class="dot"></span>题材热度 TOP15 <span style="font-size:12px;color:#8b949e;font-weight:400">同花顺强势股归因 · 共 %(n_hot)d 只强势股</span></h2>
  <div class="desc">对当日强势股的人工运营题材标签（reason）按「+」拆分后做词频统计</div>
  <div class="chart-box" style="margin-bottom:14px">%(svg_theme)s</div>
  %(hot_table)s</section>

<section>
  <h2><span class="dot g"></span>行业涨跌榜 <span style="font-size:12px;color:#8b949e;font-weight:400">东财行业板块 · 共 %(n_ind)d 个行业</span></h2>
  <div class="desc">领涨：%(lead_line)s ｜ 领跌：%(lag_line)s</div>
  <div class="grid2">
    <div class="chart-box">%(svg_ind)s</div>
    <div>%(ind_table)s</div>
  </div>
</section>

<section>
  <h2><span class="dot y"></span>龙虎榜净买入 TOP20 <span style="font-size:12px;color:#8b949e;font-weight:400">东财全市场龙虎榜</span></h2>
  <div class="desc">按当日龙虎榜净买入额降序</div>
  <div class="chart-box" style="margin-bottom:14px">%(svg_lhb)s</div>
  %(lhb_table)s
</section>

<footer>
  <b>数据来源：</b>东方财富数据中心·全市场龙虎榜（datacenter-web.eastmoney.com）｜
  同花顺热点·强势股题材归因（zx.10jqka.com.cn）｜东方财富·行业板块行情（push2.eastmoney.com）<br>
  <b>交易日期：</b>%(date)s ｜ <b>报告生成时间：</b>%(gen_time)s ｜
  涨跌幅单位 %%，金额单位 ¥（亿/万），红涨绿跌（A股惯例）<br>
  本报告由公开数据自动汇总生成，仅供复盘参考，不构成投资建议。市场有风险，投资需谨慎。
</footer>
</div>
</body>
</html>""" % {
        "date": esc(trade_date),
        "css": CSS,
        "n_lhb": len(lhb),
        "net_card": net_card,
        "net_cls": net_cls,
        "total_net": esc(fmt_money(total_net_wan)) if lhb else "—",
        "top_theme": esc(top_theme[0]),
        "top_theme_n": top_theme[1],
        "theme_sub": esc(theme_sub) if theme_sub else "当日强势股题材词频第一",
        "lead_name": esc(lead["name"]) if lead else "—",
        "lead_pct": ("%+.2f%%" % lead["change_pct"]) if lead else "",
        "up_ind": up_ind, "down_ind": down_ind,
        "n_hot": len(hot_stocks),
        "svg_theme": svg_bar_chart_theme(theme_freq[:15]),
        "hot_table": build_hot_stock_table(hot_stocks),
        "n_ind": len(industries),
        "lead_line": esc("%s %+.2f%%（领涨股 %s %+.2f%%）" % (
            lead["name"], lead["change_pct"], lead["leader"],
            lead["leader_change"])) if lead else "—",
        "lag_line": esc("%s %+.2f%%" % (lag["name"], lag["change_pct"])) if lag else "—",
        "svg_ind": svg_bar_chart_industry(industries),
        "ind_table": build_industry_table(industries),
        "svg_lhb": svg_bar_chart_lhb(lhb),
        "lhb_table": build_lhb_table(lhb) if lhb else "<p class='empty'>当日无龙虎榜数据</p>",
        "gen_time": esc(gen_time),
    }
    return html


def main():
    ap = argparse.ArgumentParser(description="A股市场情绪日报生成器")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                    help="交易日期 YYYY-MM-DD，默认今天（自动回溯最近有数据的交易日）")
    ap.add_argument("--out", default=None, help="输出 HTML 路径")
    args = ap.parse_args()

    print("[1/3] 抓取全市场龙虎榜（东财）...", file=sys.stderr)
    lhb, trade_date = resolve_trade_date(args.date)
    print("      交易日 %s，上榜 %d 只" % (trade_date, len(lhb)), file=sys.stderr)

    print("[2/3] 抓取强势股题材归因（同花顺）...", file=sys.stderr)
    hot_stocks = fetch_ths_hot(trade_date)
    if not hot_stocks and trade_date != args.date:
        hot_stocks = fetch_ths_hot(args.date)
    theme_freq = theme_frequency(hot_stocks)
    print("      强势股 %d 只，题材 %d 个" % (len(hot_stocks), len(theme_freq)),
          file=sys.stderr)

    print("[3/3] 抓取行业板块排名（东财）...", file=sys.stderr)
    try:
        industries = fetch_industry()
    except requests.RequestException as e:
        print("[WARN] 行业板块抓取失败: %s（行业部分将显示为空）" % e, file=sys.stderr)
        industries = []
    print("      行业 %d 个" % len(industries), file=sys.stderr)

    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = render_html(trade_date, lhb, theme_freq, hot_stocks, industries, gen_time)

    out = args.out or ("市场情绪日报-%s.html" % trade_date)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("OK -> %s (%.1f KB)" % (out, len(html.encode("utf-8")) / 1024),
          file=sys.stderr)

    # 摘要输出给调用方
    summary = {
        "trade_date": trade_date,
        "lhb_count": len(lhb),
        "lhb_net_yi": round(sum(s["net_buy_wan"] for s in lhb) / 10000, 2),
        "top_themes": theme_freq[:3],
        "lead_industry": [industries[0]["name"], industries[0]["change_pct"]] if industries else None,
        "lag_industry": [industries[-1]["name"], industries[-1]["change_pct"]] if industries else None,
        "out": out,
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
