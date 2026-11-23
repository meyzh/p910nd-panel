#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USB 打印机网络共享面板（一体版 + 登录鉴权 + 网页配置 + 打印来源监控）

端口：
    9100 —— RAW / JetDirect 原始打印服务（libusb 直连 USB 打印机）
    8910 —— Web 面板

配置文件：/app/data/config.json（首次启动自动生成，网页里可改）
任务记录：/app/data/jobs.json（最近 1000 条，重启不丢）

不依赖内核 usbprinter / usblp，适配裁剪内核的 NAS。
"""

import collections
import datetime
import hashlib
import hmac
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request

from flask import Flask, Response, jsonify, redirect, request, session

try:
    import usb.core
    import usb.util
    PYUSB_OK = True
    PYUSB_ERROR = ""
except Exception as exc:
    usb = None
    PYUSB_OK = False
    PYUSB_ERROR = str(exc)

# ==================== 基础配置 ====================
# 打印机 USB ID 不预置任何具体型号：换一台机器部署时，这里不会带着别人的打印机，
# 装好后在网页「设备设置」页扫描 USB 设备（或手动填 vid:pid）再保存即可。
VID = None
PID = None
DEFAULT_DEVICE_ID = ""
PRINTER_ID = DEFAULT_DEVICE_ID
DEVICE_ID_SAMPLE = "1234:5678"   # 界面/文档里的格式示例，不是真实设备
PROJECT_URL = "https://github.com/meyzh/p910nd-panel"   # 项目主页，页脚 GitHub 图标指向这里
def _env_port(name, default):
    """端口可用环境变量覆盖：host 模式下端口被别的东西占用时改这里"""
    try:
        val = int(str(os.environ.get(name) or "").strip() or default)
        if 1 <= val <= 65535:
            return val
    except Exception:
        pass
    return default


PRINT_PORT = _env_port("PRINT_PORT", 9100)
WEB_PORT = _env_port("WEB_PORT", 8910)
USB_TIMEOUT_MS = 5000
CHUNK_SIZE = 4096
JOB_KEEP_MIN = 100
JOB_KEEP_MAX = 5000
JOB_KEEP = max(JOB_KEEP_MIN, min(JOB_KEEP_MAX, int(os.environ.get("JOB_KEEP", "1000") or 1000)))   # 打印监控保留条数，可用环境变量或网页设置调整

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
JOBS_PATH = os.path.join(DATA_DIR, "jobs.json")
STATS_PATH = os.path.join(DATA_DIR, "stats.json")
LOG_PATH = os.path.join(DATA_DIR, "panel.log")
LOG_KEEP_LINES = 3000          # 启动时从日志文件载入最近多少行
LOG_FILE_LIMIT = 1024 * 1024   # 日志文件超过 1 MB 就压缩回最近 LOG_KEEP_LINES 行
PUSH_API = "https://www.pushplus.plus/send"
PUSH_BATCH_API = "https://www.pushplus.plus/batchSend"

# Pushplus 官方支持的发送渠道
ALL_CHANNELS = ["wechat", "app", "extension", "webhook", "clawbot", "cmcc", "qq", "cp", "mail", "sms", "voice"]
CHANNEL_LABELS = {
    "wechat": "微信公众号",
    "app": "APP",
    "extension": "浏览器插件/桌面端",
    "webhook": "第三方 Webhook",
    "clawbot": "微信 ClawBot",
    "cmcc": "新消息 ClawBot(移动)",
    "qq": "QQ 机器人",
    "cp": "企业微信应用",
    "mail": "邮箱",
    "sms": "短信(收费)",
    "voice": "语音(收费)",
}
CHANNEL_NEED_OPTION = ("webhook", "cp", "mail", "qq")   # 需要在个人中心配渠道编码
CHANNEL_TEXT_TEMPLATE = ("clawbot", "cmcc", "qq", "sms", "voice")  # 这些渠道建议纯文本
JOB_FAIL_COOLDOWN = 600

START_TS = time.time()
SPEED_MAP = {0: "未知", 1: "1.5 Mbps", 2: "12 Mbps", 3: "480 Mbps", 4: "5 Gbps", 5: "10 Gbps"}

logs = []          # 进程内全量保留：只要容器没重启，日志一条都不丢
lock = threading.RLock()
cfg_lock = threading.RLock()
jobs_lock = threading.RLock()
job_lock = threading.Lock()   # 同一时刻只允许一个任务占用 USB：两个任务交替写会把数据流搅乱
jobs = collections.deque(maxlen=JOB_KEEP)

state = {
    "usb_online": False,
    "printer_name": "",
    "usb_bus": "",
    "usb_addr": "",
    "usb_speed": "",
    "port_listening": False,
    "active_clients": 0,
    "jobs": 0,
    "bytes": 0,
    "last_job_time": "",
    "last_job_size": 0,
    "last_error": "",
    "reconnects": 0,
}

push_info = {"count": 0, "last_time": "", "last_result": ""}


# ==================== 日志 ====================

_log_writes = [0]
_log_fh = [None]


def _close_log_fh():
    fh = _log_fh[0]
    _log_fh[0] = None
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass


def _trim_log_file():
    """日志文件过大时压回最近 LOG_KEEP_LINES 行，避免无限增长。"""
    _close_log_fh()
    try:
        if os.path.getsize(LOG_PATH) <= LOG_FILE_LIMIT:
            return
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            tail = fh.read().splitlines()[-LOG_KEEP_LINES:]
        with open(LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(tail) + "\n")
    except Exception:
        pass


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    with lock:
        logs.append(line)
        # 同时写进 data/panel.log：进程被自动/手动重启后还能看到重启前的记录
        # 句柄长期持有：高频日志（打印反复失败时）逐行 open/close 会明显拖慢整台机器
        try:
            if not os.path.isdir(DATA_DIR):
                os.makedirs(DATA_DIR)
            fh = _log_fh[0]
            if fh is None or getattr(fh, "closed", False):
                fh = open(LOG_PATH, "a", encoding="utf-8")
                _log_fh[0] = fh
            fh.write(line + "\n")
            fh.flush()
            _log_writes[0] += 1
            if _log_writes[0] % 200 == 0:
                _trim_log_file()
        except Exception:
            _log_fh[0] = None
    print(line, flush=True)


def load_log_history():
    """启动时把上一次运行留在 panel.log 里的日志载回内存。"""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            tail = fh.read().splitlines()[-LOG_KEEP_LINES:]
    except Exception:
        return 0
    with lock:
        logs.extend(tail)
    return len(tail)


def set_state(**kwargs):
    with lock:
        state.update(kwargs)


def uptime_str():
    total = int(time.time() - START_TS)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    mins, secs = divmod(rest, 60)
    if days:
        # 超过一天也要带秒，否则页面上看着像不刷新
        return "%d天%d小时%d分%d秒" % (days, hours, mins, secs)
    if hours:
        return "%d小时%d分%d秒" % (hours, mins, secs)
    return "%d分%d秒" % (mins, secs)


def fmt_bytes(num):
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit) if unit != "B" else "%d B" % int(size)
        size /= 1024.0
    return "%d B" % int(num or 0)


# ==================== 密码 / 配置文件 ====================

PWD_ITER = 120000
SESSION_HOURS = 12


def hash_password(pwd, salt, algo="pbkdf2"):
    """密码只以哈希形式落盘：新密码用 PBKDF2-SHA256，旧数据兼容 sha256(salt+pwd)"""
    if algo == "legacy":
        return hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()
    return hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), PWD_ITER).hex()


def pwd_problem(pwd):
    """返回不合规原因，合规返回空串"""
    if len(pwd) < 8:
        return "密码至少 8 位"
    if len(pwd) > 64:
        return "密码最长 64 位"
    if pwd.strip() != pwd:
        return "密码首尾不能有空格"
    if pwd.isdigit():
        return "密码不能是纯数字"
    return ""


def build_default_config():
    return {
        "version": 2,
        "secret_key": os.urandom(24).hex(),
        "device": {
            # 留空 = 未配置，首次上线后到「设备设置」页扫描或手填
            "id": os.environ.get("PRINTER_ID", "").strip(),
        },
        # 不再预置账号密码：首次打开网页时由使用者自己设置，且只存哈希
        "auth": {
            "salt": "",
            "hash": "",
            "algo": "pbkdf2",
            "set": False,
        },
        "push": {
            "enable": os.environ.get("PUSH_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off"),
            "token": os.environ.get("PUSHPLUS_TOKEN", "").strip(),
            "channel": (os.environ.get("PUSHPLUS_CHANNEL", "wechat").strip() or "wechat"),
            "option": os.environ.get("PUSHPLUS_OPTION", "").strip(),
            "to": os.environ.get("PUSHPLUS_TO", "").strip(),
            "topic": os.environ.get("PUSHPLUS_TOPIC", "").strip(),
            "template": "auto",
            "offline_alert_sec": int(os.environ.get("OFFLINE_ALERT_SEC", "60") or 60),
            "job_fail_push": os.environ.get("JOB_FAIL_PUSH", "1").strip().lower() not in ("0", "false", "no", "off"),
        },
        "notes": {},      # 来源备注：设备主机名或 IP -> 自定义名字
        "src_hidden": [],  # 来源管理里删除掉的来源 IP，不再出现在列表
        "jobs_keep": 0,   # 打印监控保留条数，0 = 跟随环境变量 JOB_KEEP
    }


def load_config():
    if not os.path.isdir(DATA_DIR):
        try:
            os.makedirs(DATA_DIR)
        except Exception:
            pass
    cfg = None
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as exc:
            print("[WARN] 配置文件读取失败，使用默认配置：%s" % exc, flush=True)
            cfg = None
    if not cfg:
        cfg = build_default_config()
        save_config(cfg)
        return cfg

    default = build_default_config()
    for key in ("version", "secret_key"):
        if not cfg.get(key):
            cfg[key] = default[key]
    if not isinstance(cfg.get("device"), dict):
        cfg["device"] = default["device"]
    cfg["device"].setdefault("id", default["device"]["id"])
    if not isinstance(cfg.get("auth"), dict):
        cfg["auth"] = dict(default["auth"])
    old_algo = cfg["auth"].get("algo")
    for key, val in default["auth"].items():
        cfg["auth"].setdefault(key, val)
    if cfg["auth"].get("hash"):
        # 老版本配置文件里已有密码哈希 → 视为已初始化；没写算法说明是按 sha256(salt+pwd) 存的
        cfg["auth"]["set"] = True
        if not old_algo:
            cfg["auth"]["algo"] = "legacy"
    cfg["auth"].pop("default_pwd", None)
    cfg["auth"].pop("username", None)
    if not isinstance(cfg.get("push"), dict):
        cfg["push"] = default["push"]
    for key, val in default["push"].items():
        if key not in cfg["push"]:
            cfg["push"][key] = val
    if not isinstance(cfg.get("notes"), dict):
        cfg["notes"] = {}
    if not isinstance(cfg.get("src_hidden"), list):
        cfg["src_hidden"] = []
    else:
        cfg["src_hidden"] = [str(x) for x in cfg["src_hidden"] if str(x).strip()]
    try:
        cfg["jobs_keep"] = int(cfg.get("jobs_keep") or 0)
    except Exception:
        cfg["jobs_keep"] = 0
    return cfg


def save_config(cfg=None):
    data = cfg if cfg is not None else CFG
    try:
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as exc:
        print("[ERROR] 配置保存失败：%s" % exc, flush=True)
        return False


CFG = load_config()


def cfg_job_keep():
    """配置里的保留条数；0 表示沿用环境变量 JOB_KEEP"""
    try:
        n = int(CFG.get("jobs_keep") or 0)
    except Exception:
        n = 0
    if n <= 0:
        return JOB_KEEP
    return max(JOB_KEEP_MIN, min(JOB_KEEP_MAX, n))


def apply_job_keep(n, persist=False):
    """调整打印监控的保留条数：重建 deque，超出部分立刻丢弃最老的记录"""
    global JOB_KEEP, jobs
    try:
        n = int(n)
    except Exception:
        n = JOB_KEEP
    n = max(JOB_KEEP_MIN, min(JOB_KEEP_MAX, n))
    with jobs_lock:
        current = list(jobs)
        JOB_KEEP = n
        jobs = collections.deque(current[-n:], maxlen=n)
        trimmed = len(current) - len(jobs)
    if persist:
        with cfg_lock:
            CFG["jobs_keep"] = n
        save_config()
    if trimmed > 0:
        save_jobs()
        log("打印监控保留条数改为 %d 条，已裁掉最早的 %d 条记录" % (n, trimmed))
    elif persist:
        log("打印监控保留条数改为 %d 条" % n)
    return JOB_KEEP


apply_job_keep(cfg_job_keep())

HEX_CHARS = "0123456789abcdef"


def parse_device_id(text):
    """解析设备 ID：支持 1234:5678 / 1234-5678 / 0x1234:0x5678 / 12345678"""
    raw = str(text or "").strip().lower().replace("0x", "")
    for sep in (":", "-", "_", "/", ".", " ", ",", "|"):
        raw = raw.replace(sep, "")
    if len(raw) != 8:
        return None
    for ch in raw:
        if ch not in HEX_CHARS:
            return None
    try:
        vid = int(raw[:4], 16)
        pid = int(raw[4:], 16)
    except Exception:
        return None
    return vid, pid, "%04x:%04x" % (vid, pid)


def apply_device_id(dev_id):
    """把设备 ID 应用到全局（改动后 USB 查找立即按新 ID 走）

    传空字符串表示“清空设备配置”，此时不猜任何型号，USB 查找直接跳过。
    """
    global VID, PID, PRINTER_ID
    text = str(dev_id or "").strip()
    if not text:
        VID, PID, PRINTER_ID = None, None, ""
        return True
    parsed = parse_device_id(text)
    if not parsed:
        return False
    VID, PID, norm = parsed
    PRINTER_ID = norm
    return True


def device_label():
    """界面上显示用的设备标识，未配置时给一句人话而不是空白。"""
    return PRINTER_ID or "未配置"


def device_conf():
    with cfg_lock:
        return dict(CFG.get("device", {}))


apply_device_id(device_conf().get("id"))


def push_conf():
    with cfg_lock:
        return dict(CFG.get("push", {}))


def auth_conf():
    with cfg_lock:
        return dict(CFG.get("auth", {}))


def notes_conf():
    with cfg_lock:
        return dict(CFG.get("notes", {}) or {})


def auth_ready():
    """是否已经设置过访问密码"""
    return bool(auth_conf().get("hash"))


def set_password(pwd):
    """写入新密码：只保存盐 + PBKDF2 哈希，绝不落明文"""
    salt = os.urandom(16).hex()
    with cfg_lock:
        a = CFG.setdefault("auth", {})
        a["salt"] = salt
        a["algo"] = "pbkdf2"
        a["hash"] = hash_password(pwd, salt)
        a["set"] = True
        a.pop("default_pwd", None)
        a.pop("username", None)
    save_config()
    return True


def check_pwd(pwd):
    """恒定时间比较；老版 sha256 哈希验证通过后自动升级到 PBKDF2"""
    conf = auth_conf()
    stored = conf.get("hash") or ""
    if not stored:
        return False
    salt = conf.get("salt", "")
    algo = conf.get("algo") or "legacy"
    if algo == "pbkdf2":
        return hmac.compare_digest(hash_password(pwd, salt), stored)
    if hmac.compare_digest(hash_password(pwd, salt, "legacy"), stored):
        set_password(pwd)
        return True
    return False


# ==================== 登录失败限制 ====================

login_guard = {}
login_guard_lock = threading.RLock()
LOGIN_MAX_FAIL = 5
LOGIN_LOCK_SEC = 60


def login_wait(ip):
    with login_guard_lock:
        rec = login_guard.get(ip)
        if not rec:
            return 0
        left = int(rec.get("until", 0) - time.time())
        return left if left > 0 else 0


def login_fail(ip):
    with login_guard_lock:
        rec = login_guard.setdefault(ip, {"count": 0, "until": 0})
        rec["count"] = int(rec.get("count", 0)) + 1
        if rec["count"] >= LOGIN_MAX_FAIL:
            rec["count"] = 0
            rec["until"] = time.time() + LOGIN_LOCK_SEC


def login_ok(ip):
    with login_guard_lock:
        login_guard.pop(ip, None)


# ==================== 推送 ====================

def push_ready():
    conf = push_conf()
    return bool(conf.get("enable")) and bool(str(conf.get("token", "")).strip())


def do_push(title, content):
    conf = push_conf()
    if not conf.get("enable"):
        return False, "推送总开关已关闭"
    token = str(conf.get("token", "")).strip()
    if not token:
        return False, "未配置 Pushplus Token"

    channels = [c.strip() for c in str(conf.get("channel", "wechat")).split(",") if c.strip()]
    channels = [c for c in channels if c in ALL_CHANNELS] or ["wechat"]

    template = str(conf.get("template", "auto")).strip() or "auto"
    if template == "auto":
        # 纯文本类渠道用 txt，其余用 html，避免出现一堆转义字符
        template = "txt" if all(c in CHANNEL_TEXT_TEMPLATE for c in channels) else "html"

    option = str(conf.get("option", "")).strip()

    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": template,
    }
    url = PUSH_API
    if len(channels) > 1:
        # 多渠道一次发送，option 需要与渠道一一对应（不需要的留空）
        payload["channel"] = ",".join(channels)
        payload["option"] = ",".join([option if c in CHANNEL_NEED_OPTION else "" for c in channels])
        url = PUSH_BATCH_API
    else:
        payload["channel"] = channels[0]
        if channels[0] in CHANNEL_NEED_OPTION and option:
            payload["option"] = option

    topic = str(conf.get("topic", "")).strip()
    if topic:
        payload["topic"] = topic
    to = str(conf.get("to", "")).strip()
    if to:
        payload["to"] = to

    ok = False
    msg = ""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "ignore")
        try:
            ret = json.loads(body)
        except Exception:
            ret = {}
        if ret.get("code") == 200:
            ok = True
            msg = "推送成功（%s）" % "/".join(CHANNEL_LABELS.get(c, c) for c in channels)
        else:
            msg = "接口返回：%s" % (ret.get("msg") or body[:120])
    except Exception as exc:
        msg = "请求失败：%s" % exc

    with lock:
        push_info["count"] += 1
        push_info["last_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        push_info["last_result"] = ("成功" if ok else "失败") + "：" + msg
    log("推送[%s] → %s（%s）" % (title, "成功" if ok else "失败", msg))
    return ok, msg


def push_async(title, content):
    threading.Thread(target=do_push, args=(title, content), daemon=True).start()


def push_html(lines):
    return "<br>".join(lines)


# ==================== 打印机回传状态（PJL 双向通道） ====================
#
# 9100 RAW 一般只管写；但大多数打印机的 USB 接口同时暴露一个 Bulk IN 端点，
# 可以用 PJL 的 @PJL INFO STATUS 问出当前状态（CODE= 数字 + DISPLAY= 文字）。
# 这里做三件事：发查询、解析回包、把代码和英文面板文字翻成中文。

PJL_STATUS_LOCK = threading.RLock()
PJL_STATUS = {
    "supported": None,   # None=还没试过；True=有 IN 端点；False=该机型没有回传通道
    "code": "",          # 打印机回传的 CODE=
    "en": "",            # DISPLAY= 的原文
    "zh": "",            # 翻译成中文的状态
    "code_zh": "",       # 代码段含义（HP PJL 附录 D 的分类）
    "at": "",            # 最后一次成功读取的时间
    "error": "",         # 读取失败的原因（面板上原样显示）
    "online": None,      # 打印机自称 ONLINE=TRUE/FALSE
}
_LAST_STATUS_KEY = {"key": ""}

# DISPLAY 英文面板文字 → 中文。按关键词匹配，命中多条就拼接。
DISPLAY_ZH_RULES = [
    (r"\bpaper jam\b|\bjam(m?ed)?\b", "卡纸"),
    (r"\bout of paper\b|\bpaper out\b|\bpaper empty\b|\bno paper\b", "缺纸"),
    (r"\bload paper\b|\badd paper\b|\binsert paper\b|\bmanual feed\b", "需要装纸"),
    (r"\btray\s*\d*\s*empty\b|\bcassette\s*\d*\s*empty\b", "纸盒空"),
    (r"\btray\s*\d*\s*open\b|\bcassette\s*\d*\s*open\b|\bdrawer open\b", "纸盒未插好"),
    (r"\btoner low\b|\blow toner\b", "碳粉不足"),
    (r"\btoner empty\b|\bno toner\b|\breplace toner\b|\btoner\b.*\bempty\b", "碳粉用尽，需更换"),
    (r"\bwaste toner\b.*\b(full|near full)\b", "废粉仓将满"),
    (r"\bdrum\b.*\b(low|empty|replace|life)\b", "感光鼓寿命将至"),
    (r"\bcover open\b|\bdoor open\b|\bopen cover\b|\bopen door\b|\bclose cover\b|\bclose door\b", "盖板（机门）未关"),
    (r"\boutput tray\b.*\bfull\b|\bstack full\b|\btray full\b", "出纸盘已满"),
    (r"\bstaples?\b.*\b(empty|low|out)\b", "订书钉不足"),
    (r"\bsleep(ing)?\b|\bpower save\b|\bstand ?by\b", "休眠中"),
    (r"\bwarming up\b|\bwarm ?up\b", "预热中"),
    (r"\binitializ", "初始化中"),
    (r"\bcalibrat", "校准中"),
    (r"\bprinting\b|\bprocessing\b|\bbusy\b", "正在打印"),
    (r"\bready\b|\bidle\b", "就绪"),
    (r"\boffline\b|\bnot ready\b", "离线（未就绪）"),
    (r"\bfuser\b|\boverheat\b|\btemperature\b", "定影单元异常（温度）"),
    (r"\bpaper\b.*\bmismatch\b|\bmedia\b.*\bmismatch\b|\bincorrect paper\b|\bpaper size\b", "纸张规格与任务不符"),
    (r"\bduplex\b", "双面单元异常"),
    (r"\bmemory full\b", "内存不足"),
    (r"\bdisk\b.*\bfull\b", "存储空间已满"),
    (r"\bnetwork\b.*\berror\b", "网络异常"),
    (r"\bservice call\b|\bcall service\b|\berror\b", "打印机故障（需检修）"),
    (r"\bpaused\b|\bcancel", "任务被暂停/取消"),
    (r"\battention\b|\bcheck\b", "需要人工确认"),
]

# HP PJL 附录 D 的代码分段（只到“这一段代表什么”这一层，不逐条编造）
PJL_CODE_RANGES = [
    (10000, 10999, "状态/耗材提示类信息（一般不影响继续打印）"),
    (11000, 11999, "后台送纸提示（如请装纸）"),
    (12000, 12999, "纸盒状态提示（缺纸、纸盒未到位）"),
    (15000, 15999, "出纸仓状态提示（出纸盘满）"),
    (20000, 20999, "PJL 解析错误（命令写法有问题）"),
    (25000, 25999, "PJL 解析警告"),
    (27000, 27999, "PJL 语义错误（参数不被支持）"),
    (30000, 30999, "可自动恢复的异常（打印机自行处理）"),
    (32000, 32999, "PJL 文件系统错误"),
    (35000, 35999, "潜在需人工介入（建议检查纸张/耗材）"),
    (40000, 40999, "需人工介入：缺纸 / 卡纸 / 盖板未关 / 耗材用尽等"),
    (41000, 41999, "需人工送纸（手动进纸）"),
    (42000, 42999, "卡纸"),
    (43000, 43999, "外部/附加送纸设备异常"),
]


def explain_pjl_code(code):
    """把 CODE= 的数字翻成“这一段的含义”，翻不出来就空着"""
    try:
        n = int(str(code).strip())
    except Exception:
        return ""
    for lo, hi, zh in PJL_CODE_RANGES:
        if lo <= n <= hi:
            return zh
    return ""


def translate_display(text):
    """把 DISPLAY=（英文面板文字）翻成中文；翻不出来时返回空字符串"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    hits = []
    for pat, zh in DISPLAY_ZH_RULES:
        if re.search(pat, lower) and zh not in hits:
            hits.append(zh)
    if not hits:
        return ""
    # 「就绪」单独命中时别再拼别的东西，避免出现「就绪、正在打印」
    if len(hits) > 1 and "就绪" in hits and ("正在打印" in hits or "预热中" in hits or "初始化中" in hits):
        hits = [h for h in hits if h != "就绪"]
    return "、".join(hits)


def printer_status_snapshot():
    with PJL_STATUS_LOCK:
        return dict(PJL_STATUS)


def set_printer_status(**kw):
    with PJL_STATUS_LOCK:
        for k, v in kw.items():
            if k in PJL_STATUS:
                PJL_STATUS[k] = v


def printer_status_text():
    """面板上显示的一句话：有中文状态就用中文，否则退回原文或错误"""
    with PJL_STATUS_LOCK:
        st = dict(PJL_STATUS)
    if st["code"] or st["zh"] or st["en"]:
        main = st["zh"] or st["en"] or st["code_zh"]
        return main or "已收到状态"
    if st["supported"] is False:
        return "该机型没有回传通道（未暴露 Bulk IN 端点）"
    if st["error"]:
        return "读取失败：%s" % st["error"]
    return "尚未读取（等待下一个查询周期）"


def format_status_brief():
    """给任务记录/日志用的一行摘要：卡纸（CODE=40022 · 需人工介入…）"""
    st = printer_status_snapshot()
    parts = []
    if st.get("zh") or st.get("en"):
        parts.append(st.get("zh") or st.get("en"))
    if st.get("code"):
        code_part = "CODE=%s" % st["code"]
        if st.get("code_zh"):
            code_part += "（%s）" % st["code_zh"]
        parts.append(code_part)
    return " · ".join(parts)


def has_cjk(text):
    """文本里是否已经含中文/日文——有就不用再按英文词表翻"""
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff66-\uff9f]", str(text or "")))


# DISPLAY= 里的字节编码各家不同（京瓷常按面板语言回 GBK / Shift-JIS，也有机器回 UTF-8），
# 这里逐个试解，按“像不像正常文本”打分挑最靠谱的那个。
STATUS_ENCODINGS = (
    ("gb18030", 3),      # 中文面板最常见
    ("shift_jis", 5),    # 日文面板
    ("cp932", 3),
    ("big5", 1),         # 繁体
)


def _score_text(s):
    score = 0
    for ch in s:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:          # 汉字
            score += 4
        elif 0x3040 <= o <= 0x30FF:        # 假名
            score += 4
        elif 32 <= o < 127:                # 可打印 ASCII
            score += 2
        elif 0xFF01 <= o <= 0xFF5E:        # 全角字符
            score += 2
        elif o < 32:                       # 控制字符
            score -= 3
        elif 0x80 <= o <= 0x2FF:           # 拉丁扩展 / 修饰符：解错时的典型产物
            score -= 6
        elif 0x2500 <= o <= 0x257F:        # 制表符块
            score -= 6
        else:
            score -= 1
    return score


def smart_decode_text(raw):
    """把 DISPLAY= 的原始字节解成可读文本。

    顺序很重要：先看是不是 UTF-8（能严格解出且内容是正常文字就直接用），
    再按 GBK / Shift-JIS / Big5 逐个试，最后才退回 latin-1。
    全部都不像正常文本时返回空字符串，由调用方提示“回传文字无法识别”。
    """
    if isinstance(raw, str):
        raw = raw.encode("latin-1", "ignore")
    raw = bytes(raw)
    if not raw:
        return ""

    # 1) 严格 UTF-8 且内容正常：直接采用（中文/英文都常见）
    try:
        s = raw.decode("utf-8")
        if _score_text(s) >= 2:
            return s
    except Exception:
        pass

    # 2) UTF-16（有些固件按双字节回）：整串高低字节明显成对时试一下
    if len(raw) >= 4 and raw.count(0) >= len(raw) // 3:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                s = raw.decode(enc)
            except Exception:
                continue
            if has_cjk(s) and _score_text(s) >= 6:
                return s

    # 3) 常见中文 / 日文 / 繁体编码
    best, best_score = "", -999
    for enc, bonus in STATUS_ENCODINGS:
        try:
            s = raw.decode(enc)
        except Exception:
            continue
        sc = _score_text(s) + bonus
        if sc > best_score:
            best, best_score = s, sc

    if best_score >= 4:
        return best

    # 4) 兜底：英文之外没有可用信息就如实说“识别不了”，别把乱码显示到面板
    try:
        s = raw.decode("latin-1")
    except Exception:
        return ""
    if re.search(r"[A-Za-z]{2,}", s) or _score_text(s) >= 4:
        return s
    return ""


# ==================== USB 打印机 ====================

class UsbPrinter(object):
    def __init__(self):
        self.dev = None
        self.ep_out = None
        self.ep_in = None
        self.iface_num = None
        self.sig = None
        self.io_lock = threading.Lock()
        self._fail_log = {"msg": "", "ts": 0}
        self._last_reset = 0
        self._status_at = 0.0

    def _find_dev(self):
        if not PYUSB_OK or not PRINTER_ID or VID is None or PID is None:
            return None
        try:
            # 每次查找都新建一个 libusb backend，等价于新建 libusb 上下文：
            # 拔插后进程里旧上下文缓存的设备列表会指向已经不存在的句柄，
            # 继续用它读配置就会一直是 Errno 5。
            backend = None
            try:
                import usb.backend.libusb1 as _libusb1
                backend = _libusb1.get_backend()
            except Exception:
                backend = None
            if backend is not None:
                try:
                    return usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
                except Exception:
                    pass
            return usb.core.find(idVendor=VID, idProduct=PID)
        except Exception as exc:
            log("USB 设备查找失败：%s" % exc)
            return None

    @staticmethod
    def _get_product(dev):
        try:
            return (dev.product or "").strip()
        except Exception:
            return "USB 打印机"

    def _speed_text(self, dev):
        speed = getattr(dev, "speed", None)
        if speed is None:
            return ""
        return SPEED_MAP.get(int(speed), "%s" % speed)

    def _close(self):
        if self.dev is not None:
            try:
                if self.iface_num is not None:
                    usb.util.release_interface(self.dev, self.iface_num)
            except Exception:
                pass
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
        self.dev = None
        self.ep_out = None
        self.ep_in = None
        self.iface_num = None
        self.sig = None

    def is_ready(self):
        return self.ep_out is not None

    def _log_fail(self, msg):
        """同一类 USB 错误 60 秒内只记一条，避免日志被刷屏"""
        now = time.time()
        last = self._fail_log
        if msg == last.get("msg") and now - last.get("ts", 0) < 60:
            last["ts"] = now
            return
        self._fail_log = {"msg": msg, "ts": now}
        log(msg)

    def _config_for(self, dev):
        """取设备配置描述符。

        拔插之后部分打印机（尤其京瓷这类）的 GET_CONFIGURATION 控制传输会持续
        返回 Errno 5，导致 get_active_configuration 和 set_configuration 都失败。
        这里只做「不改变设备状态」的两次尝试；都失败则返回 None，
        由调用方改用 sysfs 端点直写（那条路不发任何控制请求）。
        """
        err = None
        try:
            return dev.get_active_configuration(), "", None
        except Exception as exc:
            err = exc
        try:
            dev.set_configuration()
            return dev.get_active_configuration(), "", None
        except Exception as exc:
            err = exc
        # 注意：这里不再做 USB reset。reset 会让内核重新枚举、旧句柄直接失效，
        # 反而把设备推进“控制传输一直 EIO”的状态。
        return None, "", err

    def diag(self):
        """把 USB 现场信息整理成若干行日志，用来自查连不上的原因。"""
        if not PRINTER_ID:
            return ["尚未设置打印机 ID：请打开面板「设备设置」页，点“扫描 USB 设备”选中后保存"]
        out = ["目标设备 ID：%s" % PRINTER_ID]
        if not PYUSB_OK:
            out.append("pyusb 不可用：%s" % PYUSB_ERROR)
            return out
        try:
            devs = list(usb.core.find(find_all=True))
            out.append("libusb 枚举到 %d 个设备" % len(devs))
        except Exception as exc:
            out.append("libusb 枚举失败：%s" % exc)
            return out
        want = (PRINTER_ID or "").strip().lower()
        hit = 0
        for d in devs:
            try:
                cur = "%04x:%04x" % (d.idVendor, d.idProduct)
            except Exception:
                continue
            if cur != want:
                continue
            hit += 1
            node = "/dev/bus/usb/%03d/%03d" % (d.bus, d.address)
            try:
                st = os.stat(node)
                out.append("设备 %s 在 %s，权限 %s" % (cur, node, oct(st.st_mode & 0o777)))
            except Exception as exc:
                out.append("设备 %s 应为 %s，但 stat 失败：%s" % (cur, node, exc))
            try:
                cfg = d.get_active_configuration()
                out.append("读取活动配置：成功（bConfigurationValue=%s）" % cfg.bConfigurationValue)
            except Exception as exc:
                out.append("读取活动配置：失败 %s" % exc)
            try:
                usb.util.dispose_resources(d)
            except Exception:
                pass
        if not hit:
            out.append("枚举列表里找不到该设备（可能已物理拔出，或主机没重新枚举）")
        try:
            sp = self._sysfs_endpoint(VID, PID)
            if sp:
                out.append("sysfs 端点：接口 %d，输出端点 0x%02x（可直写）" % (sp[0], sp[1]))
            else:
                out.append("sysfs 端点：未取到（/sys/bus/usb/devices 里没有匹配的打印接口）")
        except Exception as exc:
            out.append("sysfs 端点读取异常：%s" % exc)
        return out

    def _purge(self):
        """丢弃进程内所有 pyusb 设备句柄。

        拔插之后旧句柄指向的设备已经不存在，继续用它会持续返回 Errno 5；
        清一遍再重新查找，等价于“不重启容器”的重新枚举。
        """
        try:
            for d in usb.core.find(find_all=True):
                try:
                    usb.util.dispose_resources(d)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _sysfs_present(vid, pid):
        """设备是否还挂在系统上：直接看 sysfs 里有没有这个 VID/PID。

        拔线后 libusb 的旧句柄可能还在枚举列表里（返回的是幽灵设备），
        但 /sys/bus/usb/devices 下的目录会立刻消失，所以这是最可靠的“拔掉了”判据。
        返回 True（在）/ False（不在）/ None（读不到 sysfs，无法判断）。
        """
        base = "/sys/bus/usb/devices"
        try:
            names = os.listdir(base)
        except Exception:
            return None
        want_v = "%04x" % vid
        want_p = "%04x" % pid
        for name in names:
            path = os.path.join(base, name)
            try:
                with open(os.path.join(path, "idVendor")) as fh:
                    v = fh.read().strip()
                with open(os.path.join(path, "idProduct")) as fh:
                    p = fh.read().strip()
            except Exception:
                continue
            if v.lower() == want_v and p.lower() == want_p:
                return True
        return False

    def sysfs_present(self):
        if not PRINTER_ID or VID is None or PID is None:
            return None
        return self._sysfs_present(VID, PID)

    @staticmethod
    def _sysfs_endpoints(vid, pid):
        """不依赖 USB 控制传输，直接从 sysfs 读出打印接口的端点信息。

        拔插后设备可能连 GET_CONFIGURATION 都返回 EIO，但只要设备在，
        sysfs 里的 bInterfaceClass / bEndpointAddress 一定读得到。
        返回 (接口号, 输出端点, 输入端点) 或 None；输入端点不存在时为 None。
        """
        base = "/sys/bus/usb/devices"
        try:
            names = os.listdir(base)
        except Exception:
            return None
        want_v = "%04x" % vid
        want_p = "%04x" % pid
        for name in names:
            path = os.path.join(base, name)
            try:
                with open(os.path.join(path, "idVendor")) as fh:
                    v = fh.read().strip()
                with open(os.path.join(path, "idProduct")) as fh:
                    p = fh.read().strip()
            except Exception:
                continue
            if v.lower() != want_v or p.lower() != want_p:
                continue
            try:
                children = os.listdir(path)
            except Exception:
                continue
            for child in children:
                if ":" not in child:
                    continue
                sub = os.path.join(path, child)
                try:
                    with open(os.path.join(sub, "bInterfaceClass")) as fh:
                        cls = int(fh.read().strip(), 16)
                    with open(os.path.join(sub, "bInterfaceNumber")) as fh:
                        num = int(fh.read().strip(), 16)
                except Exception:
                    continue
                if cls != 7:
                    continue
                ep_out = None
                ep_in = None
                try:
                    eps = os.listdir(sub)
                except Exception:
                    eps = []
                for ep in eps:
                    if not ep.startswith("ep_"):
                        continue
                    try:
                        with open(os.path.join(sub, ep, "bEndpointAddress")) as fh:
                            addr = int(fh.read().strip(), 16)
                        with open(os.path.join(sub, ep, "type")) as fh:
                            typ = int(fh.read().strip(), 16)
                    except Exception:
                        continue
                    if typ != 2:          # 2 = Bulk
                        continue
                    if addr & 0x80:       # 0x80 置位是 IN：状态回传走这个端点
                        if ep_in is None:
                            ep_in = addr
                    elif ep_out is None:
                        ep_out = addr
                if ep_out is not None:
                    return num, ep_out, ep_in
        return None

    @staticmethod
    def _sysfs_endpoint(vid, pid):
        """兼容旧调用：只要 (接口号, 输出端点)"""
        hit = UsbPrinter._sysfs_endpoints(vid, pid)
        if not hit:
            return None
        return hit[0], hit[1]

    def _open_once(self, dev, silent=False):
        cfg, how, cerr = self._config_for(dev)
        if cfg is None:
            # 控制传输全挂：改用 sysfs 端点 + usbfs 直接 bulk 写，完全不发控制请求
            hit = self._sysfs_endpoints(VID, PID)
            if hit:
                num, addr, addr_in = hit
                try:
                    usb.util.claim_interface(dev, num)
                except Exception as exc:
                    self._log_fail("控制传输失败且接口占用失败：%s" % exc)
                    return False
                self.dev = dev
                self.iface_num = num
                self.ep_out = addr
                self.ep_in = addr_in
                self.sig = "%s-%s" % (getattr(dev, "bus", ""), getattr(dev, "address", ""))
                set_state(
                    usb_online=True,
                    printer_name=self._get_product(dev),
                    usb_bus=str(getattr(dev, "bus", "-")),
                    usb_addr=str(getattr(dev, "address", "-")),
                    usb_speed=self._speed_text(dev),
                    last_error="",
                )
                set_printer_status(supported=addr_in is not None)
                log("USB 控制传输异常，已改用 sysfs 端点 0x%02x 直写连接（状态端点：%s）：%s"
                    % (addr, ("0x%02x" % addr_in) if addr_in else "无", cerr))
                self._fail_log = {"msg": "", "ts": 0}
                return True
            self._log_fail("读取 USB 配置失败：%s（sysfs 也取不到端点）" % cerr)
            return False
        if how == "cached":
            self._log_fail("USB 控制传输异常，已改用枚举缓存的配置描述符连接：%s" % cerr)

        iface = None
        try:
            for itf in cfg:
                if itf.bInterfaceClass == 7:
                    iface = itf
                    break
        except Exception:
            iface = None
        if iface is None:
            try:
                iface = cfg[(0, 0)]
            except Exception as exc:
                log("USB 接口解析失败：%s" % exc)
                return False

        try:
            if dev.is_kernel_driver_active(iface.bInterfaceNumber):
                dev.detach_kernel_driver(iface.bInterfaceNumber)
        except Exception:
            pass

        try:
            usb.util.claim_interface(dev, iface.bInterfaceNumber)
        except Exception as exc:
            # 退回缓存描述符时设备可能还没被配置，先显式设置一次再占用
            try:
                dev.set_configuration(cfg.bConfigurationValue)
                usb.util.claim_interface(dev, iface.bInterfaceNumber)
            except Exception:
                self._log_fail("占用 USB 接口失败（可能被其他程序占用）：%s" % exc)
                return False

        try:
            ep_out = usb.util.find_descriptor(
                iface,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_OUT,
            )
        except Exception:
            ep_out = None
        if ep_out is None:
            log("未找到打印机的 USB 输出端点")
            try:
                usb.util.release_interface(dev, iface.bInterfaceNumber)
            except Exception:
                pass
            return False

        self.dev = dev
        self.ep_out = ep_out
        try:
            ep_in = usb.util.find_descriptor(
                iface,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_IN,
            )
        except Exception:
            ep_in = None
        self.ep_in = ep_in
        self.iface_num = iface.bInterfaceNumber
        self.sig = "%s-%s" % (getattr(dev, "bus", ""), getattr(dev, "address", ""))
        set_printer_status(supported=ep_in is not None)

        product = self._get_product(dev)
        set_state(
            usb_online=True,
            printer_name=product,
            usb_bus=str(getattr(dev, "bus", "-")),
            usb_addr=str(getattr(dev, "address", "-")),
            usb_speed=self._speed_text(dev),
            last_error="",
        )
        log("打印机已连接：%s（%s）" % (product or "USB 打印机", PRINTER_ID))
        self._fail_log = {"msg": "", "ts": 0}
        return True

    def connect(self, dev=None, silent=False):
        with self.io_lock:
            self._close()
            if not PRINTER_ID:
                # 还没配置打印机：不猜任何型号，直接把状态说明写清楚
                set_state(usb_online=False, printer_name="", usb_bus="", usb_addr="",
                          usb_speed="", last_error="尚未设置打印机 ID")
                return False
            for attempt in (0, 1):
                cur = dev or self._find_dev()
                if cur is None:
                    set_state(usb_online=False, printer_name="", usb_bus="", usb_addr="", usb_speed="")
                    if not silent:
                        log("未检测到打印机 %s" % device_label())
                    return False
                if self._open_once(cur, silent=silent):
                    return True
                try:
                    usb.util.dispose_resources(cur)
                except Exception:
                    pass
                if attempt == 0:
                    # 第一次失败：清掉所有旧句柄后重新查找再试一次
                    self._purge()
                    dev = None
                    time.sleep(0.5)
            # 两次都没连上：状态必须落回“未连接”，否则面板会一直显示已连接
            set_state(
                usb_online=False,
                printer_name="",
                usb_bus="",
                usb_addr="",
                usb_speed="",
                last_error="USB 设备无法连接（%s）" % device_label(),
            )
            return False

    def disconnect(self, reason=""):
        with self.io_lock:
            was_ready = self.ep_out is not None
            self._close()
        if was_ready:
            # 句柄都没了，之前读到的那条状态已经过期，不能继续显示
            set_printer_status(code="", en="", zh="", code_zh="", online=None,
                               error="打印机未连接",
                               at=time.strftime("%Y-%m-%d %H:%M:%S"))
        if was_ready and reason:
            log(reason)

    def reconnect(self):
        """按当前设备 ID 重新连接"""
        self.disconnect()
        return self.connect()

    @staticmethod
    def _is_timeout(exc):
        txt = str(exc).lower()
        return ("timed out" in txt) or ("timeout" in txt) or ("errno 110" in txt)

    def _fail_write(self, exc):
        """写入彻底失败时释放句柄并落回离线，交给守护线程重新连一遍。

        超时（Errno 110）几乎都是打印机侧没取数据（休眠 / 缺纸 / 卡纸 / 盖板未关 /
        拔插后没恢复），继续拿着同一个句柄重试只会反复超时。
        """
        timeout = self._is_timeout(exc)
        try:
            self._close()
        except Exception:
            pass
        if timeout:
            set_state(
                usb_online=False,
                printer_name="",
                usb_bus="",
                usb_addr="",
                usb_speed="",
                last_error="写入超时（Errno 110）：打印机休眠 / 缺纸 / 卡纸 / 盖板未关",
            )
            log("USB 写入超时（Errno 110）：15 秒内一个字节都没送出去，"
                "常见原因是打印机休眠、缺纸、卡纸、盖板未关，或拔插后设备没恢复；已释放句柄，等待自动重连")
        else:
            set_state(
                usb_online=False,
                printer_name="",
                usb_bus="",
                usb_addr="",
                usb_speed="",
                last_error=str(exc),
            )
            log("USB 写入失败：%s；已释放句柄，等待自动重连" % exc)

    def write(self, data):
        with self.io_lock:
            if self.ep_out is None:
                raise RuntimeError("打印机未连接")
            offset = 0
            total = len(data)
            while offset < total:
                buf = data[offset:offset + CHUNK_SIZE]
                retry = 0
                while True:
                    try:
                        if isinstance(self.ep_out, int):
                            # sysfs 端点模式：用设备句柄按端点地址直接 bulk 写，不经过描述符
                            written = self.dev.write(self.ep_out, buf, timeout=USB_TIMEOUT_MS)
                        else:
                            written = self.ep_out.write(buf, timeout=USB_TIMEOUT_MS)
                        break
                    except Exception as exc:
                        retry += 1
                        if retry >= 3:
                            self._fail_write(exc)
                            raise RuntimeError("写入 USB 失败：%s" % exc)
                        if self._is_timeout(exc) and retry == 1:
                            log("USB 写入超时（第 1 次）：等待打印机取数据后重试")
                            time.sleep(1.0)
                            continue
                        time.sleep(0.2)
                offset += written if written else len(buf)

    def _raw_write(self, data, timeout_ms):
        """底层写，不触发“写失败就置离线”的那套处理，专供 PJL 查询用"""
        if isinstance(self.ep_out, int):
            self.dev.write(self.ep_out, data, timeout=timeout_ms)
        else:
            self.ep_out.write(data, timeout=timeout_ms)

    def _raw_read(self, size, timeout_ms):
        if isinstance(self.ep_in, int):
            return bytes(self.dev.read(self.ep_in, size, timeout=timeout_ms))
        return bytes(self.ep_in.read(size, timeout=timeout_ms))

    def query_status(self, wait_ms=2500):
        """用 PJL 的 @PJL INFO STATUS 问一次打印机状态。

        打印机回的是：
            CODE=40022
            DISPLAY="PAPER JAM"
            ONLINE=TRUE
        解析出 CODE 与 DISPLAY 后就翻成中文塞进 PJL_STATUS。
        整套流程用 try 兜住：读状态失败绝不能让打印任务挂掉。
        """
        if self.ep_out is None:
            set_printer_status(error="打印机未连接")
            return None
        if self.ep_in is None:
            set_printer_status(supported=False, error="该机型未暴露 Bulk IN 端点，无法回传状态",
                               at=time.strftime("%Y-%m-%d %H:%M:%S"))
            return None
        with self.io_lock:
            if self.ep_out is None or self.ep_in is None:
                return None
            try:
                # 先清掉上一次残留的回包，避免把旧状态当成新状态
                try:
                    for _ in range(4):
                        self._raw_read(256, 200)
                except Exception:
                    pass

                req = b"\x1b%-12345X@PJL INFO STATUS\r\n\x1b%-12345X\r\n"
                self._raw_write(req, USB_TIMEOUT_MS)
                deadline = time.time() + max(0.5, wait_ms / 1000.0)
                buf = b""
                while time.time() < deadline and len(buf) < 4096:
                    try:
                        chunk = self._raw_read(512, 400)
                    except Exception as exc:
                        if self._is_timeout(exc):
                            break
                        raise
                    if not chunk:
                        time.sleep(0.05)
                        continue
                    buf += chunk
                    if b"CODE=" in buf and buf.count(b"\x0c") >= 2:
                        break
                    if b"CODE=" in buf and b"DISPLAY=" in buf and b"\x0c" in buf[-8:]:
                        break
            except Exception as exc:
                msg = str(exc) or exc.__class__.__name__
                set_printer_status(error="读取状态失败：%s" % msg,
                                   at=time.strftime("%Y-%m-%d %H:%M:%S"))
                log("读取打印机状态失败：%s" % msg)
                return None

            if not buf:
                set_printer_status(error="打印机没有回应状态查询（可能不支持 PJL 状态回读）",
                                   at=time.strftime("%Y-%m-%d %H:%M:%S"))
                return printer_status_snapshot()

            code = ""
            display = ""
            disp_raw = b""
            online = None
            m = re.search(rb"CODE\s*=\s*(\d+)", buf)
            if m:
                code = m.group(1).decode("ascii", "ignore")
            m = re.search(rb'DISPLAY\s*=\s*"([^"]*)"', buf)
            if m:
                disp_raw = m.group(1)
            else:
                m = re.search(rb"DISPLAY\s*=\s*([^\r\n\x0c]+)", buf)
                if m:
                    disp_raw = m.group(1).strip().strip(b'"')
            display = smart_decode_text(disp_raw).strip()
            m = re.search(rb"ONLINE\s*=\s*(TRUE|FALSE)", buf, re.I)
            if m:
                online = m.group(1).upper() == b"TRUE"

            if disp_raw and not display:
                # 解码不出来也要留证据，方便按真实字节再补编码规则
                hexkey = "hex|%s" % disp_raw[:32].hex(" ")
                if hexkey != _LAST_STATUS_KEY.get("hex"):
                    _LAST_STATUS_KEY["hex"] = hexkey
                    log("打印机回传的状态文字无法识别，原始字节：%s" % disp_raw[:64].hex(" "))

            if not code and not display:
                set_printer_status(error="回包里没有 CODE/DISPLAY（该机型可能不按 PJL 规范回包）",
                                   at=time.strftime("%Y-%m-%d %H:%M:%S"))
                log("打印机状态回包无法解析，原始字节：%s" % buf[:160].hex(" "))
                return printer_status_snapshot()

            # 打印机自带中文/日文文字时直接用，别硬翻成英文对照表
            if has_cjk(display):
                zh = display
            else:
                zh = translate_display(display)
            code_zh = explain_pjl_code(code)
            set_printer_status(code=code, en=display, zh=zh, code_zh=code_zh,
                               online=online, supported=True, error="",
                               at=time.strftime("%Y-%m-%d %H:%M:%S"))

        brief = format_status_brief()
        key = "%s|%s" % (code, display)
        if brief and key != _LAST_STATUS_KEY.get("key"):
            _LAST_STATUS_KEY["key"] = key
            log("打印机状态：%s（原始：%s）" % (brief, (display or "无文字说明")))
        return printer_status_snapshot()


printer = UsbPrinter()


# ==================== 累计任务 / 累计数据（跨重启保留） ====================

TOTALS_PATH = os.path.join(DATA_DIR, "totals.json")
totals_lock = threading.RLock()
totals_meta = {"since": ""}


def load_totals():
    """把上次运行的累计任务数与累计流量读回来"""
    try:
        with open(TOTALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with lock:
        state["jobs"] = int(data.get("jobs", 0) or 0)
        state["bytes"] = int(data.get("bytes", 0) or 0)
    totals_meta["since"] = str(data.get("since", "") or "")


def save_totals():
    try:
        with lock:
            jobs_n = int(state["jobs"])
            bytes_n = int(state["bytes"])
        if not totals_meta.get("since"):
            totals_meta["since"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        tmp = TOTALS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"jobs": jobs_n, "bytes": bytes_n, "since": totals_meta["since"]}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TOTALS_PATH)
    except Exception as exc:
        print("[WARN] 累计统计保存失败：%s" % exc, flush=True)


# ==================== 打印任务记录 ====================

def load_jobs():
    if not os.path.exists(JOBS_PATH):
        return
    try:
        with open(JOBS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            with jobs_lock:
                jobs.clear()
                for item in data[-JOB_KEEP:]:
                    if isinstance(item, dict):
                        jobs.append(item)
    except Exception as exc:
        print("[WARN] 任务记录读取失败：%s" % exc, flush=True)


def save_jobs():
    try:
        with jobs_lock:
            data = list(jobs)
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        tmp = JOBS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, JOBS_PATH)
    except Exception as exc:
        print("[WARN] 任务记录保存失败：%s" % exc, flush=True)


host_cache = {}
host_cache_ts = {}
HOST_CACHE_TTL = 1800


# ==================== 每日打印统计 ====================

stats_lock = threading.RLock()
daily_stats = {}


def load_stats():
    global daily_stats
    try:
        if os.path.exists(STATS_PATH):
            with open(STATS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("days"), dict):
                daily_stats = data["days"]
    except Exception as exc:
        print("[WARN] 统计文件读取失败：%s" % exc, flush=True)


def save_stats():
    try:
        with stats_lock:
            # 只保留最近 180 天，避免文件无限增长
            cutoff = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%Y-%m-%d")
            data = dict((k, v) for k, v in daily_stats.items() if k >= cutoff)
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        tmp = STATS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"days": data}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATS_PATH)
    except Exception as exc:
        print("[WARN] 统计文件保存失败：%s" % exc, flush=True)


def bump_daily(ok, size, ip="", host=""):
    """累计当天统计，并按来源机器（IP）分别计数。"""
    key = time.strftime("%Y-%m-%d")
    with stats_lock:
        item = daily_stats.setdefault(key, {"ok": 0, "fail": 0, "bytes": 0})
        if ok:
            item["ok"] = int(item.get("ok", 0)) + 1
            item["bytes"] = int(item.get("bytes", 0)) + int(size or 0)
        else:
            item["fail"] = int(item.get("fail", 0)) + 1
        if ip:
            src = item.setdefault("src", {})
            rec = src.get(ip)
            if not isinstance(rec, dict):
                rec = {"n": 0, "ok": 0, "fail": 0, "bytes": 0, "host": ""}
                src[ip] = rec
            rec["n"] = int(rec.get("n", 0)) + 1
            if ok:
                rec["ok"] = int(rec.get("ok", 0)) + 1
                rec["bytes"] = int(rec.get("bytes", 0)) + int(size or 0)
            else:
                rec["fail"] = int(rec.get("fail", 0)) + 1
            cur_host = host or host_cache.get(ip, "")
            if cur_host:
                rec["host"] = cur_host
    save_stats()



def nbstat_name(ip, timeout=0.7):
    """NetBIOS 节点状态查询（等价 nbtstat -A）：Windows 客户端没有 PTR 也能问到主机名"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        tid = int(time.time()) & 0xFFFF
        pkt = (struct.pack(">HHHHHH", tid, 0x0000, 1, 0, 0, 0)
               + b"\x20" + b"CK" + b"A" * 30 + b"\x00"
               + struct.pack(">HH", 0x0021, 0x0001))
        sock.sendto(pkt, (ip, 137))
        data, _ = sock.recvfrom(2048)
    except Exception:
        return ""
    finally:
        if sock is not None:
            sock.close()
    try:
        head = data.find(b"\x00\x21\x00\x01", 12)
        if head < 0:
            return ""
        found = re.search(rb"[A-Za-z0-9][A-Za-z0-9._-]{1,15}", data[head + 11:])
        return found.group(0).decode("ascii", "ignore") if found else ""
    except Exception:
        return ""


def ptr_lookup(ip, timeout=2.0):
    """PTR 反查，带超时（系统 DNS 对不存在的 IP 会卡好几秒）"""
    box = {}

    def run():
        try:
            box["n"] = socket.gethostbyaddr(ip)[0]
        except Exception:
            box["n"] = ""

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    return (box.get("n") or "").strip()


def local_ipv4s():
    """列出本机非回环 IPv4（host 网络下就是 NAS 的真实局域网地址）"""
    out = []
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _idx, name in socket.if_nameindex():
            if name == "lo" or name.startswith(("docker", "br-", "veth", "virbr")):
                continue
            try:
                req = struct.pack("256s", name[:15].encode())
                res = fcntl.ioctl(s.fileno(), 0x8915, req)   # SIOCGIFADDR
                ip = socket.inet_ntoa(res[20:24])
                if ip and not ip.startswith("127."):
                    out.append(ip)
            except Exception:
                continue
        s.close()
    except Exception:
        pass
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 53))
            out.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return out


def primary_lan_ip():
    ips = local_ipv4s()
    for ip in ips:
        if ip.startswith("192.168."):
            return ip
    for ip in ips:
        if ip.startswith("10.") or ip.startswith("172."):
            return ip
    return ips[0] if ips else ""


def lookup_host(ip):
    """主机名查询：先问 NetBIOS（Windows 客户端一定有），再退 DNS 反查"""
    nb = nbstat_name(ip)
    if nb:
        return nb
    name = ptr_lookup(ip)
    # 只把 IP 拼成 192-168-77-20 这种没意义的名字，当没有
    if name and not re.match(r"^[\d\-_.]+$", name):
        return name
    return ""


def resolve_hostname(ip, record):
    """反查客户端主机名（后台线程，失败留空；空结果 30 分钟后重试）"""
    cached = host_cache.get(ip, "")
    cached_at = host_cache_ts.get(ip, 0)
    if cached or (cached_at and time.time() - cached_at < HOST_CACHE_TTL):
        record["host"] = cached
        save_jobs()
        return
    name = lookup_host(ip)
    host_cache[ip] = name
    host_cache_ts[ip] = time.time()
    record["host"] = name
    save_jobs()


def backfill_hosts():
    """启动时给历史上没有主机名的来源补查一次，让跨 IP 归并对旧数据也生效"""
    ips = set()
    with jobs_lock:
        for j in jobs:
            if j.get("ip") and not j.get("host"):
                ips.add(j["ip"])
    with stats_lock:
        for item in daily_stats.values():
            for ip, rec in (item.get("src") or {}).items():
                if isinstance(rec, dict) and not rec.get("host"):
                    ips.add(ip)
    changed = False
    for ip in list(ips)[:30]:
        if host_cache.get(ip):
            continue
        name = lookup_host(ip)
        host_cache[ip] = name
        host_cache_ts[ip] = time.time()
        if not name:
            continue
        with jobs_lock:
            for j in jobs:
                if j.get("ip") == ip and not j.get("host"):
                    j["host"] = name
                    changed = True
        with stats_lock:
            for item in daily_stats.values():
                rec = (item.get("src") or {}).get(ip)
                if isinstance(rec, dict) and not rec.get("host"):
                    rec["host"] = name
                    changed = True
    if changed:
        save_jobs()
        save_stats()


# ==================== 打印服务 ====================

last_fail_push_at = 0


def notify_job_fail(peer, err, size):
    global last_fail_push_at
    if not push_ready() or not push_conf().get("job_fail_push"):
        return
    if time.time() - last_fail_push_at < JOB_FAIL_COOLDOWN:
        return
    last_fail_push_at = time.time()
    push_async(
        "打印任务失败",
        push_html([
            "<b>USB 打印服务：打印任务失败</b>",
            "设备：%s（%s）" % (state["printer_name"] or "USB 打印机", device_label()),
            "来源：%s" % peer,
            "已传输：%s" % fmt_bytes(size),
            "原因：%s" % err,
            "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        ]),
    )


def handle_job(conn, addr):
    ip = addr[0]
    peer = "%s:%s" % (ip, addr[1])
    started = time.time()
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip,
        "host": host_cache.get(ip, ""),
        "size": 0,
        "seconds": 0,
        "ok": None,      # None = 进行中；结束时才写 True / False
        "msg": "",
    }
    with jobs_lock:
        jobs.append(record)
    threading.Thread(target=resolve_hostname, args=(ip, record), daemon=True).start()

    with lock:
        state["active_clients"] += 1
    log("收到打印连接：%s" % peer)

    total = 0
    ok = True
    if job_lock.locked():
        log("USB 正在打印上一个任务，%s 排队等待" % peer)
    job_lock.acquire()
    try:
        # 设备已从系统里消失时立刻失败，别让客户端干等十几秒的 USB 超时
        if not PRINTER_ID:
            raise RuntimeError("尚未设置打印机 ID，请先在面板「设备设置」页扫描并保存")
        if printer.sysfs_present() is False:
            raise RuntimeError("打印机已拔出（USB 设备不在系统中）")
        if not printer.is_ready():
            if not printer.connect(silent=True):
                raise RuntimeError("打印机未就绪")
        # 任务开始前先问一次打印机自己的状态（缺纸 / 卡纸 / 盖板未关都能提前看到）
        printer.query_status()
        record["pstatus"] = format_status_brief()
        conn.settimeout(60)
        while True:
            try:
                data = conn.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            printer.write(data)
            total += len(data)
        # 任务写完再问一次：卡纸、缺纸这类问题通常在打印过程中才出现
        if total:
            printer.query_status()
            record["pstatus"] = format_status_brief()
    except Exception as exc:
        ok = False
        failed_brief = format_status_brief()   # 先把此刻的状态摘出来，disconnect 会清空它
        printer.disconnect()
        set_state(
            usb_online=False,
            printer_name="",
            usb_bus="",
            usb_addr="",
            usb_speed="",
            last_error=str(exc),
        )
        record["msg"] = str(exc) or exc.__class__.__name__
        if failed_brief:
            record["pstatus"] = failed_brief
            record["msg"] += "；打印机状态：%s" % failed_brief
        log("打印任务失败（来源 %s）：%s" % (ip, record["msg"]))
        notify_job_fail(peer, record["msg"], total)
    finally:
        job_lock.release()
        time.sleep(0.5)      # 给打印机一点消化缓冲的时间，避免下一个任务立刻撞上 110
        try:
            conn.close()
        except Exception:
            pass
        with lock:
            state["active_clients"] -= 1
            if ok and total:
                state["jobs"] += 1
                state["bytes"] += total
                state["last_job_size"] = total
            state["last_job_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        record["size"] = total
        record["seconds"] = round(time.time() - started, 2)
        record["ok"] = ok
        if ok and total == 0:
            record["msg"] = "空任务（客户端没有发送数据）"
        elif ok:
            record["msg"] = "成功"
        save_jobs()
        save_totals()
        if ok and total == 0:
            log("打印任务结束：来源 %s，客户端未发送任何数据，按空任务忽略" % ip)
        else:
            bump_daily(ok, total, ip=ip, host=record.get("host", ""))
            log("打印任务结束：来源 %s，数据 %d 字节，结果 %s" % (ip, total, "成功" if ok else "失败"))


def print_server_loop():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", PRINT_PORT))
    except OSError as exc:
        log("打印端口 %d 绑定失败：%s" % (PRINT_PORT, exc))
        log("端口被占用了：容器环境变量里把 PRINT_PORT 改成别的空闲端口（当前 %d），"
            "客户端添加打印机时填新端口" % PRINT_PORT)
        set_state(port_listening=False)
        return
    srv.listen(8)
    set_state(port_listening=True)
    log("打印服务已启动，监听 0.0.0.0:%d" % PRINT_PORT)
    while True:
        try:
            conn, addr = srv.accept()
        except Exception as exc:
            log("接收连接异常：%s" % exc)
            time.sleep(1)
            continue
        threading.Thread(target=handle_job, args=(conn, addr), daemon=True).start()


# ==================== USB 守护线程 ====================

offline_since = None
offline_alerted = False
connect_fail_streak = 0
usb_rescan_last = 0.0


def usb_rescan(force=False, reason=""):
    """让内核重新扫描 PCI/USB 总线。

    打印机休眠、USB 端口被挂起或控制器异常后，/sys/bus/usb/devices 里会彻底
    没有这台设备，此时重连 libusb 是找不回来的（只有容器重启才“碰巧”恢复）。
    写 /sys/bus/pci/rescan 让内核重新枚举，是最通用的触发方式。
    默认 5 分钟最多一次，避免反复挑动总线。
    """
    global usb_rescan_last
    now = time.time()
    if not force and now - usb_rescan_last < 300:
        return False
    usb_rescan_last = now
    try:
        with open("/sys/bus/pci/rescan", "w") as fh:
            fh.write("1")
    except Exception as exc:
        log("USB 总线重扫失败（/sys 不可写或内核不支持）：%s" % exc)
        return False
    log("已请求内核重新扫描 PCI/USB 总线%s" % ("（%s）" % reason if reason else ""))
    return True


def restart_self(force=False):
    """原地重启本进程。

    等价于 docker compose restart：挂载和容器都不动，只让进程重来一遍，
    libusb 上下文、句柄、设备列表全部重建。
    自动触发时限流 10 分钟一次；手动点击（force=True）不受限制。
    """
    if not force:
        mark = os.path.join(DATA_DIR, ".last_autorestart")
        try:
            last = os.path.getmtime(mark) if os.path.exists(mark) else 0
        except Exception:
            last = 0
        if time.time() - last < 600:
            return
        try:
            with open(mark, "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
        except Exception:
            pass
        log("USB 连续失败 4 次，自动重启进程以重新初始化 USB 子系统")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        log("重启进程失败：%s" % exc)


def watcher_loop():
    global offline_since, offline_alerted, connect_fail_streak
    if not PYUSB_OK:
        log("pyusb 不可用（%s），USB 打印功能无法启用" % PYUSB_ERROR)

    present_prev = None      # 上一轮 sysfs 里是否看得到设备，用来识别“刚被拔掉”
    no_id_logged = False
    while True:
        if not PRINTER_ID:
            # 没配打印机：什么都不连、不推送离线，只把状态写成人能看懂的一句话
            if not no_id_logged:
                no_id_logged = True
                log("尚未设置打印机 ID：打开面板「设备设置」页扫描 USB 设备并保存；"
                    "配置好之前不会尝试连接任何设备")
            offline_since = None
            offline_alerted = False
            connect_fail_streak = 0
            with lock:
                need_reset = (bool(state["usb_online"]) or bool(state["printer_name"])
                              or state.get("last_error") != "尚未设置打印机 ID")
            if need_reset:
                printer.disconnect()
                set_state(usb_online=False, printer_name="", usb_bus="", usb_addr="",
                          usb_speed="", last_error="尚未设置打印机 ID")
            time.sleep(3)
            continue
        no_id_logged = False
        dev = None
        present = None
        try:
            if PYUSB_OK:
                present = printer.sysfs_present()
                # sysfs 明确说设备不在了，就不用再问 libusb（它会给回幽灵句柄）
                if present is not False:
                    dev = printer._find_dev()
        except Exception as exc:
            log("USB 守护线程异常：%s" % exc)

        just_vanished = (present is False and present_prev is True)
        if just_vanished:
            log("打印机已拔出：USB 设备已从系统移除（%s）" % device_label())
        present_prev = present

        try:
            conf = push_conf()
            alert_sec = int(conf.get("offline_alert_sec") or 60)
            if dev is None:
                connect_fail_streak = 0
                # 只要之前连过（状态里是已连接，或句柄还没释放），就记一条断开日志
                with lock:
                    was_online = bool(state["usb_online"])
                if was_online or printer.ep_out is not None:
                    printer.disconnect()
                    set_state(
                        usb_online=False,
                        printer_name="",
                        usb_bus="",
                        usb_addr="",
                        usb_speed="",
                        last_error="未检测到打印机（%s）" % device_label(),
                    )
                    if not just_vanished:
                        log("打印机已断开：未检测到 %s，等待重新插入" % device_label())
                if offline_since is None:
                    offline_since = time.time()
                # 设备彻底从总线消失时，单纯重连没用：定期让内核重新枚举一次
                if present is False and (time.time() - offline_since) >= 90:
                    usb_rescan(reason="打印机离线超过 90 秒")
                if (not offline_alerted) and push_ready() and (time.time() - offline_since) >= alert_sec:
                    offline_alerted = True
                    push_async(
                        "打印机离线",
                        push_html([
                            "<b>USB 打印服务：打印机离线</b>",
                            "设备：%s" % device_label(),
                            "状态：已持续离线 %d 秒，无法打印" % int(time.time() - offline_since),
                            "检查：USB 线是否松动 / 打印机是否断电 / 是否被其他程序占用",
                            "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                        ]),
                    )
            else:
                if offline_alerted:
                    push_async(
                        "打印机已恢复",
                        push_html([
                            "<b>USB 打印服务：打印机已恢复</b>",
                            "设备：%s（%s）" % ((printer._get_product(dev) or "USB 打印机"), device_label()),
                            "状态：已重新连接，可以正常打印",
                            "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                        ]),
                    )
                offline_since = None
                offline_alerted = False
                cur_sig = "%s-%s" % (getattr(dev, "bus", ""), getattr(dev, "address", ""))
                if not printer.is_ready() or printer.sig != cur_sig:
                    with lock:
                        state["reconnects"] += 1
                    ok = printer.connect(dev=dev, silent=True)
                    if ok:
                        connect_fail_streak = 0
                    else:
                        connect_fail_streak += 1
                        if connect_fail_streak == 2:
                            log("USB 连接连续失败，已清理旧句柄并准备重启进程重试")
                        if connect_fail_streak >= 4:
                            restart_self()
                # 空闲时每 20 秒问一次打印机状态（正在打印时不插队，避免搅乱数据流）
                try:
                    if printer.is_ready() and not job_lock.locked() and (time.time() - printer._status_at) >= 20:
                        printer._status_at = time.time()
                        printer.query_status()
                except Exception as exc:
                    log("定时读取打印机状态异常：%s" % exc)
        except Exception as exc:
            log("USB 守护线程异常：%s" % exc)
        # 正常时 3 秒一次；连续失败时逐步拉长到 30 秒，避免反复操作把设备弄僵
        if connect_fail_streak:
            time.sleep(min(3 + 3 * connect_fail_streak, 30))
        else:
            time.sleep(3)


# ==================== 登录页 / 首次设置密码 ====================

AUTH_STYLE = """
:root{--bg:#f2f5f9;--card:#fff;--line:#e3e8ef;--txt:#1b2530;--sub:#63707e;--brand:#0a84ff;--bad:#e5484d;--ok:#12a150;}
*{box-sizing:border-box;margin:0;padding:0}
/* 纵向排布：卡片 + 页脚都在文档流里，键盘弹出页面滚动时页脚跟着走，不会压到卡片上 */
body{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;min-height:100dvh;background:linear-gradient(160deg,#eaf3ff,#f2f5f9 45%,#eef7f1);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--txt);padding:20px}
.card{width:100%;max-width:390px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:30px 26px;box-shadow:0 10px 30px rgba(20,40,70,.08)}
h1{font-size:19px;font-weight:600;text-align:center}
.tip{font-size:13px;color:var(--sub);text-align:center;margin-top:8px;line-height:1.6}
label{display:block;font-size:13px;color:var(--sub);margin:18px 0 7px}
input{width:100%;padding:12px 13px;border:1px solid var(--line);border-radius:10px;font-size:16px;outline:none;background:#fbfcfd}
input:focus{border-color:var(--brand);background:#fff}
button{width:100%;margin-top:22px;padding:13px;border:none;border-radius:10px;background:var(--brand);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:active{transform:scale(.98)}
.err{background:#fdecec;color:var(--bad);font-size:13px;padding:10px 12px;border-radius:10px;margin-top:16px;text-align:center;line-height:1.5}
.note{font-size:12px;color:var(--sub);margin-top:16px;line-height:1.7;background:#f7fafc;border:1px solid var(--line);border-radius:10px;padding:11px 13px}
/* 可视高度被键盘压得很短时，改成顶部对齐，避免居中把卡片上半截挤出屏幕 */
@media (max-height:640px){body{justify-content:flex-start;padding-top:16px;padding-bottom:16px}}
"""

# ==================== 页脚版权 ====================

def footer_html(with_license=True):
    """页脚版权：年份按当前时间自动取，不用每年手改。"""
    lic = ' · <a href="/license">查看许可</a>' if with_license else ""
    gh = (' · <a class="footgh" href="%s" target="_blank" rel="noopener"'
          ' title="GitHub 项目主页" aria-label="GitHub 项目主页">%s</a>') % (
              PROJECT_URL, ICONS.get("github", ""))
    return (
        '<div class="foot">Copyright © ' + time.strftime("%Y") +
        ' <a href="https://www.godr.cn/" target="_blank" rel="noopener">亦桉</a>'
        ' · www.godr.cn All rights reserved.'
        '<div class="foot2">未经许可禁止商业用途，二次开发须保留本版权信息'
        + lic + gh + '</div>'
        '</div>'
    )

FOOTER_CSS = (
    '.foot{text-align:center;font-size:12px;color:var(--sub);margin:16px 0 4px;line-height:1.8}'
    '.foot a{color:var(--brand);text-decoration:none}'
    '.foot a:hover{text-decoration:underline}'
    '.foot2{font-size:11.5px;color:#98a3b1;margin-top:5px;line-height:1.7}'
    '.foot2 a{color:#98a3b1;text-decoration:underline}'
    # GitHub 链接：只显示图标。不要用内联 flex 盒子去对齐（不同浏览器字体度量差异大），
    # 直接让 SVG 自己对基线居中，再微调 1px
    '.footgh{display:inline;line-height:1}'
    '.foot2 a.footgh{color:#6f7b8b;text-decoration:none}'
    '.foot2 a.footgh:hover{color:var(--brand)}'
    '.footgh svg{width:15px;height:15px;fill:currentColor;stroke:none;'
    'vertical-align:middle;position:relative;top:-1.5px;pointer-events:none}'
)

# 登录页 / 首次设置页：页脚跟卡片一样在文档流里（body 已改为纵向 flex），
# 不用 position:fixed——固定定位在手机上弹出键盘、页面滚动时会被拖到卡片上，压住输入区
AUTH_FOOTER_CSS = (
    FOOTER_CSS
    + '.foot{position:static;margin:18px 0 0;padding:0 16px;text-align:center;z-index:auto}'
)

# ==================== 内联 SVG 图标 ====================
# 不用 emoji / 符号字符：部分手机系统缺对应字形，会显示成空白方框（豆腐块）

ICONS = {
    "gear": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/>'
            '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06'
            'a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09'
            'A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83'
            'l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09'
            'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0'
            'l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09'
            'a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83'
            'l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2'
            'h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    "power": '<svg viewBox="0 0 24 24"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>'
             '<line x1="12" y1="2" x2="12" y2="12"/></svg>',
    "printer": '<svg viewBox="0 0 24 24"><polyline points="6 9 6 2 18 2 18 9"/>'
               '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
               '<rect x="6" y="14" width="12" height="8"/></svg>',
    "tag": '<svg viewBox="0 0 24 24"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10'
           'l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>',
    "chart": '<svg viewBox="0 0 24 24"><line x1="18" y1="20" x2="18" y2="10"/>'
             '<line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>',
    "bell": '<svg viewBox="0 0 24 24"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>'
            '<path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
    "key": '<svg viewBox="0 0 24 24"><path d="M21 2l-2 2"/><circle cx="7.5" cy="15.5" r="5.5"/>'
           '<path d="M11.5 11.5L16 7l2 2 2-2-2-2"/></svg>',
    # GitHub 官方 mark（实心，靠 CSS 里 fill:currentColor 上色）
    "github": '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/></svg>',
}


LICENSE_TEXT = """p910nd-panel 使用许可与声明
Copyright © 2026 亦桉 · https://www.godr.cn/
All rights reserved. 保留所有权利。

使用、复制、修改或分发本项目的任何部分，即表示你已阅读并同意以下条款。

一、允许的使用
1. 个人学习、研究、测试与自用部署。
2. 非营利组织、社区、学校的内部自用部署。
3. 在完整保留本声明的前提下，复制、修改、二次开发与非商业性再分发。

二、禁止商业用途
1. 未经版权所有者书面许可，不得将本项目或其实质性部分用于任何商业用途，
   包括但不限于：
   - 销售本项目，或以本项目为核心卖点销售商品、服务、解决方案；
   - 提供付费部署、代运维、技术支持等收费服务；
   - 以 SaaS、云打印、按次收费等形式对外提供本项目的功能；
   - 预装或捆绑进对外销售的硬件、整机、系统镜像；
   - 以本项目为主要内容开展培训、课程、付费社群并获利。
2. 商业授权需单独取得书面许可，联系方式：项目地址 https://github.com/meyzh/p910nd-panel
   或邮箱 me@godr.cn 。

三、版权与署名必须保留
1. 不得移除、隐藏、篡改本项目中的版权声明与作者署名，包括但不限于：
   源码文件中的版权注释、网页界面底部的版权信息（© 亦桉 及指向
   https://www.godr.cn/ 的链接）、README 与许可证文件。
2. 二次开发、修改、衍生版本在分发或以任何形式对外提供时，必须：
   - 完整保留许可证文件与原作者版权声明；
   - 在显著位置注明「基于 p910nd-panel（https://github.com/meyzh/p910nd-panel）修改」；
   - 保留网页界面底部的原作者版权信息，不得删除或替换为他人署名。
3. 衍生作品继续受本许可约束，不得单方面改为 MIT、Apache-2.0 等更宽松的
   许可，除非取得版权所有者的书面同意。
4. 不得以原作者名义对外宣传，或暗示原作者对衍生作品提供担保、背书。

四、免责声明
本软件按「现状」提供，不提供任何明示或暗示的担保，包括但不限于适销性、
特定用途适用性与不侵权的担保。在任何情况下，作者或版权持有人均不对因使用
本软件或与本软件相关的行为所产生的任何索赔、损害或其他责任负责。

五、许可终止
违反上述任何条款，本许可自动终止，你须立即停止使用并删除全部副本。

Non-commercial use only. Commercial use requires prior written permission
from the copyright holder. The copyright notice and author attribution
(including the footer credit "© 亦桉" linking to https://www.godr.cn/)
must be retained in all copies and derivative works."""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/logo.svg">
<title>登录 · USB 打印服务面板</title>
<style>%STYLE%</style>
</head>
<body>
<div class="card">
  <h1>USB 打印服务面板</h1>
  <div class="tip">输入访问密码进入</div>
  %ERR%
  <form method="post" action="/login">
    <label>访问密码</label>
    <input name="password" type="password" autocomplete="current-password" autofocus required>
    <button type="submit">进入面板</button>
  </form>
  <div class="note">忘记密码：删除容器数据目录里的 <b>data/config.json</b> 后重启，会重新进入首次设置流程（打印记录与推送配置也会一起重置）。</div>
</div>
%FOOTER%
</body>
</html>
"""

SETUP_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/logo.svg">
<title>首次设置密码 · USB 打印服务面板</title>
<style>%STYLE%</style>
</head>
<body>
<div class="card">
  <h1>首次设置访问密码</h1>
  <div class="tip">这是第一次打开面板，请自己设定一个访问密码<br>以后进面板只需要输入密码，不再有用户名</div>
  %ERR%
  <form method="post" action="/setup">
    <label>设置密码（至少 8 位，不能纯数字）</label>
    <input name="password" type="password" autocomplete="new-password" autofocus required>
    <label>再输一次确认</label>
    <input name="confirm" type="password" autocomplete="new-password" required>
    <button type="submit">设置并进入面板</button>
  </form>
  <div class="note">密码只以 PBKDF2 加盐哈希保存在服务器本地，不会写入任何环境变量或日志，也无法被反推出明文；日志里只记录登录成功/失败和来源 IP。</div>
</div>
%FOOTER%
</body>
</html>
"""


# ==================== 主页面 ====================

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/logo.svg">
<title>USB 打印机共享面板</title>
<style>
:root{--bg:#f2f5f9;--card:#fff;--line:#e3e8ef;--txt:#1b2530;--sub:#63707e;--ok:#12a150;--bad:#e5484d;--brand:#0a84ff;}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt);padding:18px}
.wrap{max-width:1020px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
.mt{margin-top:16px}
.head{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:nowrap}
.hinfo{flex:1 1 auto;min-width:0}
.title{font-size:20px;font-weight:600;letter-spacing:.2px}
.sub{font-size:13px;color:var(--sub);margin-top:6px}
.sub b{color:var(--txt);font-weight:600}
.right{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.icnav{display:flex;gap:8px;align-items:center;flex:none}
.iconbtn{display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:11px;background:#f3f6fa;border:1px solid var(--line);color:var(--txt);text-decoration:none;font-size:17px;line-height:1;transition:background .15s,border-color .15s,color .15s,transform .12s}
.iconbtn:hover{background:#e9f2ff;border-color:#cfe2ff;color:#0a63c8}
.iconbtn:active{transform:scale(.96)}
.iconbtn svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
/* 两个图标按钮保持同一底色，只有退出按钮悬停变红，避免一个白一个灰看着像两种样式 */
.iconbtn.out{color:var(--sub)}
.iconbtn.out:hover{background:#fdecec;border-color:#f6d3d4;color:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px;margin-top:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;min-width:0;display:flex;flex-direction:column;justify-content:center}
.stat .k{font-size:13px;color:var(--sub)}
.stat .v{font-size:20px;font-weight:700;margin-top:8px}
.stat .v.small{font-size:15px}
.stat.wide{grid-column:span 3}
.stat.tri{grid-column:span 2;text-align:center;align-items:center}
.stat.wide .khint{font-size:11.5px;color:#9aa7b4;margin-left:6px}
.svcrow{display:flex;align-items:center;gap:8px;margin-top:8px}
.svcrow .v{margin:0}
.svcdot{width:8px;height:8px;border-radius:50%;background:#b6c1cc;flex:none}
.svcdot.ok{background:var(--ok)}
.svcdot.warn{background:#c8922a}
.svcdot.bad{background:var(--bad)}
.svcstate{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-top:12px;padding-top:11px;border-top:1px dashed var(--line)}
.svcmain{font-size:14.5px;font-weight:700;line-height:1.4;word-break:break-word;color:var(--txt)}
.svcstate.ok .svcmain{color:var(--ok)}
.svcstate.warn .svcmain{color:#a8751a}
.svcstate.bad .svcmain{color:var(--bad)}
.svcstate.idle .svcmain{color:var(--sub)}
.svcat{font-size:11.5px;color:#9aa7b4;font-weight:400}
.svcmeta{margin-top:5px;font-size:12px;color:var(--sub);line-height:1.55;word-break:break-word}
.svcmeta b{color:var(--txt);font-weight:600}
.connmeta{margin-top:6px;font-size:12px;color:var(--sub)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:14px}
.rowitem{background:var(--card);padding:13px 16px;display:flex;justify-content:space-between;gap:10px;font-size:14px}
.rowitem .k{color:var(--sub);flex:none}
.rowitem .v{font-weight:600;text-align:right;word-break:break-all}
.btns{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}
button{border:none;border-radius:10px;padding:11px 18px;font-size:14px;font-weight:600;cursor:pointer;transition:transform .12s,opacity .12s}
button:active{transform:scale(.97)}
.b1{background:var(--brand);color:#fff}
.b2{background:#eaf2fb;color:#21507d}
.b4{background:#f2f4f7;color:#4a5562}
h3{font-size:15px;font-weight:600;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;gap:10px}
h3 .hint{font-size:12px;color:var(--sub);font-weight:400}
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}
.pager{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:10px;font-size:12.5px;color:var(--sub);flex-wrap:wrap}
.pager button{padding:7px 13px;border:1px solid var(--line);background:#fbfcfd;color:var(--txt);border-radius:9px;font-size:12.5px;font-weight:500;margin:0}
.pager button:disabled{opacity:.45;cursor:default}
.pager button:active:not(:disabled){transform:scale(.97)}
.pager .pgsize{margin-right:auto;display:inline-flex;align-items:center;gap:6px;color:var(--sub)}
.pager select{padding:6px 9px;border:1px solid var(--line);border-radius:9px;font-size:12.5px;background:#fbfcfd;color:var(--txt)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.toolbar select{padding:9px 12px;border:1px solid var(--line);border-radius:10px;font-size:13px;background:#fbfcfd;color:var(--txt)}
.legend{display:flex;gap:18px;font-size:12.5px;color:var(--sub);margin-bottom:8px}
.lg{display:inline-flex;align-items:center;gap:6px}
.sw{width:12px;height:12px;border-radius:3px}
.sw.ok{background:#12a150}
.sw.fail{background:#e5484d}
.chart{display:flex;align-items:flex-end;gap:5px;height:196px;padding:12px 8px 6px;border:1px solid var(--line);border-radius:12px;overflow-x:auto;background:#fcfdfe}
.col{flex:1 0 28px;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;min-width:28px;height:100%}
.bars{display:flex;flex-direction:column-reverse;align-items:center;width:100%;flex:none}
.bar{width:58%;min-height:0;border-radius:3px 3px 0 0}
.bar.ok{background:#12a150}
.bar.fail{background:#e5484d}
.dt{font-size:10.5px;color:var(--sub);margin-top:6px;white-space:nowrap;flex:none}
.col{cursor:pointer;border-radius:6px}
.col.ph{cursor:default;align-self:stretch}
.col:hover{background:#f1f6fd}
.col.on{background:#eaf2fb}
.col.on .dt{color:var(--brand);font-weight:600}
.daydetail{margin-top:12px;border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:#fbfcfd;font-size:13px}
.ddhead{position:relative;padding-right:58px;margin-bottom:10px;line-height:1.75}
.ddhead b{font-size:13.5px}
.ddsum{font-size:12.5px;color:var(--sub);margin-left:8px}
.ddsum em{font-style:normal;color:var(--brand);font-weight:600}
.ddclose{position:absolute;top:-2px;right:0;border:none;background:none;color:var(--sub);font-size:12px;cursor:pointer;padding:2px 6px;line-height:1.4}
.ddlist{display:flex;flex-direction:column;gap:6px}
.ddrow{display:flex;align-items:center;gap:10px;padding:7px 10px;background:#fff;border:1px solid var(--line);border-radius:9px;font-size:12.5px}
.ddrow .ip{font-weight:600;color:var(--txt)}
.ddrow .hn{color:var(--sub);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ddrow .cnt{color:var(--brand);font-weight:600;white-space:nowrap}
.ddrow .fl{color:#e5484d;font-weight:600;white-space:nowrap}
.ddrow .bt{color:var(--sub);white-space:nowrap}
.ddempty{color:var(--sub);font-size:12.5px;padding:4px 0}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:620px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:#f8fafc;color:var(--sub);font-weight:600}
tr:last-child td{border-bottom:none}
td.ok{color:var(--ok);font-weight:600}
td.bad{color:var(--bad);font-weight:600}
.empty{text-align:center;color:var(--sub);padding:22px;font-size:13px}
input,select{padding:11px 12px;border:1px solid var(--line);border-radius:10px;font-size:14px;outline:none;background:#fbfcfd;color:var(--txt)}
input:focus,select:focus{border-color:var(--brand);background:#fff}
.logbox{background:#0f1720;color:#7ee0a4;border-radius:12px;padding:14px;height:280px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:13px;line-height:1.65;white-space:pre-wrap}
.logbox .logline{white-space:pre-wrap;word-break:break-all}
.foot{text-align:center;font-size:12px;color:var(--sub);margin:16px 0 4px}
@media (max-width:700px){.stats{grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.stat.wide{grid-column:span 3}.stat.tri{grid-column:span 1}}
@media (max-width:560px){.title{font-size:17px}.stats{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.stat.wide,.stat.tri{grid-column:auto}.stat.wide{grid-column:span 3}.stat{padding:12px}.stat .k{font-size:11.5px}.stat .v{font-size:16px;margin-top:5px}button{flex:1 1 45%}.iconbtn{width:34px;height:34px;font-size:15px}
.pager{display:grid;grid-template-columns:1fr 1fr;gap:8px;align-items:center;justify-content:stretch}
.pager .pgsize{grid-column:1;margin-right:0;justify-self:start}
.pager .pginfo{grid-column:2;justify-self:end;text-align:right}
.pager button{width:100%;flex:0 0 auto;margin:0}input,select{font-size:16px}}
%FOOTER_CSS%
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="head">
      <div class="hinfo">
        <div class="title">USB 打印机共享面板</div>
        <div class="sub">目标设备 <span id="devid">--</span></div>
      </div>
      <div class="right">
        <div class="icnav">
          <a class="iconbtn" href="/settings" title="设置" aria-label="设置">%ICON_GEAR%</a>
          <a class="iconbtn out" href="/logout" title="退出登录" aria-label="退出登录">%ICON_POWER%</a>
        </div>
      </div>
    </div>

    <div class="stats">
      <div class="stat wide" id="stat_conn">
        <div class="k">打印机连接 <span class="khint">USB 设备连接 + 服务运行状态</span></div>
        <div class="svcrow">
          <span class="svcdot" id="s_usbdot"></span>
          <span class="v" id="s_usb">--</span>
        </div>
        <div class="svcstate idle" id="svcconn">
          <span class="svcdot" id="svc_dot"></span>
          <span class="svcmain" id="svc_main">检测中</span>
        </div>
        <div class="connmeta" id="i_connmeta">USB 直连 · 共享端口 9100</div>
      </div>
      <div class="stat wide" id="stat_svc">
        <div class="k">打印服务 <span class="khint">9100 端口监听 + 打印机回传状态</span></div>
        <div class="svcrow">
          <span class="svcdot" id="s_portdot"></span>
          <span class="v" id="s_port">--</span>
        </div>
        <div class="svcstate idle" id="svcstate">
          <span class="svcdot" id="ps_dot"></span>
          <span class="svcmain" id="i_pstate">读取中…</span>
          <span class="svcat" id="i_pat"></span>
        </div>
        <div class="svcmeta" id="i_pcode">正在向打印机查询状态…</div>
      </div>
      <div class="stat tri"><div class="k">累计任务</div><div class="v" id="s_jobs">0</div></div>
      <div class="stat tri"><div class="k">累计数据</div><div class="v small" id="s_bytes">0 B</div></div>
      <div class="stat tri"><div class="k">来源设备数</div><div class="v" id="s_src">0</div></div>
    </div>

    <div class="grid">
      <div class="rowitem"><span class="k">设备型号</span><span class="v" id="i_name">--</span></div>
      <div class="rowitem"><span class="k">USB 总线 / 地址</span><span class="v" id="i_bus">--</span></div>
      <div class="rowitem"><span class="k">USB 速率</span><span class="v" id="i_speed">--</span></div>
      <div class="rowitem"><span class="k">运行时长</span><span class="v" id="i_uptime">--</span></div>
      <div class="rowitem"><span class="k">当前连接数</span><span class="v" id="i_clients">0</span></div>
      <div class="rowitem"><span class="k">重连次数</span><span class="v" id="i_reconnects">0</span></div>
      <div class="rowitem"><span class="k">最后任务时间</span><span class="v" id="i_lasttime">--</span></div>
      <div class="rowitem"><span class="k">最后任务数据量</span><span class="v" id="i_lastsize">--</span></div>
    </div>

    <div class="btns">
      <button class="b1" onclick="refresh()">立即刷新</button>
      <button class="b4" id="autobtn" onclick="toggleAuto()">自动刷新：开</button>
      <button class="b2" onclick="reconnect()">重连打印机</button>
      <button class="b4" onclick="restartPanel()">重启服务</button>
    </div>
  </div>

  <div class="card mt">
    <h3>打印统计 <span class="hint" id="statHint">--</span></h3>
    <div class="toolbar">
      <select id="statDays" onchange="onStatDays(this.value)">
        <option value="7">近 7 天</option>
        <option value="14" selected>近 14 天</option>
        <option value="30">近 30 天</option>
      </select>
    </div>
    <div class="legend">
      <span class="lg"><span class="sw ok"></span>成功任务</span>
      <span class="lg"><span class="sw fail"></span>失败任务</span>
    </div>
    <div class="chart" id="chart"><div class="empty">加载中...</div></div>
    <div class="daydetail" id="daydetail" style="display:none"></div>
  </div>

  <div class="card mt">
    <h3>打印监控 <span class="hint" id="jobhint">最近 %JOB_KEEP% 条任务，含来源机器</span></h3>
    <div class="tblwrap">
      <table>
        <thead><tr><th>时间</th><th>来源 IP</th><th>来源主机</th><th>数据量</th><th>耗时</th><th>结果</th></tr></thead>
        <tbody id="jobbody"><tr><td colspan="6" class="empty">暂无任务记录</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <label class="pgsize">每页
        <select id="jobsize" onchange="setJobPageSize(this.value)">
          <option value="5">5</option>
          <option value="10">10</option>
          <option value="20">20</option>
          <option value="30">30</option>
          <option value="50">50</option>
          <option value="100">100</option>
        </select> 条
      </label>
      <span class="pginfo" id="jobpageinfo">共 0 条</span>
      <button id="jobprev" onclick="jobPage(-1)">上一页</button>
      <button id="jobnext" onclick="jobPage(1)">下一页</button>
    </div>
  </div>

  <div class="card mt">
    <h3>运行日志 <span class="hint" id="loghint">写入 data/panel.log，重启后仍保留</span></h3>
    <div class="logbox" id="logbox">加载中...</div>
  </div>
  <div class="foot">RAW 打印地址 tcp://本机IP:9100</div>
</div>

<script>
var autoRefresh = true;
var timer = null;

function fmtBytes(n){
  if(!n) return "0 B";
  var u=["B","KB","MB","GB"],i=0,v=n;
  while(v>=1024 && i<u.length-1){v=v/1024;i++;}
  return (i===0? v : v.toFixed(1))+" "+u[i];
}
function setText(id,txt){var e=document.getElementById(id);if(e)e.innerText=txt;}

/* ---- 打印机回传状态（PJL）：整条状态带，主状态 + 代码 + 原文 ---- */
function pstatLevel(st){
  if(!st) return "idle";
  if(st.online === false) return "bad";
  var code = String(st.code || ""), n = parseInt(code, 10);
  if(code && !isNaN(n)){
    if(n >= 40000) return "bad";
    if(n >= 30000) return "warn";
    if(n >= 10000) return "ok";
  }
  var m = String(st.main || "");
  if(/卡纸|缺纸|用尽|未关|故障|错误|检修/.test(m)) return "bad";
  if(/不足|将满|规格不符|异常/.test(m)) return "warn";
  if(/就绪|正常|已收到/.test(m)) return "ok";
  if(/休眠|预热|初始化|校准|打印中/.test(m)) return "idle";
  if(st.error) return "warn";
  return "idle";
}
function paintPstatus(st){
  var main = "读取中…", meta = "正在向打印机查询状态…", at = "", cls = "idle";
  if(st){
    main = st.main || "未读取到状态";
    cls = pstatLevel(st);
    var bits = [];
    if(st.code){
      bits.push("<b>CODE=" + esc(st.code) + "</b>" + (st.code_zh ? " · " + esc(st.code_zh) : ""));
    }else if(st.code_zh){
      bits.push(esc(st.code_zh));
    }
    if(st.en && st.en !== main) bits.push("原文：" + esc(st.en));
    if(st.error && !st.code) bits.push(esc(st.error));
    meta = bits.join(" · ") || "打印机未回传具体状态";
    at = st.at ? "更新于 " + st.at : "";
  }
  var e;
  if(e = document.getElementById("svcstate")) e.className = "svcstate " + cls;
  if(e = document.getElementById("ps_dot")) e.className = "svcdot " + cls;
  if(e = document.getElementById("i_pstate")) e.innerText = main;
  if(e = document.getElementById("i_pat")) e.innerText = at;
  if(e = document.getElementById("i_pcode")) e.innerHTML = meta;
}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"})[c];});}

/* ---- 运行时长：页面本地每秒自增，不依赖服务端轮询 ---- */
var upBase = null, upAt = 0, upTimer = null;
function fmtUptime(s){
  s = Math.max(0, Math.floor(s));
  var d = Math.floor(s/86400); s -= d*86400;
  var h = Math.floor(s/3600);  s -= h*3600;
  var m = Math.floor(s/60);    var c = s - m*60;
  if(d) return d+"天"+h+"小时"+m+"分"+c+"秒";
  if(h) return h+"小时"+m+"分"+c+"秒";
  return m+"分"+c+"秒";
}
function tickUptime(){
  if(upBase===null) return;
  setText("i_uptime", fmtUptime(upBase + (Date.now()-upAt)/1000));
}
function startUptimeTimer(){ if(!upTimer) upTimer = setInterval(tickUptime,1000); }

var lastStatsAt = 0;
var daySel = "";
var noteMap = {};
function noteOf(ip,host){
  return (ip&&noteMap[ip])||(host&&(noteMap[host]||noteMap[String(host).toLowerCase()]))||host||"—";
}

function loadNotes(){
  fetch("/api/notes", {cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
    if(!d || !d.ok) return;
    var old = JSON.stringify(noteMap);
    noteMap = d.notes || {};
    var hn = d.host_notes || {};
    for(var k in hn){ if(hn[k]) noteMap[k] = hn[k]; }
    if(JSON.stringify(noteMap) !== old){
      renderJobs();
      if(daySel) showDay(daySel);
    }
  }).catch(function(){});
}

function showDay(date){
  daySel = date;
  var cols = document.querySelectorAll("#chart .col");
  for(var i=0;i<cols.length;i++){
    cols[i].className = "col" + (cols[i].getAttribute("data-date") === date ? " on" : "");
  }
  var box = document.getElementById("daydetail");
  if(!box) return;
  box.style.display = "block";
  box.innerHTML = '<div class="ddempty">加载中…</div>';
  fetch("/api/day?date=" + encodeURIComponent(date), {cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
    if(!d.ok){ box.innerHTML = '<div class="ddempty">' + esc(d.msg || "读取失败") + '</div>'; return; }
    var rows = (d.sources || []).map(function(s){
      return '<div class="ddrow"><span class="ip">' + esc(s.ip) + '</span>'
           + '<span class="hn">' + esc(noteOf(s.ip, s.host)) + '</span>'
           + '<span class="cnt">' + s.n + ' 次</span>'
           + (s.fail ? '<span class="fl">失败 ' + s.fail + '</span>' : '')
           + '<span class="bt">' + fmtBytes(s.bytes) + '</span></div>';
    }).join("");
    box.innerHTML = '<div class="ddhead"><b>' + esc(d.date) + ' 打印明细</b>'
      + '<span class="ddsum">总计 <em>' + d.total + '</em> 次 · 成功 ' + (d.succ || 0) + ' · '
      + (d.fail ? '<span class="fl">失败 ' + d.fail + '</span>' : '失败 0') + ' · 数据 ' + fmtBytes(d.bytes) + '</span>'
      + '<button class="ddclose" onclick="closeDay()">收起</button></div>'
      + '<div class="ddlist">' + (rows || '<div class="ddempty">' + (d.total ? '当天没有来源记录' : '当天没有打印记录') + '</div>') + '</div>'
      + (d.fallback && d.total ? '<div class="ddempty" style="margin-top:8px">来源分布由最近 %JOB_KEEP% 条任务记录推算（分来源统计从本版本开始累计）</div>' : "");
  }).catch(function(){ box.innerHTML = '<div class="ddempty">读取失败</div>'; });
}

function closeDay(){
  daySel = "";
  var box = document.getElementById("daydetail");
  if(box){ box.style.display = "none"; box.innerHTML = ""; }
  var cols = document.querySelectorAll("#chart .col");
  for(var i=0;i<cols.length;i++) cols[i].className = "col";
}

function loadStats(force){
  var now = Date.now();
  if(!force && now - lastStatsAt < 10000) return;
  lastStatsAt = now;
  var days = document.getElementById("statDays").value || "14";
  fetch("/api/stats?days=" + days).then(function(r){return r.json();}).then(function(d){
    var max = Math.max(1, d.max || 1);
    var list = d.days || [];        // 服务端已按所选天数补齐占位，前端直接画
    var html = "";
    for(var i=0;i<list.length;i++){
      var it = list[i];
      if(it.empty){   // 占位日：只显示日期，没有柱子
        html += '<div class="col ph"><div class="bars"></div><div class="dt">' + it.date + '</div></div>';
        continue;
      }
      var ok = it.ok || 0, fail = it.fail || 0;
      var hOk = Math.round(ok / max * 130), hFail = Math.round(fail / max * 130);
      if(ok > 0 && hOk < 3) hOk = 3;
      if(fail > 0 && hFail < 3) hFail = 3;
      html += '<div class="col' + (it.date_full === daySel ? " on" : "") + '" data-date="' + it.date_full + '" title="' + it.date_full + ' ｜成功 ' + ok + ' ｜失败 ' + fail + ' ｜数据 ' + fmtBytes(it.bytes) + '">'
            + '<div class="bars">'
            + (ok ? '<div class="bar ok" style="height:' + hOk + 'px"></div>' : '')
            + (fail ? '<div class="bar fail" style="height:' + hFail + 'px"></div>' : '')
            + '</div><div class="dt">' + it.date + '</div></div>';
    }
    var box = document.getElementById("chart");
    if(box){
      box.innerHTML = html || '<div class="empty">暂无打印数据</div>';
      // 窄屏放不下时默认滚动到中间，让数据落在可视区中央
      if(box.scrollWidth > box.clientWidth){
        box.scrollLeft = Math.round((box.scrollWidth - box.clientWidth) / 2);
      }
      if(!box.__dayBound){
        box.__dayBound = true;
        box.addEventListener("click", function(e){
          var el = e.target;
          while(el && el !== box && !(el.className && String(el.className).indexOf("col") === 0)) el = el.parentNode;
          if(el && el !== box){
            var ds = el.getAttribute("data-date");
            if(ds) showDay(ds);
          }
        });
      }
    }
    var t = d.today || {};
    setText("statHint", "今日：成功 " + (t.ok || 0) + " · 失败 " + (t.fail || 0) + " · 数据 " + fmtBytes(t.bytes || 0));
  }).catch(function(){});
}

var JOB_PAGE_SIZES = [5, 10, 20, 30, 50, 100];
var jobPageSize = 5, jobCache = [], jobPageNo = 0;

try{
  var _sz = parseInt(localStorage.getItem("job_page_size"), 10);
  if(JOB_PAGE_SIZES.indexOf(_sz) >= 0) jobPageSize = _sz;
}catch(e){}

function setJobPageSize(v){
  var n = parseInt(v, 10);
  if(JOB_PAGE_SIZES.indexOf(n) < 0) n = 5;
  jobPageSize = n;
  jobPageNo = 0;
  try{ localStorage.setItem("job_page_size", String(n)); }catch(e){}
  renderJobs();
}

function initJobSize(){
  var sel = document.getElementById("jobsize");
  if(sel) sel.value = String(jobPageSize);
}

function renderJobs(){
  var body = document.getElementById("jobbody");
  if(!body) return;
  var total = jobCache.length;          // 本地只留最近 300 条，翻页也按这份算
  var trueTotal = jobTotal || total;    // 页面信息里显示服务端的真实总数
  var pages = Math.max(1, Math.ceil(total / jobPageSize));
  if(jobPageNo > pages - 1) jobPageNo = pages - 1;
  if(jobPageNo < 0) jobPageNo = 0;
  if(!total){
    body.innerHTML = '<tr><td colspan="6" class="empty">暂无任务记录</td></tr>';
  } else {
    var html = "", start = jobPageNo * jobPageSize, end = Math.min(start + jobPageSize, total);
    for(var i = start; i < end; i++){
      var j = jobCache[i];
      html += "<tr><td>" + esc(j.time) + "</td><td>" + esc(j.ip) + "</td><td>" + esc(noteOf(j.ip, j.host)) + "</td><td>" + fmtBytes(j.size) + "</td><td>" + (j.seconds || 0) + "s</td><td class='" + ((j.ok === true) ? "ok" : ((j.ok === false) ? "bad" : "")) + "'>" + esc(j.ok === true ? "成功" : (j.ok === false ? ("失败：" + (j.msg || "")) : "进行中")) + "</td></tr>";
    }
    body.innerHTML = html;
  }
  var info = document.getElementById("jobpageinfo");
  if(info) info.innerText = "第 " + (jobPageNo + 1) + " / " + pages + " 页 · 共 " + trueTotal + " 条"
    + (trueTotal > total ? "（本页只留最近 " + total + " 条）" : "");
  var prev = document.getElementById("jobprev"), next = document.getElementById("jobnext");
  if(prev) prev.disabled = jobPageNo <= 0;
  if(next) next.disabled = jobPageNo >= pages - 1;
}

function jobPage(delta){
  jobPageNo += delta;
  renderJobs();
}

function refresh(){
  fetch("/api/status").then(function(r){return r.json();}).then(function(d){
    setText("s_usb", d.usb_online ? "已连接" : "未连接");
    var ud = document.getElementById("s_usbdot");
    if(ud) ud.className = "svcdot " + (d.usb_online ? "ok" : "bad");
    setText("i_connmeta", d.usb_online ? ("USB 直连 · 共享端口 9100 · " + (d.printer_name || "已识别设备")) : "USB 直连 · 共享端口 9100 · 等待设备接入");
    setText("s_port", d.port_listening ? "监听中" : "未启动");
    var pd = document.getElementById("s_portdot");
    if(pd) pd.className = "svcdot " + (d.port_listening ? "ok" : "bad");
    setText("s_jobs", d.jobs);
    setText("s_bytes", fmtBytes(d.bytes));
    setText("s_src", d.source_count);
    setText("i_name", d.printer_name || (d.printer_id ? "--" : "未配置"));
    setText("i_bus", d.usb_online ? (d.usb_bus + " / " + d.usb_addr) : "--");
    setText("i_speed", d.usb_speed || "--");
    if(d.uptime_sec!=null){
      // 只在初始或明显跑偏时才校准，避免每 3 秒被服务端整数秒拉回去造成跳动
      var est = (upBase===null) ? null : upBase + (Date.now()-upAt)/1000;
      if(est===null || Math.abs(est - d.uptime_sec) > 2){ upBase = d.uptime_sec; upAt = Date.now(); }
      tickUptime();
    }
    else setText("i_uptime", d.uptime);
    setText("i_clients", d.active_clients);
    setText("i_reconnects", d.reconnects);
    setText("i_lasttime", d.last_job_time || "--");
    setText("i_lastsize", d.last_job_size ? fmtBytes(d.last_job_size) : "--");
    setText("devid", d.printer_id || "未配置");
    paintPstatus(d.printer_status);

    var pill=document.getElementById("svcconn"), pilltext=document.getElementById("svc_main"), svcdot=document.getElementById("svc_dot");
    if(d.usb_online && d.port_listening){pill.className="svcstate ok";svcdot.className="svcdot ok";pilltext.innerText="运行正常";}
    else if(d.usb_online){pill.className="svcstate warn";svcdot.className="svcdot warn";pilltext.innerText="打印机就绪，服务异常";}
    else if(!d.printer_id){pill.className="svcstate bad";svcdot.className="svcdot bad";pilltext.innerText="未配置打印机";}
    else {pill.className="svcstate bad";svcdot.className="svcdot bad";pilltext.innerText="打印机未连接";}
  }).catch(function(){});

  loadLog();
  loadStats();
  loadJobs();
}

// 打印记录单独轮询（8 秒一次，且内容没变就不重绘）：任务多时每 3 秒拉全量很吃带宽和 CPU
var jobSig = "", jobTotal = 0;
function loadJobs(){
  if(document.hidden) return;
  fetch("/api/jobs?limit=300").then(function(r){return r.json();}).then(function(d){
    var arr = (d.jobs || []).slice().reverse();
    var head = arr[0] || {};
    var sig = (d.total || 0) + "|" + (head.time || "") + "|" + (head.ok === null ? "run" : head.ok) + "|" + (head.size || 0);
    if(sig === jobSig) return;
    jobSig = sig;
    jobTotal = d.total || arr.length;
    jobCache = arr;
    renderJobs();
  }).catch(function(){});
}

var logFrom = 0, logLoaded = false, logLines = 0;
var LOG_MAX_LINES = 400;   // 每行一个节点，只保留最近 400 行：日志越多也越卡的老问题就没了

// 增量追加：不再每次把整块文本重写一遍（1593 行时那样写一次就要几百毫秒）
function logAppend(lines){
  var box = document.getElementById("logbox");
  if(!box || !lines.length) return;
  var stick = box.scrollTop + box.clientHeight + 40 >= box.scrollHeight;
  if(!logLines) box.innerHTML = "";
  var frag = document.createDocumentFragment();
  for(var i=0;i<lines.length;i++){
    var d = document.createElement("div");
    d.className = "logline";
    d.textContent = lines[i];
    frag.appendChild(d);
  }
  box.appendChild(frag);
  logLines += lines.length;
  while(logLines > LOG_MAX_LINES && box.firstElementChild){
    box.removeChild(box.firstElementChild);
    logLines--;
  }
  if(stick) box.scrollTop = box.scrollHeight;
}

function loadLog(){
  if(document.hidden) return;   // 页面在后台时不拉日志，回到前台会立刻补一次
  fetch("/api/log?since=" + logFrom, {cache:"no-store"}).then(function(r){
    var total = parseInt(r.headers.get("X-Log-Total") || "0", 10);
    if(isNaN(total)) total = 0;
    return r.text().then(function(txt){ return {txt: txt, total: total}; });
  }).then(function(res){
    var box = document.getElementById("logbox");
    if(!box) return;
    if(res.total < logFrom){          // 服务重启过，日志重新从第 0 行开始
      logFrom = 0; logLoaded = false; logLines = 0; box.innerHTML = "";
      return loadLog();
    }
    var txt = (res.txt || "").replace(/\\n+$/, "");
    var newLines = txt ? txt.split("\\n") : [];
    if(!logLoaded){
      logLoaded = true;
      if(newLines.length) logAppend(newLines.slice(-LOG_MAX_LINES));
      else{ box.innerText = "暂无日志"; logLines = 0; }
    }else if(newLines.length){
      logAppend(newLines);
    }
    logFrom = res.total;
    setText("loghint", "共 " + res.total + " 行 · 已存档到 data/panel.log");
  }).catch(function(){});
}

function reconnect(){
  fetch("/api/reconnect",{method:"POST"}).then(function(){refresh();});
}

function restartPanel(){
  if(!confirm("确定重启打印服务？约 2~3 秒内面板无法访问，正在打印的任务会中断。")) return;
  fetch("/api/restart",{method:"POST"}).then(function(){
    setTimeout(function(){ location.reload(); }, 4000);
  }).catch(function(){ setTimeout(function(){ location.reload(); }, 3000); });
}

function toggleAuto(){
  autoRefresh = !autoRefresh;
  document.getElementById("autobtn").innerText = "自动刷新：" + (autoRefresh ? "开" : "关");
  if(autoRefresh){startTimer();}else{stopTimer();}
}

function startTimer(){stopTimer();timer=setInterval(refresh,3000);}
function stopTimer(){if(timer){clearInterval(timer);timer=null;}}

// 日志单独按 3 秒轮询，跟“自动刷新”开关无关，日志永远实时
setInterval(loadLog, 3000);
document.addEventListener("visibilitychange", function(){ if(!document.hidden) loadLog(); });
// 页面从后台回到前台时补一次状态
document.addEventListener("visibilitychange", function(){ if(!document.hidden && autoRefresh) refresh(); });

function onStatDays(v){
  try{ localStorage.setItem("stat_days", String(v)); }catch(e){}
  loadStats(true);
}

// 手机窄屏默认近 7 天，PC 默认近 14 天；用户手动切换过就记住上次选择
function initStatDays(){
  var sel = document.getElementById("statDays");
  if(!sel) return;
  var saved = null;
  try{ saved = localStorage.getItem("stat_days"); }catch(e){}
  if(saved && ["7", "14", "30"].indexOf(String(saved)) >= 0){ sel.value = String(saved); return; }
  var narrow = window.matchMedia ? window.matchMedia("(max-width: 700px)").matches : (window.innerWidth <= 700);
  sel.value = narrow ? "7" : "14";
}

initStatDays();
initJobSize();
refresh();
loadJobs();
setInterval(loadJobs, 8000);
loadNotes();
setInterval(loadNotes, 30000);
startTimer();
startUptimeTimer();
</script>
%FOOTER%
</body>
</html>
"""

# ==================== 设置子页面（设备 / 推送 / 密码） ====================

SETTINGS_CSS = """
:root{--bg:#f2f5f9;--card:#fff;--line:#e3e8ef;--txt:#1b2530;--sub:#63707e;--brand:#0a84ff;--bad:#e5484d;--ok:#12a150;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--txt);padding:22px 16px 60px}
.wrap{max-width:820px;margin:0 auto}
.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.back{font-size:13px;color:var(--brand);text-decoration:none;white-space:nowrap;flex:none}
.back:hover{text-decoration:underline}
.nav{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;flex:1;min-width:0}
.nav a{text-align:center;font-size:13px;color:var(--sub);text-decoration:none;padding:7px 8px;border:1px solid var(--line);border-radius:9px;background:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav a.on{color:#fff;background:var(--brand);border-color:var(--brand);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 20px;box-shadow:0 8px 24px rgba(20,40,70,.06)}
h2{font-size:17px;font-weight:600;margin-bottom:6px}
.desc{font-size:12.5px;color:var(--sub);line-height:1.7;margin-bottom:16px}
.sethint{display:flex;align-items:flex-start;gap:10px;padding:12px 14px;border:1px solid #ffd995;background:#fffaf0;
 border-radius:12px;margin-bottom:14px;font-size:13px;color:#8a5a0a;line-height:1.6}
.sethint b{color:#7a4d00;font-weight:600}
.sethint a{color:#1f6feb;text-decoration:none}
.sethint a:hover{text-decoration:underline}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input,select{width:100%;padding:12px 13px;border:1px solid var(--line);border-radius:10px;font-size:14px;outline:none;background:#fbfcfd;color:var(--txt)}
input:focus,select:focus{border-color:var(--brand);background:#fff}
.inline{flex:1 1 220px;min-width:180px}
.item .inline{flex:1 1 100%;margin-top:10px}
.srow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
.srow .inline{flex:1 1 180px;margin-top:0}
.srcitem{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 12px}
.sinfo{flex:1 1 150px;min-width:130px}
.sinfo .sub{margin-top:2px;font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.snote{flex:0 0 132px;width:132px;padding:7px 9px;border:1px solid var(--line);border-radius:9px;background:#fbfcfd;font-size:12.5px;outline:none;color:var(--txt)}
.snote:focus{border-color:var(--brand);background:#fff}
.ldel{flex:none;width:30px;height:30px;padding:0;display:flex;align-items:center;justify-content:center;border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--sub);cursor:pointer}
.ldel:hover{border-color:var(--bad);color:var(--bad)}
.ldel svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
@media (max-width:560px){.srcitem{padding:8px 10px;gap:8px}.sinfo{flex:1 1 100%;display:flex;align-items:baseline;gap:8px;min-width:0}.sinfo .id{flex:none;font-size:13px}.sinfo .sub{margin-top:0;flex:1 1 auto;min-width:0}.snote{flex:1 1 auto;width:auto;height:30px;padding:5px 9px}.ldel{width:30px;height:30px}}
.btn{padding:12px 18px;border:none;border-radius:10px;background:var(--brand);color:#fff;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn.ghost{background:#fff;color:var(--brand);border:1px solid var(--brand)}
.btn:active{transform:scale(.98)}
.field{display:flex;flex-direction:column;gap:7px;margin-bottom:14px}
.field label{font-size:13px;color:var(--sub)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.chgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;background:#fbfcfd;border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.chk{display:flex;align-items:center;gap:8px;font-size:13.5px;cursor:pointer}
.chk input{width:17px;height:17px;padding:0;cursor:pointer}
.switchrow{display:flex;gap:22px;flex-wrap:wrap;margin:6px 0 4px}
.sec{margin-top:20px;border-top:1px solid var(--line);padding-top:16px}
.sech{font-size:13.5px;font-weight:600;margin-bottom:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.muted{font-size:12px;color:var(--sub);font-weight:400}
.list{display:flex;flex-direction:column;gap:10px}
.item{display:flex;align-items:center;justify-content:space-between;gap:12px;border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:#fcfdfe;flex-wrap:wrap}
.item.cur{border-color:var(--ok);background:#f3fbf6}
.info{flex:1 1 60%;min-width:200px}
.id{font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sub{font-size:12px;color:var(--sub);margin-top:5px;line-height:1.6}
.tag{font-size:11px;color:#0a6b3c;background:#e3f6ea;border-radius:6px;padding:2px 7px;font-weight:500}
.tag.curtag{color:#0a5fb4;background:#e6f0ff}
.pick{padding:9px 15px;border:1px solid var(--brand);border-radius:9px;background:#fff;color:var(--brand);font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.pick:hover{background:var(--brand);color:#fff}
.empty{font-size:13px;color:var(--sub);padding:16px;text-align:center;border:1px dashed var(--line);border-radius:10px}
.msg{font-size:13px;margin-top:14px;min-height:20px;line-height:1.6}
.msg.ok{color:var(--ok)}
.msg.bad{color:var(--bad)}
.btns{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:18px}
.note{font-size:12px;color:var(--sub);line-height:1.75;background:#f7fafc;border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin-top:18px}
.setlist{display:flex;flex-direction:column;gap:10px}
.setrow{display:flex;align-items:center;gap:13px;padding:14px 15px;border:1px solid var(--line);border-radius:13px;background:#fcfdfe;text-decoration:none;color:var(--txt);transition:background .15s,border-color .15s,transform .12s}
.setrow:hover{background:#f4f9ff;border-color:#cfe2ff;transform:translateX(2px)}
.setrow:active{transform:scale(.995)}
.setic{flex:none;width:36px;height:36px;border-radius:10px;background:#eef4fc;display:flex;align-items:center;justify-content:center;font-size:17px}
.setic svg{width:18px;height:18px;stroke:#1f6fd0;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.setinfo{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
.setinfo b{font-size:14.5px;font-weight:600}
.setinfo span{font-size:12px;color:var(--sub);line-height:1.6}
.setgo{flex:none;color:#b6c1cc;font-size:20px;line-height:1}
@media (max-width:560px){.bar{gap:8px}.nav{gap:6px}.nav a{padding:7px 4px;font-size:11.5px}input,select{font-size:16px}}
"""

SETTINGS_PATH_ROWS = [("设备设置", "/device"), ("来源管理", "/notes"), ("数据管理", "/data"), ("推送配置", "/push"), ("修改密码", "/password")]

SETTINGS_INDEX_ICON = {"设备设置": "printer", "来源管理": "tag", "数据管理": "chart", "推送配置": "bell", "修改密码": "key"}

SETTINGS_INDEX_DESC = {
    "设备设置": "选择要共享的 USB 打印机设备，保存后立即重连。",
    "来源管理": "查看每个来源的备注、最近一次打印时间与累计数据量，不需要的可直接删除。",
    "数据管理": "调整打印监控保留条数，清理累计任务与累计数据。",
    "推送配置": "配置 Pushplus 推送渠道与触发条件，可发一条测试推送。",
    "修改密码": "更换面板访问密码，改完旧会话会失效。",
}


def settings_index_body():
    rows = ""
    for label, href in SETTINGS_PATH_ROWS:
        rows += ('<a class="setrow" href="%s"><span class="setic">%s</span>'
                 '<span class="setinfo"><b>%s</b><span>%s</span></span>'
                 '<span class="setgo">›</span></a>') % (
                     href, ICONS.get(SETTINGS_INDEX_ICON.get(label, ""), ""), label,
                     SETTINGS_INDEX_DESC.get(label, ""))
    ip = primary_lan_ip()
    addr = ""
    if ip:
        addr = ('<div class="setrow"><span class="setic">%s</span>'
                '<span class="setinfo"><b>访问地址</b>'
                '<span>面板 http://%s:%d ｜ 打印端口 %s:%d</span></span></div>') % (
                    ICONS.get("printer", ""), ip, WEB_PORT, ip, PRINT_PORT)
    hint = ""
    if not PRINTER_ID:
        hint = ('<div class="sethint"><span class="setic">%s</span>'
                '<span><b>打印机未配置</b><br>'
                '还没设置打印机 ID，当前不会连接任何设备，收到打印任务会直接失败。'
                '去<a href="/device">设备设置</a>页点“扫描 USB 设备”选中后保存即可。</span></div>') % (
                    ICONS.get("printer", ""))
    return ('<div class="desc">面板的所有配置都在这里，点进去按需修改。</div>%s'
            '<div class="setlist">%s</div>%s') % (hint, rows, addr)


def settings_page(title, active, body, script=""):
    nav = ""
    if active:
        links = ""
        for label, href in SETTINGS_PATH_ROWS:
            cls = ' class="on"' if label == active else ""
            links += '<a%s href="%s">%s</a>' % (cls, href, label)
        nav = '<div class="nav">%s</div>' % links
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/logo.svg">
<title>%TITLE% · USB 打印服务面板</title>
<style>%STYLE%</style>
</head>
<body>
<div class="wrap">
  <div class="bar">
    <a class="back" href="/">← 返回面板</a>
    %NAV%
  </div>
  <div class="card">
    <h2>%TITLE%</h2>
    %BODY%
  </div>
</div>
<script>%SCRIPT%</script>
%FOOTER%
</body>
</html>
"""
    return (html.replace("%STYLE%", SETTINGS_CSS + FOOTER_CSS)
                .replace("%NAV%", nav)
                .replace("%TITLE%", title)
                .replace("%BODY%", body)
                .replace("%SCRIPT%", script)
                .replace("%FOOTER%", footer_html()))


DEVICE_BODY = """
<div class="desc">当前生效设备：<b>%CURTEXT%</b>。改完保存会立即按新设备重新连接，无需重启容器。</div>
<div class="row">
  <input class="inline" id="d_id" placeholder="扫描自动填入" value="%CUR%">
  <button class="btn" onclick="scan()">扫描 USB 设备</button>
  <button class="btn ghost" onclick="save()">保存设备 ID</button>
</div>
<div class="sec">
  <div class="sech">扫描结果 <span class="muted" id="scanInfo">点“扫描 USB 设备”开始</span></div>
  <div class="list" id="list"><div class="empty">尚未扫描</div></div>
</div>
<div class="msg" id="msg"></div>
<div class="note">支持写法：1234:5678、1234-5678、0x1234:0x5678、12345678（这里只是格式示例，实际填扫描出来的那台）。扫描只能看到已通电且被容器识别到的设备；插在 USB HUB 上也能扫到，但个别 HUB 供电不足会导致打印中断，建议直插 NAS。</div>
"""

DEVICE_SCRIPT = """
function setMsg(t,ok){
  var e=document.getElementById("msg");
  e.innerText=t||"";
  e.className="msg "+(ok?"ok":"bad");
}
function pick(id){
  document.getElementById("d_id").value=id;
  setMsg("已选择 "+id+"，点“保存设备 ID”生效",true);
}
function scan(){
  setMsg("正在扫描 USB 总线...",true);
  document.getElementById("scanInfo").innerText="扫描中...";
  fetch("/api/usb/scan").then(function(r){return r.json();}).then(function(d){
    if(!d.ok){
      setMsg(d.msg||"扫描失败",false);
      document.getElementById("scanInfo").innerText="扫描失败";
      document.getElementById("list").innerHTML='<div class="empty">'+(d.msg||"扫描失败")+'</div>';
      return;
    }
    var list=d.devices||[];
    document.getElementById("scanInfo").innerText="共 "+list.length+" 个设备，当前使用 "+d.current;
    if(!list.length){
      document.getElementById("list").innerHTML='<div class="empty">没扫到 USB 设备：确认打印机已开机、USB 线接在 NAS 上、容器有 /dev/bus/usb 权限</div>';
      setMsg("未发现 USB 设备",false);
      return;
    }
    var h="";
    for(var i=0;i<list.length;i++){
      var it=list[i];
      h+='<div class="item'+(it.current?' cur':'')+'" data-id="'+it.id+'">'
       + '<div class="info"><div class="id">'+it.id
       + (it.printer?'<span class="tag">打印接口</span>':'')
       + (it.current?'<span class="tag curtag">当前使用</span>':'')
       + '</div><div class="sub">'
       + (it.product||"未知设备")
       + (it.vendor?(" · "+it.vendor):"")
       + ' · Bus '+(it.bus||"?")+' Addr '+(it.address||"?")
       + ' · 接口类 '+((it.iface_class&&it.iface_class.length)?it.iface_class.join("/"):"?")
       + '</div></div>'
       + '<button class="pick">用这个</button></div>';
    }
    document.getElementById("list").innerHTML=h;
    Array.prototype.forEach.call(document.querySelectorAll(".pick"),function(b){
      b.onclick=function(){
        var box=this.parentNode;
        pick(box.getAttribute("data-id"));
      };
    });
    setMsg("扫描完成，点“用这个”自动填入",true);
  }).catch(function(e){ setMsg("扫描请求失败："+e,false); });
}
function save(){
  var id=document.getElementById("d_id").value.trim();
  if(!id){ setMsg("先填设备 ID，或点扫描后选择",false); return; }
  fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({device:{id:id}})})
    .then(function(r){return r.json();}).then(function(d){
      setMsg(d.msg||"", d.ok);
      if(!d.ok) alert(d.msg||"保存失败");
    });
}
"""

NOTE_MAX_LEN = 30

NOTES_BODY = """
<div class="desc">管理所有打印来源：起个备注名、查看每台机器最近一次发任务的时间和累计数据量，不需要的来源可以直接删掉。打印监控的“来源主机”列与统计图当天明细会优先显示备注。</div>
<div class="list" id="list"><div class="empty">加载中...</div></div>
<div class="btns">
  <button class="btn" onclick="saveNotes()">保存备注</button>
  <button class="btn ghost" onclick="loadNotes()">重新读取</button>
</div>
<div class="msg" id="msg"></div>
<div class="note">来源 IP 由统计数据和打印记录自动汇总，没打印过的机器不会出现在这里。「移除来源」只把来源从本列表移除，历史打印记录与统计仍然保留；清空备注框只是不再显示备注，不会删掉来源。备注最多 %NOTE_MAX% 个字，只保存在本机 config.json 内，不会外发。</div>
"""

NOTES_SCRIPT = """
var LIMIT = %NOTE_MAX%;
var TRASH = '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>';
function setMsg(t,ok){
  var e=document.getElementById("msg");
  e.innerText=t||"";
  e.className="msg "+(ok?"ok":"bad");
}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"})[c];});}
function fmtSize(n){
  n=Number(n||0);
  if(n<1024) return n+" B";
  if(n<1048576) return (n/1024).toFixed(1)+" KB";
  if(n<1073741824) return (n/1048576).toFixed(1)+" MB";
  return (n/1073741824).toFixed(2)+" GB";
}
function loadNotes(){
  setMsg("读取中...",true);
  var ac = (typeof AbortController!=="undefined") ? new AbortController() : null;
  var tid = setTimeout(function(){ if(ac) ac.abort(); }, 12000);
  var opt = {cache:"no-store"};
  if(ac) opt.signal = ac.signal;
  fetch("/api/notes",opt).then(function(r){return r.json();}).then(function(d){
    clearTimeout(tid);
    if(!d.ok){ setMsg(d.msg||"读取失败",false); return; }
    var items=d.items||[];
    if(!items.length){
      document.getElementById("list").innerHTML='<div class="empty">还没有打印记录：先让一台机器打印一次，再回来管理来源</div>';
      setMsg("暂无来源",true);
      return;
    }
    var h="";
    for(var i=0;i<items.length;i++){
      var it=items[i];
      var sub="最近 "+(it.last?esc(String(it.last).slice(5,16)):"—")
        +" · "+(it.n||0)+" 个 · "+fmtSize(it.bytes||0);
      var tip="";
      if(it.last&&it.last_size!=null) tip="最近一次文件 "+fmtSize(it.last_size);
      if(it.ips&&it.ips.length>1){
        sub+=" · "+it.ips.length+" 个 IP";
        tip+=(tip?"；":"")+"曾用 IP："+it.ips.join("、");
      }
      h+='<div class="item srcitem"><div class="sinfo" title="'+esc(tip)+'"><div class="id">'+esc(it.ip)
       + (it.host?'<span class="tag">'+esc(it.host)+'</span>':'')
       + '</div><div class="sub">'+sub+'</div></div>'
       + '<input class="snote" data-key="'+esc(it.key)+'" data-host="'+esc(it.host||"")+'" maxlength="'+LIMIT+'" value="'+esc(it.note || it.host || "")+'" title="备注">'
       + '<button class="ldel" data-key="'+esc(it.key)+'" title="移除来源" aria-label="移除来源">'+TRASH+'</button></div>';
    }
    document.getElementById("list").innerHTML=h;
    Array.prototype.forEach.call(document.querySelectorAll("button.ldel"),function(b){
      b.onclick=function(){ delSrc(this.getAttribute("data-key")); };
    });
    setMsg("共 "+items.length+" 个来源",true);
  }).catch(function(e){
    clearTimeout(tid);
    var t=(e&&e.name==="AbortError")?"读取超时，点「重新读取」再试":"读取失败，点「重新读取」再试";
    setMsg(t,false);
    var box=document.getElementById("list");
    if(box&&!box.querySelector("input[data-key]")) box.innerHTML='<div class="empty">'+t+'</div>';
  });
}
function delSrc(key){
  if(!confirm("「移除来源」会把 "+key+" 从来源列表里去掉，不是清空备注。\\n历史打印记录与统计数据仍会保留，确定继续？")) return;
  fetch("/api/notes/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:key})})
    .then(function(r){return r.json();}).then(function(d){
      setMsg(d.msg||"",d.ok);
      if(d.ok) loadNotes();
    }).catch(function(){ setMsg("删除失败",false); });
}
function saveNotes(){
  var notes={}, n=0;
  Array.prototype.forEach.call(document.querySelectorAll("input[data-key]"),function(el){
    var v=el.value.trim();
    if(v && v !== el.getAttribute("data-host")){ notes[el.getAttribute("data-key")]=v; n++; }
  });
  fetch("/api/notes",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({notes:notes})})
    .then(function(r){return r.json();}).then(function(d){
      setMsg(d.msg||"", d.ok);
      if(!d.ok) alert(d.msg||"保存失败");
      else loadNotes();
    }).catch(function(){ setMsg("保存失败",false); });
}
loadNotes();
"""

DATA_BODY = """
<div class="desc">累计任务与累计数据保存在 data/totals.json，容器重建、镜像更新、重启后都会读回来接着累计。每日统计单独存在 data/stats.json，保留最近 180 天。</div>
<div class="list" id="sum"><div class="empty">加载中...</div></div>
<div class="sec">
  <div class="sech">打印监控保留条数 <span class="muted">100 ~ 5000 条，默认 1000</span></div>
  <div class="row">
    <input class="inline" id="k_jobs" type="number" min="100" max="5000" step="100" style="flex:0 0 170px;min-width:140px">
    <button class="btn" onclick="saveKeep()">保存条数</button>
  </div>
  <div class="muted" id="kinfo" style="margin-top:10px"></div>
</div>
<div class="btns">
  <button class="btn" onclick="clearTotals()">清零累计任务与数据</button>
  <button class="btn ghost" onclick="clearAll()">清除打印记录与每日统计</button>
</div>
<div class="msg" id="msg"></div>
<div class="note">「清零」只把面板上的累计任务、累计数据归零，并把起始时间改成当前时间；打印监控记录与每日统计不受影响。「清除打印记录」会连同任务记录、每日统计一起删掉，不可恢复。</div>
"""

DATA_SCRIPT = """
function setMsg(t,ok){
  var e=document.getElementById("msg");
  e.innerText=t||"";
  e.className="msg "+(ok?"ok":"bad");
}
function fmtBytes(n){
  if(!n) return "0 B";
  var u=["B","KB","MB","GB"],i=0,v=n;
  while(v>=1024 && i<u.length-1){v=v/1024;i++;}
  return (i===0? v : v.toFixed(1))+" "+u[i];
}
function load(){
  fetch("/api/totals",{cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
    if(!d.ok){ setMsg(d.msg||"读取失败",false); return; }
    document.getElementById("sum").innerHTML =
      '<div class="item"><div class="info"><div class="id">累计任务</div>'
      + '<div class="sub">' + (d.since ? ("自 "+d.since+" 起统计") : "尚无清零记录") + '</div></div>'
      + '<div class="id">' + (d.jobs||0) + ' 次</div></div>'
      + '<div class="item"><div class="info"><div class="id">累计数据</div>'
      + '<div class="sub">只统计成功任务的数据量</div></div>'
      + '<div class="id">' + fmtBytes(d.bytes||0) + '</div></div>';
    setMsg("",true);
    var k = d.keep_jobs || 1000;
    var box = document.getElementById("k_jobs");
    if(box) box.value = k;
    var info = document.getElementById("kinfo");
    if(info) info.innerText = "当前保留 " + k + " 条 · 已存 " + (d.job_count||0) + " 条 · jobs.json " + fmtBytes(d.jobs_bytes||0)
      + " · 可设范围 " + (d.keep_min||100) + " ~ " + (d.keep_max||5000);
  }).catch(function(){ setMsg("读取失败",false); });
}
function saveKeep(){
  var v = parseInt(document.getElementById("k_jobs").value, 10);
  if(!v || v < 100 || v > 5000){ setMsg("保留条数需在 100 ~ 5000 之间", false); return; }
  fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({jobs_keep:v})})
    .then(function(r){return r.json();}).then(function(d){
      setMsg(d.msg||"", d.ok);
      if(!d.ok) alert(d.msg||"保存失败");
      load();
    }).catch(function(){ setMsg("保存失败",false); });
}
function clearTotals(){
  if(!confirm("确定把累计任务与累计数据清零吗？打印记录和每日统计不受影响")) return;
  fetch("/api/totals/clear",{method:"POST"}).then(function(r){return r.json();}).then(function(d){
    setMsg(d.msg||"", d.ok);
    load();
  }).catch(function(){ setMsg("清零失败",false); });
}
function clearAll(){
  if(!confirm("确定清除全部打印记录和每日统计吗？此操作不可恢复")) return;
  fetch("/api/jobs/clear",{method:"POST"}).then(function(r){return r.json();}).then(function(d){
    setMsg(d.msg||"", d.ok);
    load();
  }).catch(function(){ setMsg("清除失败",false); });
}
load();
"""

PUSH_BODY = """
<div class="desc">保存后立即生效。多个渠道勾选后会走批量接口，一次推送到全部渠道；离线/恢复/任务失败通知都按这里的设置发送。</div><div class="grid">
  <div class="field"><label>Pushplus Token</label><input id="p_token" placeholder="pushplus 个人中心里的 token"></div>
  <div class="field"><label>发送模板</label>
    <select id="p_template">
      <option value="auto">自动（文本类渠道用纯文本）</option>
      <option value="html">HTML（富文本）</option>
      <option value="txt">纯文本</option>
      <option value="markdown">Markdown</option>
      <option value="json">JSON</option>
    </select>
  </div>
</div>
<div class="field"><label>推送渠道（可多选）</label></div>
<div class="chgrid">
  <label class="chk"><input type="checkbox" value="wechat"> 微信公众号</label>
  <label class="chk"><input type="checkbox" value="app"> APP</label>
  <label class="chk"><input type="checkbox" value="extension"> 浏览器插件/桌面端</label>
  <label class="chk"><input type="checkbox" value="webhook"> 第三方 Webhook</label>
  <label class="chk"><input type="checkbox" value="clawbot"> 微信 ClawBot</label>
  <label class="chk"><input type="checkbox" value="cmcc"> 新消息 ClawBot(移动)</label>
  <label class="chk"><input type="checkbox" value="qq"> QQ 机器人</label>
  <label class="chk"><input type="checkbox" value="cp"> 企业微信应用</label>
  <label class="chk"><input type="checkbox" value="mail"> 邮箱</label>
  <label class="chk"><input type="checkbox" value="sms"> 短信(收费)</label>
  <label class="chk"><input type="checkbox" value="voice"> 语音(收费)</label>
</div>
<div class="grid">
  <div class="field"><label>渠道配置编码 option（webhook/企业微信/邮箱/QQ 必填）</label><input id="p_option" placeholder="个人中心渠道设置里的编码"></div>
  <div class="field"><label>接收对象 to（好友令牌/企业微信用户ID，可空）</label><input id="p_to" placeholder="可留空"></div>
  <div class="field"><label>群组编码 topic（一对一留空）</label><input id="p_topic" placeholder="可留空"></div>
  <div class="field"><label>离线多少秒后推送</label><input id="p_offline" type="number" min="5" max="86400" step="5"></div>
</div>
<div class="switchrow">
  <label class="chk"><input id="p_enable" type="checkbox"> 启用推送</label>
  <label class="chk"><input id="p_jobfail" type="checkbox"> 打印任务失败也推送</label>
</div>
<div class="btns">
  <button class="btn" onclick="savePush()">保存推送配置</button>
  <button class="btn ghost" onclick="loadConf()">重新载入</button>
  <button class="btn ghost" onclick="pushtest()">发一条测试推送</button>
</div>
<div class="msg" id="msg"></div>
<div class="note">webhook / 企业微信应用 / QQ 机器人 / 邮箱 需要先到 Pushplus 个人中心「渠道设置」里建好配置拿到编码，填到 option 里，否则会报渠道未配置。短信、语音是收费渠道，按积分扣费。</div>
"""

PUSH_SCRIPT = """
function setMsg(t,ok){
  var e=document.getElementById("msg");
  e.innerText=t||"";
  e.className="msg "+(ok===false?"bad":"ok");
}
function chBoxes(){
  return Array.prototype.slice.call(document.querySelectorAll(".chgrid input[type=checkbox]"));
}
function loadConf(){
  fetch("/api/config").then(function(r){return r.json();}).then(function(d){
    var p=d.push||{};
    document.getElementById("p_token").value=p.token||"";
    document.getElementById("p_template").value=p.template||"auto";
    document.getElementById("p_option").value=p.option||"";
    document.getElementById("p_to").value=p.to||"";
    document.getElementById("p_topic").value=p.topic||"";
    document.getElementById("p_offline").value=p.offline_alert_sec||60;
    document.getElementById("p_enable").checked=!!p.enable;
    document.getElementById("p_jobfail").checked=!!p.job_fail_push;
    var picked=(p.channels&&p.channels.length)?p.channels:[(p.channel||"wechat")];
    chBoxes().forEach(function(cb){cb.checked=picked.indexOf(cb.value)>=0;});
    setMsg("已载入当前配置",true);
  }).catch(function(){});
}
function savePush(){
  var picked=chBoxes().filter(function(cb){return cb.checked;}).map(function(cb){return cb.value;});
  if(!picked.length){ setMsg("请至少选择一个推送渠道",false); return; }
  var body={push:{
    token:document.getElementById("p_token").value.trim(),
    channel:picked,
    template:document.getElementById("p_template").value,
    option:document.getElementById("p_option").value.trim(),
    to:document.getElementById("p_to").value.trim(),
    topic:document.getElementById("p_topic").value.trim(),
    offline_alert_sec:parseInt(document.getElementById("p_offline").value||"60",10),
    enable:document.getElementById("p_enable").checked,
    job_fail_push:document.getElementById("p_jobfail").checked
  }};
  fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
    .then(function(r){return r.json();}).then(function(d){
      setMsg(d.msg||"", d.ok);
      if(!d.ok) alert(d.msg||"保存失败");
    });
}
function pushtest(){
  setMsg("正在发送测试推送...",true);
  fetch("/api/push_test",{method:"POST"}).then(function(r){return r.json();}).then(function(d){
    setMsg(d.msg||(d.ok?"已发送":"发送失败"), d.ok);
  }).catch(function(){ setMsg("请求失败",false); });
}
loadConf();
"""

PASSWORD_BODY = """
<div class="desc">面板只有一个密码，没有用户名。密码只保存 PBKDF2 加盐哈希，不会出现在环境变量、配置文件明文或日志里。</div>
<div class="grid">
  <div class="field"><label>当前密码</label><input id="a_old" type="password" autocomplete="current-password"></div>
  <div class="field"><label>新密码（至少 8 位，不能纯数字）</label><input id="a_new" type="password" autocomplete="new-password"></div>
  <div class="field"><label>再输一次新密码</label><input id="a_new2" type="password" autocomplete="new-password"></div>
</div>
<div class="btns">
  <button class="btn" onclick="saveAuth()">保存新密码</button>
</div>
<div class="msg" id="msg"></div>
<div class="note">改密成功后所有已登录的设备（包括其他浏览器、手机）都会立即失效，需要用新密码重新登录。忘记密码只能删掉 data/config.json 重启后重新首次设置。</div>
"""

PASSWORD_SCRIPT = """
function setMsg(t,ok){
  var e=document.getElementById("msg");
  e.innerText=t||"";
  e.className="msg "+(ok===false?"bad":"ok");
}
function saveAuth(){
  var oldp=document.getElementById("a_old").value;
  var newp=document.getElementById("a_new").value;
  var newp2=document.getElementById("a_new2").value;
  if(!oldp){ setMsg("请先输入当前密码",false); return; }
  if(!newp){ setMsg("请输入新密码",false); return; }
  if(newp!==newp2){ setMsg("两次输入的新密码不一致",false); return; }
  fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({auth:{old_password:oldp,new_password:newp}})})
    .then(function(r){return r.json();}).then(function(d){
      setMsg(d.msg||"", d.ok);
      if(d.ok){
        document.getElementById("a_old").value="";
        document.getElementById("a_new").value="";
        document.getElementById("a_new2").value="";
      }
    });
}
"""


app = Flask(__name__)
app.secret_key = CFG.get("secret_key") or os.urandom(24).hex()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_NAME"] = "p910panel"
app.permanent_session_lifetime = datetime.timedelta(hours=SESSION_HOURS)


@app.after_request
def no_cache_api(resp):
    """所有 /api 响应都不允许缓存，否则轮询会读到旧数据（日志尤其明显）"""
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


def login_page(err=""):
    block = ('<div class="err">%s</div>' % err) if err else ""
    return (LOGIN_HTML.replace("%STYLE%", AUTH_STYLE + AUTH_FOOTER_CSS)
                      .replace("%ERR%", block)
                      .replace("%FOOTER%", footer_html()))


def setup_page(err=""):
    block = ('<div class="err">%s</div>' % err) if err else ""
    return (SETUP_HTML.replace("%STYLE%", AUTH_STYLE + AUTH_FOOTER_CSS)
                      .replace("%ERR%", block)
                      .replace("%FOOTER%", footer_html()))


def client_ip():
    return request.remote_addr or "?"


@app.before_request
def require_login():
    path = request.path
    if path in ("/favicon.ico", "/logo.svg", "/robots.txt", "/license", "/healthz"):
        return None

    ready = auth_ready()
    if not ready:
        # 还没设密码：所有入口都引导到首次设置页，避免面板裸奔
        if path == "/setup":
            return None
        if path == "/login":
            return redirect("/setup")
        if path.startswith("/api/"):
            return jsonify({"ok": False, "msg": "尚未设置访问密码"}), 401
        return redirect("/setup")

    if path in ("/login", "/setup"):
        return None
    if session.get("ok") == 1:
        return None
    if path.startswith("/api/"):
        return jsonify({"ok": False, "msg": "未登录"}), 401
    return redirect("/login")


@app.route("/healthz")
def healthz():
    """容器健康检查用：不登录、不读 USB，只证明面板进程还活着。"""
    return jsonify({"ok": True, "uptime": int(time.time() - START_TS)})


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if auth_ready():
        return redirect("/login")
    err = ""
    if request.method == "POST":
        pwd = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        prob = pwd_problem(pwd)
        if prob:
            err = prob
        elif pwd != confirm:
            err = "两次输入的密码不一致"
        else:
            set_password(pwd)
            session.clear()
            session["ok"] = 1
            session.permanent = True
            log("首次设置访问密码完成（来源 %s）" % client_ip())
            return redirect("/")
    return Response(setup_page(err), mimetype="text/html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_ready():
        return redirect("/setup")
    err = ""
    ip = client_ip()
    if request.method == "POST":
        wait = login_wait(ip)
        if wait:
            err = "尝试次数过多，请 %d 秒后再试" % wait
        else:
            pwd = request.form.get("password", "")
            if check_pwd(pwd):
                login_ok(ip)
                session.clear()
                session["ok"] = 1
                session.permanent = True
                log("登录成功（来源 %s）" % ip)
                return redirect("/")
            login_fail(ip)
            log("登录失败（来源 %s）" % ip)
            err = "密码错误"
    return Response(login_page(err), mimetype="text/html")


@app.route("/logout")
def logout():
    was = session.get("ok") == 1
    session.clear()
    if was:
        log("已退出登录（来源 %s）" % client_ip())
    return redirect("/login")


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.route("/license")
def page_license():
    year = time.strftime("%Y")
    text = LICENSE_TEXT
    if year != "2026":
        text = text.replace("Copyright © 2026", "Copyright © 2026-%s" % year, 1)
    text = (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/logo.svg">
<title>使用许可与声明 · USB 打印服务面板</title>
<style>
:root{--bg:#f2f5f9;--card:#fff;--line:#e3e8ef;--txt:#1b2530;--sub:#63707e;--brand:#0a84ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--txt);padding:22px 16px}
.wrap{max-width:760px;margin:0 auto}
.top{margin-bottom:14px}
.top a{font-size:13px;color:var(--brand);text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px 22px;box-shadow:0 8px 24px rgba(20,40,70,.06)}
h1{font-size:17px;font-weight:600;margin-bottom:14px}
pre{white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:13px;line-height:1.85;color:var(--txt)}
.foot{text-align:center;font-size:12px;color:var(--sub);margin:16px 0 4px;line-height:1.8}
.foot a{color:var(--brand);text-decoration:none}
.foot a:hover{text-decoration:underline}
.foot2{font-size:11.5px;color:#98a3b1;margin-top:5px;line-height:1.7}
.foot2 a{color:#98a3b1;text-decoration:underline}
</style>
</head>
<body>
<div class="wrap">
  <div class="top"><a href="/">← 返回面板</a></div>
  <div class="card">
    <h1>使用许可与声明</h1>
    <pre>%TEXT%</pre>
  </div>
  %FOOTER%
</div>
</body>
</html>
"""
    return Response(html.replace("%TEXT%", text)
                        .replace("%FOOTER%", footer_html(with_license=False)),
                    mimetype="text/html")


# ==================== 项目 logo（godr 芽苗图形标） ====================

LOGO_SVG = '''<svg version="1.1" xmlns="http://www.w3.org/2000/svg" viewBox="214 213 1109 1109"> <path d="M0 0 C0.782 0.05 1.564 0.099 2.37 0.151 C12.906 0.905 23.389 2.462 33.789 4.292 C41.567 5.654 49.363 6.554 57.21 7.386 C126.527 14.818 205.945 32.481 252.219 89.656 C269.256 111.225 284.627 136.571 289.457 163.992 C290.062 166.812 290.062 166.812 291.036 168.703 C292.904 172.541 293.374 176.763 294.125 180.938 C294.214 181.424 294.214 181.424 294.664 183.889 C295.879 190.896 296.58 197.803 296.735 204.913 C296.939 212.617 298.418 217.658 303.562 223.438 C304.473 224.487 305.382 225.537 306.289 226.59 C306.752 227.123 307.215 227.657 307.692 228.207 C324.286 247.657 338.234 269.754 351.062 291.812 C351.453 292.478 351.843 293.143 352.245 293.828 C372.915 329.258 386.326 368.4 397.062 407.812 C399.122 405.753 399.702 404.543 400.727 401.875 C400.883 401.473 400.883 401.473 401.676 399.44 C402.01 398.573 402.344 397.706 402.688 396.812 C408.441 382.171 414.794 367.88 421.994 353.892 C422.721 352.477 423.444 351.059 424.162 349.64 C433.263 331.665 443.689 314.439 455.062 297.812 C456.418 295.772 457.773 293.73 459.125 291.688 C462.907 285.977 462.907 285.977 464.829 283.943 C471.573 276.144 471.037 267.908 471.101 258.108 C471.249 240.568 472.675 224.591 478.062 207.812 C478.322 206.948 478.581 206.084 478.848 205.193 C493.511 156.385 529.561 119.197 573.522 95.219 C585.562 88.79 597.658 83.801 610.688 79.75 C611.705 79.433 612.723 79.117 613.771 78.791 C630.2 73.82 646.829 70.145 663.689 66.99 C721.88 56.044 721.88 56.044 743.991 42.831 C747.651 41.032 750.036 41.141 754.062 41.812 C757 44.25 757 44.25 759.062 46.812 C759.392 47.142 759.722 47.472 760.062 47.812 C761.235 64.011 758.576 79.053 755.062 94.812 C754.713 96.446 754.364 98.081 754.016 99.715 C752.267 107.703 750.01 115.438 747.309 123.152 C746.374 125.898 745.54 128.617 744.746 131.402 C736.998 157.427 723.883 182.832 708.062 204.812 C707.303 205.876 706.544 206.939 705.762 208.035 C700.904 214.671 695.636 220.775 690.062 226.812 C685.642 231.837 685.642 231.837 683.496 234.293 C667.545 250.808 647.669 263.059 627.062 272.812 C626.259 273.199 625.455 273.586 624.627 273.985 C617.162 277.494 609.51 280.274 601.688 282.875 C601.035 283.093 600.382 283.312 599.709 283.537 C589.113 287.06 578.544 289.772 567.492 291.438 C565.735 291.709 563.982 292.003 562.231 292.31 C555.226 293.542 548.278 294.341 541.188 294.875 C540.117 294.967 539.046 295.06 537.943 295.155 C532.564 295.594 527.298 295.851 521.902 295.668 C508.122 295.298 508.122 295.298 503.035 299.039 C500.797 302.041 499.077 304.944 497.395 308.272 C495.831 311.255 494.102 314.105 492.344 316.977 C472.86 348.889 456.147 381.779 443.062 416.812 C442.314 418.781 441.565 420.75 440.816 422.719 C434.456 439.626 429.446 456.876 425.573 474.517 C425.03 476.96 424.456 479.395 423.883 481.832 C420.977 494.568 419.038 507.367 417.375 520.312 C417.272 521.102 417.17 521.891 417.064 522.704 C416.331 528.4 415.686 534.104 415.062 539.812 C414.972 540.62 414.881 541.427 414.787 542.259 C412.713 561.227 412.845 580.126 412.959 599.175 C412.983 603.778 412.992 608.38 413.003 612.982 C413.039 627.604 413.097 642.226 413.165 656.847 C413.223 669.248 413.267 681.649 413.285 694.051 C413.287 694.985 413.288 695.92 413.289 696.882 C413.35 745.937 411.533 794.948 407.062 843.812 C406.905 845.559 406.747 847.306 406.589 849.052 C405.546 860.553 404.445 872.047 403.291 883.537 C403.145 884.995 402.999 886.454 402.854 887.912 C401.511 901.395 399.872 914.753 397.543 928.103 C396.39 934.72 395.423 941.339 394.562 948 C393.29 957.67 391.608 967.213 389.673 976.772 C388.971 980.266 388.293 983.764 387.613 987.262 C387.097 989.862 386.58 992.462 386.062 995.062 C385.826 996.288 385.589 997.514 385.344 998.778 C381.782 1016.359 381.782 1016.359 375.125 1021.75 C374.477 1022.295 373.828 1022.841 373.16 1023.402 C366.646 1027.782 359.672 1027.64 352.062 1026.812 C345.413 1024.467 340.518 1018.86 336.93 1012.945 C332.991 1003.259 337.163 991.204 339.433 981.57 C347.505 946.802 352.072 911.04 356.312 875.625 C356.396 874.938 356.479 874.252 356.565 873.544 C357.522 865.587 358.243 857.656 358.696 849.658 C359.075 843.14 359.763 836.673 360.5 830.188 C360.768 827.738 361.035 825.289 361.301 822.84 C361.366 822.244 361.366 822.244 361.694 819.226 C365.131 787.398 365.202 755.677 365.131 723.702 C365.121 718.511 365.116 713.321 365.111 708.131 C365.101 698.025 365.084 687.919 365.062 677.812 C364.857 678.192 364.857 678.192 363.816 680.11 C363.276 681.103 362.736 682.096 362.179 683.119 C361.644 684.104 361.109 685.089 360.558 686.104 C359.009 688.91 357.373 691.627 355.625 694.312 C352.164 699.647 352.164 699.647 350.555 702.281 C342.204 715.878 332.323 728.611 322.062 740.812 C321.161 741.921 320.261 743.03 319.363 744.141 C318.451 745.26 317.538 746.38 316.625 747.5 C315.801 748.514 314.978 749.529 314.129 750.574 C313.447 751.313 312.765 752.051 312.062 752.812 C311.402 752.812 310.743 752.812 310.062 752.812 C309.798 753.402 309.534 753.991 309.262 754.598 C308.017 756.897 306.717 758.402 304.867 760.238 C304.238 760.867 303.608 761.495 302.96 762.142 C302.293 762.796 301.625 763.451 300.938 764.125 C300.255 764.8 299.572 765.475 298.868 766.17 C294.405 770.558 289.899 774.836 285.062 778.812 C284.249 779.492 283.436 780.171 282.598 780.871 C229.424 824.89 165.827 851.911 96.251 852.053 C93.397 852.062 90.544 852.094 87.689 852.125 C75.525 852.195 63.998 851.143 52.062 848.812 C51.005 848.614 49.947 848.415 48.858 848.21 C46.289 847.703 43.739 847.151 41.188 846.562 C40.734 846.458 40.734 846.458 38.44 845.929 C11.208 839.418 -14.067 827.938 -36.938 811.812 C-37.888 811.15 -38.838 810.487 -39.816 809.805 C-58.534 796.296 -73.868 779.429 -85.938 759.812 C-86.325 759.186 -86.713 758.56 -87.113 757.916 C-102.342 733.018 -111.218 703.968 -111.938 674.812 C-111.965 673.854 -111.992 672.896 -112.02 671.908 C-113.406 616.374 -94.892 564.274 -56.523 523.558 C-55.002 521.969 -53.47 520.391 -51.938 518.812 C-51.664 518.526 -51.664 518.526 -50.278 517.075 C-42.063 508.485 -33.508 500.855 -23.938 493.812 C-23.3 493.336 -22.663 492.86 -22.006 492.369 C28.388 454.845 90.744 438.036 153.062 446.812 C153.92 446.931 154.778 447.049 155.661 447.171 C196.986 453.135 238.137 470.844 270.401 497.443 C272.012 498.771 273.636 500.081 275.262 501.391 C281.644 506.595 287.634 512.159 293.5 517.938 C294.162 518.582 294.825 519.227 295.507 519.891 C302.029 526.422 308.08 533.315 308.438 542.875 C308.365 549.736 306.171 555.346 301.57 560.477 C296.911 564.605 292.386 566.101 286.25 566.125 C285.274 566.142 284.298 566.159 283.293 566.176 C273.506 565.075 268.958 560.249 262.438 553.312 C255.245 545.896 248.07 539.052 239.698 533 C238.023 531.784 236.375 530.537 234.727 529.285 C219.807 518.102 203.567 510.109 186.062 503.812 C185.278 503.529 184.493 503.245 183.684 502.953 C171.048 498.474 158.277 495.936 145.062 493.812 C144.145 493.636 143.228 493.459 142.283 493.278 C138.446 492.724 134.665 492.631 130.797 492.609 C130.005 492.604 129.212 492.598 128.396 492.592 C126.718 492.583 125.041 492.576 123.363 492.572 C120.862 492.563 118.361 492.532 115.859 492.5 C101.494 492.405 87.639 494.232 73.75 498 C72.976 498.209 72.202 498.419 71.405 498.635 C46.539 505.622 23.559 517.125 3.062 532.812 C2.12 533.512 1.178 534.212 0.207 534.934 C-13.863 545.853 -26.329 558.853 -35.761 573.957 C-36.894 575.744 -38.076 577.483 -39.285 579.219 C-48.796 593.045 -55.211 608.766 -59.938 624.812 C-60.085 625.289 -60.085 625.289 -60.832 627.699 C-70.505 661.863 -64.017 699.313 -47.902 730.379 C-28.506 764.275 5.931 787.804 43.234 797.992 C46.724 798.886 50.233 799.639 53.765 800.343 C56.189 800.838 58.596 801.391 61.008 801.941 C116.761 813.898 177.109 796.885 224.129 766.476 C249.715 749.635 274.669 728.403 293.062 703.812 C293.56 703.164 294.057 702.516 294.57 701.849 C318.838 670.221 336.164 635.769 348.062 597.812 C348.311 597.031 348.56 596.249 348.816 595.444 C368.898 531.569 368.228 442.416 340.5 380.062 C339.35 377.462 338.553 375.142 337.797 372.426 C335.905 366.03 333.413 359.913 330.875 353.75 C330.629 353.152 330.629 353.152 329.386 350.128 C289.882 254.722 289.882 254.722 262.062 240.812 C260.465 240.68 258.862 240.607 257.259 240.563 C256.256 240.533 255.252 240.502 254.218 240.471 C253.123 240.442 252.027 240.413 250.898 240.383 C248.553 240.309 246.208 240.235 243.863 240.16 C240.187 240.048 236.511 239.94 232.834 239.838 C211.5 239.239 190.327 238.036 169.625 232.5 C169.038 232.348 169.038 232.348 166.065 231.576 C156.871 229.14 147.882 226.377 139.062 222.812 C138.41 222.55 137.757 222.288 137.084 222.017 C112.277 211.902 89.962 197.8 70.589 179.272 C69.559 178.287 68.524 177.308 67.485 176.333 C48.841 158.812 33.741 136.908 22.969 113.738 C22.071 111.83 21.135 109.946 20.188 108.062 C14.425 96.458 9.988 84.362 5.463 72.235 C2.416 64.072 -0.66 55.92 -3.765 47.778 C-4.414 46.071 -5.06 44.363 -5.703 42.654 C-8.92 34.1 -12.111 25.821 -16.804 17.939 C-18.574 14.618 -18.78 12.462 -17.938 8.812 C-13.175 1.348 -8.742 -0.623 0 0 Z " fill="#3B3D3F" transform="translate(443.9375,254.1875)"/> <path d="M0 0 C0.782 0.05 1.564 0.099 2.37 0.151 C12.906 0.905 23.389 2.462 33.789 4.292 C41.567 5.654 49.363 6.554 57.21 7.386 C126.527 14.818 205.945 32.481 252.219 89.656 C269.256 111.225 284.627 136.571 289.457 163.992 C290.062 166.812 290.062 166.812 291.036 168.712 C292.876 172.477 293.376 176.589 294.125 180.688 C294.301 181.646 294.478 182.605 294.659 183.592 C296.442 193.953 296.885 204.317 297.062 214.812 C293.117 211.526 289.374 208.262 286 204.375 C275.333 192.303 264.454 180.166 252.203 169.676 C249.731 167.524 247.34 165.293 244.938 163.062 C239.138 157.775 233.038 152.946 226.818 148.165 C225.118 146.855 223.427 145.535 221.738 144.211 C205.155 131.253 187.487 119.965 169.062 109.812 C168.358 109.421 167.653 109.029 166.927 108.625 C151.046 99.838 134.603 92.604 117.062 87.812 C116.377 87.617 115.691 87.421 114.984 87.219 C112.874 86.773 110.967 86.714 108.812 86.75 C108.194 86.76 108.194 86.76 105.062 86.812 C104.85 89.128 104.85 89.128 105.062 91.812 C106.476 93.411 106.476 93.411 108.465 94.766 C109.198 95.27 109.931 95.775 110.687 96.295 C114.618 98.806 118.596 101.212 122.625 103.562 C134.598 110.654 146.111 118.159 157.176 126.609 C159.097 128.076 161.032 129.524 162.98 130.953 C174.326 139.286 184.84 148.55 195.148 158.121 C197.054 159.881 198.975 161.624 200.914 163.348 C212.495 173.696 223.288 184.996 232.941 197.168 C234.885 199.592 236.897 201.942 238.936 204.286 C243.923 210.044 248.533 216.017 253 222.188 C253.544 222.937 254.087 223.686 254.647 224.457 C257.734 228.773 260.706 233.051 263.062 237.812 C262.733 238.803 262.402 239.793 262.062 240.812 C254.811 240.598 247.56 240.366 240.31 240.123 C237.855 240.042 235.4 239.966 232.944 239.893 C211.585 239.259 190.352 238.043 169.625 232.5 C168.45 232.195 167.276 231.89 166.065 231.576 C156.871 229.14 147.882 226.377 139.062 222.812 C138.41 222.55 137.757 222.288 137.084 222.017 C112.277 211.902 89.962 197.8 70.589 179.272 C69.559 178.287 68.524 177.308 67.485 176.333 C48.841 158.812 33.741 136.908 22.969 113.738 C22.071 111.83 21.135 109.946 20.188 108.062 C14.425 96.458 9.988 84.362 5.463 72.235 C2.416 64.072 -0.66 55.92 -3.765 47.778 C-4.414 46.071 -5.06 44.363 -5.703 42.654 C-8.92 34.1 -12.111 25.821 -16.804 17.939 C-18.574 14.618 -18.78 12.462 -17.938 8.812 C-13.175 1.348 -8.742 -0.623 0 0 Z " fill="#62924C" transform="translate(443.9375,254.1875)"/> <path d="M0 0 C2.938 2.437 2.938 2.437 5 5 C5.33 5.33 5.66 5.66 6 6 C7.173 22.199 4.513 37.241 1 53 C0.651 54.634 0.301 56.268 -0.047 57.902 C-1.796 65.89 -4.052 73.625 -6.754 81.34 C-7.689 84.086 -8.522 86.804 -9.316 89.59 C-17.065 115.615 -30.18 141.019 -46 163 C-46.759 164.063 -47.519 165.127 -48.301 166.223 C-53.158 172.859 -58.426 178.962 -64 185 C-68.42 190.024 -68.42 190.024 -70.566 192.48 C-86.518 208.995 -106.394 221.246 -127 231 C-127.804 231.387 -128.607 231.774 -129.435 232.172 C-136.901 235.681 -144.552 238.462 -152.375 241.062 C-153.028 241.281 -153.681 241.499 -154.354 241.725 C-164.95 245.248 -175.519 247.958 -186.571 249.625 C-189.336 250.052 -192.088 250.541 -194.844 251.023 C-200.838 252.022 -206.818 252.652 -212.875 253.125 C-213.938 253.21 -215.002 253.295 -216.097 253.382 C-226.737 254.165 -237.338 254.191 -248 254 C-238.291 227.498 -215.473 202.871 -197 182 C-196.19 181.078 -195.381 180.157 -194.547 179.207 C-188.412 172.368 -181.968 165.836 -175.5 159.312 C-174.634 158.433 -173.768 157.554 -172.875 156.648 C-167.703 151.469 -162.356 146.714 -156.641 142.144 C-154.158 140.128 -151.776 138.041 -149.406 135.895 C-144.956 131.899 -140.293 128.261 -135.5 124.688 C-135.07 124.365 -135.07 124.365 -132.892 122.735 C-126.665 118.121 -120.304 113.784 -113.732 109.677 C-111.807 108.471 -109.9 107.244 -108 106 C-107.254 105.517 -106.509 105.034 -105.74 104.536 C-105.006 104.03 -104.272 103.525 -103.516 103.004 C-102.843 102.55 -102.17 102.095 -101.478 101.627 C-100 100 -100 100 -99.78 97.312 C-99.853 96.549 -99.925 95.786 -100 95 C-134.337 91.677 -176.075 125.8 -201.375 146.312 C-203.252 147.873 -205.127 149.435 -207 151 C-207.987 151.801 -208.975 152.601 -209.992 153.426 C-218.026 160.03 -225.447 167.232 -232.812 174.562 C-233.3 175.045 -233.788 175.528 -234.291 176.025 C-240.243 181.927 -245.828 188.007 -251.046 194.575 C-252.917 196.897 -254.863 199.112 -256.875 201.312 C-263.294 208.458 -269.073 216.152 -274.891 223.789 C-275.35 224.391 -275.81 224.994 -276.284 225.614 C-277.164 226.77 -278.042 227.928 -278.916 229.089 C-279.921 230.41 -280.957 231.708 -282 233 C-282.33 233 -282.66 233 -283 233 C-283.064 223.999 -283.058 215.001 -283 206 C-283 205.225 -282.999 204.45 -282.999 203.652 C-282.757 158.762 -256.564 115.284 -225.605 84.629 C-202.568 63.196 -173.262 47.23 -143.375 37.938 C-142.357 37.621 -141.34 37.304 -140.291 36.978 C-123.862 32.007 -107.233 28.332 -90.373 25.177 C-32.182 14.231 -32.182 14.231 -10.072 1.019 C-6.412 -0.781 -4.026 -0.671 0 0 Z " fill="#62924C" transform="translate(1198,296)"/> </svg>'''


@app.route("/logo.svg")
def logo_svg():
    return Response(LOGO_SVG, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/")
def index():
    html = (HTML.replace("%FOOTER_CSS%", FOOTER_CSS)
                .replace("%ICON_GEAR%", ICONS["gear"])
                .replace("%ICON_POWER%", ICONS["power"])
                .replace("%FOOTER%", footer_html())
                .replace("%JOB_KEEP%", str(JOB_KEEP)))
    return Response(html, mimetype="text/html")


@app.route("/device")
def page_device():
    body = (DEVICE_BODY.replace("%CURTEXT%", device_label())
                      .replace("%CUR%", PRINTER_ID or ""))
    return Response(settings_page("设备设置", "设备设置", body, DEVICE_SCRIPT), mimetype="text/html")


@app.route("/settings")
def page_settings():
    return Response(settings_page("设置", "", settings_index_body()), mimetype="text/html")


@app.route("/push")
def page_push():
    return Response(settings_page("推送配置", "推送配置", PUSH_BODY, PUSH_SCRIPT), mimetype="text/html")


@app.route("/data")
def page_data():
    return Response(settings_page("数据管理", "数据管理", DATA_BODY, DATA_SCRIPT), mimetype="text/html")


@app.route("/api/totals", methods=["GET"])
def api_totals():
    with lock:
        jobs_n = int(state.get("jobs", 0) or 0)
        bytes_n = int(state.get("bytes", 0) or 0)
    with jobs_lock:
        job_count = len(jobs)
        keep = JOB_KEEP
    try:
        jobs_bytes = os.path.getsize(JOBS_PATH)
    except Exception:
        jobs_bytes = 0
    return jsonify({
        "ok": True,
        "jobs": jobs_n,
        "bytes": bytes_n,
        "since": totals_meta.get("since", ""),
        "keep_jobs": keep,
        "keep_min": JOB_KEEP_MIN,
        "keep_max": JOB_KEEP_MAX,
        "job_count": job_count,
        "jobs_bytes": jobs_bytes,
    })


@app.route("/api/totals/clear", methods=["POST"])
def api_totals_clear():
    with lock:
        state["jobs"] = 0
        state["bytes"] = 0
        state["last_job_size"] = 0
    totals_meta["since"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_totals()
    log("累计任务与累计数据已清零")
    return jsonify({"ok": True, "msg": "累计任务与累计数据已清零"})


@app.route("/notes")
def page_notes():
    body = NOTES_BODY.replace("%NOTE_MAX%", str(NOTE_MAX_LEN))
    script = NOTES_SCRIPT.replace("%NOTE_MAX%", str(NOTE_MAX_LEN))
    return Response(settings_page("来源管理", "来源管理", body, script), mimetype="text/html")


@app.route("/api/notes", methods=["GET"])
def api_notes_get():
    notes = notes_conf()
    with cfg_lock:
        hidden = set(CFG.get("src_hidden") or [])
    known = {}
    with stats_lock:
        snap = dict(daily_stats)
    for item in snap.values():
        src = item.get("src") or {}
        if not isinstance(src, dict):
            continue
        for ip, rec in src.items():
            if not isinstance(rec, dict):
                continue
            a = known.setdefault(ip, {"n": 0, "bytes": 0, "host": "", "last": "", "last_size": None})
            a["n"] += int(rec.get("n", 0) or 0)
            a["bytes"] += int(rec.get("bytes", 0) or 0)
            if rec.get("host") and not a["host"]:
                a["host"] = rec["host"]
    with jobs_lock:
        jsnap = list(jobs)
    for j in jsnap:
        ip = j.get("ip")
        if not ip:
            continue
        a = known.setdefault(ip, {"n": 0, "bytes": 0, "host": "", "last": "", "last_size": None})
        a["job_n"] = int(a.get("job_n", 0) or 0) + 1
        a["job_bytes"] = int(a.get("job_bytes", 0) or 0) + int(j.get("size", 0) or 0)
        t = j.get("time") or ""
        if t > a["last"]:
            a["last"] = t
            a["last_size"] = int(j.get("size", 0) or 0)
            a["last_ok"] = j.get("ok")
        if j.get("host") and not a["host"]:
            a["host"] = j["host"]
    for key in notes.keys():
        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", str(key)):
            known.setdefault(key, {"n": 0, "bytes": 0, "host": "", "last": "", "last_size": None})
    # 按「设备」聚合：有主机名就以主机名为身份，路由器重新分配 IP 后仍归到同一台设备；
    # 没有主机名（反查失败）只能按 IP 区分
    groups = {}
    for ip, v in known.items():
        host = (v.get("host") or host_cache.get(ip, "") or "").strip()
        gid = ("h:" + host.lower()) if host else ("i:" + ip)
        g = groups.get(gid)
        if not g:
            g = {"key": host or ip, "host": host, "ips": [], "n": 0, "bytes": 0,
                 "last": "", "last_size": None, "last_ok": None, "last_ip": ip}
            groups[gid] = g
        g["ips"].append(ip)
        g["n"] += int(v["n"] or v.get("job_n", 0) or 0)
        g["bytes"] += int(v["bytes"] or v.get("job_bytes", 0) or 0)
        if v["last"] > g["last"]:
            g["last"] = v["last"]
            g["last_size"] = v["last_size"]
            g["last_ok"] = v.get("last_ok")
            g["last_ip"] = ip
    items = []
    host_notes = {}
    for g in groups.values():
        if g["key"] in hidden or any(ip in hidden for ip in g["ips"]):
            continue
        note = notes.get(g["key"], "")
        if not note:
            for ip in g["ips"]:
                if notes.get(ip):
                    note = notes[ip]
                    break
        if note and g["host"]:
            host_notes[g["host"]] = note
        items.append({"key": g["key"], "ip": g["last_ip"] or g["ips"][0],
                      "ips": sorted(g["ips"]), "host": g["host"], "note": note,
                      "n": g["n"], "bytes": g["bytes"], "last": g["last"],
                      "last_size": g["last_size"], "last_ok": g["last_ok"]})
    items.sort(key=lambda x: (x["last"], x["n"]), reverse=True)
    return jsonify({"ok": True, "notes": notes, "host_notes": host_notes,
                    "items": items, "max_len": NOTE_MAX_LEN})


@app.route("/api/notes/delete", methods=["POST"])
def api_notes_delete():
    data = request.get_json(silent=True) or {}
    key = str(data.get("key") or data.get("ip") or "").strip()
    if not key:
        return jsonify({"ok": False, "msg": "缺少来源标识"})
    with cfg_lock:
        hidden = CFG.setdefault("src_hidden", [])
        if not isinstance(hidden, list):
            hidden = []
            CFG["src_hidden"] = hidden
        if key not in hidden:
            hidden.append(key)
        CFG.get("notes", {}).pop(key, None)
    save_config()
    log("来源管理：已移除来源 %s" % key)
    return jsonify({"ok": True, "msg": "已从来源列表移除 %s" % key})


@app.route("/api/notes", methods=["POST"])
def api_notes_save():
    data = request.get_json(silent=True) or {}
    raw = data.get("notes")
    if not isinstance(raw, dict):
        return jsonify({"ok": False, "msg": "格式错误：需要 notes 对象"})
    cleaned = {}
    for ip, note in raw.items():
        ip = str(ip or "").strip()
        note = str(note or "").strip()
        if not ip:
            continue
        if len(note) > NOTE_MAX_LEN:
            return jsonify({"ok": False, "msg": "备注最多 %d 个字，%s 超长了" % (NOTE_MAX_LEN, ip)})
        if note:
            cleaned[ip] = note
    with cfg_lock:
        CFG["notes"] = cleaned
    save_config()
    log("来源管理：备注已更新，共 %d 条" % len(cleaned))
    return jsonify({"ok": True, "msg": "已保存 %d 条来源备注" % len(cleaned), "notes": cleaned})


@app.route("/password")
def page_password():
    return Response(settings_page("修改密码", "修改密码", PASSWORD_BODY, PASSWORD_SCRIPT), mimetype="text/html")


@app.route("/api/usb/scan")
def api_usb_scan():
    if not PYUSB_OK:
        return jsonify({"ok": False, "devices": [], "msg": "容器内 pyusb 不可用：%s" % PYUSB_ERROR})
    try:
        devs = list(usb.core.find(find_all=True) or [])
    except Exception as exc:
        log("USB 扫描失败：%s" % exc)
        return jsonify({"ok": False, "devices": [], "msg": "扫描失败：%s" % exc})

    found = []
    for dev in devs:
        try:
            vid = "%04x" % int(dev.idVendor)
            pid = "%04x" % int(dev.idProduct)
        except Exception:
            continue
        item = {
            "id": "%s:%s" % (vid, pid),
            "vid": vid,
            "pid": pid,
            "product": "",
            "vendor": "",
            "serial": "",
            "bus": getattr(dev, "bus", ""),
            "address": getattr(dev, "address", ""),
            "iface_class": [],
        }
        for attr, key in (("product", "product"), ("manufacturer", "vendor"), ("serial_number", "serial")):
            try:
                val = getattr(dev, attr)
                if val:
                    item[key] = str(val)
            except Exception:
                pass
        try:
            for cfg in dev:
                for intf in cfg:
                    try:
                        cls = int(getattr(intf, "bInterfaceClass", 0) or 0)
                    except Exception:
                        cls = 0
                    if cls and cls not in item["iface_class"]:
                        item["iface_class"].append(cls)
        except Exception:
            pass
        item["printer"] = 7 in item["iface_class"]
        item["current"] = (item["id"] == (PRINTER_ID or "").lower())
        found.append(item)

    found.sort(key=lambda x: (not x["printer"], not x["current"], x["id"]))
    log("USB 扫描：共 %d 个设备，其中打印类 %d 个" % (len(found), len([x for x in found if x["printer"]])))
    return jsonify({"ok": True, "devices": found, "current": PRINTER_ID})


@app.route("/api/status")
def api_status():
    conf = push_conf()
    with lock:
        data = dict(state)
        data["push_count"] = push_info["count"]
        data["push_last_time"] = push_info["last_time"]
        data["push_last_result"] = push_info["last_result"]
    with jobs_lock:
        source_count = len(set([j.get("ip") for j in jobs if j.get("ip")]))
    data["source_count"] = source_count
    # 以真实端点状态为准：拔掉打印机会立刻反映为未连接，避免 state 残留导致面板一直显示已连接
    if not printer.is_ready():
        data["usb_online"] = False
        data["printer_name"] = ""
        data["usb_bus"] = ""
        data["usb_addr"] = ""
        data["usb_speed"] = ""
    data["uptime"] = uptime_str()
    data["uptime_sec"] = round(time.time() - START_TS, 1)
    data["pyusb_ok"] = PYUSB_OK
    data["pyusb_error"] = PYUSB_ERROR
    data["printer_id"] = PRINTER_ID
    data["print_port"] = PRINT_PORT
    data["web_port"] = WEB_PORT
    data["push_enabled"] = bool(conf.get("enable"))
    data["push_token_set"] = bool(str(conf.get("token", "")).strip())
    data["push_channel"] = conf.get("channel", "")
    data["push_topic"] = conf.get("topic", "")
    data["push_job_fail"] = bool(conf.get("job_fail_push"))
    data["offline_alert_sec"] = int(conf.get("offline_alert_sec") or 60)
    st = printer_status_snapshot()
    st["main"] = printer_status_text()
    data["printer_status"] = st
    return jsonify(data)


@app.route("/api/log")
def api_log():
    """返回日志。支持 ?since=N：只取第 N 行之后的新日志，前端据此增量追加。

    日志在进程内不做截断，只有容器重启才会清空。
    """
    try:
        since = int(request.args.get("since") or 0)
    except Exception:
        since = 0
    with lock:
        total = len(logs)
        if 0 <= since < total:
            lines = logs[since:]
        elif since >= total:
            lines = []
        else:
            lines = logs[:]
    body = "\n".join(lines)
    if body:
        body += "\n"
    resp = Response(body, mimetype="text/plain")
    resp.headers["X-Log-Total"] = str(total)
    resp.headers["X-Log-From"] = str(since)
    # 不加 no-store，浏览器会拿缓存里的旧日志，看起来就像“日志不刷新”
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/jobs")
def api_jobs():
    """默认只返回最近 200 条：任务多时全量下发会让面板每几秒传上百 KB。"""
    try:
        limit = int(request.args.get("limit") or 200)
    except Exception:
        limit = 200
    limit = max(1, min(limit, JOB_KEEP_MAX))
    with jobs_lock:
        total = len(jobs)
        src = list(jobs)[-limit:] if limit < total else list(jobs)
        data = [dict(item) for item in src]
    return jsonify({"jobs": data, "total": total})


@app.route("/api/jobs/clear", methods=["POST"])
def api_jobs_clear():
    with jobs_lock:
        jobs.clear()
    save_jobs()
    with stats_lock:
        daily_stats.clear()
    save_stats()
    log("打印记录与每日统计已清除")
    return jsonify({"ok": True, "msg": "已清除打印记录与统计"})


@app.route("/api/stats")
def api_stats():
    try:
        days = int(request.args.get("days", "14"))
    except Exception:
        days = 14
    if days not in (7, 14, 30):
        days = 14

    out = []
    today = datetime.date.today()
    window_start = today - datetime.timedelta(days=days - 1)
    with stats_lock:
        snapshot = dict(daily_stats)
    # 起点对齐到窗口内第一条有数据的那天，前面没有数据的空日期不再画出来
    earliest = None
    for k, v in snapshot.items():
        try:
            d = datetime.datetime.strptime(k, "%Y-%m-%d").date()
        except Exception:
            continue
        n = int(v.get("ok", 0) or 0) + int(v.get("fail", 0) or 0)
        if n <= 0 or d > today or d < window_start:
            continue
        if earliest is None or d < earliest:
            earliest = d
    start = earliest if earliest is not None else window_start
    span = (today - start).days + 1
    for i in range(span - 1, -1, -1):
        day = today - datetime.timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        item = snapshot.get(key) or {}
        out.append({
            "date": day.strftime("%m-%d"),
            "date_full": key,
            "ok": int(item.get("ok", 0) or 0),
            "fail": int(item.get("fail", 0) or 0),
            "bytes": int(item.get("bytes", 0) or 0),
        })
    today_key = today.strftime("%Y-%m-%d")
    today_item = snapshot.get(today_key) or {}
    # 补齐到所选天数：有数据的这段居中，前后用带日期的空位占着（空位只显示日期，不画柱子）
    if len(out) < days:
        pad = days - len(out)
        pad_l = pad // 2
        pad_r = pad - pad_l
        left = []
        for i in range(pad_l, 0, -1):
            day = start - datetime.timedelta(days=i)
            left.append({"date": day.strftime("%m-%d"), "date_full": day.strftime("%Y-%m-%d"),
                         "ok": 0, "fail": 0, "bytes": 0, "empty": True})
        right = []
        for i in range(1, pad_r + 1):
            day = today + datetime.timedelta(days=i)
            right.append({"date": day.strftime("%m-%d"), "date_full": day.strftime("%Y-%m-%d"),
                          "ok": 0, "fail": 0, "bytes": 0, "empty": True})
        out = left + out + right
    return jsonify({
        "days": out,
        "max": max([d["ok"] + d["fail"] for d in out] + [1]),
        "today": {
            "ok": int(today_item.get("ok", 0) or 0),
            "fail": int(today_item.get("fail", 0) or 0),
            "bytes": int(today_item.get("bytes", 0) or 0),
        },
    })


@app.route("/api/day")
def api_day():
    """某一天的打印明细：总次数 + 各来源机器次数。"""
    date_txt = (request.args.get("date") or "").strip()
    try:
        day = datetime.datetime.strptime(date_txt, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"ok": False, "msg": "日期格式应为 YYYY-MM-DD"})
    key = day.strftime("%Y-%m-%d")

    with stats_lock:
        item = dict(daily_stats.get(key) or {})
        src = dict(item.get("src") or {})

    sources = []
    for ip, rec in src.items():
        if not isinstance(rec, dict):
            continue
        sources.append({
            "ip": ip,
            "host": rec.get("host", "") or "",
            "n": int(rec.get("n", 0) or 0),
            "ok": int(rec.get("ok", 0) or 0),
            "fail": int(rec.get("fail", 0) or 0),
            "bytes": int(rec.get("bytes", 0) or 0),
        })

    fallback = False
    if not sources:
        # 老版本没按来源记账，退回用 jobs.json 里当天的记录统计（适用于最近 100 条）
        with jobs_lock:
            snap = [dict(x) for x in jobs]
        agg = {}
        for j in snap:
            if (str(j.get("time") or "")[:10]) != key:
                continue
            if j.get("ok") is None:
                continue          # 进行中 / 未结束的任务不计入
            ip = j.get("ip") or "?"
            a = agg.setdefault(ip, {"n": 0, "ok": 0, "fail": 0, "bytes": 0, "host": ""})
            a["n"] += 1
            if j.get("ok") is True:
                a["ok"] += 1
                a["bytes"] += int(j.get("size", 0) or 0)
            else:
                a["fail"] += 1
            if j.get("host"):
                a["host"] = j.get("host")
        for ip, a in agg.items():
            sources.append({"ip": ip, "host": a["host"], "n": a["n"],
                            "ok": a["ok"], "fail": a["fail"], "bytes": a["bytes"]})
        fallback = True

    sources.sort(key=lambda x: (-x["n"], x["ip"]))
    ok_n = int(item.get("ok", 0) or 0)
    fail_n = int(item.get("fail", 0) or 0)
    bytes_n = int(item.get("bytes", 0) or 0)
    if fallback and not (ok_n or fail_n):
        ok_n = sum(x["ok"] for x in sources)
        fail_n = sum(x["fail"] for x in sources)
        bytes_n = sum(x["bytes"] for x in sources)

    return jsonify({
        "ok": True,
        "date": key,
        "total": ok_n + fail_n,
        "succ": ok_n,
        "fail": fail_n,
        "bytes": bytes_n,
        "sources": sources,
        "fallback": fallback,
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    conf = push_conf()
    auth = auth_conf()
    safe = {
        "device": {"id": PRINTER_ID},
        "push": {
            "enable": bool(conf.get("enable")),
            "token": conf.get("token", ""),
            "channel": conf.get("channel", "wechat"),
            "channels": [c for c in str(conf.get("channel", "wechat")).split(",") if c],
            "option": conf.get("option", ""),
            "to": conf.get("to", ""),
            "topic": conf.get("topic", ""),
            "template": conf.get("template", "auto"),
            "offline_alert_sec": int(conf.get("offline_alert_sec") or 60),
            "job_fail_push": bool(conf.get("job_fail_push")),
        },
        "auth": {
            "set": bool(auth.get("hash")),
        },
    }
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(silent=True) or {}
    msg_list = []

    dev_body = body.get("device")
    if isinstance(dev_body, dict) and "id" in dev_body:
        new_id = str(dev_body.get("id", "")).strip()
        old_id = PRINTER_ID
        if new_id and not parse_device_id(new_id):
            return jsonify({"ok": False, "msg": "设备 ID 格式不正确，应形如 1234:5678（4 位十六进制:4 位十六进制）"})
        apply_device_id(new_id)
        with cfg_lock:
            CFG.setdefault("device", {})["id"] = PRINTER_ID
        save_config()
        if PRINTER_ID:
            msg_list.append("设备 ID 已改为 %s" % PRINTER_ID)
        else:
            msg_list.append("设备配置已清空，扫描选中一台设备后再保存即可")
        if PRINTER_ID != old_id:
            log("设备 ID 已更新：%s → %s" % (old_id or "未配置", PRINTER_ID or "未配置（暂停连接）"))
            threading.Thread(target=printer.reconnect, daemon=True).start()
        else:
            log("设备 ID 未变化：%s" % device_label())

    keep_body = body.get("jobs_keep")
    if keep_body is not None:
        try:
            n = int(keep_body)
        except Exception:
            return jsonify({"ok": False, "msg": "保留条数必须是数字"})
        if n < JOB_KEEP_MIN or n > JOB_KEEP_MAX:
            return jsonify({"ok": False, "msg": "保留条数需在 %d ~ %d 之间" % (JOB_KEEP_MIN, JOB_KEEP_MAX)})
        kept = apply_job_keep(n, persist=True)
        msg_list.append("打印监控保留条数已改为 %d 条" % kept)

    if isinstance(body.get("push"), dict):
        p = body["push"]
        # 先校验渠道，再写配置：避免中途报错留下改了一半的状态
        picked = None
        if "channel" in p:
            val = p["channel"]
            if isinstance(val, (list, tuple)):
                picked = [str(x).strip() for x in val if str(x).strip()]
            else:
                picked = [x.strip() for x in str(val).split(",") if x.strip()]
            invalid = [x for x in picked if x not in ALL_CHANNELS]
            if invalid:
                return jsonify({"ok": False, "msg": "不支持的渠道：%s" % ",".join(invalid)})
            if not picked:
                return jsonify({"ok": False, "msg": "请至少选择一个推送渠道"})
        with cfg_lock:
            conf = CFG.setdefault("push", {})
            if "token" in p:
                conf["token"] = str(p["token"]).strip()
            if picked is not None:
                conf["channel"] = ",".join(picked)
            if "option" in p:
                conf["option"] = str(p["option"]).strip()
            if "to" in p:
                conf["to"] = str(p["to"]).strip()
            if "template" in p:
                tpl = str(p["template"]).strip().lower()
                conf["template"] = tpl if tpl in ("auto", "html", "txt", "markdown", "json") else "auto"
            if "topic" in p:
                conf["topic"] = str(p["topic"]).strip()
            if "enable" in p:
                conf["enable"] = bool(p["enable"])
            if "job_fail_push" in p:
                conf["job_fail_push"] = bool(p["job_fail_push"])
            if "offline_alert_sec" in p:
                try:
                    sec = int(p["offline_alert_sec"])
                except Exception:
                    sec = 60
                conf["offline_alert_sec"] = max(5, min(86400, sec))
        save_config()
        msg_list.append("推送配置已保存")
        log("推送配置已更新（离线提醒 %ds）" % push_conf().get("offline_alert_sec"))

    auth_body = body.get("auth")
    if isinstance(auth_body, dict):
        old_pwd = str(auth_body.get("old_password", ""))
        new_pwd = str(auth_body.get("new_password", ""))
        if new_pwd:
            ip = client_ip()
            if not old_pwd or not check_pwd(old_pwd):
                log("改密失败：当前密码不正确（来源 %s）" % ip)
                return jsonify({"ok": False, "msg": "当前密码不正确，密码未修改"})
            prob = pwd_problem(new_pwd)
            if prob:
                return jsonify({"ok": False, "msg": prob})
            set_password(new_pwd)
            # 同时换掉 secret_key：所有旧会话（含其他设备上的登录）立刻失效
            with cfg_lock:
                CFG["secret_key"] = os.urandom(24).hex()
            save_config()
            app.secret_key = CFG["secret_key"]
            session.clear()
            session["ok"] = 1
            session.permanent = True
            msg_list.append("密码已修改，其他设备上的登录已失效")
            log("访问密码已修改（来源 %s），旧会话全部失效" % ip)
        elif not msg_list:
            return jsonify({"ok": True, "msg": "没有需要保存的改动"})

    return jsonify({"ok": True, "msg": "；".join(msg_list) or "已保存"})


@app.route("/api/reconnect", methods=["POST"])
def api_reconnect():
    log("收到手动重连指令")
    printer.disconnect()
    ok = printer.connect()
    if not ok:
        # 连不上时把现场信息打出来，便于判断是设备节点、权限还是控制传输的问题
        for line in printer.diag():
            log("USB 诊断：" + line)
        if printer.sysfs_present() is False:
            # 设备已经不在总线上，重连 libusb 没有意义：让内核重新枚举后再试一次
            if usb_rescan(force=True, reason="手动重连时设备不在总线"):
                time.sleep(3)
                ok = printer.connect()
                if ok:
                    log("USB 总线重扫后已重新识别到打印机")
    return jsonify({"ok": ok})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    log("收到手动重启指令，进程将在 1 秒后重启")
    timer = threading.Timer(1.0, lambda: restart_self(force=True))
    timer.daemon = True
    timer.start()
    return jsonify({"ok": True, "msg": "服务正在重启，约 2~3 秒后恢复"})


@app.route("/api/push_test", methods=["POST"])
def api_push_test():
    with lock:
        st = dict(state)
    ok, msg = do_push(
        "推送测试",
        push_html([
            "<b>USB 打印服务：推送测试</b>",
            "设备：%s（%s）" % (st.get("printer_name") or "USB 打印机", device_label()),
            "打印机状态：%s" % ("在线" if st.get("usb_online") else "离线"),
            "渠道：%s" % push_conf().get("channel", ""),
            "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        ]),
    )
    return jsonify({"ok": ok, "msg": msg})


if __name__ == "__main__":
    load_jobs()
    load_stats()
    load_totals()
    _hist = load_log_history()
    log("========== USB 打印机共享面板启动 ==========")
    if _hist:
        log("（进程重启过，已从 data/panel.log 载入上次运行的 %d 行日志）" % _hist)
    log("配置文件：%s" % CONFIG_PATH)
    log("目标设备 ID：%s" % (PRINTER_ID or "未配置（打开网页「设备设置」页扫描 USB 设备并保存即可）"))
    # host 网络模式下 docker ps 看不到端口映射，这里直接把可用地址打印出来
    _ip = primary_lan_ip()
    if _ip:
        log("面板地址：http://%s:%d" % (_ip, WEB_PORT))
        log("打印端口：%s:%d（其他设备添加打印机时填这个）" % (_ip, PRINT_PORT))
    else:
        log("面板端口：%d，打印端口：%d（未能自动探测本机 IP，请用 NAS 的局域网 IP 访问）"
            % (WEB_PORT, PRINT_PORT))
    if PYUSB_OK:
        log("pyusb 模块已加载")
    else:
        log("pyusb 不可用：%s" % PYUSB_ERROR)
    conf = push_conf()
    if not conf.get("enable"):
        log("推送已关闭（可在网页里开启）")
    elif not str(conf.get("token", "")).strip():
        log("推送未配置 Token：登录面板 → 推送配置里填写")
    else:
        log("推送已启用：渠道 %s，离线 %d 秒后提醒"
            % (conf.get("channel"), int(conf.get("offline_alert_sec") or 60)))
    if auth_ready():
        log("访问密码已设置：打开面板只需输入密码（忘记可删 data/config.json 后重启重设）")
    elif _ip:
        log("尚未设置访问密码：请打开 http://%s:%d 按提示设置" % (_ip, WEB_PORT))
    else:
        log("尚未设置访问密码：请打开 http://本机IP:%d 按提示设置" % WEB_PORT)
    printer.connect()
    threading.Thread(target=print_server_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    threading.Thread(target=backfill_hosts, daemon=True).start()
    # 面板端口先探一下，被占用时给出人话提示，别让容器只在日志里刷 Traceback
    try:
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _probe.bind(("0.0.0.0", WEB_PORT))
        _probe.close()
    except OSError as exc:
        log("面板端口 %d 绑定失败：%s" % (WEB_PORT, exc))
        log("端口被占用了：容器环境变量里把 WEB_PORT 改成别的空闲端口（当前 %d）后重启" % WEB_PORT)
        sys.exit(1)
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)
