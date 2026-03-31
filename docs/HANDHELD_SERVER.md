# Handheld Server

面向 `Handheld` App 的服务端入口，运行在单台 `CVM` 内，职责只包含：

- WebSocket 对话链路
- 会话列表与历史读取
- 服务健康状态
- 服务端自身初始化状态
- 最近日志读取

不包含：

- `CVM` 创建、开机、关机、销毁
- 云厂商 API 调用
- 安装脚本下发

这些都应该由 `App / Handheld` 负责。

## 启动

```bash
uv run nanobot handheld-serve --token your-static-token
```

可选参数：

- `--host`：默认 `127.0.0.1`
- `--port`：默认 `18789`
- `--workspace`
- `--config`
- `--verbose`

也可以通过环境变量传 token：

```bash
NANOBOT_HANDHELD_TOKEN=your-static-token uv run nanobot handheld-serve
```

## 部署建议

- 这里默认就是 **`systemd` 部署**
- 你的场景是长期存在的 `CVM`，机器重启后自动恢复服务是默认需求

已经提供：

- 安装脚本：`deploy/install-handheld-server.sh`
- `systemd` 模板：`deploy/systemd/nanobot-handheld.service`

推荐执行顺序就是你说的那样：

1. 安装基础依赖：`curl` / `git`
2. 安装 `uv`
3. 拉取 / 更新 `nanobot` 代码
4. `uv sync --extra api`
5. 生成默认 `config.json`
6. 准备 workspace、token 环境文件
7. 安装并启用 `systemd`

最小示例：

```bash
export NANOBOT_HANDHELD_TOKEN='replace-with-a-long-random-token'
bash deploy/install-handheld-server.sh
```

常用可覆盖变量：

- `NANOBOT_REPO_URL`
- `NANOBOT_BRANCH`
- `NANOBOT_INSTALL_DIR`
- `NANOBOT_CONFIG_PATH`
- `NANOBOT_WORKSPACE_DIR`
- `NANOBOT_HANDHELD_HOST`
- `NANOBOT_HANDHELD_PORT`
- `NANOBOT_SYSTEMD_MODE`：`user` 或 `system`

脚本会在缺少配置时自动执行一次：

```bash
uv run nanobot onboard --config <config-path> --workspace <workspace-dir>
```

所以装完后只需要补上你自己的 provider / model / api key 配置即可。

## HTTP Endpoints

- `GET /health`
- `GET /status`
- `GET /init-status`
- `GET /logs`

除 `/health` 外，其他接口都需要：

```http
Authorization: Bearer <token>
```

## WebSocket

连接地址：

```text
ws://host:port/ws
```

鉴权方式：

- 连接时带 `Authorization: Bearer <token>`，或
- 首帧发送 `auth`

示例：

```json
{
  "type": "auth",
  "request_id": "req-1",
  "payload": {
    "token": "your-static-token"
  }
}
```

支持的入站命令：

- `auth`
- `send_message`
- `list_sessions`
- `get_history`
- `get_server_status`
- `ping`

核心出站事件：

- `auth_ok`
- `message_start`
- `message_delta`
- `message_end`
- `task_status`
- `session_state`
- `server_status`
- `init_status`
- `error`
- `sessions_list`
- `history`

## 初始化状态边界

`init_status` 只表示服务端自身阶段：

- `starting`
- `loading_config`
- `loading_runtime`
- `recovering_sessions`
- `ready`
- `failed`

不表示：

- `creating_vm`
- `booting_cvm`
- `stopping_cvm`

这些云侧生命周期状态应由 `App / Handheld` 自己维护。
