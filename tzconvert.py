#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tzconvert.py —— 跨时区换算工具（纯 Python 标准库，单文件）

输入格式（行式文本；空行与 '#' 之后的内容忽略）：

  时区定义（无夏令时）:
      TZ <名称> <标准偏移>
  时区定义（有夏令时）:
      TZ <名称> <标准偏移> <夏令时偏移> <夏令时起> <夏令时止>
  换算请求:
      REQ <时间> <源时区> <目标时区>

  偏移格式: ±HH:MM           如 +08:00  -05:00
  起止格式: MM-DDTHH:MM      当地墙上时间，区间为左闭右开 [起, 止)
  时间格式: YYYY-MM-DDTHH:MM（也允许用空格分隔日期与时刻）

用法:
  python3 tzconvert.py 输入文件
  python3 tzconvert.py < 输入文件
  python3 tzconvert.py --demo       运行内置示例
  python3 tzconvert.py --selftest   运行内置自测
"""

from __future__ import annotations

import calendar
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
LOCAL_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
MD_TIME_RE = re.compile(r"^(\d{2})-(\d{2})T(\d{2}):(\d{2})$")

MAX_OFFSET_HOUR = 14  # 现实中时区偏移不超过 ±14:00


# ------------------------------------------------------------- 基础解析

def parse_offset(text):
    """把 ±HH:MM 解析为 timedelta。"""
    m = OFFSET_RE.match(text)
    if not m:
        raise ValueError("偏移格式非法: %r（应为 ±HH:MM）" % text)
    sign, hour, minute = m.group(1), int(m.group(2)), int(m.group(3))
    if hour > MAX_OFFSET_HOUR or minute > 59 or (hour == MAX_OFFSET_HOUR and minute):
        raise ValueError("偏移超出合法范围: %r" % text)
    delta = timedelta(hours=hour, minutes=minute)
    return delta if sign == "+" else -delta


def parse_mdtime(text):
    """把 MM-DDTHH:MM 解析为 (月, 日, 时, 分)；用闰年 2000 校验，允许 02-29。"""
    m = MD_TIME_RE.match(text)
    if not m:
        raise ValueError("夏令时起止格式非法: %r（应为 MM-DDTHH:MM）" % text)
    month, day, hour, minute = map(int, m.groups())
    try:
        datetime(2000, month, day, hour, minute)
    except ValueError:
        raise ValueError("夏令时起止日期不存在: %r" % text)
    return (month, day, hour, minute)


def parse_local_time(text):
    """把 YYYY-MM-DDTHH:MM 解析为 datetime；不存在的日期（如 02-30）在此拦截。"""
    if not LOCAL_TIME_RE.match(text):
        raise ValueError("时间格式非法: %r（应为 YYYY-MM-DDTHH:MM）" % text)
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValueError("时间格式非法（日期不存在）: %r" % text)


def fmt_offset(delta):
    total = int(delta.total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "%s%02d:%02d" % (sign, total // 60, total % 60)


def fmt_dt(moment):
    return moment.strftime("%Y-%m-%dT%H:%M")


# ------------------------------------------------------------- 时区模型

def mdt_in_year(year, mdt):
    """把 (月,日,时,分) 落到具体年份；02-29 在非闰年钳到 02-28。"""
    month, day, hour, minute = mdt
    day = min(day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, hour, minute)


@dataclass
class TimeZone:
    name: str
    std_offset: timedelta
    dst_offset: timedelta | None = None
    dst_start: tuple | None = None   # (月, 日, 时, 分)，当地墙上时间
    dst_end: tuple | None = None

    @property
    def has_dst(self):
        return self.dst_offset is not None

    def in_dst(self, local):
        """当地墙上时间是否落在夏令时区间 [start, end) 内。"""
        if not self.has_dst:
            return False
        start = mdt_in_year(local.year, self.dst_start)
        end = mdt_in_year(local.year, self.dst_end)
        return start <= local < end

    def offset_at_local(self, local):
        return self.dst_offset if self.in_dst(local) else self.std_offset


# ------------------------------------------------------------- 换算核心

@dataclass
class Conversion:
    source_local: datetime
    source_offset: timedelta
    source_dst: bool
    utc: datetime
    target_local: datetime
    target_offset: timedelta
    target_dst: bool


def convert(moment, source, target):
    """换算链：源当地时间 -> UTC 时刻 -> 目标当地时间。

    源端：直接用给定的当地墙上时间判定是否夏令时，求得 UTC 时刻。
    目标端：先按标准偏移试算目标墙上时间，再用它判定是否夏令时，
            最后取对应偏移得到目标当地时间（跨日由 datetime 算术自然处理）。
    """
    source_offset = source.offset_at_local(moment)
    utc = moment - source_offset
    tentative = utc + target.std_offset
    target_dst = target.in_dst(tentative)
    target_offset = target.dst_offset if target_dst else target.std_offset
    return Conversion(moment, source_offset, source.in_dst(moment),
                      utc, utc + target_offset, target_offset, target_dst)


# ------------------------------------------------------------- 文档解析

def parse_document(text):
    """第一遍解析：收集时区定义与原始请求，错误只记录不中断。"""
    zones = {}
    requests = []
    errors = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        keyword = parts[0].upper()
        if keyword == "TZ":
            zone = parse_timezone(parts, lineno, errors)
            if zone is None:
                continue
            if zone.name in zones:
                errors.append((lineno, "时区 %r 重复定义，保留首次定义，忽略本次" % zone.name))
                continue
            zones[zone.name] = zone
        elif keyword == "REQ":
            requests.append((lineno, parts[1:]))
        else:
            errors.append((lineno, "无法识别的指令: %r（应为 TZ 或 REQ）" % parts[0]))
    return zones, requests, errors


def parse_timezone(parts, lineno, errors):
    if len(parts) not in (3, 6):
        errors.append((lineno, "TZ 定义字段数非法（应为 3 或 6 个字段）"))
        return None
    name = parts[1]
    try:
        std = parse_offset(parts[2])
    except ValueError as exc:
        errors.append((lineno, "时区 %s: %s" % (name, exc)))
        return None
    if len(parts) == 3:
        return TimeZone(name, std)
    try:
        dst = parse_offset(parts[3])
        start = parse_mdtime(parts[4])
        end = parse_mdtime(parts[5])
    except ValueError as exc:
        errors.append((lineno, "时区 %s: %s" % (name, exc)))
        return None
    if mdt_in_year(2000, start) >= mdt_in_year(2000, end):
        errors.append((lineno,
                       "时区 %s: 夏令时起止倒挂（%s 不早于 %s），该时区按无夏令时处理"
                       % (name, parts[4], parts[5])))
        return TimeZone(name, std)
    return TimeZone(name, std, dst, start, end)


def parse_request(parts):
    if len(parts) == 3:
        time_text, src_name, dst_name = parts
    elif len(parts) == 4:  # 日期与时刻用空格分隔的写法
        time_text = parts[0] + "T" + parts[1]
        src_name, dst_name = parts[2], parts[3]
    else:
        raise ValueError("REQ 请求字段数非法（应为 时间 源时区 目标时区）")
    return parse_local_time(time_text), src_name, dst_name


def run(text):
    """第二遍处理：所有定义就绪后再执行请求（允许请求引用后定义的时区）。"""
    zones, raw_requests, errors = parse_document(text)
    results = []
    for lineno, parts in raw_requests:
        try:
            moment, src_name, dst_name = parse_request(parts)
        except ValueError as exc:
            errors.append((lineno, str(exc)))
            continue
        missing = [n for n in (src_name, dst_name) if n not in zones]
        if missing:
            errors.append((lineno, "时区未定义: %s" % ", ".join(missing)))
            continue
        results.append((lineno, moment, src_name, dst_name,
                        convert(moment, zones[src_name], zones[dst_name])))
    errors.sort(key=lambda item: item[0])
    return results, errors


# ------------------------------------------------------------- 输出

def render(results, errors):
    out = ["====== 换算结果 ======"]
    if not results:
        out.append("（无成功换算）")
    for lineno, moment, src_name, dst_name, conv in results:
        day_diff = (conv.target_local.date() - conv.source_local.date()).days
        cross = "同日" if day_diff == 0 else "跨日 %+d 天" % day_diff
        src_tag = "%s(%s%s)" % (src_name, fmt_offset(conv.source_offset),
                                " DST" if conv.source_dst else "")
        dst_tag = "%s(%s%s)" % (dst_name, fmt_offset(conv.target_offset),
                                " DST" if conv.target_dst else "")
        out.append("[行%-3d] %s %s -> UTC %s -> %s %s  [%s]" % (
            lineno, fmt_dt(moment), src_tag, fmt_dt(conv.utc),
            fmt_dt(conv.target_local), dst_tag, cross))
    out.append("")
    out.append("====== 错误清单 ======")
    if not errors:
        out.append("（无错误）")
    for lineno, message in errors:
        out.append("[行%-3d] %s" % (lineno, message))
    return "\n".join(out)


# ------------------------------------------------------------- 示例与自测

DEMO_INPUT = """\
# 时区定义
TZ UTC      +00:00
TZ Beijing  +08:00
TZ NewYork  -05:00 -04:00 03-08T02:00 11-01T02:00
TZ London   +00:00 +01:00 03-29T01:00 10-25T02:00
TZ BadDST   +08:00 +09:00 11-01T02:00 03-08T02:00   # 起止倒挂
TZ Beijing  +09:00                                  # 重复定义

# 换算请求
REQ 2026-07-01T12:00 Beijing NewYork     # 北京标准时 / 纽约夏令时
REQ 2026-01-01T12:00 Beijing NewYork     # 跨日：结果落在 2025-12-31
REQ 2026-03-08T09:00 Beijing NewYork     # 纽约夏令时切换日
REQ 2026-02-30T10:00 Beijing UTC         # 时间格式非法
REQ 2026-06-01T08:00 Beijing Atlantis    # 目标时区未定义
REQ 2026-06-01T08:00 BadDST UTC          # 倒挂时区按标准偏移换算
"""


def selftest():
    bj = TimeZone("Beijing", timedelta(hours=8))
    ny = TimeZone("NewYork", timedelta(hours=-5), timedelta(hours=-4),
                  (3, 8, 2, 0), (11, 1, 2, 0))
    utc = TimeZone("UTC", timedelta(0))

    # 1) 标准偏移换算
    conv = convert(datetime(2026, 1, 15, 12, 0), bj, utc)
    assert fmt_dt(conv.target_local) == "2026-01-15T04:00"

    # 2) 夏令时区间判定（左闭右开）
    assert not ny.in_dst(datetime(2026, 3, 8, 1, 59))
    assert ny.in_dst(datetime(2026, 3, 8, 2, 0))
    assert ny.in_dst(datetime(2026, 10, 31, 12, 0))
    assert not ny.in_dst(datetime(2026, 11, 1, 2, 0))

    # 3) 目标处于夏令时
    conv = convert(datetime(2026, 7, 1, 12, 0), bj, ny)
    assert (fmt_dt(conv.target_local), conv.target_dst) == ("2026-07-01T00:00", True)

    # 4) 跨日边界（向后、向前）
    conv = convert(datetime(2026, 1, 1, 12, 0), bj, ny)
    assert fmt_dt(conv.target_local) == "2025-12-31T23:00"
    conv = convert(datetime(2026, 1, 1, 23, 30), utc, bj)
    assert fmt_dt(conv.target_local) == "2026-01-02T07:30"

    # 5) 错误场景：重复定义 / 起止倒挂 / 时间非法 / 时区未定义
    results, errors = run("\n".join([
        "TZ UTC +00:00",
        "TZ UTC +01:00",                                        # 重复定义
        "TZ Weird +08:00 +09:00 11-01T02:00 03-08T02:00",       # 起止倒挂
        "REQ 2026-13-40T25:61 UTC UTC",                         # 时间格式非法
        "REQ 2026-01-01T00:00 UTC Nowhere",                     # 时区未定义
        "REQ 2026-06-01T08:00 Weird UTC",                       # 倒挂时区按标准偏移
    ]))
    report = "\n".join(msg for _, msg in errors)
    assert "重复定义" in report
    assert "倒挂" in report
    assert "时间格式非法" in report
    assert "时区未定义" in report
    assert len(results) == 1
    assert fmt_dt(results[0][4].target_local) == "2026-06-01T00:00"


def main(argv):
    args = argv[1:]
    if args and args[0] == "--selftest":
        selftest()
        print("selftest: all assertions passed")
        return 0
    if args and args[0] == "--demo":
        text = DEMO_INPUT
    elif args:
        with open(args[0], encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    results, errors = run(text)
    print(render(results, errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
