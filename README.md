# AK2API 外部调用

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

返回可用模型及其时长、分辨率、画幅和素材数量限制。Wan 3.0 支持 4～30 秒的整数时长。

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
