# Akool 协议链路摘要

实现依据为 2026-08-27 原生 CDP 记录，不使用 Playwright。敏感 Cookie、JWT、上传签名和账密不进入仓库。

1. 浏览器在 `akool.com` 登录，网站脚本提交账密、动态 Turnstile token 和浏览器信息。
2. `GET /interface/user-api/api/v6/verify/user` 校验 Cookie 会话。
3. `GET /interface/faceswap-api/api/v1/faceswap/user/info` 获取 `credit` 与 `lock_credit`。
4. 素材链路为 upload signature、S3 PUT、profile create。
5. `POST /interface/content-api/api/v7/content/calculateFee` 动态询价，固定积分模式。
6. `POST /interface/content-api/api/v7/content/image2Video/createBySourcePrompt/batch` 提交。
7. `GET /interface/content-api/api/v6/content/resourceResult/list` 按资源 `_id` 轮询，`video_status` 1/2/3 分别为提交、处理、完成。
8. 完成结果优先读取 `external_video`，其次 `video`。

含视频素材的全能参考请求必须同时携带 `prompt_info(type=video)`、提示词中的
`{{video_profile_id}}` 引用和 `videoUrl`。`videoUrl` 在单视频时为字符串，多个
视频时为数组；不要把任务结果中的 `reference_video_urls` 当作提交字段。
Seedance 2.5 使用 `doubao/seedance-2-5/reference-to-video`，并携带
`video_extend=false`、`all_in_one_reference=true`。其素材上限为 30 图、10 视频、
10 音频，视频与音频累计时长分别不超过 30 秒。

2026-08-30 的补充 CDP 记录确认：

- Wan 3.0 使用 `alibaba/wan-3.0/image-to-video`，分辨率值保持为
  `480P`、`720P`、`1080P`，提交包含 `audio_type=1`、
  `all_in_one_reference=true` 和 `generate_audio=true`。
- Minimax H3 使用 `minimax/h3/reference-to-video`，而不是
  `minimax/h3/image-to-video`。分辨率为 `768P` 或 `2k`，提交包含
  `audio_type=3`，不传 `generate_audio`、`all_in_one_reference`、`web_search`，
  但图片、视频和参考音频仍通过 `imageUrl`、`videoUrl`、
  `reference_audio_urls` 传递。
- Seedance 2.0 即使包含视频素材，仍使用
  `doubao-seedance-2-0-260128/image-to-video`；标准版和 Fast 版提交
  `web_search=true`。
- UI 的自适应画幅不会提交 `ratio` 字段；指定画幅时才提交实际比例。
- `calculateFee` 中素材 ID 分别放入 `image_profile_ids` 与
  `video_profile_ids`，没有对应素材时省略该字段。

提交接口返回业务码 `1104`（`your credits is not enough`）时，当前账号的余额
快照已不可信。任务会刷新该账号余额、释放并发槽位与预扣额度、排除该账号，
随后在新账号下重新上传素材并再次询价、提交。

SSE 在记录中出现 HTTP/2 协议错误，因此实现以资源列表轮询为准。提交接口 HTTP 200 仍需检查业务 `code == 1000` 和非空 `successList`。

## 查询限流与账号并发

任务取得资源 ID 后，查询接口的 HTTP 429、业务限流消息（例如
`Too many requests from your IP, please retry after 28 seconds`），以及临时网络或
HTTP 服务端错误都不代表生成失败。任务保持运行，保留原账号并发槽位和积分预扣，
按 `Retry-After` 或消息中的等待秒数重试查询；连续查询错误会退避，仍受任务总超时限制。
重试查询不会重新提交生成。生成结果明确失败时，继续按失败处理。

历史上被限流误判失败且保留了账号和资源 ID 的任务，点击重试会恢复原任务查询。
提交接口本身不自动重放，以免重复创建任务。

账号槽位按任务持久化，重复获取或释放不会重复增减并发计数。启动时先恢复已有任务的
占用，再调度新任务；已提交任务继续使用原账号，即使账号后来禁用或并发上限降低。
账号同步未指定 `max_concurrency` 时保留已有设置。

## 每日签到

2026-09-05 原生 CDP 记录确认签到链路：

1. `GET /interface/faceswap-api/api/v6/content/sign/stats` 查询当天状态。
2. 仅在 `today_signed=false` 且 `can_checkedin=true` 时，无请求体调用
   `POST /interface/faceswap-api/api/v6/content/sign`。
3. 签到成功后重新调用账号信息接口，刷新 `credit`、`sign_reward_stats` 和
   `last_daily_credit_add_time`。

签到 POST 不自动重放。实现先以 UTC 日期和本地保存的 `last_sign` 去重，再以上游状态为准，
并复用账号 Cookie 与固定代理。低余额自动禁用判断在签到及余额刷新之后执行，避免尚可领取奖励的账号被提前禁用。
