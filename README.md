# Inception Mercury 3 Web-to-API Bridge

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-orange.svg)](LICENSE)

An OpenAI-compatible **API Gateway** for Inception Labs Mercury 3 (Diffusion LLM).

基于 `chat.inceptionlabs.ai` Web 协议实现的 **OpenAI 兼容反向代理服务**。支持 Docker 双引擎架构一键切换、4 档思考深度与实时联网搜索。

---

## 🔀 双引擎架构对比与选型指南 (Engine Comparison)

本项目提供 **两种独立引擎**，按需随意切换：

| 维度 / 特性 | 方案一：Headless 浏览器桥接模式 (`browser`) | 方案二：纯协议零浏览器极简模式 (`curl`) |
| :--- | :--- | :--- |
| **推荐适用场景** | **云机房 / 裸金属 VPS / IDC 节点（首尔/美西等）** | **清洁 IP 出口 / 住宅代理 / 配置了上游代理的 VPS** |
| **Vercel WAF 挑战** | **100% 自动突破**（Chromium 真实运行 WASM 算力） | 依赖 IP 洁净度（机房 IP 若被判定风险需挂上游代理） |
| **是否需要浏览器** | 需要（内置已剥离调试符号的 Debian 原生 Chromium） | **彻底不要浏览器 (Zero-Browser)** |
| **Docker 镜像体积** | **~650MB**（已深度瘦身，清空 apt/deb 缓存） | **仅 ~50MB ~ 60MB**（极轻量纯 Python 镜像） |
| **容器内存占用** | 约 120MB ~ 150MB | 约 **25MB**（极低系统开销） |
| **首字响应延迟** | 约 1.2s ~ 1.5s | 约 **300ms ~ 500ms** |
| **一键启动文件** | `docker-compose.yml` | `docker-compose.slim.yml` |

---

## 🌟 核心特性 (Features)

- **🚀 双引擎无缝切换**：
  - `ENGINE=browser`：基于 Playwright 的无头 Chromium 沙箱桥接，自动通关 Vercel Security Checkpoint 与 Cloudflare Turnstile，专治各种机房 IP 拦截。
  - `ENGINE=curl`：基于 `curl_cffi` 的纯协议直接通信，彻底剔除浏览器依赖，内存占用仅 25MB。
- **🧠 4 档思考深度 (Reasoning Effort)**：
  - `instant`：极速模式，零思考延迟
  - `low`：轻量级思考
  - `medium`：默认平衡模式
  - `high`：深度逻辑推演（长思维链）
  - 完美适配 OpenAI `reasoning_effort` 与 DeepSeek R1 规范的 `reasoning_content`。
- **🌐 实时联网搜索 (Live Web Search)**：原生支持开启网络搜索，自动捕获并返回来源引用（`citations`）。
- **⚡ 双模传输全兼容**：标准 SSE 流式直出（`stream=true`）与非流式聚合返回（`stream=false`）完全一致。
- **🚀 全异步非阻塞高并发与 Token 资源池 (Session Pooling)**：`curl` 引擎全面升级为 `AsyncSession` 原生协程迭代，主线程零阻塞；内置多会话 Token 轮询池（`POOL_SIZE` 默认 5），彻底破除单并发卡顿瓶颈。
- **🛠️ 工具调用与函数执行支持 (Tool / Function Calling Emulation)**：内置网关层双向协议虚拟化转译，完整兼容 OpenAI `tools`、`functions`、`tool_choice`，支持 Agent 循环与多轮工具调用，无缝对接 Dify、Cherry Studio、LangChain 等框架。
- **🔌 即插即用生态**：无缝接入 OneAPI / NewAPI / NextChat / OpenAI SDK / Dify / 沉浸式翻译等生态。

---

## 🚀 快速部署 (Deployment)

### 方案一：机房 VPS 推荐 —— 浏览器桥接模式（自动过盾）

适合各类云服务器（首尔、东京、美西机房等直连），内置 Chromium 自动执行 Vercel Checkpoint：

```bash
# 1. 克隆代码仓库
git clone https://github.com/ZiChuanLan/inception-web-api.git
cd inception-web-api

# 2. 启动服务（镜像内预装 Debian 原生 Chromium）
docker compose up -d --build

# 3. 查看日志（看到 "Browser engine ready" 即就绪）
docker compose logs -f
```

---

### 方案二：追求极速与极低资源 —— 纯协议零浏览器模式（50MB 镜像）

**彻底不要浏览器**！镜像大小直接暴降至 50MB，内存仅占 25MB。如果你的 VPS 处于机房 IP，只需在配置里加一行上游代理（如本地 Clash 或住宅代理）：

```bash
# 一键以纯协议极简模式构建并启动
docker compose -f docker-compose.slim.yml up -d --build

# 查看日志
docker compose -f docker-compose.slim.yml logs -f
```

*(如需指定上游代理，只需在 `docker-compose.slim.yml` 中取消注释 `PROXY_URL=http://your-proxy:port` 即可)*

---

### 方式三：裸机 Python / 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 1. 如果使用浏览器引擎（ENGINE=browser）
# 确保系统安装了 Chrome 或运行: python -m playwright install chromium
python app.py

# 2. 如果使用纯协议引擎（ENGINE=curl）
ENGINE=curl python app.py
```

如需配置 systemd 后台常驻守护进程：

```ini
# /etc/systemd/system/inception-api.service
[Unit]
Description=Inception Mercury Web-to-API Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/inception-web-api
ExecStart=/usr/bin/python3 /opt/inception-web-api/app.py
Restart=always
RestartSec=5
Environment=PORT=8011
Environment=ENGINE=browser
# Environment=ENGINE=curl
# Environment=PROXY_URL=http://127.0.0.1:7890
Environment=API_KEY=sk-mysecretkey

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now inception-api
```

---

## ⚙️ 环境变量配置 (Environment Variables)

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `PORT` | `8011` | 服务监听端口 |
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `ENGINE` | `browser` | 引擎模式：`browser`（浏览器桥接，稳过机房盾）或 `curl`（纯协议零浏览器，仅 50MB） |
| `PROXY_URL` | 空（直连） | 上游代理地址（如 `http://127.0.0.1:7890` 或 SOCKS5）。使用 `curl` 引擎且处于机房 IP 时强烈推荐配置 |
| `API_KEY` | 空 | 自定义 API 鉴权密钥。若设置，请求需携带 `Authorization: Bearer <API_KEY>`；留空则直接放行 |
| `POOL_SIZE` | `5` | 会话 Token 轮询池容量。多并发请求自动轮询使用不同 Token，防止单会话频控 |
| `DEFAULT_REASONING_EFFORT` | `medium` | 全局默认思考深度：`instant`（零思考极速）、`low`、`medium`、`high`。方便不支持传参的客户端全局免思考 |
| `DEFAULT_MODEL` | `mercury-3` | 默认模型名称 |
| `CHROME_PATH` | 自动探测 | Chromium 可执行文件路径（默认自动探测 `/usr/bin/chromium` 等） |
| `MAX_MSG_CHARS` | `1400000` | 单条 message 字符硬上限（防上游 413）。超过则保留前一半 + 后一半，中间插 truncation 标记。`0` 表示不按字符截断。旧版硬编码 12000，长上下文会被静默腰斩 |
| `MAX_INPUT_TOKENS` | `950000` | 单条 message 的**估算 token** 上限（这才是真正的上下文预算）。中文按 1 字≈1 token、其他按 4 字符≈1 token 估算。实测上游约 1M token：99 万中文字符 5/5 召回（16s）；>1.0M token 时尾部（含最后的提问）被上游静默丢弃；1.4M~1.6M 字符直接 413。`0` 表示不按 token 截断 |
| `UPSTREAM_RETRIES` | `2` | 上游失败重试次数。仅在**首个 token 之前**重试（中途重试会导致内容重复），每次重试前强制刷新会话 token；413/400 这类确定性错误不重试 |
| `FIRST_TOKEN_TIMEOUT` | `180` | 等首个 token 的超时秒数，超时即报错（旧版无限等待，上游卡住时请求会永久挂着） |
| `STREAM_IDLE_TIMEOUT` | `120` | 流式过程中两个 chunk 之间的最大间隔秒数 |

---

## 🔌 客户端接入指南 (Client Usage)

- **Base URL**: `http://<你的服务器IP>:8011/v1`
- **API Key**: 任意填写（若配置了 `API_KEY` 环境变量则填对应值）
- **Models**:
  - `mercury-3`（默认模型，平衡中等思考）
  - `mercury-3-instant`（免思考延迟，极速出字）
  - `mercury-3-high`（深度思考模型）
  - `mercury-3-search`（默认开启联网搜索）

### Python 代码调用示例

```python
from openai import OpenAI

client = OpenAI(
    api_key="none",
    base_url="http://localhost:8011/v1"
)

# 1. 极速模式流式输出
response = client.chat.completions.create(
    model="mercury-3-instant",
    messages=[
        {"role": "user", "content": "请用一句话赞美清晨的阳光"}
    ],
    stream=True
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="", flush=True)

# 2. 深度推理模式（获取思考过程）
response = client.chat.completions.create(
    model="mercury-3",
    messages=[
        {"role": "user", "content": "9.11 和 9.9 哪个大？"}
    ],
    reasoning_effort="high",
    stream=False
)

print("回答:", response.choices[0].message.content)
if hasattr(response.choices[0].message, "reasoning_content"):
    print("思考过程:", response.choices[0].message.reasoning_content)

# 3. 工具调用与 Agent 模式 (Tool / Function Calling)
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市的实时天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称，如北京"}
                },
                "required": ["city"]
            }
        }
    }
]

tool_res = client.chat.completions.create(
    model="mercury-3",
    messages=[{"role": "user", "content": "今天北京天气怎么样？"}],
    tools=tools,
    tool_choice="auto"
)

if tool_res.choices[0].message.tool_calls:
    for tc in tool_res.choices[0].message.tool_calls:
        print("命中工具调用:", tc.function.name, "参数:", tc.function.arguments)
```

---

## 🧪 实测验证 (Verified)

- ✅ **Vercel Security Checkpoint**：通过（含模拟数据中心 IP 出口）。
- ✅ **Cloudflare Turnstile**：不感知（浏览器原生网络栈 + 页内 fetch）。
- ✅ **并发请求**：多路并发实测各自返回正确结果（内部按 `reqId` 隔离分发）。
- ✅ **首字延迟**：`curl` 引擎约 300ms；`browser` 引擎约 1.2s。
- ✅ **流式与非流式**：OpenAI SSE chunk 格式与非流式单次聚合返回均 100% 修复并跑通。
- ✅ **Tool Calling 虚拟化转译**：`tools` / `functions` 结构解析、系统提示词自动注入、模型调用结果拦截与标准 `tool_calls` 打包全部通过 12 项回归测试。

---

## ⚠️ 免责声明 (Disclaimer)

- **仅供学习与学术研究**：本项目仅供计算机网络协议逆向、API 网关架构设计及大模型接口适配的**技术学习、安全审计与学术研究交流**使用。
- **非官方项目**：本项目为独立开源探索项目，与 Inception Labs（或其任何商标与关联方）不存在任何从属、赞助、合作或背书关系。
- **遵守服务条款与法律规范**：使用者在部署和运行本项目时，须自觉遵守当地法律法规以及目标平台的服务条款（Terms of Service）。请勿将本项目用于任何商业牟利、批量滥用、拒绝服务攻击或任何侵犯第三方合法权益的行为。
- **无担保与责任限制**：本项目按“现状”（AS-IS）提供，作者与贡献者不对使用、部署或分发本项目所导致的任何直接、间接损失、服务限制或法律责任承担后果。

---

## 📄 License

[GNU AGPLv3](LICENSE) © 2026 ZiChuanLan

本项目采用 **GNU Affero General Public License v3.0**（AGPL-3.0-or-later）授权。

> **为什么是 AGPL 而不是 MIT？**
> AGPL 在 GPL 的基础上专门针对**网络服务**做了补充：任何人在服务器上部署本项目、并通过网络向他人提供 API 服务时，若对代码做了修改，**必须向该服务的所有使用者公开修改后的完整源代码**。
> 这样既能保持项目完全开源、任何人都可自由使用和修改，也能避免本项目的协议适配与工具转译等实现被闭源打包、转手做成收费中转服务。

---
### 友情链接
[Linuxdo](https://linux.do)
