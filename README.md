# AK2API 外部调用

## 在线添加账号与网页登录

管理页面的“在线添加账号”会先创建停用的账号草稿，再打开同源浏览器窗口。在 Akool 页面正常登录并完成验证后，点击“完成登录”；系统校验账号身份与余额后才允许新账号参与调度。已停用或余额不足的旧账号不会因打开、关闭登录窗口而自动启用。

账号列表和任务列表均提供“网页登录/打开”。任务行固定打开该任务关联的账号；没有关联账号时按钮禁用。有在途任务、登录或维护操作的账号不能同时被人工接管。登录窗口有效期为 20 分钟，关闭后立即撤销远程桌面访问。

在线登录使用账号自己的持久浏览器配置。窗口打开时每 5 秒校验并同步会话；后续 Akool 认证请求也从同一浏览器发出，并保存更新后的 Cookie、身份、浏览器信息及代理绑定。旧数据库 Cookie 不会覆盖已有效登录的浏览器，身份不匹配或配置发生变化时会拒绝使用该会话。重新验证后才能继续使用。

身份验证成功后会初始化官网内容上传提示的本地确认标记，避免首次生成时才弹出提示。已确认状态不重复写入，不点击生成或恢复待提交动作；初始化失败时不会让账号进入可用状态。确认记录是本地浏览器回读，不是上游协议接口回执。

该功能要求配置管理登录密码，并随镜像提供 Chromium、Xvfb、x11vnc、Fluxbox 与 noVNC；无需开放 VNC 或 CDP 外部端口。账号配置和浏览器会话保存在现有数据挂载中。

预发布工作流为 `.github/workflows/deploy-test.yml`：GitHub 托管 runner 完成测试、镜像构建与发布；3.5 pre 比较直连和现有代理的实际下载速度，只导入经摘要校验的镜像。部署等待任务、登录及维护自然空闲，保留原配置、数据备份和回滚镜像，不在 3.5 pre 构建或安装依赖。

以下路径均相对于服务地址。请求使用 API Key 鉴权：

```http
Authorization: Bearer <API_KEY>
```

也支持 `X-API-Key: <API_KEY>`。

## 查询模型

```http
GET /v1/models
Authorization: Bearer <API_KEY>
```

返回可用模型及其时长、分辨率、画幅和素材数量限制。Wan 3.0 支持 4～30 秒；另支持 `kling-4.0`（3～30秒、720p/1080p）与 `vidu-q4`（3～16秒、540p至4k）。新模型参数、逐条音频时长及视频参考限制见 [外部接口文档](docs/API_INTEGRATION.zh-CN.md)。

## 创建视频

```http
POST /v1/videos
Authorization: Bearer <API_KEY>
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

`image_urls`、`video_urls`、`audio_urls` 均为可选参数；不传素材可进行文生视频。具体素材限制以模型信息为准。

`background` 默认为 `true`，返回任务后轮询查询；设为 `false` 时同步等待，达到同步等待时限后仍可通过任务 ID 继续查询。

## 查询结果

使用创建接口返回的 `id` 查询任务：

```http
GET /v1/videos/{task_id}
Authorization: Bearer <API_KEY>
```

任务状态包括 `queued`、`preparing`、`submitted`、`running`、`succeeded`、`failed`、`expired`。
成功时从 `data[].url` 获取视频地址；失败时读取 `error`。

`GET /v1/videos/{task_id}/content` 在成功后重定向到第一个视频地址；任务未完成时返回 HTTP 409。

## 兼容接口

| 用途 | 接口 |
| --- | --- |
| 创建视频 | `POST /v1/videos/generations` |
| Responses 创建任务 | `POST /v1/responses` |
| Responses 查询任务 | `GET /v1/responses/{task_id}` |
| 通用任务创建 | `POST /api/v3/contents/generations/tasks` |
| 通用任务查询 | `GET /api/v3/contents/generations/tasks/{task_id}` |

Responses 接口可使用字符串 `input` 传入提示词，其余视频参数同上；成功时 `status` 为 `completed`，视频地址位于 `output[].video_url`。
