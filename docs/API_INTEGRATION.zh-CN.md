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
Seedance 2.5 Reference、Wan 3.0、Minimax H3、Kling 4.0 与 Vidu Q4。可通过 `GET /v1/models`
读取每个模型的时长、分辨率、画幅和素材数量限制；外部别名可在设置中的
`AK Model Map` 配置。

Wan 3.0 的输出时长 `duration` 支持 4～30 秒的整数值，包含 16～30 秒。

新模型调用名为 `kling-4.0`、`vidu-q4`，并兼容对应上游 reference 模型名：

| 模型 | 输出时长 | 分辨率 | 参考素材 |
| --- | --- | --- | --- |
| kling-4.0 | 3～30 秒 | 720p / 1080p | 最多10图、5视频、3音频，合计15项 |
| vidu-q4 | 3～16 秒 | 540p / 720p / 1080p / 2k / 4k | 最多12图、3音频，合计15项；不支持视频参考 |

Kling 音频每条 5～30 秒，视频每条至少3秒且合计不超过30秒；Vidu 音频仅MP3、每条3～12秒。
多条音频逐条校验，不把音频时长相加作为上述上限。Kling 默认音画同出，不向上游发送其不支持的音频开关；参考音频单独传递。
未指定参数时 Kling 默认10秒/720p/自适应，Vidu 默认5秒/720p/16:9。Vidu 支持无图请求，不插入合成黑图。

提交成功后固定查询已受理的资源，不因 SSE 断开或暂未查询到资源而重提。
每轮先读取最近记录，再限量续查历史游标；连续查询能够越过原来的最近100条边界。
查询状态与生成状态分开记录，素材格式、时长和内容审核错误分别归一化。

状态包括 `queued`、`preparing`、`submitted`、`running`、`succeeded`、`failed`、`expired`。成功时 `data[].url` 为视频地址；失败时对外返回稳定中文错误，上游原始错误只在控制台任务详情中显示。

## 账号同步

账号登录并验证身份后，系统会在该账号的 Akool 浏览器中预确认内容上传提示，并回读确认结果。
官网机制是浏览器本地的 `akool-generation-consent=true`，没有后台确认接口或服务端协议版本。
已确认的浏览器不会重复写入。`tos_hash`、`tos_accepted_at` 仅记录本地机制摘要与确认时间，不代表服务器协议回执。
预确认不点击生成按钮，也不执行弹窗 Continue 所带的恢复生成动作；失败时账号保持协议待确认状态。
已经受理的任务仍可继续查询，不会因本地提示的状态变化而重新提交。

控制台账号操作中的“人工验证”通过原生 CDP 操作原有浏览器，可手动完成 Cloudflare
验证后保存会话。对应 `/api/accounts/{id}/browser` 系列接口只允许管理会话访问，
截图不缓存，不对外暴露 Cookie、令牌或任意脚本执行能力。

`POST /api/accounts/sync` 使用 `AK_SYNC_TOKEN`，支持同步 `email`、`password`、`cookie_header`、`cookie_records`、`proxy_url`、`max_concurrency` 和 `auto_login`。相同邮箱更新现有账号，不重复创建。
