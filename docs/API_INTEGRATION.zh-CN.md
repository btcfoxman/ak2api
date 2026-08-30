# AK2API 外部接口

## 鉴权

外部接口使用 `Authorization: Bearer <AK_API_KEY>` 或 `X-API-Key`。

## 视频任务

- 创建：`POST /v1/videos` 或 `POST /v1/videos/generations`
- 查询：`GET /v1/videos/{task_id}`
- 结果重定向：`GET /v1/videos/{task_id}/content`
- Responses 异步模式：`POST /v1/responses`、`GET /v1/responses/{id}`

请求支持 `prompt`、`model`、`duration`、`resolution`、`aspect_ratio`、`image_urls`、`video_urls`、`audio_urls`、`generate_audio`、`web_search`、`video_extend`、`all_in_one_reference` 和 `negative_prompt`。

已接入模型包括 Seedance 2.0 Mini、Seedance 2.0 Fast、Seedance 2.0、
Seedance 2.5 Reference、Wan 3.0 和 Minimax H3。可通过 `GET /v1/models`
读取每个模型的时长、分辨率、画幅和素材数量限制；外部别名可在设置中的
`AK Model Map` 配置。

状态包括 `queued`、`preparing`、`submitted`、`running`、`succeeded`、`failed`、`expired`。成功时 `data[].url` 为视频地址；失败时对外返回稳定中文错误，上游原始错误只在控制台任务详情中显示。

## 账号同步

`POST /api/accounts/sync` 使用 `AK_SYNC_TOKEN`，支持同步 `email`、`password`、`cookie_header`、`cookie_records`、`proxy_url`、`max_concurrency` 和 `auto_login`。相同邮箱更新现有账号，不重复创建。
