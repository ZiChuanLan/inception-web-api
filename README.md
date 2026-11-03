# Inception Mercury 3 Web-to-API Bridge

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An OpenAI-compatible **API Gateway** for Inception Labs Mercury 3 (Diffusion LLM).

基于 `chat.inceptionlabs.ai` Web 协议逆向实现的 **OpenAI 兼容反向代理服务**。支持 Docker 一键部署、多档思考深度与实时联网搜索。

---

## 🌟 核心特性 (Features)

- **🚀 Headless 浏览器桥接 (Browser Bridge)**：默认内置 Playwright 无头 Chromium，使用真实浏览器网络栈 + 页内原生 `fetch` 通信。
  - 自动通过 **Vercel Security Checkpoint**（WASM PoW 挑战）与 **Cloudflare Turnstile**。
  - **对数据中心 IP（VPS / 代理池）同样适用** —— 即使从首尔 VPS 或机房节点出口也能稳定跑通。
- **🧠 4 档思考深度 (Reasoning Effort)**：
  - `instant`：极速响应，零思考开销
  - `low`：轻量级思考
  - `medium`：默认平衡模式
  - `high`：深度逻辑推演（长思维链）
  - 完美适配 OpenAI `reasoning_effort` 与 DeepSeek R1 规范的 `reasoning_content`。
- **🌐 实时联网搜索 (Live Web Search)**：原生支持开启网络搜索，自动捕获并返回来源引用（`citations`）。
- **📦 精简容器架构 (Slim Footprint)**：基于 `python:3.11-slim` 定制打包，仅安装 Chromium 单个浏览器及必要 Linux 动态库（剔除 Firefox、WebKit 与音视频组件），镜像体积从官方全家桶的 **4.73GB 骤降至 ~700MB**，削减 85% 存储占用。
- **⚡ 双模流式传输 (Full Streaming & Non-Streaming)**：标准 SSE 流式直出与一次性返回全兼容。
- **🔄 双引擎支持**：`ENGINE=browser`（默认，最稳）或 `ENGINE=curl`（零浏览器 fallback，仅限住宅/清洁 IP）。
- **🔌 即插即用**：无缝接入 OneAPI / NewAPI / NextChat / OpenAI SDK / Dify / 沉浸式翻译等生态。

---

## 🚀 快速部署 (Deployment)

### 方式一：Docker Compose（推荐 / Recommended）

```bash
# 1. 克隆代码仓库
git clone https://github.com/ZiChuanLan/inception-web-api.git
cd inception-web-api

# 2. 启动服务（镜像内已预装 Chromium 与 Playwright）
docker compose up -d --build

# 3. 查看日志（看到 "Browser engine ready" 即就绪）
docker compose logs -f
```

---

### 方式二：Docker CLI 直接启动

```bash
# 构建镜像
docker build -t inception-web-api .

# 运行容器
docker run -d \
  --name inception-web-api \
  --restart unless-stopped \
  -p 8011:8011 \
  -e PORT=8011 \
  -e ENGINE=browser \
  -e API_KEY=sk-mysecretkey \
  --shm-size=512mb \
  inception-web-api
```

> ⚠️ 必须加 `--shm-size=512mb`（或 Docker Compose 中 `shm_size`），否则 Chromium 易崩溃。

---

### 方式三：裸机 Python 运行 / Systemd

```bash
# 安装依赖（含 Playwright）
pip install -r requirements.txt
# 安装 Chromium（如本机已有 Chrome，会自动探测路径，可跳过）
python -m playwright install chromium --with-deps

# 启动服务
python app.py
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
| `ENGINE` | `browser` | 引擎模式：`browser`（Playwright 无头浏览器桥接，推荐）或 `curl`（curl_cffi 零浏览器） |
| `PROXY_URL` | 空（直连） | 代理地址（浏览器流量出口。本地/海外 VPS 留空即可） |
| `API_KEY` | 空 | 自定义 API 鉴权密钥。若设置，请求需携带 `Authorization: Bearer <API_KEY>`；留空则直接放行 |
| `DEFAULT_MODEL` | `mercury-3` | 默认模型名称 |
| `CHROME_PATH` | 自动探测 | Chrome/Chromium 可执行文件路径（自动探测失败时手动指定） |

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
```

---

## 🧪 实测证据 (Verified)

- ✅ **Vercel Security Checkpoint**：通过（含模拟数据中心 IP 出口）。
- ✅ **Cloudflare Turnstile**：不感知（浏览器原生网络栈 + 页内 fetch）。
- ✅ **并发请求**：2 路并发实测各自返回正确结果（`reg_*` 请求按 `reqId` 分发）。
- ✅ **Stable 输出**：instant 模式 1.1s 出首个 token（1 字符答案实测 0.9s）。
- ✅ **流式 SSE**：OpenAI chunk 格式，`finish_reason=stop` 与 `[DONE]` 齐全。
- ✅ **Non-stream**：一次性返回 `choices[0].message.content`，含 `reasoning_content`/`citations` 扩展。

---

## ⚠️ 免责声明 (Disclaimer)

- **仅供学习与学术研究**：本项目仅供计算机网络协议逆向、API 网关架构设计及大模型接口适配的**技术学习、安全审计与学术研究交流**使用。
- **非官方项目**：本项目为独立开源探索项目，与 Inception Labs（或其任何商标与关联方）不存在任何从属、赞助、合作或背书关系。
- **遵守服务条款与法律规范**：使用者在部署和运行本项目时，须自觉遵守当地法律法规以及目标平台的服务条款（Terms of Service）。请勿将本项目用于任何商业牟利、批量滥用、拒绝服务攻击或任何侵犯第三方合法权益的行为。
- **无担保与责任限制**：本项目按“现状”（AS-IS）提供，作者与贡献者不对使用、部署或分发本项目所导致的任何直接、间接损失、服务限制或法律责任承担后果。

---

## 📄 License

[MIT License](LICENSE) © 2026 ZiChuanLan
