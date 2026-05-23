# B站 4K 播放与编解码器优化总结

## 1. 核心问题：4K AVC (H.264) 解码崩溃
* **表现**：解析 4K (qn=120) 视频后，浏览器成功加载 `4096x1716` 元数据，但在 MSE 中进行 demux/decode 时，播放 1 秒即崩溃，并抛出错误 `PipelineStatus::CHUNK_DEMUXER_ERROR_APPEND_FAILED: Failed to prepare video sample for decode`。
* **原因**：Chrome 等浏览器在 MSE 中处理高分辨率 (4K) 的 AVC/H.264 (即 `avc1.640033` Level 5.2) 时，会超出解码器的硬件/软件限制，导致解复用器和解码器崩溃。4K 播放通常必须使用 AV1 (`av01`) 或 HEVC (`hev1`) 编码。

## 2. 解决方案：动态客户端编解码适配与画质锁定

### 2.1 前端：支持编码实时检测
* 在 [static/index.html](static/index.html) 中，当初始化 `dash.js` 播放器前，使用标准 HTML5 MSE API 检测浏览器对各个编解码器的支持情况：
  ```javascript
  const supported = [];
  if (MediaSource.isTypeSupported('video/mp4; codecs="av01.0.00M.08"')) supported.push('av01');
  if (MediaSource.isTypeSupported('video/mp4; codecs="hev1.1.6.L150.90"')) supported.push('hev1');
  if (MediaSource.isTypeSupported('video/mp4; codecs="avc1.640033"')) supported.push('avc1');
  ```
* 将检测出的编码格式列表作为 query 参数拼接到 MPD URL 后发送（如 `/api/proxy/bilibili/mpd/{mpd_id}?codecs=av01,hev1,avc1`）。

### 2.2 后端：动态按需构建 MPD
* 在 [vendors/bilibili.py](vendors/bilibili.py) 中，`bili_mpd_cache` 缓存不再直接保存预生成的 MPD XML 文本，而是保存原始的 `play_info`、用户要求的最高画质 `max_qn` 等上下文信息。
* 当客户端请求 MPD 时，后端根据传入的 `codecs` 参数，过滤出该浏览器支持的流，并按优先级排序：**AV1 (`av01`) -> HEVC (`hev1`) -> AVC (`avc1`)**。
  * **AV1**：Chrome/Edge 等浏览器兼容性极好（支持软件解码且带宽消耗极省）。
  * **HEVC**：Safari 以及开启了硬件加速的 Chromium 支持优异。
  * **AVC**：最坏情况下的兜底备份。

### 2.3 画质强锁定与防降级
* 在筛选视频流时，强制锁定在当前过滤后的最高画质 `target_qn` 上，只向 MPD 输出当前画质的单个 Representation。
* **效果**：不仅彻底解决了 `dash.js` 因为 ABR 算法自动降画质到 360P/480P 的问题，还完美解决了多端同步房间中因为不同用户设备编码兼容性不同导致的起播失败，实现了“千人千面，全员满画质，按需择编码”。
