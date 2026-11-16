# 🖨️ p910nd-panel

把一台 USB 打印机变成网络打印机的单容器服务：内置 **9100 RAW / JetDirect** 打印服务，同时提供 **8910** Web 管理面板。

适用于内核裁剪、没有 `usblp`/`usbprinter` 模块、也没有 `/dev/usb/lp*` 节点的系统（例如飞牛 fnOS）。这类环境里 p910nd 完全跑不起来，本项目改用 **pyusb 直接操作 USB 打印接口（class 7）**，绕开内核打印驱动。

## ✨ 功能

- 📡 **9100 RAW 打印**：标准 JetDirect 端口，客户端按 TCP/IP + RAW 方式添加即可
- 🖥️ **8910 Web 面板**：运行状态、打印监控、日志、统计图表、操作按钮
- 🔐 **首次自设密码**：无用户名，只有密码；PBKDF2-SHA256 加盐哈希存储
- 🔌 **设备设置**：可扫描 USB 设备并点选填入，保存后热重连，不用重启容器
- 🔔 **Pushplus 推送**：11 个渠道，覆盖离线 / 恢复 / 任务失败三类事件
- 📝 **打印来源监控**：记录来源 IP、反向解析主机名、数据量、耗时、结果，保留最近 100 条
- 📊 **每日统计**：按天统计成功 / 失败 / 字节数，保留 180 天，支持 7 / 14 / 30 天柱状图

## 🚀 快速开始

### 方式一：直接拉镜像（推荐）

镜像由 GitHub Actions 自动构建，一条地址同时包含 `linux/amd64`、`linux/arm64`、`linux/arm/v7`、`linux/386` 四种架构，拉取时按设备架构自动匹配，不用挑标签：

```
# 只需要一个 compose 文件和一个数据目录
mkdir -p p910nd-panel/data && cd p910nd-panel
# 放入 docker-compose.pull.yml 并改名为 docker-compose.yml
docker compose pull
docker compose up -d
```

镜像地址：`ghcr.io/meyzh/p910nd-panel:latest`

> 
> 💡 拉取报权限错误说明镜像包还是私有：GitHub → Packages → p910nd-panel → Package settings → 改为 Public。
> 想换用自己的镜像仓库，只改 `.env` 里的 `PANEL_IMAGE`，compose 文件不用动。

### 方式二：从源码构建

```
git clone [https://github.com/meyzh/p910nd-panel.git](https://github.com/meyzh/p910nd-panel.git)
cd p910nd-panel
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

🌐 浏览器打开 `http://<NAS-IP>:8910`，首次访问会跳到 `/setup` 让你设置管理密码。

🖨️ 客户端添加打印机：设备类型 **TCP/IP**，IP 填 NAS 地址，**端口 9100**，协议 **RAW / 标准**，驱动选你打印机的型号。

## 📁 文件说明

| 文件 | 说明 |
| --- | --- |
| `panel.py` | 主程序，打印服务 + Web 面板全部功能 |
| `Dockerfile` | 镜像构建（Debian + Python + Flask + pyusb + libusb） |
| `docker-compose.yml` | 容器定义（本地构建） |
| `docker-compose.pull.yml` | 容器定义（拉现成镜像，支持 `.env` 变量） |
| `.env.example` | 镜像地址、端口、推送等可覆盖参数 |
| `.github/workflows/build.yml` | 推送到 main 自动构建镜像 → GHCR |
| `.github/workflows/build-acr.yml` | 手动触发，构建镜像并推送到自定义镜像仓库（选配） |
| `部署文档.md` | 单机部署的完整步骤 |
| `远端部署.md` | 部署到另一台远端机器的完整步骤（含跨网访问、客户端添加） |
| `LICENSE` | 使用许可：禁止商业用途，二次开发须保留版权 |
| `data/` | 持久化目录（配置、任务记录、统计），**不入库** |

## 💾 数据与配置

`data/` 目录下三个文件：

| 文件 | 内容 |
| --- | --- |
| `config.json` | 设备 ID、密码哈希、`secret_key`、推送配置 |
| `jobs.json` | 最近 100 条打印任务 |
| `stats.json` | 每日统计，保留 180 天 |

✅ 备份 = 备份整个 `data/` 目录；恢复 = 原样放回后重启容器。
🔑 忘记密码：`docker compose down && rm data/config.json && docker compose up -d`，重新走 `/setup`。

常用环境变量（只作为首次启动的初始值，之后以 `data/config.json` 为准）：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `PRINTER_ID` | 打印机 `vid:pid`，可留空后在 `/device` 页扫描填入 | 空 |
| `PUSHPLUS_TOKEN` / `PUSHPLUS_CHANNEL` | Pushplus 令牌 / 默认渠道 | 空 / `wechat` |
| `PUSH_ENABLE` | 1 开推送，0 关 | `1` |
| `OFFLINE_ALERT_SEC` | 离线多少秒后报警 | `60` |
| `JOB_FAIL_PUSH` | 打印失败是否推送 | `1` |
| `TZ` | 时区 | `Asia/Shanghai` |

## 🔗 接口

页面：`/setup` `/login` `/logout` `/` `/device` `/push` `/password` `/robots.txt`

API：`/api/status` `/api/log` `/api/jobs` `/api/jobs/clear` `/api/stats?days=7|14|30` `/api/config` `/api/reconnect` `/api/test` `/api/push_test` `/api/usb/scan`

除 `/setup`、`/login`、`/robots.txt` 外均需登录。

## ✅ 兼容性

- 🖥️ 宿主：任意 Linux + Docker（飞牛 fnOS、群晖、威联通、unRAID、Ubuntu、树莓派等）。Windows / macOS 的 Docker Desktop 不支持 USB 直通。
- 🖨️ 打印机：USB 接口、暴露 class 7 打印接口并带 Bulk OUT 端点即可，换机型只需在 `/device` 页改设备 ID。
- ⚙️ 容器需要 `privileged: true` 与 `/dev/bus/usb` 映射。

## ⚠️ 已知限制

- 仅实现 RAW 9100，不支持 IPP / AirPrint / Bonjour / LPD，iPhone 无法直接添加
- 单进程服务单台打印机；要带多台就开多个容器（端口、设备 ID、`data` 各自独立）
- 只写 USB 不读状态通道，打印机报错不会回显到面板

## 📜 许可与声明

本项目**仅允许非商业使用**，完整条款见 [LICENSE](LICENSE)。

- 🚫 **禁止商用**：未经作者书面许可，不得将本项目或其核心功能用于销售、付费部署、SaaS 租赁、预装售卖、收费服务等任何商业用途。商业授权请联系 [](https://www.godr.cn/)[https://www.godr.cn/](https://www.godr.cn/)。
- ©️ **版权必须保留**：二次开发、修改、衍生版本在分发或对外提供时，必须完整保留 `LICENSE` 与原作者版权声明，保留网页界面底部的版权信息（`© 亦桉` 及指向 [](https://www.godr.cn/)[https://www.godr.cn/](https://www.godr.cn/) 的链接），并在显著位置注明「基于 p910nd-panel 修改」。
- 📌 **不得改许可**：衍生作品继续受本许可约束，不能单方面改为 MIT / Apache-2.0 等更宽松的许可。
- ❌ **不得冒名**：不得以原作者名义宣传，或暗示原作者为衍生作品背书。

Copyright ©️ 2026 亦桉 · [](https://www.godr.cn/) www.godr.cn All rights reserved.

如果你需要，我可以再单独输出一份 `docker-compose.pull.yml` 的完整文本。
