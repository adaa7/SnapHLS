# HLS 预览与缩略图截取工具

本工具用于浏览包含 HLS 目录结构的视频资源，播放 `playlist.m3u8` 并将当前帧截图覆盖为 `thumbnail.jpg`。

## 安装依赖（Windows）

1. 安装 VLC 播放器（系统级）：
   - 访问 `https://www.videolan.org/vlc/` 下载并安装（默认路径）。
2. 安装 Python 依赖：
   ```bash
   pip install -r requirements.txt
   ```

## 运行

```bash
python main.py
```

首次运行将读取同目录的 `Config.json`。可在应用中点击“选择根目录”指向资源根目录。

## 目录要求（示例）

```
video
├── id_xxx_标题
│   ├── cover.jpg
│   ├── XiaoYing_Video_..._hls
│   │   ├── playlist0.ts
│   │   ├── playlist1.ts
│   │   └── playlist.m3u8
│   └── ...
```

- `_hls` 目录内须包含 `playlist.m3u8`（可在 `Config.json` 自定义文件名）。
- 截图会保存为 `_hls/thumbnail.jpg`，可在 `Config.json` 修改为其它文件名。

## 常见问题

- 若无法播放或截图，请确认：
  - 已安装 VLC（桌面应用）。
  - 已安装 `python-vlc` 和 `PySide6` 依赖。
  - 选择了正确的 `_hls` 目录，且 `playlist.m3u8` 存在。

## 自定义配置

编辑 `Config.json`：
- `root_directory`：资源根目录
- `theme`: `light`/`dark`
- `vlc_snapshot_width`/`vlc_snapshot_height`：VLC 截图尺寸（0 表示原始大小）
- `snapshot_filename`/`cover_filename`/`first_frame_filename`：文件名约定
- `m3u8_filename`：m3u8 文件名（默认 `playlist.m3u8`）
- `accepted_video_dir_suffix`：HLS 目录后缀（默认 `_hls`）

## 扩展点

- 可在 `MainWindow` 中新增菜单/动作，集成 FTP 下载/同步逻辑（`ftp` 字段已预留）。
- 可增加批量处理、关键帧提取、自动封面等功能。

