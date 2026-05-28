# SyncTV Couple (Web 端同步观影系统) - v1.0 正式版 🚀

[![Version](https://img.shields.io/badge/version-v1.0--stable-red.svg)](https://github.com/StevenTopp/mpv-server)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-v0.95.0%2B-green)](https://fastapi.tiangolo.com/)

`SyncTV Couple` 是一款专为情侣及好友设计的高颜值、轻量级**实时高保真同步观影系统**。本系统采用前后端分离设计，后端基于 Python FastAPI 异步框架与 WebSocket 协议实现多房间高精度状态同步；前端基于原生 HTML5 + 现代化 CSS 磨砂玻璃美学，提供丝滑的网页播放与第三方资源一键解析。

**v1.0 正式版**实现了跨三端（Web 网页端、Windows WPF 客户端、Android 原生客户端）的高精度双向同步联动，彻底打破移动端及 PC 端的格式解码与播放同步限制，进入工业级稳定阶段！

---

## 🌟 v1.0 核心更新与稳定特性

本版本（v1.0）正式合入了历经多周深度调试与重构的 **BUG-012 至 BUG-017 协同稳定修复**，大幅度提升了高频复杂交互下的同步稳定度：
* **缓冲拉流高保真探测 (BUG-012/013/014)**：统一了本地主动拖拽寻址与远程同步的缓冲探测逻辑，通过 `RunBufferProbe` 静音拉流机制，确保所有客户端在实际拉流就绪、可平滑播放后才宣布 Ready，杜绝了画面瞬间卡死、音频抢跑等顽疾。
* **高频寻址世代计数器机制 (BUG-017)**：在网页端主控逻辑与 `remote_index.html` 中引入了 `programmaticSeekGeneration` 世代锁。通过异步闭包校验，完美过滤和拦截了旧世代的 seek fallback 超时回调，彻底解决多次快速连续拖动进度条时遮罩提前消失以及独自播放的顽疾。
* **微调对齐优化与跳过探测 (BUG-016)**：当房间内播放状态转换至同步完成起播（微调对齐阶段）时，客户端自动判断并跳过冗余的缓冲探测流程，杜绝了起播瞬间反复暂停的问题。

---

## 🌟 核心功能特性

### 1. 👫 多人多房间实时高精度同步
* **状态双向同步**：房间内任意成员的操作（播放、暂停、进度跳转、播放倍速调整）都会通过高并发 WebSocket 管道在毫秒级内分发并同步给房间内的所有其他成员。
* **协同就绪机制 (Ready State)**：引入成员就绪状态机，避免因网络加载差异导致进度脱节。当某位成员进行跳转时，系统会智能等待全员就绪后再协同继续播放。
* **高精度自动追赶**：内置时间戳偏移检测，当本地播放进度与房间主进度偏差超过设定阈值（如 1.5 秒）时，系统会自动平滑微调或强制追赶进度，实现绝对同步。

### 2. 📺 Bilibili 深度集成与多画质共享
* **扫码快捷登录**：支持在房间内直接展示哔哩哔哩登录二维码，手机端扫码即可安全登录。
* **BV/链接智能解析**：直接粘贴 Bilibili 视频链接或 BV 号，系统自动提取视频多画质直链、分 P 列表、标题及实时弹幕。
* **高清画质共享**：登录凭证生效后，解析出的超清直链（如 1080P/4K，取决于登录账号权限）可瞬间广播共享给房间内所有人同步播放。

### 3. 📂 Alist 云盘网盘无缝接入
* **云盘多站接入**：支持在系统后台登录多个 Alist 云盘实例。
* **可视化目录导航**：前端提供极简美观的网盘文件树浏览器，支持一键目录跳转与在线搜索。
* **多网盘直链提取**：支持主流云盘（百度网盘、阿里云盘、夸克网盘、天翼云盘等）的在线提取直链，并在房间中一键开启多端同步播放，支持智能防盗链头部伪装。

### 4. 💬 实时弹幕与全屏互动系统
* **跨端弹幕广播**：房间内成员发送的弹幕可在所有端（Web 网页端、Android 原生播放器）实时飞过，支持自定义字体大小、弹幕颜色。
* **在线弹幕抓取**：播放 Bilibili 视频时，系统可实时同步抓取 B 站当前视频的原生弹幕并在房间中播放，带给您与千万人同屏观影的氛围。

---

## 🏗️ 架构与安全防护

* **第三方凭据本地化隔离**：用户的 Bilibili 登录 Cookies、Alist 账号及加密 Token 均安全 compartmentalized 在本地的 `data/vendor_state.json` 中。
* **极简版本管理**：该配置文件已在 `.gitignore` 规则中彻底黑名单化，**绝不上传 GitHub**，确保您的个人服务器安全与账号密码隐私万无一失。
* **异步事件循环**：后端依托 `asyncio` 与 `FastAPI Websockets` 构建，单实例可轻松支撑百人同时在线低延迟观影。

---

## 🚀 快速启动指南

### 1. 安装依赖环境
确保已安装 Python 3.8 或更高版本。进入项目目录执行：
```bash
pip install -r requirements.txt
```

### 2. 启动服务
通过 `uvicorn` 高性能 Web 服务器运行主程序：
```bash
# 默认启动在 9990 端口（可自由修改）
python main.py --host 0.0.0.0 --port 9990 --debug
```

---

## 🛠️ 项目结构
```text
mpv-server/
├── core/                # 核心同步逻辑 (房间管理、Websocket 消息分发)
├── data/                # 本地数据目录 (vendor_state.json - 自动生成且已忽略)
├── static/              # 前端静态资源 (网页交互主页 index.html, index.css, index.js)
├── vendors/             # 第三方解析模块 (Alist APIs, Bilibili APIs)
├── main.py              # FastAPI 入口服务
├── remote_index.html    # 原生客户端桥接定制控制面板
├── requirements.txt     # 项目 Python 依赖包清单
└── README.md            # 项目功能说明手册
```
