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

SSE 在记录中出现 HTTP/2 协议错误，因此实现以资源列表轮询为准。提交接口 HTTP 200 仍需检查业务 `code == 1000` 和非空 `successList`。
