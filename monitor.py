#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
机票价格监控（单程 / 往返）

数据源：飞猪官方 flyai-cli (search-flight)
通知 : Server酱 (推送至微信)
看板 : HTML (dashboard.html，GitHub Pages 部署)

模式：
  - 单程：只配置 date_start/date_end，监控 origin→destination
  - 往返：额外配置 return_date_start/return_date_end/min_nights/max_nights，
          同时监控去程 origin→destination 与回程 destination→origin，
          并按“在目的地停留 min~max 晚”自动配对出最低价往返组合

筛选规则：
  - 每个出发日期每次全量查询（已过去的日期自动跳过）
  - 公务舱 价格 <= business_max_price，经济舱 价格 <= economy_max_price（单程价）
  - 中转次数 <= max_transfers
变化对比：与上次记录对比，标注 新增/降价/涨价/不再符合

三阶段动态频率（北京时间，飞猪额度优化；往返每轮约 32 次调用）：
  - 阶段① 抓低价 (今天 ~ phase1_end)：1次/天 (12点)
  - 阶段② 稳观察 (phase1_end ~ phase2_end)：2次/天 (0/12点)
  - 阶段③ 冲刺期 (phase2_end ~ monitor_end)：3次/天 (0/12/18点)
  cron 每天触发 3 次（北京 0/12/18 点），脚本按阶段决定是否执行
"""
import json
import subprocess
import os
import sys
import time
import html
import signal
import itertools
import urllib.request
import urllib.parse
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
NOTIFIED_PATH = BASE_DIR / "notified.json"
PUSH_HISTORY_PATH = BASE_DIR / "push_history.json"
DASHBOARD_PATH = BASE_DIR / "dashboard.html"
LOG_PATH = BASE_DIR / "monitor.log"

DIRECTIONS = {"outbound": "去程", "return": "回程"}


def _is_manual_run() -> bool:
    """MANUAL_RUN 宽松判断：支持 '1'/'true'/'True'/'yes' 等。"""
    v = os.environ.get("MANUAL_RUN", "")
    return v in ("1", "true", "True", "yes", "Yes", "on")


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

# 推荐航班筛选默认值（单程模式使用，可被 config.json recommend 字段覆盖）
DEFAULT_RECOMMEND_BUSINESS_MAX_PRICE = 18000
DEFAULT_RECOMMEND_ECONOMY_MAX_PRICE = 5500
DEFAULT_RECOMMEND_DATE_START = "2026-12-20"
DEFAULT_RECOMMEND_DATE_END = "2027-01-10"

# 飞猪 API 累计调用阈值：首赠 5000 次，取 90% 做首次告警
FLYAI_WARN_THRESHOLD = 4500


# ---------------- 基础工具 ----------------

def _check_date(cfg, field):
    try:
        datetime.strptime(cfg[field], "%Y-%m-%d")
    except (ValueError, TypeError):
        log.error("config.json 字段 %s 日期格式错误（应为 YYYY-MM-DD）: %r", field, cfg.get(field))
        sys.exit(1)


def load_config():
    """加载配置并校验必填字段；可选字段缺失时使用默认值兜底"""
    if not CONFIG_PATH.exists():
        log.error("config.json 不存在！请参考 config.example.json 创建。")
        log.error("GitHub Actions 用户：检查 workflow 中是否执行了 cp config.example.json config.json")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 环境变量覆盖（非空才覆盖，避免空字符串覆盖有效配置）
    serverchan_env = os.environ.get("SERVERCHAN_KEY")
    if serverchan_env:
        cfg["serverchan_key"] = serverchan_env
    flyai_env = os.environ.get("FLYAI_API_KEY")
    if flyai_env:
        cfg["flyai_api_key"] = flyai_env

    # 城市名称映射（三字码 → 中文名，用于推送和看板文案）
    CITY_NAMES = {
        "SHA": "上海", "PVG": "上海", "SHA(上海)": "上海",
        "PAR": "巴黎", "CDG": "巴黎", "ORY": "巴黎", "BVA": "巴黎",
        "TPE": "台北", "TSA": "台北",
    }
    cfg.setdefault("origin_name", CITY_NAMES.get(cfg.get("origin", ""), cfg.get("origin", "出发地")))
    cfg.setdefault("destination_name", CITY_NAMES.get(cfg.get("destination", ""), cfg.get("destination", "目的地")))

    required_fields = {
        "origin": "出发地（三字码，如 PAR）",
        "destination": "目的地（三字码，如 TPE）",
        "date_start": "去程出发日期起始（YYYY-MM-DD）",
        "date_end": "去程出发日期截止（YYYY-MM-DD）",
        "business_max_price": "公务舱单程价格上限",
        "economy_max_price": "经济舱单程价格上限",
        "max_transfers": "最大中转次数（0=直飞, 1=最多1次中转）",
    }
    missing = [f for f in required_fields if f not in cfg or cfg.get(f) in (None, "")]
    if missing:
        log.error("config.json 缺失必填字段: %s", ", ".join(missing))
        for f in missing:
            log.error("  - %s: %s", f, required_fields[f])
        sys.exit(1)

    schedule = cfg.get("schedule", {})
    schedule_missing = [f for f in ("phase1_end", "phase2_end", "monitor_end") if f not in schedule]
    if schedule_missing:
        log.error("config.json schedule 字段缺失: %s", ", ".join(schedule_missing))
        sys.exit(1)

    for field in ("date_start", "date_end"):
        _check_date(cfg, field)

    # 往返模式（可选）：配置了 return_date_start/end 即启用
    cfg["round_trip"] = bool(cfg.get("return_date_start") and cfg.get("return_date_end"))
    cfg["min_nights"] = int(cfg.get("min_nights", 5))
    cfg["max_nights"] = int(cfg.get("max_nights", 9))
    if cfg["round_trip"]:
        for field in ("return_date_start", "return_date_end"):
            _check_date(cfg, field)
        if cfg["min_nights"] > cfg["max_nights"]:
            log.error("min_nights(%d) 不能大于 max_nights(%d)", cfg["min_nights"], cfg["max_nights"])
            sys.exit(1)

    rec_cfg = cfg.get("recommend", {}) or {}
    cfg["recommend"] = {
        "business_max_price": rec_cfg.get("business_max_price", DEFAULT_RECOMMEND_BUSINESS_MAX_PRICE),
        "economy_max_price": rec_cfg.get("economy_max_price", DEFAULT_RECOMMEND_ECONOMY_MAX_PRICE),
        "date_start": rec_cfg.get("date_start", DEFAULT_RECOMMEND_DATE_START),
        "date_end": rec_cfg.get("date_end", DEFAULT_RECOMMEND_DATE_END),
    }

    push_cfg = cfg.get("push", {}) or {}
    cfg["push"] = {
        "urgent_drop_pct": push_cfg.get("urgent_drop_pct", 5),
        "urgent_drop_abs": push_cfg.get("urgent_drop_abs", 500),
    }

    if not cfg.get("flyai_api_key"):
        log.warning("flyai_api_key 未配置，查询将失败！请配置 FLYAI_API_KEY 环境变量或 config.json 字段")
    if not cfg.get("serverchan_key"):
        log.warning("serverchan_key 未配置，将无法推送微信通知")

    return cfg


def route_title(cfg):
    """文案中的航线标题：往返用 ⇄，单程用 →"""
    arrow = "⇄" if cfg.get("round_trip") else "→"
    return f"{cfg['origin_name']}{arrow}{cfg['destination_name']}"


# ---------------- 阶段判断 ----------------

def get_current_phase(cfg):
    """判断当前所处阶段
    返回 (phase, should_run, max_push_per_day)
    - phase: 0=已停止 1=抓低价 2=稳观察 3=冲刺期

    GitHub Actions 的 cron 常延迟几分钟到近一小时，因此“当前小时”或“上一小时”
    落在执行时段内都算命中（cron 触发点间隔 ≥6h，不会重复触发）。
    手动触发旁路：MANUAL_RUN=1 时跳过时段检查，立即执行"""
    now = datetime.now()
    today = now.date()
    phase1_end = datetime.strptime(cfg["schedule"]["phase1_end"], "%Y-%m-%d").date()
    phase2_end = datetime.strptime(cfg["schedule"]["phase2_end"], "%Y-%m-%d").date()
    monitor_end = datetime.strptime(cfg["schedule"]["monitor_end"], "%Y-%m-%d").date()
    current_hour = now.hour

    if today > monitor_end:
        return 0, False, 0

    # 各阶段执行时段（北京时间）+ 推送额度（额度=每日执行次数）
    phase1_hours = {12}          # 抓低价：1次/天
    phase2_hours = {0, 12}       # 稳观察：2次/天
    phase3_hours = {0, 12, 18}   # 冲刺期：3次/天
    phase_max_push = {1: 1, 2: 2, 3: 3}

    if today <= phase1_end:
        phase, phase_hours = 1, phase1_hours
    elif today <= phase2_end:
        phase, phase_hours = 2, phase2_hours
    else:
        phase, phase_hours = 3, phase3_hours

    if _is_manual_run():
        return phase, True, phase_max_push[phase]

    in_slot = current_hour in phase_hours or (current_hour - 1) % 24 in phase_hours
    return phase, in_slot, phase_max_push[phase]


# ---------------- 推送历史 + API 用量 ----------------

def load_push_history():
    """读取 push_history.json 并做旧格式迁移（保留 _meta.flyai_calls_total）"""
    if not PUSH_HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(PUSH_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    meta = data.pop("_meta", {}) if "_meta" in data else {}
    migrated_any = False
    new_data = {}
    for k, v in data.items():
        if len(k) == 10 and k[4] == "-" and k[7] == "-":
            if isinstance(v, (int, float)):
                new_data[k] = {"run": int(v)}
                migrated_any = True
            elif isinstance(v, dict):
                if "run" not in v and len(v) > 0:
                    new_data[k] = {"run": sum(v.values())}
                    migrated_any = True
                else:
                    new_data[k] = v
            else:
                new_data[k] = v
        else:
            new_data[k] = v
    if meta:
        new_data["_meta"] = meta
    if migrated_any:
        log.info("push_history 格式已迁移（旧类型分计 → 统一 run 计数）")
    return new_data


def save_push_history(history):
    try:
        _atomic_write(PUSH_HISTORY_PATH, json.dumps(history, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning("save_push_history 失败: %s", e)


def get_flyai_calls_total(history):
    return int(history.get("_meta", {}).get("flyai_calls_total", 0))


def add_flyai_calls(history, n):
    if n <= 0:
        return
    if "_meta" not in history:
        history["_meta"] = {}
    history["_meta"]["flyai_calls_total"] = int(history["_meta"].get("flyai_calls_total", 0)) + n


def can_push(history, max_per_day):
    today = datetime.now().strftime("%Y-%m-%d")
    today_record = history.get(today, {})
    if isinstance(today_record, dict):
        today_count = sum(today_record.values())
    else:
        today_count = int(today_record)
    return today_count < max_per_day


def record_push(history):
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in history:
        history[today] = {}
    if isinstance(history[today], dict):
        history[today]["run"] = history[today].get("run", 0) + 1
    else:
        history[today] = {"run": int(history[today]) + 1}
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    for k in list(history.keys()):
        if k != "_meta" and k < cutoff:
            del history[k]


_DATES_CACHE = {}


def generate_dates(start, end):
    """生成 [start, end] 闭区间的所有日期，格式 YYYY-MM-DD"""
    cache_key = (start, end)
    if cache_key in _DATES_CACHE:
        return _DATES_CACHE[cache_key]
    dates = []
    cur = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    while cur <= end_dt:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    _DATES_CACHE[cache_key] = dates
    return dates


def get_flyai_cmd():
    """优先直接用 node 运行本地 flyai-bundle.cjs，否则回退 npx"""
    bundle = BASE_DIR / "node_modules" / "@fly-ai" / "flyai-cli" / "dist" / "flyai-bundle.cjs"
    if not bundle.exists():
        return ["npx", "flyai"]
    for node_path in ["/usr/local/bin/node", "/opt/homebrew/bin/node", "/usr/bin/node"]:
        if os.path.exists(node_path):
            return [node_path, str(bundle)]
    return ["node", str(bundle)]


# ---------------- flyai 查询 ----------------

def _call_flyai_once(cmd, env, timeout, dep_date):
    """执行一次 flyai CLI 调用，返回 (itemList, blocked, api_failed, log_info)"""
    try:
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
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.communicate()
            log.warning("flyai 查询超时(已强制终止) %s", dep_date)
            return [], False, True, ("timeout", "", 0)
        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        if "risk control" in stderr or "Abnormal access" in stderr or "403" in stderr:
            log.warning("触发飞猪风控(403): %s", dep_date)
            return [], True, True, ("blocked", "", 0)
        if proc.returncode != 0:
            log.warning("flyai exit=%s: %s", proc.returncode, stderr[:200])
            return [], False, True, ("exit_error", stderr[:200], 0)
        data = json.loads(stdout)
        status = str(data.get("status", ""))
        message = data.get("message", "") or ""
        system_msg = data.get("systemMessage", "") or ""
        if status != "0":
            empty_indicators = ["结果为空", "无数据", "无符合", "暂无"]
            if any(s in message for s in empty_indicators):
                log.info("查询成功 %s: 0 条结果（%s）", dep_date, message)
                return [], False, False, (status, message, 0)
            log.warning("flyai API 错误: status=%s message=%s systemMessage=%s", status, message, system_msg)
            return [], False, True, (status, message, 0)
        items = data.get("data", {}).get("itemList", []) or []
        log.info("查询成功 %s: %d 条结果", dep_date, len(items))
        if system_msg:
            log.info("systemMessage %s: %s", dep_date, system_msg)
        return items, False, False, (status, message, len(items))
    except json.JSONDecodeError as e:
        log.warning("flyai JSON 解析失败 %s: %s", dep_date, e)
        return [], False, True, ("json_error", str(e), 0)
    except Exception as e:
        log.warning("flyai 查询异常 %s: %s", dep_date, e)
        return [], False, True, ("exception", str(e), 0)


def run_flyai(cfg, dep_date, seat_class, max_price, origin=None, destination=None):
    """调用 flyai search-flight，返回 (itemList, blocked, api_failed)
    origin/destination 缺省为 cfg 中的去程方向；回程查询时传入对调后的值。
    不传 --journey-type，由本地代码按 max_transfers 过滤；--sort-type 4 按时长升序。"""
    env = os.environ.copy()
    if cfg.get("flyai_api_key"):
        env["FLYAI_API_KEY"] = cfg["flyai_api_key"]
    timeout = cfg.get("query_timeout_sec", 30)
    origin = origin or cfg["origin"]
    destination = destination or cfg["destination"]

    cmd = get_flyai_cmd() + [
        "search-flight",
        "--origin", origin,
        "--destination", destination,
        "--dep-date", dep_date,
        "--max-price", str(max_price),
        "--sort-type", "4",
    ]
    if seat_class:
        cmd.extend(["--seat-class-name", seat_class])

    log.info("查询 %s→%s %s (timeout=%ss, max_transfers=%d)",
             origin, destination, dep_date, timeout, cfg.get("max_transfers", 0))
    items, blocked, api_failed, _ = _call_flyai_once(cmd, env, timeout, dep_date)
    return items, blocked, api_failed


_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_dt(s):
    if not s:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _fmt_duration_cn(minutes):
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
    """Markdown [text](url) 遇到 ) 会截断 URL，对 ) ( 空格做百分号编码"""
    if not url:
        return url
    return url.replace(")", "%29").replace("(", "%28").replace(" ", "%20")


def _extract_jump_url(url):
    """从飞猪 webview 容器 URL 中提取实际购票短链接（内层 url 参数）"""
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


def _fetch_total_price(jump_url):
    """从飞猪购票短链接 302 跳转的 Location 中提取含税总价 flp(xxx)，失败返回 None"""
    if not jump_url:
        return None
    try:
        import http.client as http_client
        parsed = urllib.parse.urlsplit(jump_url)
        # The booking URL is supplied by an external API. Never fetch arbitrary hosts.
        host = (parsed.hostname or "").lower().rstrip(".")
        trusted_domains = ("taobao.com", "tb.cn", "fliggy.com", "alitrip.com")
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not any(host == d or host.endswith("." + d) for d in trusted_domains)):
            log.warning("拒绝非可信购票 URL host=%s", host)
            return None
        conn = http_client.HTTPSConnection(host, timeout=10)
        conn.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        resp = conn.getresponse()
        location = resp.getheader("Location", "")
        conn.close()
        if not location:
            return None
        params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        fpt = params.get("fpt", [""])[0]
        match = re.search(r"flp\((\d+)\)", fpt)
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return None


def _atomic_write(path, content, encoding="utf-8"):
    """原子写入：先写临时文件，再 os.replace"""
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


def _classify_seat_category(seat_class_name):
    """根据 API 返回的 seatClassName 归类（飞猪 API 忽略 --seat-class-name 参数）"""
    if not seat_class_name:
        return "economy"
    if "经济" in seat_class_name:
        return "economy"
    if "公务" in seat_class_name or "商务" in seat_class_name:
        return "business"
    if "头等" in seat_class_name:
        return "first"
    return "economy"


def _type_label(f):
    return "直飞" if f.get("is_direct") else f"中转{f.get('transfer_count', 1)}次"


def parse_flight(item, direction="outbound"):
    """从原始 item 提取结构化航班信息（此时 price 为不含税票面价，
    含税价由 apply_total_price 在通过初筛后再取，避免对所有航班都发 HTTP 请求）"""
    journeys = item.get("journeys") or [{}]
    journey = journeys[0] if journeys else {}
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

    transfers = []
    transfer_minutes = 0
    for i in range(len(segments) - 1):
        cur_seg = segments[i]
        next_seg = segments[i + 1]
        city = cur_seg.get("arrCityName", "")
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

    # 总时长：segment.duration 求和（飞猪已时区校正）+ 中转停留；起降时间是本地时区，
    # 直接相减会少算时区差
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
    if not total_minutes:
        journey_dur = journey.get("duration") or journey.get("totalDuration")
        if journey_dur is not None:
            try:
                dur = float(journey_dur)
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
    if not total_minutes:
        first_dep = _parse_dt(dep_dt)
        last_arr = _parse_dt(arr_dt)
        if first_dep and last_arr:
            total_minutes = (last_arr - first_dep).total_seconds() / 60.0

    route_text = " → ".join([first.get("depCityName", "")] +
                            [s.get("arrCityName", "") for s in segments])

    transfer_summary = ""
    if transfer_count > 0:
        transfer_summary = "；".join(
            f"{t['city']}（停留{t['stay_str']}，{t['time_window']}）" for t in transfers
        )

    base_price = float(item.get("ticketPrice", 0) or 0)
    jump_url_short = _extract_jump_url(item.get("jumpUrl", ""))
    seat_category = _classify_seat_category(first.get("seatClassName", ""))

    return {
        "direction": direction,
        "price": base_price,        # 初始为票面价，apply_total_price 后变为含税总价
        "base_price": base_price,
        "total_price": base_price,
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
        "jump_url": jump_url_short,
        # key = 方向|实际舱位|航班号|起飞时刻（精确到分钟）
        "dedupe_key": f"{direction}|{seat_category}|{flight_numbers}|{dep_dt[:16]}",
    }


def apply_total_price(f):
    """只有验证了含税总价才返回 True；否则保留未验证票面价且不得进入低价推荐。"""
    base = f["base_price"]
    total = _fetch_total_price(f["jump_url"])
    source = "base"
    if total is None or total <= 0:
        f["price_verified"] = False
        log.warning("无法验证含税价格，跳过航班: %s", f["flight_numbers"])
        return False
    elif base > 0:
        ratio = total / base
        if ratio > 5.0 or ratio < 0.5:
            log.warning("价格异常: %s 票面¥%.0f 含税¥%.0f 比值%.1f，回退票面价",
                        f["flight_numbers"], base, total, ratio)
            f["price_verified"] = False
            log.warning("无法信任含税价格，跳过航班: %s", f["flight_numbers"])
            return False
        else:
            source = "total"
    else:
        source = "total"
    f["price"] = total
    f["total_price"] = total
    f["price_verified"] = True
    log.info("价格: %s %s 票面¥%.0f 含税¥%.0f (source=%s)",
             DIRECTIONS.get(f["direction"], ""), f["flight_numbers"], base, total, source)
    return True


def sort_flights(flights):
    """去程在前、回程在后；同方向按日期、价格升序"""
    return sorted(flights, key=lambda f: (0 if f.get("direction", "outbound") == "outbound" else 1,
                                          f["dep_date"], f["price"]))


def pick_recommended(flights, recommend_cfg=None):
    """（单程模式）挑选推荐航班：价格≤阈值、日期在窗口内、总时长最短"""
    if recommend_cfg is None:
        recommend_cfg = {
            "business_max_price": DEFAULT_RECOMMEND_BUSINESS_MAX_PRICE,
            "economy_max_price": DEFAULT_RECOMMEND_ECONOMY_MAX_PRICE,
            "date_start": DEFAULT_RECOMMEND_DATE_START,
            "date_end": DEFAULT_RECOMMEND_DATE_END,
        }
    date_start = recommend_cfg["date_start"]
    date_end = recommend_cfg["date_end"]

    def pick(category, max_price):
        candidates = [
            f for f in flights
            if f["seat_category"] == category
            and f["price"] <= max_price
            and date_start <= f["dep_date"] <= date_end
            and f.get("total_duration", 0) > 0
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda f: f["total_duration"])
        return candidates[0]

    return {
        "business": pick("business", recommend_cfg["business_max_price"]),
        "economy": pick("economy", recommend_cfg["economy_max_price"]),
    }


# ---------------- 往返配对 ----------------

def pair_round_trips(flights, cabin, min_nights, max_nights, top_n=5):
    """把去程与回程配成往返组合，按总价升序返回前 top_n 个。
    每个“到达日”取最便宜的去程、每个“回程出发日”取最便宜的回程；
    停留晚数 = 回程出发日 - 去程到达日（均为当地日期，可正确处理跨日到达）。"""
    outs, rets = {}, {}
    for f in flights:
        if f["seat_category"] != cabin:
            continue
        if f.get("direction") == "outbound":
            d, bucket = (f.get("arr_time") or "")[:10], outs
        elif f.get("direction") == "return":
            d, bucket = (f.get("dep_time") or "")[:10], rets
        else:
            continue
        if len(d) != 10:
            continue
        if d not in bucket or f["price"] < bucket[d]["price"]:
            bucket[d] = f
    pairs = []
    for arr_d, o in outs.items():
        for dep_d, r in rets.items():
            try:
                nights = (datetime.strptime(dep_d, "%Y-%m-%d") - datetime.strptime(arr_d, "%Y-%m-%d")).days
            except ValueError:
                continue
            if min_nights <= nights <= max_nights:
                pairs.append({"out": o, "ret": r, "nights": nights, "total": o["price"] + r["price"]})
    pairs.sort(key=lambda p: (p["total"], p["nights"]))
    return pairs[:top_n]


def best_pairs(cfg, flights):
    """往返模式下返回 {"economy": [...], "business": [...]}，单程模式返回 {}"""
    if not cfg.get("round_trip"):
        return {}
    return {
        cabin: pair_round_trips(flights, cabin, cfg["min_nights"], cfg["max_nights"], n)
        for cabin, n in (("economy", 5), ("business", 3))
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
    """保存前清理：miss_count >= 3 的记录视为已确认消失；dep_date 异常的坏数据"""
    cleaned = {}
    removed = 0
    for key, v in notified.items():
        mc = v.get("miss_count", 0)
        dep = v.get("dep_date", "")
        if mc >= 3 or not dep or dep == "?":
            removed += 1
            continue
        cleaned[key] = v
    if removed:
        log.info("清理 notified: 移除 %d 条已确认消失/坏数据记录，保留 %d 条", removed, len(cleaned))
    try:
        _atomic_write(NOTIFIED_PATH, json.dumps(cleaned, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning("save_notified 失败: %s", e)


def diff_flights(current_flights, notified, unqueried=None, successful_queries=None):
    """对比本次结果与上次记录，给每个航班打变化标签
    返回 (带标签的航班列表, 变化统计dict, 消失航班列表)
    unqueried: 本次未成功查询的 "方向|日期" 集合（风控跳过 + 接口失败/超时），
    这些日期下的历史记录不参与消失判断。
    消失判定：连续 2 次运行都未出现（miss_count >= 2）才加入 gone"""
    stats = {"new": 0, "cheaper": 0, "higher": 0, "unchanged": 0}
    gone = []
    unqueried_set = set(unqueried or [])
    successful_set = set(successful_queries) if successful_queries is not None else None

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

    for key, prev in notified.items():
        if key in current_keys:
            continue
        prev_dep_date = prev.get("dep_date", "")
        prev_dir = prev.get("direction", "outbound")
        query_key = f"{prev_dir}|{prev_dep_date}"
        # A missing flight is meaningful only after a successful query for its date.
        # Past/unscheduled dates and API failures must never advance miss_count.
        if query_key in unqueried_set or (successful_set is not None and query_key not in successful_set):
            continue
        miss_count = prev.get("miss_count", 0) + 1
        prev["miss_count"] = miss_count
        if miss_count >= 2:
            seat_cat = prev.get("seat_category", "")
            cat_label = "公务舱" if seat_cat == "business" else "经济舱" if seat_cat == "economy" else seat_cat
            gone.append({
                "key": key,
                "price": prev.get("price", 0),
                "dep_date": prev_dep_date,
                "direction": prev_dir,
                "dir_label": DIRECTIONS.get(prev_dir, ""),
                "seat_category": seat_cat,
                "cat_label": cat_label,
                "flight_info": key,
                "last_notified": prev.get("last_notified", ""),
            })

    return current_flights, stats, gone


def update_notified(notified, flights, now_str):
    """本次出现的航班重置 miss_count=0；未出现的保留 diff_flights 中已递增的 miss_count"""
    for f in flights:
        key = f["dedupe_key"]
        prev = notified.get(key, {})
        notified[key] = {
            "price": f["price"],
            "first_seen": prev.get("first_seen", now_str),
            "last_notified": now_str,
            "seat_category": f["seat_category"],
            "direction": f.get("direction", "outbound"),
            "dep_date": f["dep_date"],
            "miss_count": 0,
        }


# ---------------- Server酱 推送 ----------------

def send_serverchan(key, title, desp, max_retries=2):
    """通过 Server酱 推送微信通知；超长正文（>30KB）自动截断航班细节；失败重试"""
    if not key:
        log.warning("serverchan_key 未配置，跳过推送")
        return False
    MAX_BYTES = 30 * 1024
    if len(desp.encode("utf-8")) > MAX_BYTES:
        log.warning("推送正文过大 (> %d KB 限制)，自动截断航班细节", MAX_BYTES // 1024)
        desp = _truncate_for_push(desp, MAX_BYTES)
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("code") == 0:
                    log.info("Server酱 推送成功 (尝试 %d/%d)", attempt + 1, max_retries + 1)
                    return True
                log.warning("Server酱 推送返回非0: %s", body)
        except Exception as e:
            log.error("Server酱 推送失败 (尝试 %d/%d): %s", attempt + 1, max_retries + 1, e)
        if attempt < max_retries:
            time.sleep(3)
    return False


def _truncate_for_push(desp, max_bytes):
    """超长正文截断：保留总结+往返组合/推荐，航班细节列表替换为看板链接"""
    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    link = f"[在线看板]({_md_safe_url(dashboard_url)})" if dashboard_url else "在线看板"
    cut_pos = None
    for marker in ("### 💼 公务舱", "### 公务舱", "### 经济舱"):
        p = desp.find(marker)
        if p > 0 and (cut_pos is None or p < cut_pos):
            cut_pos = p
    if cut_pos:
        suffix = f"\n> 📋 **航班细节内容过大已省略，请在 {link} 查看完整航班明细（含购票链接）**\n\n---\n"
        result = desp[:cut_pos] + suffix
        while len(result.encode("utf-8")) > max_bytes:   # 极少情况：总结部分本身超限，逐步缩减
            cut_pos = int(cut_pos * 0.9)
            result = desp[:cut_pos] + suffix
        return result
    limit = max_bytes // 3
    return desp[:limit] + f"\n\n_内容过大已截断，请查看 {link} 获取完整信息_\n"


def change_text(f):
    c = f.get("change", "")
    if c == "new":
        return "🆕新增"
    if c == "cheaper":
        return f"⬇降价 ¥{f['prev_price']:.0f}→¥{f['price']:.0f}"
    if c == "higher":
        return f"⬆涨价 ¥{f['prev_price']:.0f}→¥{f['price']:.0f}"
    return "—不变"


def is_urgent_drop(f, cfg):
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


def _range_text(cfg):
    out_n = len(generate_dates(cfg["date_start"], cfg["date_end"]))
    text = f"{DIRECTIONS['outbound']} {cfg['date_start']} ~ {cfg['date_end']} ({out_n}天)"
    if cfg.get("round_trip"):
        ret_n = len(generate_dates(cfg["return_date_start"], cfg["return_date_end"]))
        text += (f" · {DIRECTIONS['return']} {cfg['return_date_start']} ~ {cfg['return_date_end']} ({ret_n}天)"
                 f" · 在{cfg['destination_name']}停留 {cfg['min_nights']}~{cfg['max_nights']} 晚")
    return text


def _pair_lines_md(pairs, cabin_label, cfg):
    lines = [f"**{cabin_label}**"]
    if not pairs:
        lines.append("_暂无满足停留晚数的往返组合（去程或回程无命中）_")
        lines.append("")
        return lines
    for i, p in enumerate(pairs, 1):
        lines.append(f"{i}. **¥{p['total']:.0f}** · 在{cfg['destination_name']}停留 {p['nights']} 晚")
        for tag, f in ((DIRECTIONS["outbound"], p["out"]), (DIRECTIONS["return"], p["ret"])):
            lines.append(
                f"   - {tag} {f['dep_date']} {f['flight_numbers']} {_type_label(f)} "
                f"¥{f['price']:.0f} · {f['total_duration_str']} · "
                f"[购票]({_md_safe_url(_extract_jump_url(f['jump_url']))})"
            )
    lines.append("")
    return lines


def format_run_message(all_flights, stats, gone, cfg, blocked_dates, flyai_warn_line=None):
    """格式化本次运行结果推送（总结 + 往返组合/推荐 + 航班细节）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    business = sort_flights([f for f in all_flights if f["seat_category"] == "business"])
    economy = sort_flights([f for f in all_flights if f["seat_category"] == "economy"])
    rt = cfg.get("round_trip")
    dlabel = (lambda f: DIRECTIONS.get(f.get("direction"), "") + " ") if rt else (lambda f: "")

    title = f"{route_title(cfg)}机票监控 命中{len(all_flights)}条(公务{len(business)}/经济{len(economy)})"

    lines = [
        f"## ✈️ {route_title(cfg)} 机票监控结果",
        f"_检测时间: {now}_",
        "",
        "### 📊 本次总结",
        f"- **查询范围**: {_range_text(cfg)}",
        f"- **命中航班**: 公务舱 {len(business)} 条 / 经济舱 {len(economy)} 条（单程价）",
        f"- **变化对比**: 🆕新增 {stats['new']} · ⬇降价 {stats['cheaper']} · ⬆涨价 {stats['higher']} · —不变 {stats['unchanged']} · ❌不再符合 {len(gone)}",
    ]

    urgent_flights = [f for f in all_flights if is_urgent_drop(f, cfg)]
    cheaper_flights = [f for f in all_flights if f.get("change") == "cheaper"]
    if urgent_flights:
        urgent_flights.sort(key=lambda f: f["price"])
        u = urgent_flights[0]
        lines.append(f"- **🚨降价提醒**: {len(urgent_flights)}条紧急降价，最低 ¥{u['price']:.0f} ({dlabel(u)}{u['flight_numbers']}, {u['dep_date']})")
    elif cheaper_flights:
        cheaper_flights.sort(key=lambda f: f["price"])
        c = cheaper_flights[0]
        lines.append(f"- **⬇降价提醒**: {len(cheaper_flights)}条降价，最低 ¥{c['price']:.0f} ({dlabel(c)}{c['flight_numbers']}, {c['dep_date']})")

    if blocked_dates:
        lines.append(f"- **⚠️未查询**: {len(blocked_dates)}个日期因飞猪风控/接口失败未查询")
    if flyai_warn_line:
        lines.append(flyai_warn_line)
    lines.append("")

    if rt:
        pairs = best_pairs(cfg, all_flights)
        lines.append(f"### 🧳 最低价往返组合（{cfg['min_nights']}~{cfg['max_nights']}晚）")
        lines.append("_总价 = 去程 + 回程 单程含税价之和，每个日期取最低价航班_")
        lines.append("")
        lines.extend(_pair_lines_md(pairs.get("economy", []), "💺 经济舱 Top5", cfg))
        lines.extend(_pair_lines_md(pairs.get("business", []), "💼 公务舱 Top3", cfg))
    else:
        rec_cfg = cfg.get("recommend", {})
        rec = pick_recommended(all_flights, rec_cfg)
        lines.append("### ⭐ 推荐航班")
        lines.append(
            f"_筛选: 公务≤¥{rec_cfg.get('business_max_price', DEFAULT_RECOMMEND_BUSINESS_MAX_PRICE)} "
            f"/ 经济≤¥{rec_cfg.get('economy_max_price', DEFAULT_RECOMMEND_ECONOMY_MAX_PRICE)}, "
            f"日期 {rec_cfg.get('date_start', DEFAULT_RECOMMEND_DATE_START)}"
            f"~{rec_cfg.get('date_end', DEFAULT_RECOMMEND_DATE_END)}, 总时长最短_"
        )
        lines.append("")
        for cat, label in [("business", "💼 公务舱"), ("economy", "💺 经济舱")]:
            f = rec[cat]
            if not f:
                lines.append(f"- **{label}**: 无")
                continue
            dep_t = f["dep_time"][11:16] if len(f["dep_time"]) >= 16 else ""
            arr_t = f["arr_time"][11:16] if len(f["arr_time"]) >= 16 else ""
            lines.append(f"- **{label}**: {f['dep_date']} {_type_label(f)} ¥{f['price']:.0f} · 总时长{f['total_duration_str']}")
            lines.append(f"  - 航班: {f['flight_numbers']}")
            lines.append(f"  - 起降: {f['dep_airport']} {dep_t} → {f['arr_airport']} {arr_t}")
            if f["transfer_count"] > 0:
                lines.append(f"  - 中转: {f['transfer_summary']}")
            lines.append(f"  - 购票: [点击购票]({_md_safe_url(_extract_jump_url(f['jump_url']))})")
        lines.append("")

    def render_flight(f, idx):
        dep_t = f["dep_time"][11:16] if len(f["dep_time"]) >= 16 else ""
        arr_t = f["arr_time"][11:16] if len(f["arr_time"]) >= 16 else ""
        out = [
            f"**{idx}. {dlabel(f)}{f['dep_date']} {_type_label(f)} ¥{f['price']:.0f}** {change_text(f)}",
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

    lines.append("### 💼 公务舱（单程≤¥%.0f）" % cfg["business_max_price"])
    if business:
        for i, f in enumerate(business, 1):
            lines.append(render_flight(f, i))
    else:
        lines.append("_无符合条件航班_")
        lines.append("")

    lines.append("### 💺 经济舱（单程≤¥%.0f）" % cfg["economy_max_price"])
    if economy:
        for i, f in enumerate(economy, 1):
            lines.append(render_flight(f, i))
    else:
        lines.append("_无符合条件航班_")
        lines.append("")

    if gone:
        lines.append("### ❌ 不再符合条件航班（连续2次未出现，可能涨价超阈值或停售）")
        for g in gone[:10]:
            dep = g.get("dep_date", "")
            cat = g.get("cat_label", "")
            dl = (g.get("dir_label", "") + " ") if rt else ""
            if dep and cat:
                lines.append(f"- {dl}{dep} {cat} ¥{g['price']:.0f}")
            else:
                lines.append(f"- {g['key']} ¥{g['price']:.0f}")
        if len(gone) > 10:
            lines.append(f"_...及其它 {len(gone)-10} 条_")
        lines.append("")

    lines.append("---")
    lines.append("_⚠️ 价格为含税总价，实际购票价格可能因动态调价而略有不同_")
    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    if dashboard_url:
        lines.append(f"_数据来源: 飞猪 flyai | 在线看板: {dashboard_url}_")
    else:
        lines.append("_数据来源: 飞猪 flyai | 完整看板见本地 dashboard.html_")
    return title, "\n".join(lines)


# ---------------- HTML 看板 ----------------

def generate_html(all_flights, stats, gone, cfg, blocked_dates):
    """生成 HTML 看板"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    business = sort_flights([f for f in all_flights if f["seat_category"] == "business"])
    economy = sort_flights([f for f in all_flights if f["seat_category"] == "economy"])
    rt = cfg.get("round_trip")
    dlabel = (lambda f: DIRECTIONS.get(f.get("direction"), "") + " ") if rt else (lambda f: "")

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
            dep_t = f["dep_time"][11:16] if len(f["dep_time"]) >= 16 else ""
            arr_t = f["arr_time"][11:16] if len(f["arr_time"]) >= 16 else ""
            url = html.escape(_extract_jump_url(f["jump_url"]))
            route_lines = [f'<b>{html.escape(f["route"])}</b>']
            for t in f["transfers"]:
                route_lines.append(
                    f"{html.escape(t['city'])}<br>"
                    f"<span class='muted'>停留{html.escape(t['stay_str'])}，{html.escape(t['time_window'])}</span>"
                )
            route_cell = "<hr class='seg-sep'>".join(route_lines)
            rows.append(f"""
<tr>
  <td class="date">{dlabel(f)}{f['dep_date']}</td>
  <td>{_type_label(f)}</td>
  <td class="price">¥{f['price']:.0f}</td>
  <td>{html.escape(f['flight_numbers'])}</td>
  <td>{html.escape(f['dep_airport'])} {dep_t}<br><span class="muted">→</span> {html.escape(f['arr_airport'])} {arr_t}</td>
  <td class="route">{route_cell}</td>
  <td class="dur">{html.escape(f['total_duration_str'])}</td>
  <td>{change_badge(f)}</td>
  <td><a href="{url}" target="_blank" title="实际价格以飞猪页面为准">购票</a></td>
</tr>""")
        return """<table>
<thead><tr><th>出发日期</th><th>类型</th><th>价格</th><th>航班号</th><th>起降</th><th>路线</th><th>总时长</th><th>变化</th><th>操作</th></tr></thead>
<tbody>""" + "".join(rows) + "</tbody></table>"

    gone_html = ""
    if gone:
        gone_rows = []
        for g in gone:
            dep = g.get("dep_date", "")
            cat = g.get("cat_label", "")
            dl = (g.get("dir_label", "") + " ") if rt else ""
            if dep and cat:
                label = f"{html.escape(dl)}{html.escape(dep)} {html.escape(cat)} ¥{g['price']:.0f}"
            else:
                label = f"{html.escape(g['key'])} ¥{g['price']:.0f}"
            gone_rows.append(
                f"<li>{label} <span class='muted'>({html.escape(g.get('last_notified',''))})</span></li>"
            )
        gone_html = f"<div class='gone'><h3>❌ 不再符合条件航班（{len(gone)}）</h3><ul>{''.join(gone_rows)}</ul></div>"

    blocked_html = ""
    if blocked_dates:
        blocked_html = f"<div class='warn'>⚠️ 以下日期因飞猪风控/接口失败未能查询: {html.escape(', '.join(blocked_dates))}</div>"

    def leg_cell(f):
        url = html.escape(_extract_jump_url(f["jump_url"]))
        return (f"{f['dep_date']} · {html.escape(f['flight_numbers'])}<br>"
                f"<span class='muted'>{_type_label(f)} · {html.escape(f['total_duration_str'])}</span> "
                f"<b class='price'>¥{f['price']:.0f}</b> "
                f"<a href='{url}' target='_blank'>购票</a>")

    def render_pairs(pairs, label):
        if not pairs:
            return f"<h3>{label}</h3><p class='empty'>暂无满足停留晚数的往返组合</p>"
        rows = "".join(
            f"<tr><td class='price'>¥{p['total']:.0f}</td><td>{p['nights']} 晚</td>"
            f"<td>{leg_cell(p['out'])}</td><td>{leg_cell(p['ret'])}</td></tr>"
            for p in pairs
        )
        return (f"<h3>{label}</h3><table><thead><tr><th>往返总价</th>"
                f"<th>在{html.escape(cfg['destination_name'])}停留</th><th>去程</th><th>回程</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    if rt:
        pairs = best_pairs(cfg, all_flights)
        recommend_html = f"""<div class="recommend">
<h2>🧳 最低价往返组合（停留 {cfg['min_nights']}~{cfg['max_nights']} 晚）</h2>
<p class="muted">总价 = 去程 + 回程 单程含税价之和，每个日期取最低价航班</p>
{render_pairs(pairs.get('economy', []), '💺 经济舱 Top5')}
{render_pairs(pairs.get('business', []), '💼 公务舱 Top3')}
</div>"""
    else:
        rec_cfg = cfg.get("recommend", {})
        rec = pick_recommended(all_flights, rec_cfg)
        rec_cards = []
        for cat, label, emoji in [("business", "公务舱", "💼"), ("economy", "经济舱", "💺")]:
            f = rec[cat]
            if not f:
                rec_cards.append(f'<div class="rec-card"><h3>{emoji} {label}</h3><p class="empty">无</p></div>')
                continue
            dep_t = f["dep_time"][11:16] if len(f["dep_time"]) >= 16 else ""
            arr_t = f["arr_time"][11:16] if len(f["arr_time"]) >= 16 else ""
            url = html.escape(_extract_jump_url(f["jump_url"]))
            transfer_html = ""
            if f["transfer_count"] > 0:
                transfer_html = f"<div class='rec-info'>中转: {html.escape(f['transfer_summary'])}</div>"
            rec_cards.append(f"""
<div class="rec-card">
<h3>{emoji} {label} <span class="rec-price">¥{f['price']:.0f}</span></h3>
<div class="rec-info">{f['dep_date']} · {_type_label(f)} · 总时长 {html.escape(f['total_duration_str'])}</div>
<div class="rec-info">{html.escape(f['flight_numbers'])}</div>
<div class="rec-info">{html.escape(f['dep_airport'])} {dep_t} → {html.escape(f['arr_airport'])} {arr_t}</div>
{transfer_html}
<a href="{url}" target="_blank" class="rec-buy" title="实际价格以飞猪页面为准">点击购票</a>
</div>""")
        recommend_html = f"""<div class="recommend">
<h2>⭐ 推荐航班</h2>
<p class="muted">筛选: 公务≤¥{rec_cfg.get('business_max_price', DEFAULT_RECOMMEND_BUSINESS_MAX_PRICE)} / 经济≤¥{rec_cfg.get('economy_max_price', DEFAULT_RECOMMEND_ECONOMY_MAX_PRICE)}, 日期 {rec_cfg.get('date_start', DEFAULT_RECOMMEND_DATE_START)}~{rec_cfg.get('date_end', DEFAULT_RECOMMEND_DATE_END)}, 总时长最短</p>
<div class="rec-row">{''.join(rec_cards)}</div>
</div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{route_title(cfg)}机票监控看板</title>
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
td.price,b.price{{font-weight:700;color:#d4380d;white-space:nowrap}}
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
.recommend{{margin:16px 0;padding:16px 20px;background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);border-left:4px solid #faad14;overflow-x:auto}}
.recommend h2{{margin-top:0;color:#874d00}}
.rec-row{{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px}}
.rec-card{{flex:1;min-width:280px;padding:14px 16px;background:#fffbf0;border:1px solid #ffe58f;border-radius:8px}}
.rec-card h3{{margin:0 0 8px 0;color:#333}}
.rec-price{{float:right;color:#d4380d;font-weight:700;font-size:18px}}
.rec-info{{font-size:13px;color:#555;margin:3px 0;line-height:1.5}}
.rec-buy{{display:inline-block;margin-top:8px;padding:6px 16px;background:#1a4fbf;color:#fff!important;border-radius:4px;font-size:13px}}
.rec-buy:hover{{background:#16409e;text-decoration:none}}
.price-note{{color:#999;font-size:12px;margin:4px 0 8px 0}}
</style></head><body>
<h1>✈️ {route_title(cfg)} 机票监控看板</h1>
<p class="update">最近更新: {now}</p>
<p class="price-note">⚠️ 价格为含税总价（API 票面价 + 税费），购票链接打开后为飞猪航班深链，实际价格可能因动态调价而略有不同。</p>
<div class="summary">
<div><b>查询范围:</b> {html.escape(_range_text(cfg))}</div>
<div><b>命中航班:</b> 公务舱 {len(business)} 条 / 经济舱 {len(economy)} 条（单程价）</div>
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
<h2>💼 公务舱（单程≤¥{cfg['business_max_price']:.0f}）</h2>
{render_table(business)}
<h2>💺 经济舱（单程≤¥{cfg['economy_max_price']:.0f}）</h2>
{render_table(economy)}
{gone_html}
<hr><p class="muted">数据来源: 飞猪 flyai-cli · 本看板由 monitor.py 自动生成</p>
<p class="muted">🌐 在线看板: <a href="{os.environ.get('DASHBOARD_URL', '#')}">{os.environ.get('DASHBOARD_URL', '未设置')}</a></p>
</body></html>"""
    try:
        _atomic_write(DASHBOARD_PATH, html_content)
        log.info("已更新本地看板 %s", DASHBOARD_PATH)
    except Exception as e:
        log.warning("generate_html 保存失败: %s", e)


# ---------------- 主流程 ----------------

def build_tasks(cfg, today_str):
    """生成查询任务 [(direction, origin, destination, date)]，已过去的日期跳过；
    往返模式下去程/回程交替排列，这样中途被风控打断时两个方向都有覆盖"""
    legs = [("outbound", cfg["origin"], cfg["destination"], cfg["date_start"], cfg["date_end"])]
    if cfg.get("round_trip"):
        legs.append(("return", cfg["destination"], cfg["origin"],
                     cfg["return_date_start"], cfg["return_date_end"]))
    per_leg = []
    for direction, o, d, s, e in legs:
        per_leg.append([(direction, o, d, dt) for dt in generate_dates(s, e) if dt >= today_str])
    tasks = []
    for group in itertools.zip_longest(*per_leg):
        tasks.extend(t for t in group if t is not None)
    return tasks


def main():
    log.info("=" * 50)
    cfg = load_config()
    log.info("开始监控 %s 机票（%s）", route_title(cfg), "往返" if cfg["round_trip"] else "单程")

    phase, should_run, max_push_per_day = get_current_phase(cfg)
    phase_names = {0: "已停止", 1: "抓低价", 2: "稳观察", 3: "冲刺期"}
    log.info("当前阶段: %s(%d) | 应执行查询: %s | 推送额度: %d条/天",
             phase_names[phase], phase, should_run, max_push_per_day)

    if not should_run:
        if phase == 0:
            log.info("监控已停止（截止日 %s 已过），如需重启请修改 config 的 monitor_end", cfg["schedule"]["monitor_end"])
        else:
            log.info("当前为%s阶段，当前时段不执行查询，跳过（节省额度）", phase_names[phase])
        # 注意：跳过时不再生成看板。否则 workflow 会把用残缺数据拼出的看板
        # 部署到 Pages，覆盖掉上一次的完整看板。
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    tasks = build_tasks(cfg, today_str)
    log.info("本轮共 %d 个查询任务（%s）", len(tasks), _range_text(cfg))

    # 飞猪 API 忽略 --seat-class-name，每个日期只查一次，用较高阈值预筛，
    # 再按 seatClassName 实际归类后用对应阈值过滤
    api_max_price = max(cfg["business_max_price"], cfg["economy_max_price"])
    category_max_price = {
        "business": cfg["business_max_price"],
        "economy": cfg["economy_max_price"],
        "first": cfg.get("first_max_price", 999999),
    }

    found = {}                 # dedupe_key -> flight
    query_count = 0
    fail_count = 0
    api_fail_total = 0
    blocked = False
    unqueried = set()          # "方向|日期"：本轮未成功查询（风控跳过/接口失败）
    successful_queries = set()
    unqueried_labels = []
    skip_parse = skip_transfer = skip_price = 0

    for idx, (direction, o, d, date) in enumerate(tasks):
        tag = f"{direction}|{date}"
        label = f"{DIRECTIONS[direction]} {date}" if cfg["round_trip"] else date
        if blocked:
            unqueried.add(tag)
            unqueried_labels.append(label)
            continue
        items, is_blocked, api_failed = run_flyai(cfg, date, "", api_max_price, origin=o, destination=d)
        query_count += 1
        if is_blocked or api_failed:
            unqueried.add(tag)
            unqueried_labels.append(label)
            fail_count += 1
            if api_failed:
                api_fail_total += 1
            if fail_count >= RISK_BLOCK_FAIL_THRESHOLD:
                reason = "风控" if is_blocked else "API连续失败"
                log.warning("连续 %d 次%s，提前结束本轮剩余查询", fail_count, reason)
                blocked = True
                continue
        else:
            fail_count = 0
            successful_queries.add(tag)
        for item in items:
            f = parse_flight(item, direction)
            if not f:
                skip_parse += 1
                continue
            if f["transfer_count"] > cfg["max_transfers"]:
                skip_transfer += 1
                continue
            cat_max = category_max_price.get(f["seat_category"], 999999)
            # 先用票面价预筛：含税价 >= 票面价，票面价已超阈值就不必再发请求取含税价
            if f["base_price"] > cat_max:
                skip_price += 1
                continue
            if not apply_total_price(f):
                skip_price += 1
                continue
            if f["price"] > cat_max:
                skip_price += 1
                log.info("剔除(超价): %s %s %s 含税¥%.0f > ¥%.0f",
                         f["dep_date"], f["flight_numbers"], f["seat_category"], f["price"], cat_max)
                continue
            prev_f = found.get(f["dedupe_key"])
            if prev_f is None or f["price"] < prev_f["price"]:
                found[f["dedupe_key"]] = f
        if idx < len(tasks) - 1:
            time.sleep(cfg.get("query_interval_sec", 12))

    all_flights = list(found.values())
    log.info("筛选统计: parse失败=%d 中转过滤=%d 超价过滤=%d 命中=%d",
             skip_parse, skip_transfer, skip_price, len(all_flights))
    log.info("查询完成: %d 次查询, 命中 %d 条, 接口/风控失败 %d 次, 未查询 %d 个日期",
             query_count, len(all_flights), api_fail_total, len(unqueried))
    if api_fail_total > 0 and api_fail_total >= query_count * 0.5:
        log.warning("⚠️ 本次 API 失败率 %.0f%%，请检查 flyai_api_key 额度或参数配置",
                    api_fail_total / max(query_count, 1) * 100)

    notified = load_notified()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_flights, stats, gone = diff_flights(
        all_flights, notified, unqueried, successful_queries=successful_queries
    )
    log.info("变化: 新增%d 降价%d 涨价%d 不变%d 不再符合%d",
             stats["new"], stats["cheaper"], stats["higher"], stats["unchanged"], len(gone))

    # Do not replace a previously valid dashboard with an empty page after total API failure.
    all_queries_failed = query_count > 0 and api_fail_total == query_count
    if all_queries_failed:
        log.warning("本轮全部 API 请求失败，保留上一次发布的看板")
    else:
        generate_html(all_flights, stats, gone, cfg, unqueried_labels)

    push_history = load_push_history()
    pushed = False

    add_flyai_calls(push_history, query_count)
    total_calls = get_flyai_calls_total(push_history)
    flyai_warn_line = None
    if total_calls >= FLYAI_WARN_THRESHOLD:
        pct = total_calls / 5000 * 100
        flyai_warn_line = (
            f"⚠️ **飞猪 API 额度告警**: 已累计调用 {total_calls} 次 / 5000（约 {pct:.0f}%）。"
            f"请留意剩余额度，即将耗尽时可在飞猪申请日包。"
        )
        log.warning(flyai_warn_line)

    has_content = len(all_flights) > 0 or len(gone) > 0
    fail_rate = api_fail_total / max(query_count, 1)
    is_api_abnormal = fail_rate >= 0.5 or query_count == 0

    if has_content:
        if can_push(push_history, max_push_per_day):
            title, desp = format_run_message(all_flights, stats, gone, cfg, unqueried_labels,
                                             flyai_warn_line=flyai_warn_line)
            if send_serverchan(cfg.get("serverchan_key", ""), title, desp):
                record_push(push_history)
                pushed = True
                log.info("已推送本次运行结果 (命中%d条, 不再符合%d条)", len(all_flights), len(gone))
            else:
                log.warning("推送失败")
        else:
            log.info("今日推送已达阶段上限(%d条)，跳过推送", max_push_per_day)
    elif is_api_abnormal:
        if can_push(push_history, max_push_per_day):
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            title = f"⚠️ {route_title(cfg)}机票监控 抓取异常"
            dashboard_url = os.environ.get("DASHBOARD_URL", "")
            desp_parts = [
                "## ⚠️ 抓取异常告警",
                f"_检测时间: {now}_",
                "",
                "### 📊 异常详情",
                f"- **查询范围**: {_range_text(cfg)}",
                f"- **API 调用**: {query_count} 次（失败 {api_fail_total} 次）",
                f"- **失败率**: {fail_rate * 100:.0f}%",
            ]
            if unqueried_labels:
                desp_parts.append(f"- **未查询**: {len(unqueried_labels)} 个日期（风控或接口失败）")
            if query_count == 0:
                desp_parts.append("- **⚠️ 0次查询**: 没有可查询的日期或被风控完全拦截")
            if flyai_warn_line:
                desp_parts.append("")
                desp_parts.append(flyai_warn_line)
            desp_parts.append("")
            desp_parts.append("_请检查 flyai_api_key 额度或风控状态，或稍后重试_")
            if dashboard_url:
                desp_parts.append(f"_在线看板: {dashboard_url}_")
            if send_serverchan(cfg.get("serverchan_key", ""), title, "\n".join(desp_parts)):
                record_push(push_history)
                pushed = True
                log.info("已推送抓取异常告警")
            else:
                log.warning("异常告警推送失败")
        else:
            log.info("今日推送已达阶段上限，跳过异常告警推送")
    else:
        log.info("抓取正常但0命中，不推送（仅更新看板），失败率: %.0f%%", fail_rate * 100)

    save_push_history(push_history)

    update_notified(notified, all_flights, now_str)
    save_notified(notified)

    if not pushed:
        log.info("本次未推送微信，仅更新看板与记录")

    if all_queries_failed:
        raise RuntimeError("全部飞猪 API 查询失败；已记录告警并保留旧看板")
    log.info("监控结束，看板: %s", DASHBOARD_PATH)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("监控异常: %s", e)
        sys.exit(1)
