# FPS.ms Multi-Account Auto Renew

基于 **GitHub Actions + Patchright + Xvfb + sing-box** 的 [FPS.ms](https://panel.fps.ms) 免费服务器多账号全自动续期项目。针对严格风控环境设计，实现 **单号单节点、完全环境隔离、随机冷却间隔与 Telegram 截图播报**。

---

## 核心特性

* **多协议 sing-box 独立出口**：原生解析 `vless://`（含 Reality/TLS/WS/gRPC）、`vmess://`、`hy2://` / `hysteria2://`、`trojan://`、`ss://`，每个账号绑定专属节点。
* **多账号防关联隔离**：
  * **进程与缓存隔离**：每个账号独立启停 `sing-box` 与全新 Chromium 实例，使用独立临时 `user_data_dir`，用完即毁。
  * **出口 IP 碰撞熔断**：若检测到两个账号解析出相同出口 IP，立即中止后者并推送告警，杜绝串 IP 关联。
  * **时区自动对齐**：通过代理探测出口 IP 所在地理时区，自动同步浏览器 `timezone_id`，并屏蔽 WebRTC 与本地 DNS 泄漏。
  * **反检测浏览**：采用去除了 CDP 特征的 `Patchright` + `xvfb-run` 真实有头模式；优先使用 Cookie 绕过 Cloudflare Turnstile。
  * **随机化调度**：非整点 Cron + 启动前随机抖动 + 账号执行顺序随机打乱 + 账号间 2~6 分钟随机冷却。
* **Telegram 实时推送**：每个账号单独发送续期前后对比与面板截图，多账号运行结束后发送汇总报告。

---

## FPS.ms 免费服务器续期规则

* 免费套餐（如 `c.pico`）仅在**剩余时间不足 24 小时**时可点击 `+ Add 24 hours` 续期。
* 计时归零后服务器将暂停（Suspended），数据保留 **28 天**，超期永久删除。
* 默认工作流每天在 **北京时间 10:17 与 22:29** 各运行一次，确保稳定命中续期窗口。

---

## 快速部署

### 1. 创建私有仓库

强烈建议将本项目存放于 **Private（私有）** 仓库中，上传项目内所有文件。

### 2. 导出账号 Cookie（强烈推荐）

使用 Cookie 登录可直接跳过 `/auth/login` 与 Cloudflare Turnstile 校验：

1. 在本地浏览器登录 `https://panel.fps.ms`（建议用该账号对应的代理节点登录）。
2. 安装浏览器插件 **Cookie-Editor**，打开插件点击 **Export → JSON** 复制全部内容。

### 3. 配置 GitHub Secrets

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加以下 3 个变量：

| Secret 名称 | 必填 | 说明 |
| :--- | :---: | :--- |
| `FPS_ACCOUNTS` | 是 | 多账号配置 JSON 数组（格式见下方示例） |
| `TG_BOT_TOKEN` | 是 | Telegram Bot Token（从 `@BotFather` 获取） |
| `TG_CHAT_ID` | 是 | 接收消息的 Telegram Chat ID（从 `@userinfobot` 获取） |

#### `FPS_ACCOUNTS` 配置示例

```json
[
  {
    "name": "Account-NL",
    "proxy_url": "vless://uuid@example.com:443?encryption=none&security=reality&sni=[www.microsoft.com](https://www.microsoft.com)&fp=chrome&pbk=xxx&sid=xxx&type=tcp#NL",
    "cookies": [
      {
        "name": "session_cookie_name",
        "value": "paste_cookie_editor_json_here",
        "domain": "panel.fps.ms",
        "path": "/"
      }
    ],
    "email": "acc1@example.com",
    "password": "your_password_1",
    "server_id": "4ee7b7b5",
    "timezone": ""
  },
  {
    "name": "Account-US",
    "proxy_url": "hy2://password@us.example.com:443?sni=us.example.com#US",
    "cookies": "cookie_key1=val1; cookie_key2=val2",
    "email": "acc2@example.com",
    "password": "your_password_2",
    "server_id": ""
  }
]
```

**字段说明：**

* `name`：账号备注名，用于日志、Telegram 通知与固定设备分辨率映射。
* `proxy_url`：**必填**，该账号专属的 sing-box 节点链接。**切勿多个账号共用同一出口 IP 的节点**。
* `cookies`：推荐填写，支持直接粘贴 Cookie-Editor 导出的 JSON 数组，也支持 `a=1; b=2` 字符串。
* `email` / `password`：可选兜底，仅当 Cookie 失效时才会尝试账密登录。
* `server_id`：可选，例如面板 URL `/server/4ee7b7b5` 中的 `4ee7b7b5`；留空会自动点击 Dashboard 的第一个服务器。
* `timezone`：可选，留空会自动匹配出口 IP 所在时区（例如 `Europe/Amsterdam`）。

---

## 手动测试

1. 进入仓库顶部的 **Actions** 标签页。
2. 左侧选择 **FPS.ms Multi-Account Auto Renew**。
3. 点击 **Run workflow**（默认开启快速测试，跳过长等待，账号间间隔 10~20 秒）。
4. 观察 Telegram 是否收到每个账号的面板截图、独立出口 IP 及汇总报告。

---

## 防封与运维建议

* **固定纯净节点**：每个账号长期固定同一个低风控节点，不要使用免费机场公共节点或频繁切换国家。
* **及时更新 Cookie**：如果 Telegram 提示某账号 Cookie 过期且 Turnstile 拦截，请本地重新导出该账号的 Cookie 替换进 `FPS_ACCOUNTS`。
* **控制账号数量与超时**：默认工作流超时时间为 `50` 分钟。若账号超过 8 个，请适当调大 `.github/workflows/renew.yml` 中的 `timeout-minutes` 或缩短 `ACCOUNT_GAP_MAX`。
* **遵守平台容器规范**：避免在免费容器内运行明确违反 FPS.ms AUP 的代理/中继程序，防止因容器业务本身触发连带封禁。
