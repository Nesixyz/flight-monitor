#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
机票价格监控（支持去程/回程）

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


def _is_manual_run() -> bool:
    """MANUAL_RUN 宽松判断：支持 '1'/'true'/'True'/'yes' 等。
    GitHub Actions inputs 的 value='true' 在表达式中与 'true' 比较，
    返回值是 Boolean true，序列化到 env 会变成字符串 "True"，
    所以不能只判断 == "1"。"""
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

# 推荐航班筛选默认值（可被 config.json recommend 字段覆盖，便于临时调整而不改代码）
DEFAULT_RECOMMEND_BUSINESS_MAX_PRICE = 10000
DEFAULT_RECOMMEND_ECONOMY_MAX_PRICE = 5000
DEFAULT_RECOMMEND_DATE_START = "2026-10-06"
DEFAULT_RECOMMEND_DATE_END = "2026-10-09"

# 飞猪 API 累计调用阈值：超过则在推送开头给额度告警提醒
#   首赠 5000 次，取 90% 做首次告警
FLYAI_WARN_THRESHOLD = 4500


# ---------------- 基础工具 ----------------

def load_config():
    """加载配置并校验必填字段
    缺失关键字段时 log.error 明确列出缺了什么，避免后续 KeyError 难以定位
    可选字段（recommend/push）缺失时使用默认值兜底"""
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
    }
    cfg.setdefault("origin_name", CITY_NAMES.get(cfg.get("origin", ""), cfg.get("origin", "出发地")))
    cfg.setdefault("destination_name", CITY_NAMES.get(cfg.get("destination", ""), cfg.get("destination", "目的地")))

    # 必填字段校验：缺失时明确报错，避免 KeyError 在后续调用栈深处暴露
    required_fields = {
        "origin": "出发地（三字码，如 SHA/PVG）",
        "destination": "目的地（三字码，如 PAR/CDG）",
        "date_start": "出发日期起始（YYYY-MM-DD）",
        "date_end": "出发日期截止（YYYY-MM-DD）",
        "business_max_price": "公务舱价格上限",
        "economy_max_price": "经济舱价格上限",
        "max_transfers": "最大中转次数（0=直飞, 1=最多1次中转）",
    }
    missing = [f for f in required_fields if f not in cfg or cfg.get(f) in (None, "")]
    if missing:
        log.error("config.json 缺失必填字段: %s", ", ".join(missing))
        for f in missing:
            log.error("  - %s: %s", f, required_fields[f])
        sys.exit(1)

    # schedule 子字段校验（get_current_phase 强依赖）
    schedule = cfg.get("schedule", {})
    schedule_required = ["phase1_end", "phase2_end", "monitor_end"]
    schedule_missing = [f for f in schedule_required if f not in schedule]
    if schedule_missing:
        log.error("config.json schedule 字段缺失: %s", ", ".join(schedule_missing))
        log.error("请参考 config.example.json 中的 schedule 配置")
        sys.exit(1)

    # 可选字段：recommend（推荐航班筛选条件，未配置时用默认值）
    rec_cfg = cfg.get("recommend", {}) or {}
    cfg["recommend"] = {
        "business_max_price": rec_cfg.get("business_max_price", DEFAULT_RECOMMEND_BUSINESS_MAX_PRICE),
        "economy_max_price": rec_cfg.get("economy_max_price", DEFAULT_RECOMMEND_ECONOMY_MAX_PRICE),
        "date_start": rec_cfg.get("date_start", DEFAULT_RECOMMEND_DATE_START),
        "date_end": rec_cfg.get("date_end", DEFAULT_RECOMMEND_DATE_END),
    }

    # 可选字段：push（保证至少有默认阈值）
    push_cfg = cfg.get("push", {}) or {}
    cfg["push"] = {
        "urgent_drop_pct": push_cfg.get("urgent_drop_pct", 5),
        "urgent_drop_abs": push_cfg.get("urgent_drop_abs", 500),
    }

    # API key 校验（缺失会导致查询失败但不立即退出，给出明确警告）
    if not cfg.get("flyai_api_key"):
        log.warning("flyai_api_key 未配置，查询将失败！请配置 FLYAI_API_KEY 环境变量或 config.json 字段")
    if not cfg.get("serverchan_key"):
        log.warning("serverchan_key 未配置，将无法推送微信通知")

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
    if _is_manual_run():
        return phase, True, phase_max_push[phase]

    return phase, current_hour in phase_hours, phase_max_push[phase]


# ---------------- 推送历史 + API 用量 ----------------

def load_push_history():
    """读取 push_history.json 并做旧格式迁移：
    - 每日记录若为旧类型分计格式 {urgent:1, summary:2} → 迁移为 {run:3}
    - 旧的 int 格式（直接存数字）→ 迁移为 {run: N}
    - 同时保留 _meta.flyai_calls_total 用于 API 调用次数累计
    """
    if not PUSH_HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(PUSH_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # 保留元信息（flyai_calls_total 等），独立于日期字段
    meta = data.pop("_meta", {}) if "_meta" in data else {}
    migrated_any = False
    today_prefix = datetime.now().strftime("%Y-%")
    # 所有形如 YYYY-MM-DD 的 key 视为日期记录
    new_data = {}
    for k, v in data.items():
        if len(k) == 10 and k[4] == "-" and k[7] == "-":
            # 日期记录
            if isinstance(v, int) or isinstance(v, float):
                # 旧 int 格式 → {run: v}
                new_data[k] = {"run": int(v)}
                migrated_any = True
            elif isinstance(v, dict):
                if "run" not in v and len(v) > 0:
                    # 旧类型分计 {urgent:1, summary:2} → {run: 3}
                    new_data[k] = {"run": sum(v.values())}
                    migrated_any = True
                else:
                    new_data[k] = v
            else:
                new_data[k] = v
        else:
            # 非日期字段原样保留（兼容未来扩展）
            new_data[k] = v
    if meta:
        new_data["_meta"] = meta
    if migrated_any:
        log.info("push_history 格式已迁移（旧类型分计 → 统一 run 计数）")
    return new_data


def save_push_history(history):
    try:
        _atomic_write(
            PUSH_HISTORY_PATH,
            json.dumps(history, ensure_ascii=False, indent=2)
        )
    except Exception as e:
        # 保存失败不致整个作业崩溃，仅告警（下次运行会丢失计数 → 可能多推1条，影响极小）
        log.warning("save_push_history 失败: %s", e)


def get_flyai_calls_total(history):
    """读取累计 flyai API 调用次数（保存在 _meta 中）"""
    return int(history.get("_meta", {}).get("flyai_calls_total", 0))


def add_flyai_calls(history, n):
    """累计 flyai API 调用次数 n 次"""
    if n <= 0:
        return
    if "_meta" not in history:
        history["_meta"] = {}
    history["_meta"]["flyai_calls_total"] = (
        int(history["_meta"].get("flyai_calls_total", 0)) + n
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


# generate_dates 结果缓存（监控日期范围在一次运行中固定）
_DATES_CACHE = {}


def generate_dates(start, end):
    """生成 [start, end] 闭区间的所有日期，格式 YYYY-MM-DD
    同一 start/end 在单次运行中只会解析一次，避免重复调用"""
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

def _call_flyai_once(cmd, env, timeout, dep_date):
    """执行一次 flyai CLI 调用，返回 (itemList, blocked, api_failed, log_info)
    log_info: (status, message, item_count) 用于外层判断是否需要降级"""
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
            is_empty_result = any(s in message for s in empty_indicators)
            if is_empty_result:
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


def run_flyai(cfg, dep_date, seat_class, max_price):
    """调用 flyai search-flight，返回 (itemList, blocked, api_failed)
    - itemList: 飞猪返回的航班列表，失败时为空列表
    - blocked: True 表示触发风控(403)，建议停止后续查询
    - api_failed: True 表示 API 调用本身失败（非0且非"结果为空"），
                  主流程会累加 fail_count，连续失败达到阈值提前结束

    不传 --journey-type 参数，让 API 返回直达+中转混合结果，
    由本地代码按 max_transfers 过滤。
    --journey-type 2 + --sort-type 3(价格升序) 只返回最便宜的 10 条，
    全是 2 次中转航班，1 次中转被截断。"""
    env = os.environ.copy()
    if cfg.get("flyai_api_key"):
        env["FLYAI_API_KEY"] = cfg["flyai_api_key"]
    timeout = cfg.get("query_timeout_sec", 30)

    cmd = get_flyai_cmd() + [
        "search-flight",
        "--origin", cfg["origin"],
        "--destination", cfg["destination"],
        "--dep-date", dep_date,
        "--max-price", str(max_price),
        "--sort-type", "3",
    ]
    if seat_class:
        cmd.extend(["--seat-class-name", seat_class])

    log.info("查询 %s (timeout=%ss, max_transfers=%d)",
             dep_date, timeout, cfg.get("max_transfers", 0))
    items, blocked, api_failed, _ = _call_flyai_once(cmd, env, timeout, dep_date)
    return items, blocked, api_failed


# _parse_dt 支持的格式（按优先级尝试，越常用越靠前）
_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",   # 飞猪返回的标准格式
    "%Y-%m-%d %H:%M",      # 某些接口偶发只返回 HH:MM 不含秒
    "%Y/%m/%d %H:%M:%S",   # 斜杠分隔变体
    "%Y-%m-%dT%H:%M:%S",   # ISO 格式
)


def _parse_dt(s):
    """解析多种 datetime 格式为 datetime，失败返回 None
    避免飞猪接口微调格式时导致中转时长等字段算不出来"""
    if not s:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
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


def _fetch_total_price(jump_url):
    """从飞猪购票短链接的跳转 URL 中提取含税总价
    jumpUrl 短链 302 跳转后的 Location header 含 fpt 参数，格式如：
    fpt=qwen(openclaw)flap(6537)flp(8898)ai2c(sk.clawhub)
    - flap(xxx) = 不含税票面价（= API 的 ticketPrice）
    - flp(xxx)  = 含税总价（= 用户实际购票支付价）
    返回含税总价 float，提取失败返回 None"""
    if not jump_url:
        return None
    try:
        import http.client as http_client
        parsed = urllib.parse.urlparse(jump_url)
        conn = http_client.HTTPSConnection(parsed.hostname, timeout=10)
        conn.request("GET", parsed.path + "?" + parsed.query, headers={
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


def _classify_seat_category(seat_class_name):
    """根据 API 返回的 seatClassName 归类为 business/economy/other
    飞猪 API 忽略 --seat-class-name 参数，需按实际返回值归类"""
    if not seat_class_name:
        return "economy"  # 兜底
    if "经济" in seat_class_name:
        return "economy"
    if "公务" in seat_class_name or "商务" in seat_class_name:
        return "business"
    if "头等" in seat_class_name:
        return "first"
    return "economy"  # 未知舱位兜底为经济


def parse_flight(item, seat_category):
    """从原始 item 提取结构化航班信息
    route: 中转信息（城市名/停留时长/到达-出发时间）
    total_duration: 飞猪返回的该行程总时长"""
    # 空列表 [] 不走 [{}] 兜底，加一层 or 确保 journeys=[] 时也能降级为 {}
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

    base_price = float(item.get("ticketPrice", 0) or 0)
    jump_url_raw = item.get("jumpUrl", "")
    jump_url_short = _extract_jump_url(jump_url_raw)
    total_price = _fetch_total_price(jump_url_short)
    price_source = "base"
    if total_price is None:
        total_price = base_price
    elif total_price > 0 and base_price > 0:
        ratio = total_price / base_price
        if ratio > 5.0 or ratio < 0.5:
            log.warning("价格异常: %s 票面¥%.0f 含税¥%.0f 比值%.1f，回退票面价",
                        flight_numbers, base_price, total_price, ratio)
            total_price = base_price
            price_source = "base(fallback)"
        else:
            price_source = "total"
    elif total_price > 0:
        price_source = "total"
    log.info("价格: %s 票面¥%.0f 含税¥%.0f (source=%s)",
             flight_numbers, base_price, total_price, price_source)

    return {
        "price": total_price,  # 主价格字段用含税总价
        "base_price": base_price,  # 不含税票面价（保留参考）
        "total_price": total_price,
        "seat_class": first.get("seatClassName", ""),
        # 按实际舱位归类（飞猪 API 忽略 --seat-class-name 参数，返回值可能与查询不符）
        "seat_category": _classify_seat_category(first.get("seatClassName", "")),
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
        # dedupe_key 含 dep_time（精确到分钟）：
        # 同一天同舱位同航班号但不同起飞时刻视为不同行程
        # 避免留学生价/普通价等不同价格条件的航班被错误合并
        "dedupe_key": f"{seat_category}|{flight_numbers}|{dep_dt[:16]}",
    }


def pick_recommended(flights, recommend_cfg=None):
    """从本次命中航班中挑选推荐航班（公务舱/经济舱各 1 条）
    条件：价格 ≤ 推荐阈值，出发日期在推荐日期窗口内，总时长有效
    排序：总时长越短越好
    返回 {"business": f_or_None, "economy": f_or_None}
    recommend_cfg: 从 cfg["recommend"] 传入，缺省时使用默认值兜底"""
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


# ---------------- 去重记录 ----------------

def load_notified():
    if NOTIFIED_PATH.exists():
        try:
            return json.loads(NOTIFIED_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_notified(notified):
    """保存前去重清理：
    - miss_count >= 3 的记录视为已确认消失，清理掉（已在 gone 中展示过）
      避免 notified.json 无限膨胀 + 下次运行重复进 gone 列表造成噪音
    - dep_date 异常（空或 '?'）的坏数据也清理掉"""
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
        _atomic_write(
            NOTIFIED_PATH,
            json.dumps(cleaned, ensure_ascii=False, indent=2)
        )
    except Exception as e:
        # notified 写失败不致整体崩溃；下次运行会把同航班标为新增，影响极小
        log.warning("save_notified 失败: %s", e)


def diff_flights(current_flights, notified, blocked_dates=None):
    """对比本次结果与上次记录，给每个航班打变化标签
    返回 (带标签的航班列表, 变化统计dict, 消失航班列表)
    blocked_dates: 本次因风控未查询的日期列表，这些日期的 notified 记录
    不参与消失判断，避免误判
    消失判定：连续 2 次运行都未出现（miss_count >= 2）才加入 gone，
    单次未出现只递增 miss_count，避免飞猪价格波动导致误报"""
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
    # 注意：miss_count >= 3 的记录上次已被 save_notified 清理，这里最多看到 2
    for key, prev in notified.items():
        if key in current_keys:
            continue
        prev_dep_date = prev.get("dep_date", "")
        if prev_dep_date in blocked_set:
            continue
        miss_count = prev.get("miss_count", 0) + 1
        prev["miss_count"] = miss_count
        if miss_count >= 2:
            # 带上结构化字段，便于友好展示（解决 gone 列表只显示原始 key 的问题）
            seat_cat = prev.get("seat_category", "")
            cat_label = "公务舱" if seat_cat == "business" else "经济舱" if seat_cat == "economy" else seat_cat
            gone.append({
                "key": key,
                "price": prev.get("price", 0),
                "dep_date": prev_dep_date,
                "seat_category": seat_cat,
                "cat_label": cat_label,
                "flight_info": key,  # 兜底，key 中含航班号信息
                "last_notified": prev.get("last_notified", ""),
            })

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

def send_serverchan(key, title, desp, max_retries=2):
    """通过 Server酱 推送微信通知
    - 超长正文（>30KB）会自动截断航班细节，保留总结+推荐，加看板链接引导
    - 失败后重试 max_retries 次，每次间隔 3s"""
    MAX_BYTES = 30 * 1024  # Server酱 约 32KB 限制，留 2KB 余量
    desp_bytes = desp.encode("utf-8")
    if len(desp_bytes) > MAX_BYTES:
        log.warning("推送正文过大 (%d KB > %d KB 限制)，自动截断航班细节",
                    len(desp_bytes) // 1024, MAX_BYTES // 1024)
        desp = _truncate_for_push(desp, MAX_BYTES)
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


def _truncate_for_push(desp, max_bytes):
    """超长正文截断：保留总结+推荐部分，航班细节列表替换为看板链接
    策略：找到 '### 💼 公务舱' 标记处截断，插入 '内容过长→看板' 提示"""
    dashboard_url = os.environ.get("DASHBOARD_URL", "在线看板")
    truncate_markers = [
        "### 💼 公务舱",   # 常见版本
        "### 公务舱",
        "### 经济舱",
    ]
    cut_pos = None
    for marker in truncate_markers:
        p = desp.find(marker)
        if p > 0 and (cut_pos is None or p < cut_pos):
            cut_pos = p
    if cut_pos:
        # 先截断到 cut_pos
        truncated = desp[:cut_pos]
        suffix = (
            f"\n> 📋 **航班细节内容过大已省略，请在 [在线看板]({_md_safe_url(dashboard_url)}) 查看完整航班明细（含购票链接）**\n\n"
            "---\n"
        )
        # 如果加了 suffix 仍然超，再做硬截断（极少情况）
        result = truncated + suffix
        if len(result.encode("utf-8")) > max_bytes:
            result = result[: max_bytes // 3] + suffix  # 按字符近似截断
        return result
    # 没找到标记则硬截断，保留 2/3 内容并加提示
    limit = max_bytes * 2 // 3
    truncated = desp[: limit // 3]  # 按字符近似
    return truncated + f"\n\n_内容过大已截断，请查看 [在线看板]({_md_safe_url(dashboard_url)}) 获取完整信息_\n"


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


def format_run_message(all_flights, stats, gone, cfg, blocked_dates, flyai_warn_line=None):
    """格式化本次运行结果推送（总结 + 航班细节）
    总结部分含：查询范围、命中航班、变化对比、降价提醒、推荐航班、API额度告警（如有）
    航班细节部分：公务舱列表、经济舱列表、消失航班
    flyai_warn_line: 非空时插入额度告警行（放在总结末尾，风控提示之后）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    business = [f for f in all_flights if f["seat_category"] == "business"]
    economy = [f for f in all_flights if f["seat_category"] == "economy"]
    business.sort(key=lambda f: f["dep_date"], reverse=True)
    economy.sort(key=lambda f: f["dep_date"], reverse=True)

    title = f"{cfg['origin_name']}→{cfg['destination_name']}机票监控 命中{len(all_flights)}条(公务{len(business)}/经济{len(economy)})"

    lines = [
        f"## ✈️ {cfg['origin_name']} → {cfg['destination_name']} 机票监控结果",
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
    if flyai_warn_line:
        lines.append(flyai_warn_line)
    lines.append("")

    # 推荐航班（公务/经济各 1 条，价格+日期窗口+总时长最短）
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
            # 优先用结构化字段友好展示，兜底用原始 key
            dep = g.get("dep_date", "")
            cat = g.get("cat_label", "")
            if dep and cat:
                lines.append(f"- {dep} {cat} ¥{g['price']:.0f}")
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
            if dep and cat:
                label = f"{html.escape(dep)} {html.escape(cat)} ¥{g['price']:.0f}"
            else:
                label = f"{html.escape(g['key'])} ¥{g['price']:.0f}"
            gone_rows.append(
                f"<li>{label} <span class='muted'>({html.escape(g.get('last_notified',''))})</span></li>"
            )
        gone_html = f"<div class='gone'><h3>❌ 不再符合条件航班（{len(gone)}）</h3><ul>{''.join(gone_rows)}</ul></div>"

    blocked_html = ""
    if blocked_dates:
        blocked_html = f"<div class='warn'>⚠️ 以下日期因飞猪风控未能查询: {', '.join(blocked_dates)}</div>"

    # 推荐航班卡片（公务/经济各 1 条）
    rec_cfg = cfg.get("recommend", {})
    rec = pick_recommended(all_flights, rec_cfg)
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
<a href="{url}" target="_blank" class="rec-buy" title="实际价格以飞猪页面为准">点击购票</a>
</div>""")
    rcfg = cfg.get("recommend", {})
    recommend_html = f"""<div class="recommend">
<h2>⭐ 推荐航班</h2>
<p class="muted">筛选: 公务≤¥{rcfg.get('business_max_price', DEFAULT_RECOMMEND_BUSINESS_MAX_PRICE)} / 经济≤¥{rcfg.get('economy_max_price', DEFAULT_RECOMMEND_ECONOMY_MAX_PRICE)}, 日期 {rcfg.get('date_start', DEFAULT_RECOMMEND_DATE_START)}~{rcfg.get('date_end', DEFAULT_RECOMMEND_DATE_END)}, 总时长最短</p>
<div class="rec-row">{''.join(rec_cards)}</div>
</div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{cfg['origin_name']}→{cfg['destination_name']}机票监控看板</title>
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
.price-note{{color:#999;font-size:12px;margin:4px 0 8px 0}}
</style></head><body>
<h1>✈️ {cfg['origin_name']} → {cfg['destination_name']} 机票监控看板</h1>
<p class="update">最近更新: {now}</p>
<p class="price-note">⚠️ 价格为含税总价（API 票面价 + 税费），购票链接打开后为飞猪航班深链，实际价格可能因动态调价而略有不同。</p>
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
    try:
        _atomic_write(DASHBOARD_PATH, html_content)
        log.info("已更新本地看板 %s", DASHBOARD_PATH)
    except Exception as e:
        # 看板生成失败不影响主功能（推送和记录）
        log.warning("generate_html 保存失败: %s", e)


# ---------------- 主流程 ----------------

def main():
    log.info("=" * 50)
    cfg = load_config()
    log.info("开始监控 %s→%s 机票", cfg["origin_name"], cfg["destination_name"])

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
        # 即使跳过也生成看板（用 notified.json 已有数据），避免兜底页面覆盖上次正常看板
        notified = load_notified()
        skip_flights = []
        for key, info in notified.items():
            if info.get("miss_count", 0) >= 3:
                continue  # 已清理的记录不展示
            parts = key.split("|")
            flight_numbers = parts[1] if len(parts) >= 3 else ""
            skip_flights.append({
                "seat_category": info.get("seat_category", ""),
                "dep_date": info.get("dep_date", ""),
                "price": info.get("price", 0),
                "change": "unchanged",
                "prev_price": 0,
                "is_direct": False,
                "transfer_count": 0,
                "dep_time": "",
                "arr_time": "",
                "jump_url": "",
                "route": "（详细信息见上次运行）",
                "transfers": [],
                "flight_numbers": flight_numbers,
                "dep_airport": "",
                "arr_airport": "",
                "total_duration_str": "—",
            })
        skip_stats = {"new": 0, "cheaper": 0, "higher": 0, "unchanged": len(skip_flights)}
        generate_html(skip_flights, skip_stats, [], cfg, [])
        log.info("跳过运行，已用已有数据更新看板（%d 条记录）", len(skip_flights))
        return

    all_dates = generate_dates(cfg["date_start"], cfg["date_end"])
    log.info("日期范围 %s ~ %s (%d天), 每次全量查询", cfg["date_start"], cfg["date_end"], len(all_dates))

    # 飞猪 API 忽略 --seat-class-name 参数（国际航线返回值均为经济舱），
    # 改为每天只查一次，用较高阈值预筛，parse_flight 按 seatClassName 实际归类后再按对应阈值过滤
    api_max_price = max(cfg["business_max_price"], cfg["economy_max_price"])
    # 实际各舱位阈值，用于 parse_flight 归类后二次过滤
    category_max_price = {
        "business": cfg["business_max_price"],
        "economy": cfg["economy_max_price"],
        "first": cfg.get("first_max_price", 999999),
    }

    all_flights = []
    query_count = 0
    fail_count = 0
    api_fail_total = 0
    blocked = False
    blocked_dates = []
    skip_parse = 0
    skip_transfer = 0
    skip_price = 0
    for date in all_dates:
        if blocked:
            blocked_dates.append(date)
            continue
        items, is_blocked, api_failed = run_flyai(cfg, date, "", api_max_price)
        query_count += 1
        if is_blocked or api_failed:
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
        for item in items:
            f = parse_flight(item, "")
            if not f:
                skip_parse += 1
                continue
            if f["transfer_count"] > cfg["max_transfers"]:
                skip_transfer += 1
                log.info("剔除(中转超次): %s %s 中转%d次 > max_transfers=%d",
                         f["dep_date"], f["flight_numbers"],
                         f["transfer_count"], cfg["max_transfers"])
                continue
            cat = f["seat_category"]
            cat_max = category_max_price.get(cat, 999999)
            if f["price"] > cat_max:
                skip_price += 1
                log.info("剔除(超价): %s %s %s 含税¥%.0f > %s阈值¥%.0f (票面¥%.0f)",
                         f["dep_date"], f["flight_numbers"], cat,
                         f["price"], cat, cat_max, f.get("base_price", f["price"]))
                continue
            all_flights.append(f)
        time.sleep(cfg.get("query_interval_sec", 12))

    log.info("筛选统计: parse失败=%d 中转过滤=%d 超价过滤=%d 命中=%d",
             skip_parse, skip_transfer, skip_price, len(all_flights))
    log.info("查询完成: %d 次查询, 命中 %d 条符合条件航班, 风控/API失败 %d 次, 跳过 %d 天",
             query_count, len(all_flights), api_fail_total, len(blocked_dates))
    if api_fail_total > 0 and api_fail_total >= query_count * 0.5:
        # 超过一半查询失败，大概率是额度耗尽或参数错误
        log.warning("⚠️ 本次 API 失败率 %.0f%%，请检查 flyai_api_key 额度或参数配置",
                    api_fail_total / max(query_count, 1) * 100)

    # 变化对比
    notified = load_notified()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_flights, stats, gone = diff_flights(all_flights, notified, blocked_dates)
    log.info("变化: 新增%d 降价%d 涨价%d 不变%d 不再符合%d",
             stats["new"], stats["cheaper"], stats["higher"], stats["unchanged"], len(gone))

    # 始终更新本地看板
    generate_html(all_flights, stats, gone, cfg, blocked_dates)

    # 推送：每次运行有内容就推1条，额度按阶段动态匹配执行次数
    #   手动触发(MANUAL_RUN=1)即使无内容也推简短状态报告，让用户知道执行完了
    push_history = load_push_history()
    pushed = False
    is_manual = _is_manual_run()

    # ---- API 调用次数累计 & 额度告警 ----
    # 每次运行把本次 query_count 累计到 push_history._meta.flyai_calls_total
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
    is_api_abnormal = fail_rate >= 0.5 or query_count == 0  # 失败率≥50%或完全没查询，视为抓取异常

    if has_content:
        # 有内容（命中航班或消失航班）：正常推送
        if can_push(push_history, max_push_per_day):
            title, desp = format_run_message(all_flights, stats, gone, cfg, blocked_dates,
                                              flyai_warn_line=flyai_warn_line)
            if send_serverchan(cfg["serverchan_key"], title, desp):
                record_push(push_history)
                pushed = True
                log.info("已推送本次运行结果 (命中%d条, 不再符合%d条)", len(all_flights), len(gone))
            else:
                log.warning("推送失败")
        else:
            log.info("今日推送已达阶段上限(%d条)，跳过推送", max_push_per_day)
    elif is_api_abnormal:
        # 无内容但API异常（失败率高/风控）：推送异常告警，避免用户以为是真的没票
        if can_push(push_history, max_push_per_day):
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            title = f"⚠️ {cfg['origin_name']}→{cfg['destination_name']}机票监控 抓取异常"
            dashboard_url = os.environ.get("DASHBOARD_URL", "")
            desp_parts = [
                f"## ⚠️ 抓取异常告警",
                f"_检测时间: {now}_",
                "",
                f"### 📊 异常详情",
                f"- **查询范围**: {cfg['date_start']} ~ {cfg['date_end']}",
                f"- **API 调用**: {query_count} 次（失败 {api_fail_total} 次）",
                f"- **失败率**: {fail_rate * 100:.0f}%",
            ]
            if blocked_dates:
                desp_parts.append(f"- **风控跳过**: {len(blocked_dates)} 天（触发风控封禁）")
            if query_count == 0:
                desp_parts.append("- **⚠️ 0次查询**: 日期轮转或风控导致完全未执行查询")
            if flyai_warn_line:
                desp_parts.append("")
                desp_parts.append(flyai_warn_line)
            desp_parts.append("")
            desp_parts.append("_请检查 flyai_api_key 额度或风控状态，或稍后重试_")
            if dashboard_url:
                desp_parts.append(f"_在线看板: {dashboard_url}_")
            if send_serverchan(cfg["serverchan_key"], title, "\n".join(desp_parts)):
                record_push(push_history)
                pushed = True
                log.info("已推送抓取异常告警")
            else:
                log.warning("异常告警推送失败")
        else:
            log.info("今日推送已达阶段上限，跳过异常告警推送")
    else:
        # 抓取正常但0命中（且无消失航班）：不推送，节省额度
        log.info("抓取正常但0命中，不推送（仅更新看板），失败率: %.0f%%", fail_rate * 100)

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
