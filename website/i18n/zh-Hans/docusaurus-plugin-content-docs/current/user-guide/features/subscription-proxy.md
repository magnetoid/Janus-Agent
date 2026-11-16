---
sidebar_position: 15
title: "订阅代理"
description: "将 OAuth 提供商订阅（例如 xAI Grok OAuth）用作外部应用的 OpenAI 兼容端点"
---

# 订阅代理

订阅代理是一个本地 HTTP 服务器，让外部应用——OpenViking、Karakeep、Open WebUI，以及任何支持 OpenAI 兼容聊天补全（chat completions）的应用——能够将你在 Janus 中登录的 OAuth 提供商订阅用作其 LLM 端点。代理会自动附加正确的凭据（并在需要时自动刷新），因此应用无需静态 API 密钥。

这与 [API 服务器](./api-server.md) 不同：

| | API 服务器 | 订阅代理 |
|---|---|---|
| 服务内容 | 你的 Agent（完整工具集、记忆、技能） | 原始模型推理 |
| 使用场景 | "将 Janus 用作聊天后端" | "从其他应用使用我的 OAuth 订阅" |
| 认证 | 你的 `API_SERVER_KEY` | 任意 bearer（代理附加真实凭据） |
| 工具调用 | 是——Agent 执行工具 | 否——仅透传 |

当你需要将 **Agent** 作为后端时，使用 API 服务器。当你只需要通过订阅访问**模型**时，使用代理。

## 可用提供商

```bash
janus proxy providers
```

当前内置：

| 提供商（`--provider`） | 上游 | 登录方式 |
|---|---|---|
| `xai` | xAI Grok OAuth（SuperGrok / X Premium+），`https://api.x.ai/v1` | `janus auth add xai-oauth --type oauth`（或在 `janus model` 中选择 **xAI Grok OAuth**） |

可以通过在 `janus_cli/proxy/adapters/` 中实现 `UpstreamAdapter` 接口来添加更多 OAuth 提供商（参见下文的[添加更多 OAuth 提供商](#添加更多-oauth-提供商)）。

## 快速开始

### 1. 登录你的提供商（仅需一次）

```bash
janus auth add xai-oauth --type oauth
```

这会打开浏览器进行 xAI OAuth 流程。Janus 将令牌存储在 `~/.janus/auth.json` 中——与所有 Janus 提供商登录信息存放在同一位置。详情参见 [xAI Grok OAuth 指南](../../guides/xai-grok-oauth.md)；如果 Janus 运行在远程主机上，参见 [通过 SSH 进行 OAuth](../../guides/oauth-over-ssh.md)。

### 2. 启动代理

```bash
janus proxy start --provider xai
```

```
Starting Janus proxy for xAI Grok OAuth
  Listening on:  http://127.0.0.1:8645/v1
  Forwarding to: (resolved per-request from your subscription)
  Use any bearer token in the client — the proxy attaches your real credential.
```

保持在前台运行。如需在注销后继续运行，请使用 `tmux`、`nohup` 或 systemd 单元。

### 3. 将你的应用指向代理

任何 OpenAI 兼容应用的配置都使用相同的三元组：

```
Base URL:   http://127.0.0.1:8645/v1
API key:    任意值（例如 "sk-unused"）
Model:      grok-4.3    # 你的订阅可用的任意模型
```

代理会忽略来自你应用的 `Authorization` 请求头，并将你真实的 OAuth 凭据附加到上游请求中。当 bearer 令牌临近过期时，刷新会自动进行；遇到 `401`/`429` 时，xAI 适配器会刷新凭据或轮换到凭据池中的下一个凭据并重试一次。

## 检查状态

```bash
janus proxy status
```

```
Janus proxy upstream adapters

  [xai     ] xAI Grok OAuth — ready
```

如果看到 `not logged in`，请运行该提供商的登录命令（xAI 为 `janus auth add xai-oauth --type oauth`）。如果看到 `credentials need attention`，说明存储的刷新令牌已被撤销——请重新登录。

## 允许的路径

代理只转发上游实际提供的路径。对于 xAI：

| 路径 | 用途 |
|------|---------|
| `/v1/chat/completions` | 聊天补全（流式 + 非流式） |
| `/v1/responses` | Responses API |
| `/v1/completions` | 旧版文本补全 |
| `/v1/embeddings` | 嵌入 |
| `/v1/models` | 模型列表 |

其他路径（`/v1/images/generations`、`/v1/audio/speech` 等）返回 404，并附带指向允许路径的明确错误信息。这可以防止异常客户端向上游泄露奇怪的请求。

## 配置 OpenViking

[OpenViking](https://github.com/volcengine/OpenViking) 是一个上下文数据库，需要一个 LLM 提供商用于其 VLM（用于提取记忆的视觉/语言模型）和嵌入模型。借助代理，你可以将其 `vlm.api_base` 指向本地代理：

编辑 `~/.openviking/ov.conf`：

```json
{
  "vlm": {
    "provider": "openai",
    "model": "grok-4.3",
    "api_base": "http://127.0.0.1:8645/v1",
    "api_key": "unused-proxy-attaches-real-creds"
  }
}
```

然后在终端中与 `openviking-server` 一起启动代理：

```bash
# 终端 1
janus proxy start --provider xai

# 终端 2
openviking-server
```

OpenViking 的 VLM 调用现在会通过你的订阅。嵌入模型一侧可能仍需要自己的提供商，具体取决于你的上游提供哪些嵌入模型。

## 配置 Karakeep（或任何书签/摘要应用）

[Karakeep](https://karakeep.app/) 使用 OpenAI 兼容 API 进行书签摘要。在其配置中：

```bash
# Karakeep .env
OPENAI_API_BASE_URL=http://127.0.0.1:8645/v1
OPENAI_API_KEY=any-non-empty-string
INFERENCE_TEXT_MODEL=grok-4.3
```

同样的模式适用于 Open WebUI、LobeChat、NextChat 或任何其他 OpenAI 兼容客户端。

## 在局域网中暴露

默认情况下，代理绑定 `127.0.0.1`（仅本机）。要让网络中的其他机器使用它：

```bash
janus proxy start --provider xai --host 0.0.0.0 --port 8645
```

⚠ **注意：** 你网络中的任何人现在都可以使用你的订阅。代理本身没有认证——它接受任意 bearer。如果要在受信任网络之外暴露，请使用防火墙、VPN 或带有适当认证的反向代理。

## 速率限制

你的订阅速率限制适用于整个代理。代理不会扇出——每个请求都使用你自己的凭据（或在触发速率限制后使用凭据池中的下一个凭据），因此提供商的正常配额照常适用。

## 架构

代理刻意保持精简。每个请求：

1. 从你的应用接收 `POST /v1/chat/completions`
2. 查找适配器的当前凭据（即将过期时刷新）
3. 原样转发请求体，并附加 `Authorization: Bearer <token>`
4. 原样流式返回响应（保留 SSE）

无转换。不记录请求体。无 Agent 循环。代理就是一个附加凭据的透传层。

## 添加更多 OAuth 提供商

适配器系统是可插拔的。添加新提供商（例如 HuggingFace、GitHub Copilot 的聊天端点、通过 OAuth 的 Anthropic）需要在 `janus_cli/proxy/adapters/<provider>.py` 中实现 `UpstreamAdapter`，并在 `adapters/__init__.py` 中注册。协议层面不兼容 OpenAI 的提供商（例如 Anthropic Messages API）需要一个转换层，这超出了当前设计的范围。
