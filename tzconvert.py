#!/usr/bin/env python3
"""tzconvert.py — 跨时区换算工具（纯标准库，单文件）

输入格式（行式文本，# 开头为注释，空行忽略）：

    TZ  <名称> <标准偏移> <夏令时偏移> <夏令时起> <夏令时止>
        偏移: +HH:MM / -HH:MM
        起止: MM-DDTHH:MM（本地墙钟，半开区间 [起, 止) 内使用夏令时偏移）
        无夏令时: 夏令时偏移写与标准偏移相同，起止写 -
    REQ <YYYY-MM-DDTHH:MM> <时区1> <时区2> [时区3 ...]
        时区 >= 2 个；多于 2 个时构成换算链，逐个输出各中转时区的本地时间。

用法:
    python3 tzconvert.py [输入文件]     # 缺省读 stdin
    退出码: 0 = 无错误; 1 = 存在错误（错误清单照常输出）

关键设计:
  1. 换算路径：源本地时间 -> UTC -> 目标本地时间。UTC 作为唯一枢纽（pivot），
     任意两时区间只做一次减法一次加法，避免偏移直接相减时的符号/跨日错误；
     换算链中各中转时区共享同一 UTC 瞬间，中转不改变最终瞬间，只展示沿途墙钟。
  2. 夏令时判定基于"本地墙钟日期"落在 [起, 止) 半开区间内；起 >= 止 视为倒挂，
     报错并对该时区禁用夏令时（退化为标准偏移），保证后续请求仍可换算。
  3. 目标侧偏移与目标本地日期互相依赖，用不动点迭代（最多 2 次即收敛）求解。
  4. 跨日/跨年由 datetime + timedelta 的归一化天然保证，不做手工日期进位。
  5. 两遍解析：先收集全部时区定义，再处理请求，因此定义与请求的书写顺序无关。
  6. 所有错误带行号收集，不中断处理，最后统一输出错误清单。
"""

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

TIME_FMT = "%Y-%m-%dT%H:%M"
OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
DST_POINT_RE = re.compile(r"^(\d{2})-(\d{2})T(\d{2}):(\d{2})$")


# ---------- 数据模型 ----------

@dataclass
class DstRule:
    start: Tuple[int, int, int, int]  # (月, 日, 时, 分)
    end: Tuple[int, int, int, int]


@dataclass
class TimeZone:
    name: str
    std_offset: timedelta
    dst_offset: timedelta
    dst: Optional[DstRule]  # None 表示无夏令时（或倒挂被禁用）

    def offset_at(self, local_dt: datetime) -> timedelta:
        """按本地墙钟判定该时刻应使用的偏移。"""
        if self.dst is not None:
            key = (local_dt.month, local_dt.day, local_dt.hour, local_dt.minute)
            if self.dst.start <= key < self.dst.end:
                return self.dst_offset
        return self.std_offset


@dataclass
class Error:
    line_no: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"行{self.line_no}: [{self.code}] {self.message}"


# ---------- 解析辅助 ----------

def parse_offset(text: str) -> timedelta:
    m = OFFSET_RE.match(text)
    if not m:
        raise ValueError(f"偏移格式非法: {text!r}（应为 +HH:MM / -HH:MM）")
    sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3))
    if hours > 14 or minutes > 59:
        raise ValueError(f"偏移超出合理范围: {text!r}")
    delta = timedelta(hours=hours, minutes=minutes)
    return delta if sign == "+" else -delta


def parse_dst_point(text: str) -> Tuple[int, int, int, int]:
    m = DST_POINT_RE.match(text)
    if not m:
        raise ValueError(f"夏令时时间点格式非法: {text!r}（应为 MM-DDTHH:MM）")
    month, day, hour, minute = (int(g) for g in m.groups())
    # 借 datetime 校验月/日/时/分的合法性（用闰年容纳 02-29）
    datetime(2000, month, day, hour, minute)
    return (month, day, hour, minute)


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, TIME_FMT)  # 非法时抛 ValueError


# ---------- 核心换算 ----------

def local_to_utc(tz: TimeZone, local_dt: datetime) -> datetime:
    return local_dt - tz.offset_at(local_dt)


def utc_to_local(tz: TimeZone, utc_dt: datetime) -> datetime:
    """UTC -> 目标本地时间。偏移取决于目标本地日期，用不动点迭代求解。"""
    offset = tz.std_offset
    for _ in range(3):
        new_offset = tz.offset_at(utc_dt + offset)
        if new_offset == offset:
            break
        offset = new_offset
    return utc_dt + offset


def convert_chain(start_local: datetime, zones: List[TimeZone]) -> List[Tuple[TimeZone, datetime]]:
    """沿时区链换算同一瞬间，返回每个时区的本地时间（含源时区）。"""
    utc_instant = local_to_utc(zones[0], start_local)
    hops = [(zones[0], start_local)]
    for tz in zones[1:]:
        hops.append((tz, utc_to_local(tz, utc_instant)))
    return hops


# ---------- 主流程 ----------

def run(lines: List[str]) -> Tuple[List[str], List[Error]]:
    zones: Dict[str, TimeZone] = {}
    errors: List[Error] = []
    results: List[str] = []
    requests: List[Tuple[int, List[str]]] = []

    # 第一遍：收集时区定义，暂存请求
    for line_no, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        keyword = parts[0].upper()
        if keyword == "TZ":
            if len(parts) != 6:
                errors.append(Error(line_no, "E_BAD_DEF",
                                    f"时区定义应为 5 个字段，实际 {len(parts) - 1} 个: {line!r}"))
                continue
            _, name, std_s, dst_s, start_s, end_s = parts
            try:
                std_off = parse_offset(std_s)
                dst_off = parse_offset(dst_s)
            except ValueError as exc:
                errors.append(Error(line_no, "E_BAD_DEF", str(exc)))
                continue
            dst_rule: Optional[DstRule] = None
            if start_s == "-" and end_s == "-":
                pass  # 无夏令时
            else:
                try:
                    start = parse_dst_point(start_s)
                    end = parse_dst_point(end_s)
                except ValueError as exc:
                    errors.append(Error(line_no, "E_BAD_DEF", str(exc)))
                    continue
                if start >= end:
                    errors.append(Error(line_no, "E_DST_RANGE",
                                        f"时区 {name} 夏令时起止倒挂（起 {start_s} >= 止 {end_s}），"
                                        f"该时区按标准偏移处理"))
                else:
                    dst_rule = DstRule(start, end)
            if name in zones:
                errors.append(Error(line_no, "E_DUP_TZ",
                                    f"时区 {name} 重复定义，保留首次定义，忽略本次"))
                continue
            zones[name] = TimeZone(name, std_off, dst_off, dst_rule)
        elif keyword == "REQ":
            requests.append((line_no, parts))
        else:
            errors.append(Error(line_no, "E_BAD_LINE",
                                f"无法识别的行（应以 TZ 或 REQ 开头）: {line!r}"))

    # 第二遍：处理换算请求
    for line_no, parts in requests:
        label = f"行{line_no}"
        if len(parts) < 4:
            errors.append(Error(line_no, "E_BAD_REQ",
                                f"请求应为 REQ <时间> <时区1> <时区2> [...]: {' '.join(parts)!r}"))
            continue
        time_text, zone_names = parts[1], parts[2:]
        try:
            start_local = parse_time(time_text)
        except ValueError:
            errors.append(Error(line_no, "E_BAD_TIME",
                                f"时间格式非法: {time_text!r}（应为 YYYY-MM-DDTHH:MM）"))
            continue
        chain: List[TimeZone] = []
        unknown = [n for n in zone_names if n not in zones]
        if unknown:
            errors.append(Error(line_no, "E_UNKNOWN_TZ",
                                f"请求引用了未定义的时区: {', '.join(unknown)}"))
            continue
        chain = [zones[n] for n in zone_names]
        hops = convert_chain(start_local, chain)
        path = " -> ".join(tz.name for tz, _ in hops)
        results.append(f"{label} 换算链 {path}:")
        for tz, local in hops:
            off = tz.offset_at(local)
            sign = "+" if off >= timedelta(0) else "-"
            total = abs(int(off.total_seconds()))
            off_s = f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"
            dst_mark = " (夏令时)" if tz.dst and tz.offset_at(local) == tz.dst_offset else ""
            results.append(f"  {tz.name:<10} {local.strftime(TIME_FMT)}  UTC{off_s}{dst_mark}")
    return results, errors


def main(argv: List[str]) -> int:
    if len(argv) > 2:
        print(__doc__)
        return 2
    if len(argv) == 2:
        with open(argv[1], encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    results, errors = run(lines)

    print("===== 换算结果 =====")
    print("\n".join(results) if results else "（无有效请求）")
    print("\n===== 错误清单 =====")
    if errors:
        for err in errors:
            print(err)
    else:
        print("（无错误）")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
