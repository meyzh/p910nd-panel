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
import socket
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
VID = 0x0482
PID = 0x0D44
DEFAULT_DEVICE_ID = "0482:0d44"
PRINTER_ID = DEFAULT_DEVICE_ID
PRINT_PORT = 9100
WEB_PORT = 8910
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
    "last_job_ok": True,
    "last_error": "",
    "reconnects": 0,
}

push_info = {"count": 0, "last_time": "", "last_result": "", "last_title": ""}


# ==================== 日志 ====================

_log_writes = [0]


def _trim_log_file():
    """日志文件过大时压回最近 LOG_KEEP_LINES 行，避免无限增长。"""
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
        try:
            if not os.path.isdir(DATA_DIR):
                os.makedirs(DATA_DIR)
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _log_writes[0] += 1
            if _log_writes[0] % 200 == 0:
                _trim_log_file()
        except Exception:
            pass
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
            "id": (os.environ.get("PRINTER_ID", DEFAULT_DEVICE_ID).strip() or DEFAULT_DEVICE_ID),
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
        "notes": {},      # 来源备注：{"192.168.77.161": "三楼打印室"}
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
    """解析设备 ID：支持 0482:0d44 / 0482-0d44 / 0x0482:0x0d44 / 04820d44"""
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
    """把设备 ID 应用到全局（改动后 USB 查找立即按新 ID 走）"""
    global VID, PID, PRINTER_ID
    parsed = parse_device_id(dev_id)
    if not parsed:
        return False
    VID, PID, norm = parsed
    PRINTER_ID = norm
    return True


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


def note_for(ip):
    """来源机器的备注（没设过返回空串）"""
    return notes_conf().get(ip, "")


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
        push_info["last_title"] = title
    log("推送[%s] → %s（%s）" % (title, "成功" if ok else "失败", msg))
    return ok, msg


def push_async(title, content):
    threading.Thread(target=do_push, args=(title, content), daemon=True).start()


def push_html(lines):
    return "<br>".join(lines)


# ==================== USB 打印机 ====================

class UsbPrinter(object):
    def __init__(self):
        self.dev = None
        self.ep_out = None
        self.iface_num = None
        self.sig = None
        self.io_lock = threading.Lock()
        self._fail_log = {"msg": "", "ts": 0}
        self._last_reset = 0

    def _find_dev(self):
        if not PYUSB_OK:
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
        return self._sysfs_present(VID, PID)

    @staticmethod
    def _sysfs_endpoint(vid, pid):
        """不依赖 USB 控制传输，直接从 sysfs 读出打印接口号和输出端点地址。

        拔插后设备可能连 GET_CONFIGURATION 都返回 EIO，但只要设备在，
        sysfs 里的 bInterfaceClass / bEndpointAddress 一定读得到。
        返回 (bInterfaceNumber, bEndpointAddress) 或 None。
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
                    if addr & 0x80:       # 0x80 置位是 IN，跳过
                        continue
                    return num, addr
        return None

    def _open_once(self, dev, silent=False):
        cfg, how, cerr = self._config_for(dev)
        if cfg is None:
            # 控制传输全挂：改用 sysfs 端点 + usbfs 直接 bulk 写，完全不发控制请求
            hit = self._sysfs_endpoint(VID, PID)
            if hit:
                num, addr = hit
                try:
                    usb.util.claim_interface(dev, num)
                except Exception as exc:
                    self._log_fail("控制传输失败且接口占用失败：%s" % exc)
                    return False
                self.dev = dev
                self.iface_num = num
                self.ep_out = addr
                self.sig = "%s-%s" % (getattr(dev, "bus", ""), getattr(dev, "address", ""))
                set_state(
                    usb_online=True,
                    printer_name=self._get_product(dev),
                    usb_bus=str(getattr(dev, "bus", "-")),
                    usb_addr=str(getattr(dev, "address", "-")),
                    usb_speed=self._speed_text(dev),
                    last_error="",
                )
                log("USB 控制传输异常，已改用 sysfs 端点 0x%02x 直写连接：%s" % (addr, cerr))
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
        self.iface_num = iface.bInterfaceNumber
        self.sig = "%s-%s" % (getattr(dev, "bus", ""), getattr(dev, "address", ""))

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
            for attempt in (0, 1):
                cur = dev or self._find_dev()
                if cur is None:
                    set_state(usb_online=False, printer_name="", usb_bus="", usb_addr="", usb_speed="")
                    if not silent:
                        log("未检测到打印机 %s" % PRINTER_ID)
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
                last_error="USB 设备无法连接（%s）" % PRINTER_ID,
            )
            return False

    def disconnect(self, reason=""):
        with self.io_lock:
            was_ready = self.ep_out is not None
            self._close()
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



def resolve_hostname(ip, record):
    """反查客户端主机名（后台线程，失败留空）"""
    if ip in host_cache:
        record["host"] = host_cache[ip]
        return
    name = ""
    try:
        name = socket.gethostbyaddr(ip)[0]
    except Exception:
        name = ""
    host_cache[ip] = name
    record["host"] = name
    save_jobs()


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
            "设备：%s（%s）" % (state["printer_name"] or "USB 打印机", PRINTER_ID),
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
        if printer.sysfs_present() is False:
            raise RuntimeError("打印机已拔出（USB 设备不在系统中）")
        if not printer.is_ready():
            if not printer.connect(silent=True):
                raise RuntimeError("打印机未就绪")
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
    except Exception as exc:
        ok = False
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
            state["last_job_ok"] = ok
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
    srv.bind(("0.0.0.0", PRINT_PORT))
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
    while True:
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
            log("打印机已拔出：USB 设备已从系统移除（%s）" % PRINTER_ID)
        present_prev = present

        try:
            conf = push_conf()
            alert_sec = int(conf.get("offline_alert_sec") or 60)
            if dev is None:
                connect_fail_streak = 0
                # 只要之前连过（状态里是已连接，或句柄还没释放），就记一条断开日志
                if state["usb_online"] or printer.ep_out is not None:
                    printer.disconnect()
                    set_state(
                        usb_online=False,
                        printer_name="",
                        usb_bus="",
                        usb_addr="",
                        usb_speed="",
                        last_error="未检测到打印机（%s）" % PRINTER_ID,
                    )
                    if not just_vanished:
                        log("打印机已断开：未检测到 %s，等待重新插入" % PRINTER_ID)
                if offline_since is None:
                    offline_since = time.time()
                if (not offline_alerted) and push_ready() and (time.time() - offline_since) >= alert_sec:
                    offline_alerted = True
                    push_async(
                        "打印机离线",
                        push_html([
                            "<b>USB 打印服务：打印机离线</b>",
                            "设备：%s" % PRINTER_ID,
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
                            "设备：%s（%s）" % ((printer._get_product(dev) or "USB 打印机"), PRINTER_ID),
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
body{display:flex;align-items:center;justify-content:center;min-height:100vh;background:linear-gradient(160deg,#eaf3ff,#f2f5f9 45%,#eef7f1);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--txt);padding:20px}
.card{width:100%;max-width:390px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:30px 26px;box-shadow:0 10px 30px rgba(20,40,70,.08)}
h1{font-size:19px;font-weight:600;text-align:center}
.tip{font-size:13px;color:var(--sub);text-align:center;margin-top:8px;line-height:1.6}
label{display:block;font-size:13px;color:var(--sub);margin:18px 0 7px}
input{width:100%;padding:12px 13px;border:1px solid var(--line);border-radius:10px;font-size:14px;outline:none;background:#fbfcfd}
input:focus{border-color:var(--brand);background:#fff}
button{width:100%;margin-top:22px;padding:13px;border:none;border-radius:10px;background:var(--brand);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:active{transform:scale(.98)}
.err{background:#fdecec;color:var(--bad);font-size:13px;padding:10px 12px;border-radius:10px;margin-top:16px;text-align:center;line-height:1.5}
.note{font-size:12px;color:var(--sub);margin-top:16px;line-height:1.7;background:#f7fafc;border:1px solid var(--line);border-radius:10px;padding:11px 13px}
"""

# ==================== 页脚版权 ====================

def footer_html(with_license=True):
    """页脚版权：年份按当前时间自动取，不用每年手改。"""
    lic = ' · <a href="/license">查看许可</a>' if with_license else ""
    return (
        '<div class="foot">Copyright © ' + time.strftime("%Y") +
        ' <a href="https://www.godr.cn/" target="_blank" rel="noopener">亦桉</a>'
        ' · www.godr.cn All rights reserved.'
        '<div class="foot2">未经许可禁止商业用途，二次开发须保留本版权信息'
        + lic + '</div>'
        '</div>'
    )

FOOTER_CSS = (
    '.foot{text-align:center;font-size:12px;color:var(--sub);margin:16px 0 4px;line-height:1.8}'
    '.foot a{color:var(--brand);text-decoration:none}'
    '.foot a:hover{text-decoration:underline}'
    '.foot2{font-size:11.5px;color:#98a3b1;margin-top:5px;line-height:1.7}'
    '.foot2 a{color:#98a3b1;text-decoration:underline}'
)

# 登录页 / 首次设置页的 body 是 flex 居中的，页脚要固定在底部居中，不能作为 flex 子项横排
AUTH_FOOTER_CSS = (
    FOOTER_CSS
    + '.foot{position:fixed;left:0;right:0;bottom:12px;margin:0;padding:0 16px;'
      'text-align:center;z-index:2}'
)

LICENSE_TEXT = """p910nd-panel 使用许可与声明
Copyright (c) 2026 亦桉 · https://www.godr.cn/
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
2. 商业授权需单独取得书面许可，联系方式见 https://www.godr.cn/ 。

三、版权与署名必须保留
1. 不得移除、隐藏、篡改本项目中的版权声明与作者署名，包括但不限于：
   源码文件中的版权注释、网页界面底部的版权信息（© 亦桉 及指向
   https://www.godr.cn/ 的链接）、README 与许可证文件。
2. 二次开发、修改、衍生版本在分发或以任何形式对外提供时，必须：
   - 完整保留许可证文件与原作者版权声明；
   - 在显著位置注明「基于 p910nd-panel（https://www.godr.cn/）修改」；
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
<title>USB 打印机共享面板</title>
<style>
:root{--bg:#f2f5f9;--card:#fff;--line:#e3e8ef;--txt:#1b2530;--sub:#63707e;--ok:#12a150;--bad:#e5484d;--brand:#0a84ff;}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt);padding:18px}
.wrap{max-width:1020px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
.mt{margin-top:16px}
.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.title{font-size:20px;font-weight:600}
.sub{font-size:13px;color:var(--sub);margin-top:6px}
.right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:8px;font-size:14px;padding:9px 15px;border-radius:999px;font-weight:600}
.pill.ok{background:#e7f7ee;color:var(--ok)}
.pill.bad{background:#fdecec;color:var(--bad)}
.pill.warn{background:#fdf4e3;color:#b4791f}
.dot{width:9px;height:9px;border-radius:50%;background:currentColor}
.user{font-size:13px;color:var(--sub)}
.link{font-size:13px;color:var(--brand);text-decoration:none}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-top:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.stat .k{font-size:13px;color:var(--sub)}
.stat .v{font-size:20px;font-weight:700;margin-top:8px}
.stat .v.small{font-size:15px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:14px}
.rowitem{background:var(--card);padding:13px 16px;display:flex;justify-content:space-between;gap:10px;font-size:14px}
.rowitem .k{color:var(--sub);flex:none}
.rowitem .v{font-weight:600;text-align:right;word-break:break-all}
.btns{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}
button{border:none;border-radius:10px;padding:11px 18px;font-size:14px;font-weight:600;cursor:pointer;transition:transform .12s,opacity .12s}
button:active{transform:scale(.97)}
.b1{background:var(--brand);color:#fff}
.b2{background:#eaf2fb;color:#21507d}
.b3{background:#eaf7ef;color:#0f7a44}
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
.col:hover{background:#f1f6fd}
.col.on{background:#eaf2fb}
.col.on .dt{color:var(--brand);font-weight:600}
.daydetail{margin-top:12px;border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:#fbfcfd;font-size:13px}
.ddhead{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.ddhead b{font-size:13.5px}
.ddsum{font-size:12.5px;color:var(--sub)}
.ddsum em{font-style:normal;color:var(--brand);font-weight:600}
.ddclose{border:none;background:none;color:var(--sub);font-size:12px;cursor:pointer;padding:2px 6px}
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
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.field{display:flex;flex-direction:column;gap:7px}
.field label{font-size:13px;color:var(--sub)}
.chgrid{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;background:#fbfcfd;border:1px solid var(--line);border-radius:12px;padding:14px}
.chk{display:flex;align-items:center;gap:8px;font-size:13.5px;cursor:pointer}
.chk input{width:17px;height:17px;padding:0;cursor:pointer}
input,select{padding:11px 12px;border:1px solid var(--line);border-radius:10px;font-size:14px;outline:none;background:#fbfcfd;color:var(--txt)}
input:focus,select:focus{border-color:var(--brand);background:#fff}
.check{display:flex;align-items:center;gap:8px;font-size:14px;padding-top:24px}
.check input{width:18px;height:18px;padding:0}
.logbox{background:#0f1720;color:#7ee0a4;border-radius:12px;padding:14px;height:280px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:13px;line-height:1.65;white-space:pre-wrap}
.foot{text-align:center;font-size:12px;color:var(--sub);margin:16px 0 4px}
@media (max-width:560px){.title{font-size:17px}.stat .v{font-size:17px}button{flex:1 1 45%}}
%FOOTER_CSS%
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="head">
      <div>
        <div class="title">USB 打印机共享面板</div>
        <div class="sub">RAW 打印端口 9100 · 面板端口 8910 · 目标设备 <span id="devid">--</span></div>
      </div>
      <div class="right">
        <div id="pill" class="pill warn"><span class="dot"></span><span id="pilltext">检测中</span></div>
        <a class="link" href="/device">设备设置</a>
        <a class="link" href="/push">推送配置</a>
        <a class="link" href="/password">修改密码</a>
        <a class="link" href="/logout">退出登录</a>
      </div>
    </div>

    <div class="stats">
      <div class="stat"><div class="k">打印机连接</div><div class="v" id="s_usb">--</div></div>
      <div class="stat"><div class="k">打印服务</div><div class="v" id="s_port">--</div></div>
      <div class="stat"><div class="k">累计任务</div><div class="v" id="s_jobs">0</div></div>
      <div class="stat"><div class="k">累计数据</div><div class="v small" id="s_bytes">0 B</div></div>
      <div class="stat"><div class="k">来源设备数</div><div class="v" id="s_src">0</div></div>
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
      <button class="b2" onclick="reconnect()">重连打印机</button>
      <button class="b3" onclick="testpage()">打印测试页</button>
      <button class="b2" onclick="pushtest()">测试推送</button>
      <button class="b4" onclick="restartPanel()">重启服务</button>
      <button class="b4" id="autobtn" onclick="toggleAuto()">自动刷新：开</button>
    </div>
  </div>

  <div class="card mt">
    <h3>打印统计 <span class="hint" id="statHint">--</span></h3>
    <div class="toolbar">
      <select id="statDays" onchange="loadStats(true)">
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

function loadNotes(){
  fetch("/api/notes", {cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
    if(!d || !d.ok) return;
    var old = JSON.stringify(noteMap);
    noteMap = d.notes || {};
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
           + '<span class="hn">' + esc(noteMap[s.ip] || s.host || "—") + '</span>'
           + '<span class="cnt">' + s.n + ' 次</span>'
           + (s.fail ? '<span class="fl">失败 ' + s.fail + '</span>' : '')
           + '<span class="bt">' + fmtBytes(s.bytes) + '</span></div>';
    }).join("");
    box.innerHTML = '<div class="ddhead"><b>' + esc(d.date) + ' 打印明细</b>'
      + '<span class="ddsum">总计 <em>' + d.total + '</em> 次 · 成功 ' + d.ok + ' · '
      + (d.fail ? '<span class="fl">失败 ' + d.fail + '</span>' : '失败 0') + ' · 数据 ' + fmtBytes(d.bytes) + '</span>'
      + '<button class="ddclose" onclick="closeDay()">收起</button></div>'
      + '<div class="ddlist">' + (rows || '<div class="ddempty">当天没有来源记录</div>') + '</div>'
      + (d.fallback ? '<div class="ddempty" style="margin-top:8px">来源分布由最近 %JOB_KEEP% 条任务记录推算（分来源统计从本版本开始累计）</div>' : "");
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
    var list = d.days || [];
    var html = "";
    for(var i=0;i<list.length;i++){
      var it = list[i], ok = it.ok || 0, fail = it.fail || 0;
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

var JOB_PAGE_SIZES = [10, 20, 30, 50, 100];
var jobPageSize = 10, jobCache = [], jobPageNo = 0;

try{
  var _sz = parseInt(localStorage.getItem("job_page_size"), 10);
  if(JOB_PAGE_SIZES.indexOf(_sz) >= 0) jobPageSize = _sz;
}catch(e){}

function setJobPageSize(v){
  var n = parseInt(v, 10);
  if(JOB_PAGE_SIZES.indexOf(n) < 0) n = 10;
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
  var total = jobCache.length;
  var pages = Math.max(1, Math.ceil(total / jobPageSize));
  if(jobPageNo > pages - 1) jobPageNo = pages - 1;
  if(jobPageNo < 0) jobPageNo = 0;
  if(!total){
    body.innerHTML = '<tr><td colspan="6" class="empty">暂无任务记录</td></tr>';
  } else {
    var html = "", start = jobPageNo * jobPageSize, end = Math.min(start + jobPageSize, total);
    for(var i = start; i < end; i++){
      var j = jobCache[i];
      html += "<tr><td>" + esc(j.time) + "</td><td>" + esc(j.ip) + "</td><td>" + esc(noteMap[j.ip] || j.host || "--") + "</td><td>" + fmtBytes(j.size) + "</td><td>" + (j.seconds || 0) + "s</td><td class='" + ((j.ok === true) ? "ok" : ((j.ok === false) ? "bad" : "")) + "'>" + esc(j.ok === true ? "成功" : (j.ok === false ? ("失败：" + (j.msg || "")) : "进行中")) + "</td></tr>";
    }
    body.innerHTML = html;
  }
  var info = document.getElementById("jobpageinfo");
  if(info) info.innerText = "第 " + (jobPageNo + 1) + " / " + pages + " 页 · 共 " + total + " 条（每页 " + jobPageSize + " 条）";
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
    setText("s_port", d.port_listening ? "监听中" : "未启动");
    setText("s_jobs", d.jobs);
    setText("s_bytes", fmtBytes(d.bytes));
    setText("s_src", d.source_count);
    setText("i_name", d.printer_name || "--");
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
    setText("devid", d.printer_id || "--");

    var pill=document.getElementById("pill"), pilltext=document.getElementById("pilltext");
    if(d.usb_online && d.port_listening){pill.className="pill ok";pilltext.innerText="运行正常";}
    else if(d.usb_online){pill.className="pill warn";pilltext.innerText="打印机就绪，服务异常";}
    else {pill.className="pill bad";pilltext.innerText="打印机未连接";}
  }).catch(function(){});

  fetch("/api/jobs").then(function(r){return r.json();}).then(function(d){
    jobCache = (d.jobs || []).slice().reverse();
    renderJobs();
  }).catch(function(){});

  loadLog();
  loadStats();
}

var logFrom = 0, logLoaded = false;

function loadLog(){
  if(document.hidden) return;   // 页面在后台时不拉日志，回到前台会立刻补一次
  var box = document.getElementById("logbox");
  fetch("/api/log?since=" + logFrom, {cache:"no-store"}).then(function(r){
    var total = parseInt(r.headers.get("X-Log-Total") || "0", 10);
    if(isNaN(total)) total = 0;
    return r.text().then(function(txt){ return {txt: txt, total: total}; });
  }).then(function(res){
    if(!box) return;
    if(res.total < logFrom){          // 服务重启过，日志重新从第 0 行开始
      logFrom = 0; logLoaded = false; box.innerText = "";
      return loadLog();
    }
    var txt = (res.txt || "").replace(/\\n+$/, "");
    var newLines = txt ? txt.split("\\n") : [];
    if(!logLoaded){
      box.innerText = newLines.length ? newLines.join("\\n") : "暂无日志";
      logLoaded = true;
      box.scrollTop = box.scrollHeight;
    } else if(newLines.length){
      var stick = box.scrollTop + box.clientHeight + 40 >= box.scrollHeight;
      var base = box.innerText === "暂无日志" ? "" : box.innerText;
      box.innerText = base ? base + "\\n" + newLines.join("\\n") : newLines.join("\\n");
      if(stick) box.scrollTop = box.scrollHeight;
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
    setText("pilltext", "正在重启…");
    setTimeout(function(){ location.reload(); }, 4000);
  }).catch(function(){ setTimeout(function(){ location.reload(); }, 3000); });
}

function testpage(){
  fetch("/api/test",{method:"POST"}).then(function(){refresh();});
}

function pushtest(){
  fetch("/api/push_test",{method:"POST"}).then(function(r){return r.json();}).then(function(d){
    alert(d.msg || (d.ok ? "推送已发送" : "推送失败"));
    refresh();
  });
}

function toggleAuto(){
  autoRefresh = !autoRefresh;
  document.getElementById("autobtn").innerText = "自动刷新：" + (autoRefresh ? "开" : "关");
  if(autoRefresh){startTimer();}else{stopTimer();}
}

function startTimer(){stopTimer();timer=setInterval(refresh,3000);}
function stopTimer(){if(timer){clearInterval(timer);timer=null;}}

// 日志单独按 2 秒轮询，跟“自动刷新”开关无关，日志永远实时
setInterval(loadLog, 2000);
document.addEventListener("visibilitychange", function(){ if(!document.hidden) loadLog(); });
// 页面从后台回到前台时补一次状态
document.addEventListener("visibilitychange", function(){ if(!document.hidden && autoRefresh) refresh(); });

initJobSize();
refresh();
loadStats(true);
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
.back,.out{font-size:13px;color:var(--brand);text-decoration:none;white-space:nowrap}
.back:hover,.out:hover{text-decoration:underline}
.nav{display:flex;gap:8px;flex-wrap:wrap}
.nav a{font-size:13px;color:var(--sub);text-decoration:none;padding:7px 13px;border:1px solid var(--line);border-radius:9px;background:#fff}
.nav a.on{color:#fff;background:var(--brand);border-color:var(--brand);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 20px;box-shadow:0 8px 24px rgba(20,40,70,.06)}
h2{font-size:17px;font-weight:600;margin-bottom:6px}
.desc{font-size:12.5px;color:var(--sub);line-height:1.7;margin-bottom:16px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input,select{width:100%;padding:12px 13px;border:1px solid var(--line);border-radius:10px;font-size:14px;outline:none;background:#fbfcfd;color:var(--txt)}
input:focus,select:focus{border-color:var(--brand);background:#fff}
.inline{flex:1 1 220px;min-width:180px}
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
"""

SETTINGS_PATH_ROWS = [("设备设置", "/device"), ("来源备注", "/notes"), ("数据管理", "/data"), ("推送配置", "/push"), ("修改密码", "/password")]


def settings_page(title, active, body, script=""):
    nav = ""
    for label, href in SETTINGS_PATH_ROWS:
        cls = ' class="on"' if label == active else ""
        nav += '<a%s href="%s">%s</a>' % (cls, href, label)
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%TITLE% · USB 打印服务面板</title>
<style>%STYLE%</style>
</head>
<body>
<div class="wrap">
  <div class="bar">
    <a class="back" href="/">← 返回面板</a>
    <div class="nav">%NAV%</div>
    <a class="out" href="/logout">退出登录</a>
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
<div class="desc">当前生效设备：<b>%CUR%</b>。改完保存会立即按新设备重新连接，无需重启容器。</div>
<div class="row">
  <input class="inline" id="d_id" placeholder="0482:0d44" value="%CUR%">
  <button class="btn" onclick="scan()">扫描 USB 设备</button>
  <button class="btn ghost" onclick="save()">保存设备 ID</button>
</div>
<div class="sec">
  <div class="sech">扫描结果 <span class="muted" id="scanInfo">点“扫描 USB 设备”开始</span></div>
  <div class="list" id="list"><div class="empty">尚未扫描</div></div>
</div>
<div class="msg" id="msg"></div>
<div class="note">支持写法：0482:0d44、0482-0d44、0x0482:0x0d44、04820d44。扫描只能看到已通电且被容器识别到的设备；插在 USB HUB 上也能扫到，但个别 HUB 供电不足会导致打印中断，建议直插 NAS。</div>
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
<div class="desc">给每台打印来源起个好记的名字（比如“三楼打印室”）。打印监控的“来源主机”列和统计图里的当天明细会优先显示备注，一看就知道是哪台机器发的任务。留空表示不设备注。</div>
<div class="list" id="list"><div class="empty">加载中...</div></div>
<div class="btns">
  <button class="btn" onclick="saveNotes()">保存备注</button>
  <button class="btn ghost" onclick="loadNotes()">重新读取</button>
</div>
<div class="msg" id="msg"></div>
<div class="note">来源 IP 由统计数据和打印记录自动汇总，没打印过的机器不会出现在这里。备注最多 %NOTE_MAX% 个字，只保存在本机 config.json 内，不会外发。</div>
"""

NOTES_SCRIPT = """
var LIMIT = %NOTE_MAX%;
function setMsg(t,ok){
  var e=document.getElementById("msg");
  e.innerText=t||"";
  e.className="msg "+(ok?"ok":"bad");
}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"})[c];});}
function loadNotes(){
  setMsg("读取中...",true);
  fetch("/api/notes",{cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
    if(!d.ok){ setMsg(d.msg||"读取失败",false); return; }
    var items=d.items||[];
    if(!items.length){
      document.getElementById("list").innerHTML='<div class="empty">还没有打印记录：先让一台机器打印一次，再回来设置备注</div>';
      setMsg("暂无可备注的来源",true);
      return;
    }
    var h="";
    for(var i=0;i<items.length;i++){
      var it=items[i];
      h+='<div class="item"><div class="info"><div class="id">'+esc(it.ip)+'</div>'
       + '<div class="sub">'+(it.host?esc(it.host)+' · ':'')+'累计 '+(it.n||0)+' 次</div></div>'
       + '<input class="inline" data-ip="'+esc(it.ip)+'" maxlength="'+LIMIT+'" value="'+esc(it.note||"")+'" placeholder="例如：三楼打印室"></div>';
    }
    document.getElementById("list").innerHTML=h;
    setMsg("共 "+items.length+" 个来源",true);
  }).catch(function(){ setMsg("读取失败",false); });
}
function saveNotes(){
  var notes={}, n=0;
  Array.prototype.forEach.call(document.querySelectorAll("input[data-ip]"),function(el){
    var v=el.value.trim();
    if(v){ notes[el.getAttribute("data-ip")]=v; n++; }
  });
  fetch("/api/notes",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({notes:notes})})
    .then(function(r){return r.json();}).then(function(d){
      setMsg(d.msg||"", d.ok);
      if(!d.ok) alert(d.msg||"保存失败");
      else loadNotes();
    }).catch(function(){ setMsg("保存失败",false); });
}
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
    if path in ("/favicon.ico", "/robots.txt", "/license"):
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
        text = text.replace("Copyright (c) 2026", "Copyright (c) 2026-%s" % year, 1)
    text = (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/")
def index():
    html = (HTML.replace("%FOOTER_CSS%", FOOTER_CSS)
                .replace("%FOOTER%", footer_html())
                .replace("%JOB_KEEP%", str(JOB_KEEP)))
    return Response(html, mimetype="text/html")


@app.route("/device")
def page_device():
    body = DEVICE_BODY.replace("%CUR%", PRINTER_ID)
    return Response(settings_page("设备设置", "设备设置", body, DEVICE_SCRIPT), mimetype="text/html")


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
    return Response(settings_page("来源备注", "来源备注", body, script), mimetype="text/html")


@app.route("/api/notes", methods=["GET"])
def api_notes_get():
    notes = notes_conf()
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
            a = known.setdefault(ip, {"n": 0, "host": ""})
            a["n"] += int(rec.get("n", 0) or 0)
            if rec.get("host") and not a["host"]:
                a["host"] = rec["host"]
    with jobs_lock:
        for j in list(jobs):
            ip = j.get("ip")
            if not ip:
                continue
            a = known.setdefault(ip, {"n": 0, "host": ""})
            if j.get("host") and not a["host"]:
                a["host"] = j["host"]
    for ip in notes.keys():
        known.setdefault(ip, {"n": 0, "host": ""})
    items = [{"ip": ip, "note": notes.get(ip, ""), "host": v["host"], "n": v["n"]}
             for ip, v in known.items()]
    items.sort(key=lambda x: (-x["n"], x["ip"]))
    return jsonify({"ok": True, "notes": notes, "items": items, "max_len": NOTE_MAX_LEN})


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
    log("来源备注已更新，共 %d 条" % len(cleaned))
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
    with jobs_lock:
        data = [dict(item) for item in jobs]
    return jsonify({"jobs": data})


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
    with stats_lock:
        snapshot = dict(daily_stats)
    for i in range(days - 1, -1, -1):
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
    total_bytes = sum(int(v.get("bytes", 0) or 0) for v in snapshot.values())
    return jsonify({
        "days": out,
        "max": max([d["ok"] + d["fail"] for d in out] + [1]),
        "today": {
            "ok": int(today_item.get("ok", 0) or 0),
            "fail": int(today_item.get("fail", 0) or 0),
            "bytes": int(today_item.get("bytes", 0) or 0),
        },
        "total_bytes": total_bytes,
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
        "ok": ok_n,
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
        parsed = parse_device_id(new_id)
        if not parsed:
            return jsonify({"ok": False, "msg": "设备 ID 格式不正确，应形如 0482:0d44"})
        old_id = PRINTER_ID
        apply_device_id(new_id)
        with cfg_lock:
            CFG.setdefault("device", {})["id"] = PRINTER_ID
        save_config()
        msg_list.append("设备 ID 已改为 %s" % PRINTER_ID)
        if PRINTER_ID != old_id:
            log("设备 ID 已更新：%s → %s，重新连接设备" % (old_id, PRINTER_ID))
            threading.Thread(target=printer.reconnect, daemon=True).start()
        else:
            log("设备 ID 未变化：%s" % PRINTER_ID)

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
        with cfg_lock:
            conf = CFG.setdefault("push", {})
            if "token" in p:
                conf["token"] = str(p["token"]).strip()
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
    return jsonify({"ok": ok})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    log("收到手动重启指令，进程将在 1 秒后重启")
    timer = threading.Timer(1.0, lambda: restart_self(force=True))
    timer.daemon = True
    timer.start()
    return jsonify({"ok": True, "msg": "服务正在重启，约 2~3 秒后恢复"})


@app.route("/api/test", methods=["POST"])
def api_test():
    if not printer.is_ready() and not printer.connect(silent=True):
        set_state(last_error="打印机未连接")
        return jsonify({"ok": False, "msg": "打印机未连接"})
    content = (
        b"\x1bE"
        b"====================\r\n"
        b"  USB Print Bridge Test Page\r\n"
        b"====================\r\n\r\n"
        b"Device : " + (state["printer_name"] or "USB Printer").encode("utf-8", "ignore") + b"\r\n"
        b"USB ID : " + PRINTER_ID.encode("utf-8") + b"\r\n"
        b"Port   : 9100\r\n\r\n"
        + time.strftime("%Y-%m-%d %H:%M:%S").encode("utf-8") + b"\r\n\r\n"
        b"Print OK.\r\n\x0c"
    )
    try:
        printer.write(content)
        with lock:
            state["jobs"] += 1
            state["bytes"] += len(content)
            state["last_job_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            state["last_job_size"] = len(content)
            state["last_job_ok"] = True
        save_totals()
        log("测试页已发送（%d 字节）" % len(content))
        return jsonify({"ok": True})
    except Exception as exc:
        set_state(last_error=str(exc), last_job_ok=False)
        log("测试页发送失败：%s" % exc)
        return jsonify({"ok": False, "msg": str(exc)})


@app.route("/api/push_test", methods=["POST"])
def api_push_test():
    ok, msg = do_push(
        "推送测试",
        push_html([
            "<b>USB 打印服务：推送测试</b>",
            "设备：%s（%s）" % (state["printer_name"] or "USB 打印机", PRINTER_ID),
            "打印机状态：%s" % ("在线" if state["usb_online"] else "离线"),
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
    log("目标设备 ID：%s（可在网页「设备设置」里修改）" % PRINTER_ID)
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
    else:
        log("尚未设置访问密码：请打开 http://本机IP:8910 按提示设置")
    printer.connect()
    threading.Thread(target=print_server_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)
