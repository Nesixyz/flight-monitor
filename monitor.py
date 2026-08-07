#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
上海 → 巴黎 机票价格监控

数据源：飞猪官方 flyai-cli (search-flight)
通知 : Server酱 (推送至微信，分级推送)
看板 : 本地 HTML (dashboard.html)

筛选规则：
  - 出发日期范围 [date_start, date_end] 闭区间，每次全量查询
  - 公务舱 票价 <= business_max_price
  - 经济舱 票价 <= economy_max_price
  - 中转次数 <= max_transfers (0=直飞, 1=最多一次中转)
  - 直飞优先；无直飞则接受中转
变化对比：与上次记录对比，标注 新增/降价/涨价/消失

三阶段动态频率（飞猪额度优化）：
  - 阶段①抓低价 (今天 ~ phase1_end)：3次/天 (0/12/18点)
  - 阶段②稳观察 (phase1_end ~ phase2_end)：2次/天 (0/12点)
  - 阶段③冲刺期 (phase2_end ~ monitor_end)：4次/天 (0/6/12/18点)
  定时任务固定每6h触发，脚本按阶段决定是否执行

推送策略（额度按阶段动态匹配执行次数）：
  - 每次运行有内容（命中航班或消失航班）即推1条，含总结+航班细节
  - 总结含：查询范围、命中航班、变化对比、降价提醒、推荐航班
  - 额度：抓低价3条/天、稳观察2条/天、冲刺期4条/天（=各阶段执行次数）
  - 无内容时不推送，仅更新看板
"""
import json
import subprocess
import os
import sys
import time
import html
import signal
import urllib.request
import urllib.parse
import logging
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
NOTIFIED_PATH = BASE_DIR / "notified.json"
PUSH_HISTORY_PATH = BASE_DIR / "push_history.json"
DASHBOARD_PATH = BASE_DIR / "dashboard.html"
LOG_PATH = BASE_DIR / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("flight-monitor")

# 风控保护：连续失败达到阈值则提前结束本轮
RISK_BLOCK_FAIL_THRESHOLD = 3

# 推荐航班筛选条件（独立于监控阈值的更严格推荐标准，用于推送/看板顶部展示）
RECOMMEND_BUSINESS_MAX_PRICE = 10000
RECOMMEND_ECONOMY_MAX_PRICE = 5000
RECOMMEND_DATE_START = "2026-09-23"
RECOMMEND_DATE_END = "2026-10-01"


# ---------------- 基础工具 ----------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if os.environ.get("SERVERCHAN_KEY"):
        cfg["serverchan_key"] = os.environ["SERVERCHAN_KEY"]
    if os.environ.get("FLYAI_API_KEY"):
        cfg["flyai_api_key"] = os.environ["FLYAI_API_KEY"]
    return cfg


# ---------------- 阶段判断 ----------------

def get_current_phase(cfg):
    """判断当前所处阶段（按购票截止 9/15 调整）
    返回 (phase, should_run, max_push_per_day)
    - phase: 0=已停止 1=抓低价 2=稳观察 3=冲刺期
    - should_run: 本次触发是否应执行查询
    - max_push_per_day: 本阶段每日推送额度上限（与执行次数匹配）

    手动触发旁路：设置环境变量 MANUAL_RUN=1 时跳过时段检查，立即执行
    （用于 Trae trigger / GitHub Actions workflow_dispatch 等手动场景）"""
    today = datetime.now().date()
    phase1_end = datetime.strptime(cfg["schedule"]["phase1_end"], "%Y-%m-%d").date()
    phase2_end = datetime.strptime(cfg["schedule"]["phase2_end"], "%Y-%m-%d").date()
    monitor_end = datetime.strptime(cfg["schedule"]["monitor_end"], "%Y-%m-%d").date()
    current_hour = datetime.now().hour

    # 监控已停止（购票截止后）
    if today > monitor_end:
        return 0, False, 0

    # 各阶段执行时段 + 推送额度（每次运行推1条，额度=每日执行次数）
    phase1_hours = {0, 12, 18}     # 抓低价：3次/天
    phase2_hours = {0, 12}         # 稳观察：2次/天
    phase3_hours = {0, 6, 12, 18}  # 冲刺期：4次/天
    phase_max_push = {1: 3, 2: 2, 3: 4}

    if today <= phase1_end:
        phase = 1
        phase_hours = phase1_hours
    elif today <= phase2_end:
        phase = 2
        phase_hours = phase2_hours
    else:
        phase = 3
        phase_hours = phase3_hours

    # 手动触发：跳过时段检查，立即执行
    if os.environ.get("MANUAL_RUN") == "1":
        return phase, True, phase_max_push[phase]

    return phase, current_hour in phase_hours, phase_max_push[phase]


# ---------------- 推送历史 ----------------

def load_push_history():
    if PUSH_HISTORY_PATH.exists():
        try:
            return json.loads(PUSH_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_push_history(history):
    _atomic_write(
        PUSH_HISTORY_PATH,
        json.dumps(history, ensure_ascii=False, indent=2)
    )


def can_push(history, max_per_day):
    """判断今日推送次数是否已达上限
    兼容旧数据格式（按类型分计），统一计算当天总数"""
    today = datetime.now().strftime("%Y-%m-%d")
    today_record = history.get(today, {})
    if isinstance(today_record, dict):
        today_count = sum(today_record.values())
    else:
        today_count = int(today_record)
    return today_count < max_per_day


def record_push(history):
    """记录本次推送，统一用 'run' key 累计"""
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in history:
        history[today] = {}
    if isinstance(history[today], dict):
        history[today]["run"] = history[today].get("run", 0) + 1
    else:
        history[today] = {"run": int(history[today]) + 1}
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    for k in list(history.keys()):
        if k < cutoff:
            del history[k]


def generate_dates(start, end):
    """生成 [start, end] 闭区间的所有日期，格式 YYYY-MM-DD"""
    dates = []
    cur = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    while cur <= end_dt:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates


def get_flyai_cmd():
    """优先直接用 node 运行本地 flyai-bundle.cjs，否则回退 npx
    只使用绝对路径，避免定时任务环境 PATH 不全"""
    bundle = BASE_DIR / "node_modules" / "@fly-ai" / "flyai-cli" / "dist" / "flyai-bundle.cjs"
    if not bundle.exists():
        return ["npx", "flyai"]
    # 按优先级查找 node 绝对路径
    for node_path in ["/usr/local/bin/node", "/opt/homebrew/bin/node", "/usr/bin/node"]:
        if os.path.exists(node_path):
            return [node_path, str(bundle)]
    # 兜底：尝试 PATH 中的 node
    return ["node", str(bundle)]


# ---------------- flyai 查询 ----------------

def run_flyai(cfg, dep_date, seat_class, max_price):
    """调用 flyai search-flight，返回 (itemList, blocked)
    blocked=True 表示触发风控，建议停止后续查询"""
    cmd = get_flyai_cmd() + [
        "search-flight",
        "--origin", cfg["origin"],
        "--destination", cfg["destination"],
        "--dep-date", dep_date,
        "--seat-class-name", seat_class,
        "--max-price", str(max_price),
        "--sort-type", "3",
    ]
    env = os.environ.copy()
    if cfg.get("flyai_api_key"):
        env["FLYAI_API_KEY"] = cfg["flyai_api_key"]
    timeout = cfg.get("query_timeout_sec", 60)
    log.info("查询 %s %s (timeout=%ss)", dep_date, seat_class, timeout)
    try:
        # start_new_session 创建新进程组，timeout 后可 killpg 强制杀死 node 及子进程
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            cwd=str(BASE_DIR),
            env=env,
        )
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 强制杀死整个进程组
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.communicate()
            log.warning("flyai 查询超时(已强制终止) %s %s", dep_date, seat_class)
            return [], False
        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        # 风控检测
        if "risk control" in stderr or "Abnormal access" in stderr or "403" in stderr:
            log.warning("触发飞猪风控(403): %s %s", dep_date, seat_class)
            return [], True
        if proc.returncode != 0:
            log.warning("flyai exit=%s: %s", proc.returncode, stderr[:200])
            return [], False
        data = json.loads(stdout)
        if str(data.get("status", "")) != "0":
            msg = data.get("systemMessage", "")
            log.warning("flyai status!=0: %s", msg)
            return [], False
        log.info("查询成功 %s %s: %d 条结果", dep_date, seat_class,
                 len(data.get("data", {}).get("itemList", []) or []))
        return data.get("data", {}).get("itemList", []) or [], False
    except json.JSONDecodeError as e:
        log.warning("flyai JSON 解析失败 %s %s: %s", dep_date, seat_class, e)
        return [], False
    except Exception as e:
        log.warning("flyai 查询异常 %s %s: %s", dep_date, seat_class, e)
        return [], False


def _parse_dt(s):
    """解析 'YYYY-MM-DD HH:MM:SS' 为 datetime，失败返回 None"""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _fmt_duration_cn(minutes):
    """分钟数格式化为中文 'X时Y分' """
    if minutes is None or minutes < 0:
        return "-"
    h, m = divmod(int(minutes), 60)
    if h > 0 and m > 0:
        return f"{h}时{m}分"
    if h > 0:
        return f"{h}时"
    return f"{m}分"


def _fmt_dt_short(dt_str):
    """'2026-09-18 02:30:00' → '09-18 02:30'"""
    if not dt_str or len(dt_str) < 16:
        return dt_str or ""
    return dt_str[5:16]


def _md_safe_url(url):
    """对 Markdown 链接中的 URL 做安全编码
    Markdown [text](url) 遇到 ) 会截断 URL，需对 ) ( 空格 做百分号编码
    百分号编码是 URL 规范，服务器端会正确解码，不影响链接有效性"""
    if not url:
        return url
    return url.replace(")", "%29").replace("(", "%28").replace(" ", "%20")


def _extract_jump_url(url):
    """从飞猪 webview 容器 URL 中提取实际购票短链接
    API 返回格式: https://router.feizhu.com/multi/webview?url=https%3A%2F%2Frouter.feizhu.com%2Fws%2F4ATjQx
    外层 webview 容器在微信/手机浏览器中无法拉起飞猪小程序，导致"获取跳转链接失败"
    提取内层 url 参数并解码，得到可直接在浏览器打开的 HTTP 302 短链接"""
    if not url:
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        if "url" in params:
            return params["url"][0]
    except Exception:
        pass
    return url


def _atomic_write(path, content, encoding="utf-8"):
    """原子写入：先写临时文件，再 os.replace 替换原文件
    避免写入中途异常导致文件损坏"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        if isinstance(content, str):
            tmp.write_text(content, encoding=encoding)
        else:
            tmp.write_bytes(content)
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def parse_flight(item, seat_category):
    """从原始 item 提取结构化航班信息
    route: 中转信息（城市名/停留时长/到达-出发时间）
    total_duration: 飞猪返回的该行程总时长"""
    journey = item.get("journeys", [{}])[0]
    segments = journey.get("segments", [])
    if not segments:
        return None
    first = segments[0]
    last = segments[-1]
    journey_type = journey.get("journeyType", "")
    is_direct = (journey_type == "直达") or (len(segments) == 1)
    transfer_count = len(segments) - 1

    flight_numbers = " > ".join(
        f"{s.get('marketingTransportName', '')}{s.get('marketingTransportNo', '')}" for s in segments
    )
    dep_dt = first.get("depDateTime", "")
    arr_dt = last.get("arrDateTime", "")

    # 中转信息：每个中转点的城市名、停留时长、到达-出发时间
    transfers = []
    transfer_minutes = 0
    for i in range(len(segments) - 1):
        cur_seg = segments[i]
        next_seg = segments[i + 1]
        city = cur_seg.get("arrCityName", "")  # 中转城市=当前段到达城市
        arr_time = cur_seg.get("arrDateTime", "")
        dep_time = next_seg.get("depDateTime", "")
        arr_dt_obj = _parse_dt(arr_time)
        dep_dt_obj = _parse_dt(dep_time)
        stay = 0
        if arr_dt_obj and dep_dt_obj:
            stay = (dep_dt_obj - arr_dt_obj).total_seconds() / 60.0
            if stay > 0:
                transfer_minutes += stay
        transfers.append({
            "city": city,
            "stay_minutes": stay if stay > 0 else 0,
            "stay_str": _fmt_duration_cn(stay) if stay > 0 else "-",
            "arr_time": arr_time,
            "dep_time": dep_time,
            "time_window": f"{_fmt_dt_short(arr_time)} - {_fmt_dt_short(dep_time)}",
        })

    # 总时长：优先用各 segment.duration 求和 + 中转时长
    # segment.duration 单位为分钟，且为飞猪已时区校正的实际飞行时长
    # 不能用首末起降时间差，因起降时间采用本地时区，跨时区会少算时区偏移
    total_minutes = 0
    seg_duration_sum = 0
    for s in segments:
        d = s.get("duration")
        if d is not None:
            try:
                seg_duration_sum += float(d)
            except (ValueError, TypeError):
                pass
    if seg_duration_sum > 0:
        total_minutes = seg_duration_sum + transfer_minutes
    # 兜底1：journey 级别 duration / totalDuration
    if not total_minutes:
        journey_dur = journey.get("duration") or journey.get("totalDuration")
        if journey_dur is not None:
            try:
                dur = float(journey_dur)
                # 单位歧义（秒/分钟），用首末起降时间差校验
                first_dep = _parse_dt(dep_dt)
                last_arr = _parse_dt(arr_dt)
                wall_minutes = 0
                if first_dep and last_arr:
                    wall_minutes = (last_arr - first_dep).total_seconds() / 60.0
                as_min_from_sec = dur / 60.0
                if wall_minutes > 0:
                    total_minutes = as_min_from_sec if abs(as_min_from_sec - wall_minutes) < abs(dur - wall_minutes) else dur
                else:
                    total_minutes = as_min_from_sec
            except (ValueError, TypeError):
                total_minutes = 0
    # 兜底2：首末起降时间差（跨时区会偏小，仅在前两者都缺失时使用）
    if not total_minutes:
        first_dep = _parse_dt(dep_dt)
        last_arr = _parse_dt(arr_dt)
        if first_dep and last_arr:
            total_minutes = (last_arr - first_dep).total_seconds() / 60.0

    # 路线：城市链（上海→厦门→巴黎）
    route_text = " → ".join([first.get("depCityName", "")] +
                            [s.get("arrCityName", "") for s in segments])

    # 中转详情：每个中转点（城市/停留/时间窗口），中转才有
    transfer_summary = ""
    if transfer_count > 0:
        parts = []
        for t in transfers:
            parts.append(f"{t['city']}（停留{t['stay_str']}，{t['time_window']}）")
        transfer_summary = "；".join(parts)

    return {
        "price": float(item.get("ticketPrice", 0) or 0),
        "seat_class": first.get("seatClassName", ""),
        "seat_category": seat_category,
        "is_direct": is_direct,
        "transfer_count": transfer_count,
        "flight_numbers": flight_numbers,
        "dep_date": dep_dt[:10],
        "dep_time": dep_dt,
        "arr_time": arr_dt,
        "dep_city": first.get("depCityName", ""),
        "arr_city": last.get("arrCityName", ""),
        "dep_airport": first.get("depStationName", ""),
        "arr_airport": last.get("arrStationName", ""),
        "route": route_text,
        "transfers": transfers,
        "transfer_summary": transfer_summary,
        "transfer_duration": transfer_minutes,
        "total_duration": total_minutes,
        "total_duration_str": _fmt_duration_cn(total_minutes),
        "jump_url": item.get("jumpUrl", ""),
        "dedupe_key": f"{seat_category}|{flight_numbers}|{dep_dt[:10]}",
    }


def pick_recommended(flights):
    """从本次命中航班中挑选推荐航班（公务舱/经济舱各 1 条）
    条件：价格 ≤ 推荐阈值，出发日期在推荐日期窗口内，总时长有效
    排序：总时长越短越好
    返回 {"business": f_or_None, "economy": f_or_None}"""
    def pick(category, max_price):
        candidates = [
            f for f in flights
            if f["seat_category"] == category
            and f["price"] <= max_price
            and RECOMMEND_DATE_START <= f["dep_date"] <= RECOMMEND_DATE_END
            and f.get("total_duration", 0) > 0
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda f: f["total_duration"])
        return candidates[0]

    return {
        "business": pick("business", RECOMMEND_BUSINESS_MAX_PRICE),
        "economy": pick("economy", RECOMMEND_ECONOMY_MAX_PRICE),
    }


# ---------------- 去重记录 ----------------

def load_notified():
    if NOTIFIED_PATH.exists():
        try:
            return json.loads(NOTIFIED_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_notified(notified):
    _atomic_write(
        NOTIFIED_PATH,
        json.dumps(notified, ensure_ascii=False, indent=2)
    )


def diff_flights(current_flights, notified, blocked_dates=None):
    """对比本次结果与上次记录，给每个航班打变化标签
    返回 (带标签的航班列表, 变化统计dict, 消失航班列表)
    blocked_dates: 本次因风控未查询的日期列表，这些日期的 notified 记录
    不参与消失判断，避免误判
    消失判定：连续 2 次运行都未出现（miss_count >= 2）才加入 gone，
    单次未出现只递增 miss_count，避免飞猪价格波动导致误报"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stats = {"new": 0, "cheaper": 0, "higher": 0, "unchanged": 0}
    gone = []
    blocked_set = set(blocked_dates or [])

    # 标注当前航班变化
    current_keys = set()
    for f in current_flights:
        key = f["dedupe_key"]
        current_keys.add(key)
        prev = notified.get(key)
        if prev is None:
            f["change"] = "new"
            stats["new"] += 1
        else:
            prev_price = prev.get("price", 0)
            if prev_price > f["price"]:
                f["change"] = "cheaper"
                f["prev_price"] = prev_price
                stats["cheaper"] += 1
            elif prev_price < f["price"]:
                f["change"] = "higher"
                f["prev_price"] = prev_price
                stats["higher"] += 1
            else:
                f["change"] = "unchanged"
                stats["unchanged"] += 1

    # 找出不再符合条件的记录（上次有，本次无）
    # 跳过 blocked_dates：这些日期本次未查询，不能判定为消失
    # 递增 miss_count，只有连续 2 次未出现才加入 gone
    for key, prev in notified.items():
        if key in current_keys:
            continue
        prev_dep_date = prev.get("dep_date", "")
        if prev_dep_date in blocked_set:
            continue
        miss_count = prev.get("miss_count", 0) + 1
        prev["miss_count"] = miss_count
        if miss_count >= 2:
            gone.append({"key": key, "price": prev.get("price", 0),
                         "last_notified": prev.get("last_notified", "")})

    return current_flights, stats, gone


def update_notified(notified, flights, now_str):
    """用本次结果更新 notified 记录
    本次出现的航班重置 miss_count=0
    未出现的保留 diff_flights 中已递增的 miss_count"""
    for f in flights:
        key = f["dedupe_key"]
        prev = notified.get(key, {})
        notified[key] = {
            "price": f["price"],
            "first_seen": prev.get("first_seen", now_str),
            "last_notified": now_str,
            "seat_category": f["seat_category"],
            "dep_date": f["dep_date"],
            "miss_count": 0,
        }


# ---------------- Server酱 推送 ----------------

def send_serverchan(key, title, desp, max_retries=1):
    """通过 Server酱 推送微信通知
    失败后重试 max_retries 次，每次间隔 3s"""
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    last_err = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("code") == 0:
                    log.info("Server酱 推送成功 (尝试 %d/%d)", attempt + 1, max_retries + 1)
                    return True
                log.warning("Server酱 推送返回非0: %s", body)
                last_err = f"api code={body.get('code')}"
        except Exception as e:
            log.error("Server酱 推送失败 (尝试 %d/%d): %s", attempt + 1, max_retries + 1, e)
            last_err = str(e)
        if attempt < max_retries:
            time.sleep(3)
    return False


def change_text(f):
    """变化标注文本"""
    c = f.get("change", "")
    if c == "new":
        return "🆕新增"
    if c == "cheaper":
        return f"⬇降价 ¥{f['prev_price']:.0f}→¥{f['price']:.0f}"
    if c == "higher":
        return f"⬆涨价 ¥{f['prev_price']:.0f}→¥{f['price']:.0f}"
    return "—不变"


def is_urgent_drop(f, cfg):
    """判断是否为紧急降价（超过百分比阈值或绝对值阈值）"""
    if f.get("change") != "cheaper":
        return False
    prev = f.get("prev_price", 0)
    if prev <= 0:
        return False
    drop_abs = prev - f["price"]
    drop_pct = drop_abs / prev * 100
    pct_threshold = cfg.get("push", {}).get("urgent_drop_pct", 5)
    abs_threshold = cfg.get("push", {}).get("urgent_drop_abs", 500)
    return drop_pct >= pct_threshold or drop_abs >= abs_threshold


def format_run_message(all_flights, stats, gone, cfg, blocked_dates):
    """格式化本次运行结果推送（总结 + 航班细节）
    总结部分含：查询范围、命中航班、变化对比、降价提醒、推荐航班
    航班细节部分：公务舱列表、经济舱列表、消失航班"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    business = [f for f in all_flights if f["seat_category"] == "business"]
    economy = [f for f in all_flights if f["seat_category"] == "economy"]
    business.sort(key=lambda f: f["dep_date"], reverse=True)
    economy.sort(key=lambda f: f["dep_date"], reverse=True)

    title = f"上海→巴黎机票监控 命中{len(all_flights)}条(公务{len(business)}/经济{len(economy)})"

    lines = [
        f"## ✈️ 上海 → 巴黎 机票监控结果",
        f"_检测时间: {now}_",
        "",
        "### 📊 本次总结",
        f"- **查询范围**: {cfg['date_start']} ~ {cfg['date_end']} ({len(generate_dates(cfg['date_start'], cfg['date_end']))}天)",
        f"- **命中航班**: 公务舱 {len(business)} 条 / 经济舱 {len(economy)} 条",
        f"- **变化对比**: 🆕新增 {stats['new']} · ⬇降价 {stats['cheaper']} · ⬆涨价 {stats['higher']} · —不变 {stats['unchanged']} · ❌不再符合 {len(gone)}",
    ]

    # 降价提醒（合并到总结，不单独推送）
    urgent_flights = [f for f in all_flights if is_urgent_drop(f, cfg)]
    cheaper_flights = [f for f in all_flights if f.get("change") == "cheaper"]
    if urgent_flights:
        urgent_flights.sort(key=lambda f: f["price"])
        u = urgent_flights[0]
        lines.append(f"- **🚨降价提醒**: {len(urgent_flights)}条紧急降价，最低 ¥{u['price']:.0f} ({u['flight_numbers']}, {u['dep_date']})")
    elif cheaper_flights:
        cheaper_flights.sort(key=lambda f: f["price"])
        c = cheaper_flights[0]
        lines.append(f"- **⬇降价提醒**: {len(cheaper_flights)}条降价，最低 ¥{c['price']:.0f} ({c['flight_numbers']}, {c['dep_date']})")

    if blocked_dates:
        lines.append(f"- **⚠️风控**: {len(blocked_dates)}天因飞猪风控未查询")
    lines.append("")

    # 推荐航班（公务/经济各 1 条，价格+日期窗口+总时长最短）
    rec = pick_recommended(all_flights)
    lines.append("### ⭐ 推荐航班")
    lines.append(
        f"_筛选: 公务≤¥{RECOMMEND_BUSINESS_MAX_PRICE} / 经济≤¥{RECOMMEND_ECONOMY_MAX_PRICE}, "
        f"日期 {RECOMMEND_DATE_START}~{RECOMMEND_DATE_END}, 总时长最短_"
    )
    lines.append("")
    for cat, label in [("business", "💼 公务舱"), ("economy", "💺 经济舱")]:
        f = rec[cat]
        if not f:
            lines.append(f"- **{label}**: 无")
            continue
        type_label = "直飞" if f["is_direct"] else f"中转{f['transfer_count']}次"
        dep_t = f["dep_time"][11:16] if len(f["dep_time"]) >= 16 else ""
        arr_t = f["arr_time"][11:16] if len(f["arr_time"]) >= 16 else ""
        lines.append(
            f"- **{label}**: {f['dep_date']} {type_label} ¥{f['price']:.0f} · 总时长{f['total_duration_str']}"
        )
        lines.append(f"  - 航班: {f['flight_numbers']}")
        lines.append(f"  - 起降: {f['dep_airport']} {dep_t} → {f['arr_airport']} {arr_t}")
        if f["transfer_count"] > 0:
            lines.append(f"  - 中转: {f['transfer_summary']}")
        lines.append(f"  - 购票: [点击购票]({_md_safe_url(_extract_jump_url(f['jump_url']))})")
    lines.append("")

    def render_flight(f, idx):
        type_label = "直飞" if f["is_direct"] else f"中转{f['transfer_count']}次"
        ctext = change_text(f)
        dep_t = f["dep_time"][11:16] if len(f["dep_time"]) >= 16 else ""
        arr_t = f["arr_time"][11:16] if len(f["arr_time"]) >= 16 else ""
        out = [
            f"**{idx}. {f['dep_date']} {type_label} ¥{f['price']:.0f}** {ctext}",
            f"- 航班: {f['flight_numbers']}",
            f"- 起降: {f['dep_airport']} {dep_t} → {f['arr_airport']} {arr_t}",
            f"- 路线: {f['route']}",
        ]
        if f["transfer_count"] > 0:
            out.append(f"- 中转: {f['transfer_summary']}")
        out.append(f"- 总时长: {f['total_duration_str']}")
        out.append(f"- 购票: [点击购票]({_md_safe_url(_extract_jump_url(f['jump_url']))})")
        out.append("")
        return "\n".join(out)

    lines.append("### 💼 公务舱（≤¥%.0f）" % cfg["business_max_price"])
    if business:
        for i, f in enumerate(business, 1):
            lines.append(render_flight(f, i))
    else:
        lines.append("_无符合条件航班_")
        lines.append("")

    lines.append("### 💺 经济舱（≤¥%.0f）" % cfg["economy_max_price"])
    if economy:
        for i, f in enumerate(economy, 1):
            lines.append(render_flight(f, i))
    else:
        lines.append("_无符合条件航班_")
        lines.append("")

    if gone:
        lines.append("### ❌ 不再符合条件航班（连续2次未出现，可能涨价超阈值或停售）")
        for g in gone[:10]:
            lines.append(f"- {g['key']} ¥{g['price']:.0f}")
        if len(gone) > 10:
            lines.append(f"_...及其它 {len(gone)-10} 条_")
        lines.append("")

    lines.append("---")
    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    if dashboard_url:
        lines.append(f"_数据来源: 飞猪 flyai | 在线看板: {dashboard_url}_")
    else:
        lines.append("_数据来源: 飞猪 flyai | 完整看板见本地 dashboard.html_")
    return title, "\n".join(lines)


# ---------------- HTML 看板 ----------------

def generate_html(all_flights, stats, gone, cfg, blocked_dates):
    """生成本地 HTML 看板"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    business = [f for f in all_flights if f["seat_category"] == "business"]
    economy = [f for f in all_flights if f["seat_category"] == "economy"]
    business.sort(key=lambda f: f["dep_date"], reverse=True)
    economy.sort(key=lambda f: f["dep_date"], reverse=True)

    def change_badge(f):
        c = f.get("change", "")
        styles = {
            "new": "background:#e6f7e6;color:#1a7a1a;border:1px solid #1a7a1a",
            "cheaper": "background:#e6f0ff;color:#1a4fbf;border:1px solid #1a4fbf",
            "higher": "background:#fff0e6;color:#bf3a00;border:1px solid #bf3a00",
            "unchanged": "background:#f0f0f0;color:#666;border:1px solid #ccc",
        }
        labels = {"new": "🆕 新增", "cheaper": "⬇ 降价", "higher": "⬆ 涨价", "unchanged": "— 不变"}
        style = styles.get(c, styles["unchanged"])
        label = labels.get(c, "—")
        if c in ("cheaper", "higher"):
            label += f" ¥{f.get('prev_price',0):.0f}→¥{f['price']:.0f}"
        return f'<span class="badge" style="{style}">{label}</span>'

    def render_table(flights):
        if not flights:
            return '<p class="empty">无符合条件航班</p>'
        rows = []
        for f in flights:
            type_label = "直飞" if f["is_direct"] else f"中转{f['transfer_count']}次"
            dep_t = f["dep_time"][11:16] if len(f["dep_time"]) >= 16 else ""
            arr_t = f["arr_time"][11:16] if len(f["arr_time"]) >= 16 else ""
            url = html.escape(_extract_jump_url(f["jump_url"]))
            # 路线单元格：第一行城市链，下面每行一个中转点
            route_lines = [f'<b>{html.escape(f["route"])}</b>']
            for t in f["transfers"]:
                route_lines.append(
                    f"{html.escape(t['city'])}<br>"
                    f"<span class='muted'>停留{html.escape(t['stay_str'])}，{html.escape(t['time_window'])}</span>"
                )
            route_cell = "<hr class='seg-sep'>".join(route_lines)
            rows.append(f"""
<tr>
  <td class="date">{f['dep_date']}</td>
  <td>{type_label}</td>
  <td class="price">¥{f['price']:.0f}</td>
  <td>{html.escape(f['flight_numbers'])}</td>
  <td>{html.escape(f['dep_airport'])} {dep_t}<br><span class="muted">→</span> {html.escape(f['arr_airport'])} {arr_t}</td>
  <td class="route">{route_cell}</td>
  <td class="dur">{html.escape(f['total_duration_str'])}</td>
  <td>{change_badge(f)}</td>
  <td><a href="{url}" target="_blank">购票</a></td>
</tr>""")
        return """<table>
<thead><tr><th>出发日期</th><th>类型</th><th>价格</th><th>航班号</th><th>起降</th><th>路线</th><th>总时长</th><th>变化</th><th>操作</th></tr></thead>
<tbody>""" + "".join(rows) + "</tbody></table>"

    gone_html = ""
    if gone:
        gone_rows = "".join(
            f"<li>{html.escape(g['key'])} ¥{g['price']:.0f} <span class='muted'>({g.get('last_notified','')})</span></li>"
            for g in gone
        )
        gone_html = f"<div class='gone'><h3>❌ 不再符合条件航班（{len(gone)}）</h3><ul>{gone_rows}</ul></div>"

    blocked_html = ""
    if blocked_dates:
        blocked_html = f"<div class='warn'>⚠️ 以下日期因飞猪风控未能查询: {', '.join(blocked_dates)}</div>"

    # 推荐航班卡片（公务/经济各 1 条）
    rec = pick_recommended(all_flights)
    rec_cards = []
    for cat, label, emoji in [("business", "公务舱", "💼"), ("economy", "经济舱", "💺")]:
        f = rec[cat]
        if not f:
            rec_cards.append(
                f'<div class="rec-card"><h3>{emoji} {label}</h3><p class="empty">无</p></div>'
            )
            continue
        type_label = "直飞" if f["is_direct"] else f"中转{f['transfer_count']}次"
        dep_t = f["dep_time"][11:16] if len(f["dep_time"]) >= 16 else ""
        arr_t = f["arr_time"][11:16] if len(f["arr_time"]) >= 16 else ""
        url = html.escape(_extract_jump_url(f["jump_url"]))
        transfer_html = ""
        if f["transfer_count"] > 0:
            transfer_html = f"<div class='rec-info'>中转: {html.escape(f['transfer_summary'])}</div>"
        rec_cards.append(f"""
<div class="rec-card">
<h3>{emoji} {label} <span class="rec-price">¥{f['price']:.0f}</span></h3>
<div class="rec-info">{f['dep_date']} · {type_label} · 总时长 {html.escape(f['total_duration_str'])}</div>
<div class="rec-info">{html.escape(f['flight_numbers'])}</div>
<div class="rec-info">{html.escape(f['dep_airport'])} {dep_t} → {html.escape(f['arr_airport'])} {arr_t}</div>
{transfer_html}
<a href="{url}" target="_blank" class="rec-buy">点击购票</a>
</div>""")
    recommend_html = f"""<div class="recommend">
<h2>⭐ 推荐航班</h2>
<p class="muted">筛选: 公务≤¥{RECOMMEND_BUSINESS_MAX_PRICE} / 经济≤¥{RECOMMEND_ECONOMY_MAX_PRICE}, 日期 {RECOMMEND_DATE_START}~{RECOMMEND_DATE_END}, 总时长最短</p>
<div class="rec-row">{''.join(rec_cards)}</div>
</div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>上海→巴黎机票监控看板</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#fafafa;color:#222}}
h1{{color:#1a4fbf;border-bottom:2px solid #1a4fbf;padding-bottom:8px}}
h2{{margin-top:32px;color:#333}}
.summary{{background:#fff;padding:16px 20px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);margin:16px 0}}
.summary div{{margin:4px 0}}
.stat{{display:inline-block;margin-right:16px;padding:4px 10px;border-radius:4px;font-size:14px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid #eee;font-size:14px}}
th{{background:#f5f7fa;color:#333;font-weight:600}}
tr:hover{{background:#f9fbff}}
td.date{{font-weight:600;color:#1a4fbf;white-space:nowrap}}
td.price{{font-weight:700;color:#d4380d;white-space:nowrap}}
td.dur{{font-size:13px;white-space:nowrap;line-height:1.5}}
td.route{{font-size:13px;line-height:1.5}}
td.route .muted{{color:#999}}
hr.seg-sep{{border:none;border-top:1px dashed #ddd;margin:4px 0}}
a{{color:#1a4fbf;text-decoration:none}}
a:hover{{text-decoration:underline}}
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap}}
.muted{{color:#999}}
.empty{{color:#999;font-style:italic;padding:12px}}
.gone{{margin-top:24px;background:#fff5f5;padding:16px;border-radius:8px;border-left:4px solid #d4380d}}
.gone ul{{margin:8px 0;padding-left:20px}}
.warn{{margin:12px 0;padding:12px 16px;background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;color:#874d00}}
.update{{color:#999;font-size:13px}}
.recommend{{margin:16px 0;padding:16px 20px;background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);border-left:4px solid #faad14}}
.recommend h2{{margin-top:0;color:#874d00}}
.rec-row{{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px}}
.rec-card{{flex:1;min-width:280px;padding:14px 16px;background:#fffbf0;border:1px solid #ffe58f;border-radius:8px}}
.rec-card h3{{margin:0 0 8px 0;color:#333}}
.rec-price{{float:right;color:#d4380d;font-weight:700;font-size:18px}}
.rec-info{{font-size:13px;color:#555;margin:3px 0;line-height:1.5}}
.rec-buy{{display:inline-block;margin-top:8px;padding:6px 16px;background:#1a4fbf;color:#fff!important;border-radius:4px;font-size:13px}}
.rec-buy:hover{{background:#16409e;text-decoration:none}}
</style></head><body>
<h1>✈️ 上海 → 巴黎 机票监控看板</h1>
<p class="update">最近更新: {now}</p>
<div class="summary">
<div><b>查询范围:</b> {cfg['date_start']} ~ {cfg['date_end']}（{len(generate_dates(cfg['date_start'], cfg['date_end']))} 天）</div>
<div><b>命中航班:</b> 公务舱 {len(business)} 条 / 经济舱 {len(economy)} 条</div>
<div><b>变化对比:</b>
<span class="stat" style="background:#e6f7e6">🆕新增 {stats['new']}</span>
<span class="stat" style="background:#e6f0ff">⬇降价 {stats['cheaper']}</span>
<span class="stat" style="background:#fff0e6">⬆涨价 {stats['higher']}</span>
<span class="stat" style="background:#f0f0f0">—不变 {stats['unchanged']}</span>
<span class="stat" style="background:#fff5f5">❌不再符合 {len(gone)}</span>
</div>
</div>
{recommend_html}
{blocked_html}
<h2>💼 公务舱（≤¥{cfg['business_max_price']:.0f}）</h2>
{render_table(business)}
<h2>💺 经济舱（≤¥{cfg['economy_max_price']:.0f}）</h2>
{render_table(economy)}
{gone_html}
<hr><p class="muted">数据来源: 飞猪 flyai-cli · 本看板由 monitor.py 自动生成</p>
<p class="muted">🌐 在线看板: <a href="{os.environ.get('DASHBOARD_URL', '#')}">{os.environ.get('DASHBOARD_URL', '未设置')}</a></p>
</body></html>"""
    _atomic_write(DASHBOARD_PATH, html_content)
    log.info("已更新本地看板 %s", DASHBOARD_PATH)


# ---------------- 主流程 ----------------

def main():
    log.info("=" * 50)
    log.info("开始监控 上海→巴黎 机票")
    cfg = load_config()

    # 阶段判断：决定本次是否执行查询
    phase, should_run, max_push_per_day = get_current_phase(cfg)
    phase_names = {0: "已停止", 1: "抓低价", 2: "稳观察", 3: "冲刺期"}
    log.info("当前阶段: %s(%d) | 应执行查询: %s | 推送额度: %d条/天",
             phase_names[phase], phase, should_run, max_push_per_day)

    if not should_run:
        if phase == 0:
            log.info("监控已停止（购票截止日 %s 已过），如需重启请修改 config.json 的 monitor_end", cfg["schedule"]["monitor_end"])
        else:
            log.info("当前为%s阶段，当前时段不执行查询，跳过（节省额度）", phase_names[phase])
        return

    all_dates = generate_dates(cfg["date_start"], cfg["date_end"])
    log.info("日期范围 %s ~ %s (%d天), 每次全量查询", cfg["date_start"], cfg["date_end"], len(all_dates))

    queries = [
        ("公务舱", cfg["business_max_price"], "business"),
        ("经济舱", cfg["economy_max_price"], "economy"),
    ]

    all_flights = []
    query_count = 0
    fail_count = 0
    blocked = False
    blocked_dates = []
    for date in all_dates:
        if blocked:
            blocked_dates.append(date)
            continue
        for seat_class, max_price, category in queries:
            items, is_blocked = run_flyai(cfg, date, seat_class, max_price)
            query_count += 1
            if is_blocked:
                fail_count += 1
                if fail_count >= RISK_BLOCK_FAIL_THRESHOLD:
                    log.warning("连续 %d 次触发风控，提前结束本轮剩余查询", fail_count)
                    blocked = True
                    break
            else:
                fail_count = 0  # 成功则重置计数
            for item in items:
                f = parse_flight(item, category)
                if not f:
                    continue
                if f["transfer_count"] <= cfg["max_transfers"] and f["price"] <= max_price:
                    all_flights.append(f)
            time.sleep(cfg.get("query_interval_sec", 12))
        # 日期切换不再额外 sleep，query_interval_sec 已足够间隔

    log.info("查询完成: %d 次查询, 命中 %d 条符合条件航班, 风控跳过 %d 天",
             query_count, len(all_flights), len(blocked_dates))

    # 变化对比
    notified = load_notified()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_flights, stats, gone = diff_flights(all_flights, notified, blocked_dates)
    log.info("变化: 新增%d 降价%d 涨价%d 不变%d 不再符合%d",
             stats["new"], stats["cheaper"], stats["higher"], stats["unchanged"], len(gone))

    # 始终更新本地看板
    generate_html(all_flights, stats, gone, cfg, blocked_dates)

    # 推送：每次运行有内容就推1条，额度按阶段动态匹配执行次数
    push_history = load_push_history()
    pushed = False

    has_content = len(all_flights) > 0 or len(gone) > 0
    if not has_content:
        log.info("本次无命中航班且无消失航班，不推送（仅更新看板）")
    elif can_push(push_history, max_push_per_day):
        title, desp = format_run_message(all_flights, stats, gone, cfg, blocked_dates)
        if send_serverchan(cfg["serverchan_key"], title, desp):
            record_push(push_history)
            pushed = True
            log.info("已推送本次运行结果 (命中%d条, 不再符合%d条)", len(all_flights), len(gone))
        else:
            log.warning("推送失败")
    else:
        log.info("今日推送已达阶段上限(%d条)，跳过推送", max_push_per_day)

    save_push_history(push_history)

    # 更新去重记录（无论是否推送，都更新以便下次对比）
    update_notified(notified, all_flights, now_str)
    save_notified(notified)

    if not pushed:
        log.info("本次未推送微信，仅更新看板与记录")

    log.info("监控结束，看板: %s", DASHBOARD_PATH)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("监控异常: %s", e)
        sys.exit(1)
