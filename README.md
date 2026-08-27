# AK2API

Akool 多素材视频生成协议网关。项目以真实 Chrome Profile + CDP 管理账号登录，任务阶段通过 Cookie 协议完成素材上传、动态询价、视频提交和结果轮询。

## 能力

- 账号账密、Cookie、固定代理和独立浏览器 Profile 导入。
- 批量格式支持 `email|password|proxy`、`email----password----proxy`、逗号和 Tab 分隔，也可向接口提交 JSON 账号数组。
- 未指定代理时，从代理池按账号分配数量均衡绑定。
- Seedance 2.0 Mini/Fast/标准、Seedance 2.5 Reference、Minimax H3。
- 图片、视频、音频 URL 或 data URL/base64 素材。
- 提交前调用 Akool `calculateFee`，按 `credit - lock_credit` 预扣并发额度。
- SQLite 任务恢复、账号并发、排队容量、低余额自动禁用。
- `/v1/videos`、`/v1/responses` 和通用异步任务接口。
- 紧凑控制台、上下游审计、素材预览、模型映射和真实消耗参考。

项目不实现 Akool 活动权益或 unlimited 模式，所有询价请求固定为 `is_unlimited_model=false`。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8795
```

打开 `http://127.0.0.1:8795`，使用 `AK_ADMIN_TOKEN` 登录。外部接口使用：

```http
Authorization: Bearer <AK_API_KEY>
```

## 账号登录

批量导入示例：

```text
user@example.com|password|socks5://xray:20001
user2@example.com----password----socks5://xray:20002
```

每个账号固定代理、Profile 和 CDP 端口。Chrome 在 Akool 页面中填写账密并由网站脚本完成密码加密和 invisible Turnstile；系统不会构造或复用 Turnstile token。登录按设置中的并发和错峰参数执行，同一账号不会重复启动登录。遇到交互式验证时，账号会标记为“需验证”并保留同一 Profile，确认后可重新检测；CDP 传输失败会使用同一 Profile 重启一次。

Docker 内可使用 `xray:20001`，会归一化为 `socks5://xray:20001`。宿主机回环代理通过 `AK_PROXY_HOST_OVERRIDE=host.docker.internal` 改写。

## 外部调用

```http
POST /v1/videos
Authorization: Bearer <AK_API_KEY>
Content-Type: application/json
```

```json
{
  "model": "doubao-seedance-2-0-mini-260615",
  "prompt": "保持参考主体一致，镜头平稳推进",
  "duration": 4,
  "resolution": "480p",
  "aspect_ratio": "adaptive",
  "image_urls": ["https://example.com/reference.png"],
  "background": true
}
```

使用 `GET /v1/videos/{task_id}` 查询。`background=false` 可在同步等待时间内返回最终状态。完整模型能力可通过控制台“接入文档”复制，或调用 `GET /v1/models`。

## 安全约束

- Cookie、账密、抓包和数据库不得提交 Git。
- 一个任务的下载、上传、提交和查询始终绑定同一账号代理。
- 提交 POST 不做盲目传输重试，避免重复扣费。
- 删除账号前必须禁用；删除会关闭托管 Chrome 并清理受控 Profile。
