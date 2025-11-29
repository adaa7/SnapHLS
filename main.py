import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from PySide6 import QtCore, QtGui, QtWidgets

try:
    import vlc  # python-vlc
except Exception:  # noqa: BLE001
    vlc = None
try:
    from ftplib import FTP, error_perm
except Exception:  # noqa: BLE001
    FTP = None  # type: ignore
    error_perm = Exception  # type: ignore


class ConfigManager:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.data = {
            "root_directory": str(Path.cwd()),
            "theme": "light",
            "vlc_snapshot_width": 0,
            "vlc_snapshot_height": 0,
            "snapshot_filename": "thumbnail.jpg",
            "cover_filename": "cover.jpg",
            "first_frame_filename": "first_frame.jpg",
            "m3u8_filename": "playlist.m3u8",
            "accepted_video_dir_suffix": "_hls",
            "ftp": {
                "enabled": False,
                "host": "",
                "port": 21,
                "username": "",
                "password": "",
                "base_path": "",
            },
        }
        self.load()

    def load(self) -> None:
        if self.config_path.exists():
            with self.config_path.open("r", encoding="utf-8") as f:
                try:
                    loaded = json.load(f)
                    self.data.update(loaded)
                except json.JSONDecodeError:
                    pass

    def save(self) -> None:
        with self.config_path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get(self, key: str, default=None):  # type: ignore[no-untyped-def]
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:  # type: ignore[no-untyped-def]
        self.data[key] = value
        self.save()


class HlsPlayer(QtCore.QObject):
    positionChanged = QtCore.Signal(float)  # 0..1
    lengthChanged = QtCore.Signal(int)  # ms
    stateChanged = QtCore.Signal(str)
    endReached = QtCore.Signal()  # 播放结束信号

    def __init__(self, video_widget: QtWidgets.QWidget, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.video_widget = video_widget
        self.instance = None
        self.player = None
        self._end_emitted = False  # 播放结束标记
        if vlc is not None:
            # 添加 VLC 参数以优化 Direct3D11 兼容性和时间戳处理
            vlc_args = [
                "--intf", "dummy",  # 不使用图形界面
                "--no-video-title-show",  # 不显示视频标题
                "--quiet",  # 减少控制台输出
                "--no-audio-time-stretch",  # 禁用音频时间拉伸
                # HLS 播放优化
                "--live-caching=1000",  # 增加直播缓存（毫秒），提高稳定性
                "--network-caching=1000",  # 网络缓存
                "--http-reconnect",  # 自动重连
                # 时间戳和同步相关优化
                "--clock-jitter=0",  # 减少时钟抖动
                "--clock-synchro=0",  # 时钟同步方式
                "--avcodec-skiploopfilter=0",  # 不跳过循环滤波
                "--avcodec-skip-frame=0",  # 不跳过帧
                "--avcodec-skip-idct=0",  # 不跳过IDCT
                # Direct3D11 相关优化
                "--directx-hw-yuv",  # 启用硬件YUV转换
            ]
            self.instance = vlc.Instance(vlc_args)
            self.player = self.instance.media_player_new()
            self._attach_output()
            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self._poll)
            self._timer.start(200)

    def _attach_output(self) -> None:
        if self.player is None:
            return
        win_id = int(self.video_widget.winId())
        if sys.platform.startswith("win"):
            self.player.set_hwnd(win_id)
        elif sys.platform == "darwin":
            self.player.set_nsobject(win_id)
        else:
            self.player.set_xwindow(win_id)

    def open(self, m3u8_path: str) -> None:
        if self.player is None or self.instance is None:
            return
        # 先停止并释放旧媒体
        self.player.stop()
        self.player.set_media(None)
        # 加载新媒体
        media = self.instance.media_new(m3u8_path)
        self.player.set_media(media)
        self.play()
        # 延迟一段时间后获取长度（等待媒体加载完成）
        QtCore.QTimer.singleShot(500, lambda: self.lengthChanged.emit(self.get_length()))

    def play(self) -> None:
        if self.player is None:
            return
        self.player.play()
        self.stateChanged.emit("playing")

    def pause(self) -> None:
        if self.player is None:
            return
        self.player.pause()
        self.stateChanged.emit("paused")

    def stop(self) -> None:
        if self.player is None:
            return
        self.player.stop()
        # 停止时释放媒体资源
        if self.player:
            self.player.set_media(None)
        self.stateChanged.emit("stopped")

    def get_position(self) -> float:
        """获取当前播放位置（0.0 到 1.0）"""
        if self.player is None:
            return 0.0
        try:
            return float(self.player.get_position())
        except Exception:
            return 0.0
    
    def get_length(self) -> int:
        """获取视频总长度（毫秒）"""
        if self.player is None:
            return 0
        try:
            return int(self.player.get_length())
        except Exception:
            return 0
    
    def set_position(self, pos01: float) -> None:
        if self.player is None:
            return
        self.player.set_position(max(0.0, min(1.0, pos01)))
    
    def set_rate(self, rate: float) -> None:
        """设置播放速率（倍数）"""
        if self.player is None:
            return
        # VLC 支持 0.25 到 4.0 之间的播放速率
        rate = max(0.25, min(4.0, rate))
        self.player.set_rate(rate)
    
    def get_rate(self) -> float:
        """获取当前播放速率"""
        if self.player is None:
            return 1.0
        try:
            return self.player.get_rate()
        except Exception:
            return 1.0

    def snapshot(self, filepath: str, width: int = 0, height: int = 0) -> bool:
        if self.player is None:
            return False
        # video_take_snapshot(num, path, width, height)
        return self.player.video_take_snapshot(0, filepath, width, height) == 0

    def _poll(self) -> None:
        if self.player is None:
            return
        try:
            length = int(self.player.get_length())
            pos = float(self.player.get_position())
            self.lengthChanged.emit(length)
            if 0.0 <= pos <= 1.0:
                self.positionChanged.emit(pos)
                # 检测播放是否结束（position >= 0.99 表示接近结束）
                if pos >= 0.99 and not hasattr(self, "_end_emitted"):
                    self._end_emitted = True
                    QtCore.QTimer.singleShot(500, self.endReached.emit)  # 延迟500ms确保播放完成
        except Exception:  # noqa: BLE001
            pass
    
    def open(self, m3u8_path: str) -> None:
        if self.player is None or self.instance is None:
            return
        # 重置结束标记
        self._end_emitted = False
        
        # 先停止播放（异步执行，避免阻塞）
        try:
            self.player.stop()
            # 使用定时器异步等待停止完成，避免阻塞
            def load_new_media() -> None:
                try:
                    # 释放旧媒体
                    self.player.set_media(None)  # type: ignore[assignment]
                    
                    # 加载新媒体，添加播放选项以优化时间戳处理
                    media = self.instance.media_new(m3u8_path)  # type: ignore[attr-defined]
                    # 设置媒体选项以优化 HLS 播放和时间戳同步
                    media.add_options(
                        ":live-caching=1000",  # 缓存时间
                        ":network-caching=1000",  # 网络缓存
                        ":http-reconnect",  # 自动重连
                        ":hls-segment-threads=3",  # HLS 分段线程数
                        ":hls-segment-attempts=3",  # HLS 分段重试次数
                        ":hls-timeout=2000000",  # HLS 超时时间（微秒）
                        ":no-audio-time-stretch",  # 禁用音频时间拉伸
                        ":avcodec-dr",  # 禁用硬件解码回退到软件解码（解决时间戳问题）
                    )
                    self.player.set_media(media)  # type: ignore[assignment]
                    self.play()
                    # 延迟一段时间后获取长度（等待媒体加载完成）
                    QtCore.QTimer.singleShot(500, lambda: self.lengthChanged.emit(self.get_length()))
                except Exception:
                    pass  # 忽略加载错误，避免崩溃
            
            # 延迟一小段时间确保停止完成，然后加载新媒体
            QtCore.QTimer.singleShot(150, load_new_media)
        except Exception:
            pass  # 如果停止失败，继续尝试加载


class ImagePreview(QtWidgets.QWidget):
    def __init__(self, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setMinimumWidth(260)
        
        # 标题标签样式
        title_style = "font-weight: bold; color: #333; font-size: 13px; padding: 4px 0px;"
        
        self.cover_label = QtWidgets.QLabel("未加载")
        self.thumb_label = QtWidgets.QLabel("未加载")
        for lab in (self.cover_label, self.thumb_label):
            lab.setAlignment(QtCore.Qt.AlignCenter)
            lab.setStyleSheet(
                """
                border: 2px solid #ddd;
                border-radius: 8px;
                background: #f5f5f5;
                color: #999;
                font-size: 12px;
                """
            )
            lab.setFixedHeight(180)
            lab.setScaledContents(False)
        
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        
        cover_title = QtWidgets.QLabel("封面 (Cover)")
        cover_title.setStyleSheet(title_style)
        layout.addWidget(cover_title)
        layout.addWidget(self.cover_label)
        
        thumb_title = QtWidgets.QLabel("缩略图 (Thumbnail)")
        thumb_title.setStyleSheet(title_style)
        layout.addWidget(thumb_title)
        layout.addWidget(self.thumb_label)

    def set_image(self, label: QtWidgets.QLabel, path: Path) -> None:
        if path.exists():
            try:
                pix = QtGui.QPixmap(str(path))
                if not pix.isNull():
                    label.setPixmap(pix.scaled(label.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
                    label.setText("")
                else:
                    label.setText("图片加载失败")
            except Exception:
                label.setText("图片加载失败")
        else:
            label.setText("文件不存在\n(未找到)")

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        # Rescale current pixmaps
        for lab in (self.cover_label, self.thumb_label):
            pix = lab.pixmap()
            if pix is not None and not pix.isNull():
                lab.setPixmap(pix.scaled(lab.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))


class SettingsDialog(QtWidgets.QDialog):
    """设置对话框"""
    def __init__(self, cfg: ConfigManager, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("设置")
        self.setModal(True)
        self.resize(500, 400)
        
        layout = QtWidgets.QVBoxLayout(self)
        
        # 标签页
        tabs = QtWidgets.QTabWidget()
        
        # FTP 设置标签页
        ftp_tab = QtWidgets.QWidget()
        ftp_layout = QtWidgets.QFormLayout(ftp_tab)
        ftp_layout.setSpacing(12)
        
        self.ftp_host = QtWidgets.QLineEdit()
        self.ftp_host.setText(cfg.get("ftp", {}).get("host", ""))
        ftp_layout.addRow("主机:", self.ftp_host)
        
        self.ftp_port = QtWidgets.QSpinBox()
        self.ftp_port.setRange(1, 65535)
        self.ftp_port.setValue(int(cfg.get("ftp", {}).get("port", 21)))
        ftp_layout.addRow("端口:", self.ftp_port)
        
        self.ftp_username = QtWidgets.QLineEdit()
        self.ftp_username.setText(cfg.get("ftp", {}).get("username", ""))
        ftp_layout.addRow("用户名:", self.ftp_username)
        
        self.ftp_password = QtWidgets.QLineEdit()
        self.ftp_password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ftp_password.setText(cfg.get("ftp", {}).get("password", ""))
        ftp_layout.addRow("密码:", self.ftp_password)
        
        self.ftp_base_path = QtWidgets.QLineEdit()
        self.ftp_base_path.setText(cfg.get("ftp", {}).get("base_path", ""))
        ftp_layout.addRow("基础路径:", self.ftp_base_path)
        
        tabs.addTab(ftp_tab, "FTP 设置")
        
        # 下载设置标签页
        download_tab = QtWidgets.QWidget()
        download_layout = QtWidgets.QFormLayout(download_tab)
        download_layout.setSpacing(12)
        
        self.preview_duration = QtWidgets.QSpinBox()
        self.preview_duration.setRange(0, 3600)
        self.preview_duration.setSuffix(" 秒")
        self.preview_duration.setValue(int(cfg.get("preview_duration", 30)))
        self.preview_duration.setToolTip("下载视频时只下载前 N 秒用于预览。设置为 0 则下载完整视频。")
        download_layout.addRow("预览下载时长:", self.preview_duration)
        
        self.download_limit_label = QtWidgets.QLabel("（设置为 0 则下载完整视频）")
        self.download_limit_label.setStyleSheet("color: #666; font-size: 11px;")
        download_layout.addRow("", self.download_limit_label)
        
        self.auto_clean_cache = QtWidgets.QCheckBox()
        self.auto_clean_cache.setChecked(bool(cfg.get("auto_clean_cache", True)))
        self.auto_clean_cache.setToolTip("开启后，每次播放新视频前自动清理旧的缓存文件，避免磁盘空间占用过多")
        download_layout.addRow("自动清理缓存:", self.auto_clean_cache)
        
        self.multi_thread_download = QtWidgets.QCheckBox()
        self.multi_thread_download.setChecked(bool(cfg.get("multi_thread_download", False)))
        self.multi_thread_download.setToolTip("开启后，使用多线程并发下载 FTP 文件，可大幅提高下载速度（需要 FTP 服务器支持并发连接）")
        download_layout.addRow("多线程下载:", self.multi_thread_download)
        
        self.max_cache_dirs = QtWidgets.QSpinBox()
        self.max_cache_dirs.setRange(1, 50)
        self.max_cache_dirs.setValue(int(cfg.get("max_cache_dirs", 5)))
        self.max_cache_dirs.setSuffix(" 个目录")
        self.max_cache_dirs.setToolTip("保留的最大缓存目录数量，超过此数量会自动清理最旧的缓存")
        download_layout.addRow("最大缓存数量:", self.max_cache_dirs)
        
        tabs.addTab(download_tab, "下载设置")
        
        # 显示设置标签页
        display_tab = QtWidgets.QWidget()
        display_layout = QtWidgets.QFormLayout(display_tab)
        display_layout.setSpacing(12)
        
        self.filter_id_dirs = QtWidgets.QCheckBox()
        self.filter_id_dirs.setChecked(bool(cfg.get("filter_id_dirs", False)))
        self.filter_id_dirs.setToolTip("开启后，在 id_xx 目录下只显示以 _hls 结尾的目录，不显示 cover.jpg 等其他文件")
        display_layout.addRow("ID 目录只显示 HLS:", self.filter_id_dirs)
        
        filter_help = QtWidgets.QLabel("（例如：id_11_七月喵子-白丝写真27P2V 目录下只显示 *_hls 目录，隐藏 cover.jpg 等文件）")
        filter_help.setStyleSheet("color: #666; font-size: 11px; padding-left: 20px;")
        filter_help.setWordWrap(True)
        display_layout.addRow("", filter_help)
        
        self.show_only_id_folders = QtWidgets.QCheckBox()
        self.show_only_id_folders.setChecked(bool(cfg.get("show_only_id_folders", False)))
        self.show_only_id_folders.setToolTip("开启后，在根目录或 video 目录下只显示以 id_ 开头的文件夹，隐藏其他文件或文件夹")
        display_layout.addRow("只显示 ID 文件夹:", self.show_only_id_folders)
        
        id_folder_help = QtWidgets.QLabel("（例如：video 目录下只显示 id_10_...、id_11_... 等文件夹，隐藏其他内容）")
        id_folder_help.setStyleSheet("color: #666; font-size: 11px; padding-left: 20px;")
        id_folder_help.setWordWrap(True)
        display_layout.addRow("", id_folder_help)
        
        # 播放完成后的行为设置
        self.play_on_end = QtWidgets.QComboBox()
        self.play_on_end.addItems(["重新播放", "播放下一个视频"])
        play_on_end_value = cfg.get("play_on_end", "重新播放")
        if play_on_end_value == "播放下一个视频":
            self.play_on_end.setCurrentIndex(1)
        else:
            self.play_on_end.setCurrentIndex(0)
        self.play_on_end.setToolTip("视频播放完成后的操作：重新播放当前视频或自动播放下一个视频")
        display_layout.addRow("播放完成后:", self.play_on_end)
        
        tabs.addTab(display_tab, "显示设置")
        
        # 快捷键设置标签页
        shortcuts_tab = QtWidgets.QWidget()
        shortcuts_layout = QtWidgets.QFormLayout(shortcuts_tab)
        shortcuts_layout.setSpacing(12)
        
        # 默认快捷键
        default_shortcuts = {
            "play": "Space",
            "pause": "Space",
            "stop": "S",
            "snapshot": "P",
            "snapshot_cover": "C",
            "settings": "F1",
            "speed_up": "E",
            "speed_down": "Q",
            "speed_reset": "W",
        }
        
        # 从配置中读取快捷键，如果没有则使用默认值
        shortcuts_cfg = cfg.get("shortcuts", {})
        
        self.shortcut_play = QtWidgets.QKeySequenceEdit()
        play_seq = shortcuts_cfg.get("play", default_shortcuts["play"])
        self.shortcut_play.setKeySequence(QtGui.QKeySequence(play_seq))
        shortcuts_layout.addRow("播放:", self.shortcut_play)
        
        self.shortcut_pause = QtWidgets.QKeySequenceEdit()
        pause_seq = shortcuts_cfg.get("pause", default_shortcuts["pause"])
        self.shortcut_pause.setKeySequence(QtGui.QKeySequence(pause_seq))
        shortcuts_layout.addRow("暂停:", self.shortcut_pause)
        
        self.shortcut_stop = QtWidgets.QKeySequenceEdit()
        stop_seq = shortcuts_cfg.get("stop", default_shortcuts["stop"])
        self.shortcut_stop.setKeySequence(QtGui.QKeySequence(stop_seq))
        shortcuts_layout.addRow("停止:", self.shortcut_stop)
        
        self.shortcut_snapshot = QtWidgets.QKeySequenceEdit()
        snapshot_seq = shortcuts_cfg.get("snapshot", default_shortcuts["snapshot"])
        self.shortcut_snapshot.setKeySequence(QtGui.QKeySequence(snapshot_seq))
        shortcuts_layout.addRow("截图:", self.shortcut_snapshot)
        
        self.shortcut_snapshot_cover = QtWidgets.QKeySequenceEdit()
        snapshot_cover_seq = shortcuts_cfg.get("snapshot_cover", default_shortcuts["snapshot_cover"])
        self.shortcut_snapshot_cover.setKeySequence(QtGui.QKeySequence(snapshot_cover_seq))
        shortcuts_layout.addRow("截图封面:", self.shortcut_snapshot_cover)
        
        self.shortcut_settings = QtWidgets.QKeySequenceEdit()
        settings_seq = shortcuts_cfg.get("settings", default_shortcuts["settings"])
        self.shortcut_settings.setKeySequence(QtGui.QKeySequence(settings_seq))
        shortcuts_layout.addRow("设置:", self.shortcut_settings)
        
        # 播放速度控制快捷键
        self.shortcut_speed_up = QtWidgets.QKeySequenceEdit()
        speed_up_seq = shortcuts_cfg.get("speed_up", default_shortcuts["speed_up"])
        self.shortcut_speed_up.setKeySequence(QtGui.QKeySequence(speed_up_seq))
        shortcuts_layout.addRow("加快播放速度:", self.shortcut_speed_up)
        
        self.shortcut_speed_down = QtWidgets.QKeySequenceEdit()
        speed_down_seq = shortcuts_cfg.get("speed_down", default_shortcuts["speed_down"])
        self.shortcut_speed_down.setKeySequence(QtGui.QKeySequence(speed_down_seq))
        shortcuts_layout.addRow("减慢播放速度:", self.shortcut_speed_down)
        
        self.shortcut_speed_reset = QtWidgets.QKeySequenceEdit()
        speed_reset_seq = shortcuts_cfg.get("speed_reset", default_shortcuts["speed_reset"])
        self.shortcut_speed_reset.setKeySequence(QtGui.QKeySequence(speed_reset_seq))
        shortcuts_layout.addRow("重置播放速度:", self.shortcut_speed_reset)
        
        shortcuts_help = QtWidgets.QLabel("提示：点击输入框后按下键盘组合键即可设置快捷键")
        shortcuts_help.setStyleSheet("color: #666; font-size: 11px; padding-top: 10px;")
        shortcuts_help.setWordWrap(True)
        shortcuts_layout.addRow("", shortcuts_help)
        
        tabs.addTab(shortcuts_tab, "快捷键")
        
        # 其他设置标签页
        other_tab = QtWidgets.QWidget()
        other_layout = QtWidgets.QFormLayout(other_tab)
        other_layout.setSpacing(12)
        
        # 主题设置
        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItems(["明亮", "暗黑"])
        theme_value = cfg.get("theme", "light")
        if theme_value == "dark":
            self.theme_combo.setCurrentIndex(1)
        else:
            self.theme_combo.setCurrentIndex(0)
        self.theme_combo.setToolTip("选择软件界面主题：明亮或暗黑")
        other_layout.addRow("界面主题:", self.theme_combo)
        
        self.start_minimized = QtWidgets.QCheckBox()
        self.start_minimized.setChecked(bool(cfg.get("start_minimized", False)))
        self.start_minimized.setToolTip("开启后，程序启动时自动最小化到系统托盘或任务栏")
        other_layout.addRow("启动时最小化:", self.start_minimized)
        
        self.cleanup_on_exit = QtWidgets.QCheckBox()
        self.cleanup_on_exit.setChecked(bool(cfg.get("cleanup_on_exit", True)))
        self.cleanup_on_exit.setToolTip("开启后，程序关闭时自动清理所有缓存文件")
        other_layout.addRow("关闭时清理缓存:", self.cleanup_on_exit)
        
        self.cleanup_script = QtWidgets.QLineEdit()
        cleanup_script_path = cfg.get("cleanup_script", "")
        self.cleanup_script.setText(cleanup_script_path)
        self.cleanup_script.setToolTip("程序关闭时运行的批处理脚本路径（可选，留空则不运行）")
        other_layout.addRow("清理脚本路径:", self.cleanup_script)
        
        script_help = QtWidgets.QLabel("（可选：指定 .bat 文件路径，程序关闭时自动运行该脚本）")
        script_help.setStyleSheet("color: #666; font-size: 11px; padding-left: 20px;")
        script_help.setWordWrap(True)
        other_layout.addRow("", script_help)
        
        tabs.addTab(other_tab, "其他设置")
        
        layout.addWidget(tabs)
        
        # 按钮
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
    
    def get_settings(self) -> dict:
        """获取设置值"""
        default_shortcuts = {
            "play": "Space",
            "pause": "Space",
            "stop": "S",
            "snapshot": "P",
            "snapshot_cover": "C",
            "settings": "F1",
            "speed_up": "E",
            "speed_down": "Q",
            "speed_reset": "W",
        }
        return {
            "ftp": {
                "host": self.ftp_host.text().strip(),
                "port": self.ftp_port.value(),
                "username": self.ftp_username.text().strip(),
                "password": self.ftp_password.text().strip(),
                "base_path": self.ftp_base_path.text().strip(),
            },
            "preview_duration": self.preview_duration.value(),
            "filter_id_dirs": self.filter_id_dirs.isChecked(),
            "show_only_id_folders": self.show_only_id_folders.isChecked(),
            "play_on_end": self.play_on_end.currentText(),
            "auto_clean_cache": self.auto_clean_cache.isChecked(),
            "multi_thread_download": self.multi_thread_download.isChecked(),
            "max_cache_dirs": self.max_cache_dirs.value(),
            "shortcuts": {
                "play": self.shortcut_play.keySequence().toString() or default_shortcuts["play"],
                "pause": self.shortcut_pause.keySequence().toString() or default_shortcuts["pause"],
                "stop": self.shortcut_stop.keySequence().toString() or default_shortcuts["stop"],
                "snapshot": self.shortcut_snapshot.keySequence().toString() or default_shortcuts["snapshot"],
                "snapshot_cover": self.shortcut_snapshot_cover.keySequence().toString() or default_shortcuts["snapshot_cover"],
                "settings": self.shortcut_settings.keySequence().toString() or default_shortcuts["settings"],
                "speed_up": self.shortcut_speed_up.keySequence().toString() or default_shortcuts["speed_up"],
                "speed_down": self.shortcut_speed_down.keySequence().toString() or default_shortcuts["speed_down"],
                "speed_reset": self.shortcut_speed_reset.keySequence().toString() or default_shortcuts["speed_reset"],
            },
            "theme": "dark" if self.theme_combo.currentText() == "暗黑" else "light",
            "start_minimized": self.start_minimized.isChecked(),
            "cleanup_on_exit": self.cleanup_on_exit.isChecked(),
            "cleanup_script": self.cleanup_script.text().strip(),
        }


class FtpListWorker(QtCore.QThread):
    """后台列出 FTP 目录的线程"""
    finished = QtCore.Signal(list)  # (目录列表)
    error = QtCore.Signal(str)  # 错误信息
    
    def __init__(self, ftp_cfg: dict, path: str) -> None:
        super().__init__()
        self.ftp_cfg = ftp_cfg  # 保存配置，在线程中创建新的FTP连接
        self.path = path
        self.ftp: "FtpHelper | None" = None
    
    def run(self) -> None:
        try:
            # 在线程中创建新的FTP连接，避免线程安全问题
            self.ftp = FtpHelper(self.ftp_cfg)
            if not self.ftp.connect():
                self.error.emit("FTP连接失败")
                return
            
            children = self.ftp.list_dir(self.path)
            self.finished.emit(children)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            # 清理FTP连接
            if self.ftp:
                self.ftp.disconnect()


class FtpPreviewWorker(QtCore.QThread):
    """后台下载 FTP 预览图片的线程"""
    cover_downloaded = QtCore.Signal(str)  # 封面图片路径
    thumb_downloaded = QtCore.Signal(str)  # 缩略图路径
    finished = QtCore.Signal()  # 完成信号
    
    def __init__(self, ftp_cfg: dict, remote_cover: str, local_cover: str,
                 remote_first_frame: str, local_first_frame: str,
                 remote_thumb: str, local_thumb: str) -> None:
        super().__init__()
        self.ftp_cfg = ftp_cfg  # 保存配置，在线程中创建新的FTP连接
        self.remote_cover = remote_cover
        self.local_cover = local_cover
        self.remote_first_frame = remote_first_frame
        self.local_first_frame = local_first_frame
        self.remote_thumb = remote_thumb
        self.local_thumb = local_thumb
        self.ftp: "FtpHelper | None" = None
    
    def run(self) -> None:
        try:
            # 在线程中创建新的FTP连接，避免线程安全问题
            self.ftp = FtpHelper(self.ftp_cfg)
            if not self.ftp.connect():
                self.finished.emit()
                return
            
            # 下载封面
            if self.ftp.exists(self.remote_cover):
                if self.ftp.download(self.remote_cover, self.local_cover):
                    self.cover_downloaded.emit(self.local_cover)
            
            # 优先尝试下载 first_frame.jpg
            if self.ftp.exists(self.remote_first_frame):
                if self.ftp.download(self.remote_first_frame, self.local_first_frame):
                    self.thumb_downloaded.emit(self.local_first_frame)
                    return
            # 如果没有 first_frame.jpg，则尝试下载 thumbnail.jpg
            if self.ftp.exists(self.remote_thumb):
                if self.ftp.download(self.remote_thumb, self.local_thumb):
                    self.thumb_downloaded.emit(self.local_thumb)
        except Exception:
            pass  # 下载失败不影响主流程
        finally:
            # 清理FTP连接
            if self.ftp:
                self.ftp.disconnect()
            self.finished.emit()


class FtpConnectWorker(QtCore.QThread):
    """后台连接 FTP 的线程"""
    connected = QtCore.Signal(object)  # FtpHelper 对象
    failed = QtCore.Signal(str)  # 错误信息
    
    def __init__(self, ftp_cfg: dict, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.ftp_cfg = ftp_cfg
    
    def run(self) -> None:
        try:
            ftp = FtpHelper(self.ftp_cfg)
            if ftp.connect():
                self.connected.emit(ftp)
            else:
                self.failed.emit("FTP 连接失败")
        except Exception as e:
            self.failed.emit(str(e))


class CacheCleanupWorker(QtCore.QThread):
    """后台清理缓存的线程"""
    finished = QtCore.Signal(int)  # 清理的目录数量
    
    def __init__(self, max_dirs: int, current_cache_dir: Path | None = None) -> None:
        super().__init__()
        self.max_dirs = max_dirs
        self.current_cache_dir = current_cache_dir
    
    def run(self) -> None:
        try:
            cleaned_count = 0
            tmp_dir = Path(tempfile.gettempdir())
            
            # 收集所有缓存目录
            cache_dirs = []
            for item in tmp_dir.iterdir():
                if item.is_dir() and item.name.startswith("hls_cache_"):
                    # 获取目录的修改时间
                    try:
                        mtime = item.stat().st_mtime
                        cache_dirs.append((mtime, item))
                    except Exception:
                        pass
            
            if not cache_dirs:
                self.finished.emit(0)
                return
            
            # 按修改时间排序（旧的在前）
            cache_dirs.sort(key=lambda x: x[0])
            
            # 如果超过最大数量，删除最旧的（但保留当前正在使用的）
            if len(cache_dirs) > self.max_dirs:
                to_delete = cache_dirs[: len(cache_dirs) - self.max_dirs]
                for _, cache_dir in to_delete:
                    # 不删除当前正在使用的缓存
                    if self.current_cache_dir and cache_dir == self.current_cache_dir:
                        continue
                    
                    try:
                        import shutil
                        shutil.rmtree(cache_dir, ignore_errors=True)
                        cleaned_count += 1
                    except Exception:
                        pass
            
            self.finished.emit(cleaned_count)
        except Exception:
            self.finished.emit(0)


class FtpDownloadWorker(QtCore.QThread):
    """后台下载 FTP HLS 文件的线程"""
    progress = QtCore.Signal(str)  # 当前下载的文件名
    finished = QtCore.Signal(str, int, bool)  # (本地目录, 下载数量, 是否成功)

    def __init__(self, ftp_cfg: dict, remote_dir: str, local_dir: Path, m3u8_name: str, preview_duration: int = 0, use_multi_thread: bool = False) -> None:
        super().__init__()
        self.ftp_cfg = ftp_cfg  # 保存配置，在线程中创建新的FTP连接
        self.remote_dir = remote_dir
        self.local_dir = local_dir
        self.m3u8_name = m3u8_name
        self.preview_duration = preview_duration  # 预览时长（秒），0 表示下载完整
        self.use_multi_thread = use_multi_thread  # 是否使用多线程下载
        self.ftp: "FtpHelper | None" = None

    def run(self) -> None:
        try:
            # 在线程中创建新的FTP连接，避免线程安全问题
            self.ftp = FtpHelper(self.ftp_cfg)
            if not self.ftp.connect():
                self.progress.emit("错误: FTP连接失败")
                self.finished.emit(str(self.local_dir), 0, False)
                return
            
            # 先下载 m3u8 文件以解析需要下载哪些 .ts 文件
            remote_m3u8 = f"{self.remote_dir.rstrip('/')}/{self.m3u8_name}"
            local_m3u8 = self.local_dir / self.m3u8_name
            if not self.ftp.download(remote_m3u8, str(local_m3u8)):
                self.progress.emit("错误: 无法下载 m3u8 文件")
                self.finished.emit(str(self.local_dir), 0, False)
                return
            
            # 解析 m3u8 文件，确定需要下载哪些 .ts 文件
            files_to_download = []
            if self.preview_duration > 0:
                # 需要限制下载时长，解析 m3u8（已包含 m3u8_name）
                files_to_download = self._parse_m3u8_for_preview(str(local_m3u8), self.preview_duration)
            else:
                # 下载所有文件：从 m3u8 文件中解析所有 .ts 文件名
                files_to_download = self._parse_all_files_from_m3u8(str(local_m3u8))
            
            downloaded = 0
            downloaded_ts_files = []
            failed_files = []
            
            # 检查是否有足够的文件需要下载（排除 m3u8）
            ts_files_count = len([name for name in files_to_download if name != self.m3u8_name])
            
            if self.use_multi_thread and ts_files_count > 0:
                # 使用多线程并发下载
                self.progress.emit(f"使用多线程下载 ({ts_files_count} 个文件)")
                downloaded, downloaded_ts_files, failed_files = self._download_multi_thread(files_to_download)
            else:
                # 单线程顺序下载
                if self.use_multi_thread:
                    self.progress.emit(f"多线程已开启但文件数不足，使用单线程下载")
                for name in files_to_download:
                    if name == self.m3u8_name:
                        continue  # m3u8 已下载，跳过
                    
                    remote_file = f"{self.remote_dir.rstrip('/')}/{name}"
                    local_file = self.local_dir / name
                    
                    # 先检查文件是否存在
                    if not self.ftp.exists(remote_file):
                        self.progress.emit(f"跳过: {name} (文件不存在)")
                        failed_files.append(name)
                        continue
                    
                    self.progress.emit(f"下载: {name}")
                    if self.ftp.download(remote_file, str(local_file)):
                        downloaded += 1
                        if name.endswith(".ts"):
                            downloaded_ts_files.append(name)
                    else:
                        failed_files.append(name)
                        self.progress.emit(f"失败: {name}")
            
            # 修改 m3u8 文件，只保留成功下载的片段（无论是否预览模式）
            if downloaded_ts_files:
                self._update_m3u8_for_preview(str(local_m3u8), downloaded_ts_files)
            
            # 如果有些文件下载失败，给出提示
            if failed_files:
                self.progress.emit(f"警告: {len(failed_files)} 个文件下载失败或不存在")
            
            success = (self.local_dir / self.m3u8_name).exists()
            self.finished.emit(str(self.local_dir), downloaded + 1, success)  # +1 包括 m3u8
        except Exception as e:
            self.progress.emit(f"错误: {e}")
            self.finished.emit(str(self.local_dir), 0, False)
        finally:
            # 清理FTP连接
            if self.ftp:
                self.ftp.disconnect()
    
    def _download_multi_thread(self, files_to_download: list[str]) -> tuple[int, list[str], list[str]]:
        """多线程并发下载文件"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        downloaded = 0
        downloaded_ts_files = []
        failed_files = []
        
        # 过滤掉 m3u8 文件（已下载）
        files_to_download_ts = [name for name in files_to_download if name != self.m3u8_name]
        
        # 先检查所有文件是否存在（单线程，避免过多连接）
        valid_files = []
        for name in files_to_download_ts:
            remote_file = f"{self.remote_dir.rstrip('/')}/{name}"
            if self.ftp.exists(remote_file):
                valid_files.append(name)
            else:
                self.progress.emit(f"跳过: {name} (文件不存在)")
                failed_files.append(name)
        
        if not valid_files:
            return downloaded, downloaded_ts_files, failed_files
        
        # 使用线程池并发下载（最多5个线程，避免过多连接）
        max_workers = min(5, len(valid_files))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 为每个文件创建独立的下载任务
            futures = {}
            for name in valid_files:
                remote_file = f"{self.remote_dir.rstrip('/')}/{name}"
                local_file = self.local_dir / name
                
                # 为每个下载任务创建新的 FTP 连接
                future = executor.submit(self._download_single_file, remote_file, str(local_file), name)
                futures[future] = name
            
            # 收集下载结果
            for future in as_completed(futures):
                name = futures[future]
                try:
                    success = future.result()
                    if success:
                        downloaded += 1
                        if name.endswith(".ts"):
                            downloaded_ts_files.append(name)
                        self.progress.emit(f"✓ {name}")
                    else:
                        failed_files.append(name)
                        self.progress.emit(f"✗ {name}")
                except Exception as e:
                    failed_files.append(name)
                    self.progress.emit(f"✗ {name}: {str(e)}")
        
        return downloaded, downloaded_ts_files, failed_files
    
    def _download_single_file(self, remote_file: str, local_file: str, filename: str) -> bool:
        """下载单个文件（在独立线程中运行，使用独立的 FTP 连接）"""
        try:
            # 为每个线程创建独立的 FTP 连接
            ftp = FtpHelper(self.ftp_cfg)
            if not ftp.connect():
                return False
            try:
                success = ftp.download(remote_file, local_file)
                return success
            finally:
                ftp.disconnect()
        except Exception:
            return False
    
    def _parse_all_files_from_m3u8(self, m3u8_path: str) -> list[str]:
        """从 m3u8 文件中解析所有 .ts 文件名"""
        files_to_download = [self.m3u8_name]  # 总是下载 m3u8 本身
        
        try:
            with open(m3u8_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                # 查找 #EXTINF 行
                if line.startswith("#EXTINF:"):
                    # 下一行应该是文件名
                    if i + 1 < len(lines):
                        filename = lines[i + 1].strip()
                        if filename and not filename.startswith("#") and filename.endswith(".ts"):
                            files_to_download.append(filename)
                    i += 2
                else:
                    i += 1
        except Exception:
            pass  # 解析失败，返回空列表（除了 m3u8）
        
        return files_to_download
    
    def _parse_m3u8_for_preview(self, m3u8_path: str, duration_limit: float) -> list[str]:
        """解析 m3u8 文件，返回需要下载的文件列表（仅前 N 秒）"""
        files_to_download = [self.m3u8_name]  # 总是下载 m3u8 本身
        total_duration = 0.0
        
        try:
            with open(m3u8_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                # 查找 #EXTINF 行（包含时长信息）
                if line.startswith("#EXTINF:"):
                    # 解析时长，格式通常是 #EXTINF:10.0, 或 #EXTINF:10,
                    duration_str = line.split(":")[1].split(",")[0]
                    try:
                        duration = float(duration_str)
                        # 下一行应该是文件名
                        if i + 1 < len(lines):
                            filename = lines[i + 1].strip()
                            if filename and not filename.startswith("#"):
                                total_duration += duration
                                if total_duration <= duration_limit:
                                    files_to_download.append(filename)
                                else:
                                    break  # 超过时长限制，停止
                        i += 2
                    except ValueError:
                        i += 1
                else:
                    i += 1
        except Exception:
            pass  # 解析失败，返回空列表（除了 m3u8）
        
        return files_to_download
    
    def _update_m3u8_for_preview(self, m3u8_path: str, downloaded_files: list[str]) -> None:
        """更新 m3u8 文件，只保留已下载的 .ts 文件"""
        try:
            with open(m3u8_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            # 创建已下载文件的集合，方便快速查找
            downloaded_set = set(downloaded_files)
            
            # 创建新的 m3u8 内容
            new_lines = []
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                
                # 保留所有非 #EXTINF 的注释和元数据
                if line.startswith("#") and not line.startswith("#EXTINF:"):
                    new_lines.append(lines[i])
                    i += 1
                elif line.startswith("#EXTINF:"):
                    # #EXTINF 行：检查下一行是否是已下载的文件
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        filename = next_line
                        # 如果是已下载的文件，保留 #EXTINF 行和文件名行
                        if filename in downloaded_set:
                            new_lines.append(lines[i])  # #EXTINF 行（保留原始换行符）
                            new_lines.append(lines[i + 1])  # 文件名行（保留原始换行符）
                        i += 2  # 跳过 #EXTINF 和文件名行
                    else:
                        # 没有下一行，只保留 #EXTINF 行
                        new_lines.append(lines[i])
                        i += 1
                else:
                    # 普通行（文件名行）
                    # 这种情况应该已经在 #EXTINF 处理中处理了，跳过
                    i += 1
            
            # 写入修改后的 m3u8
            with open(m3u8_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except Exception as e:
            # 修改失败不影响下载，但给出提示
            try:
                self.progress.emit(f"警告: 更新 m3u8 文件失败: {e}")
            except Exception:
                pass  # 如果信号发送失败也不影响


class FtpHelper(QtCore.QObject):
    connectedChanged = QtCore.Signal(bool)

    def __init__(self, cfg: dict, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.cfg = cfg
        self.ftp: FTP | None = None  # type: ignore[name-defined]
        self.encoding = "utf-8"

    def connect(self) -> bool:
        if FTP is None:
            return False
        try:
            self.ftp = FTP()
            self.ftp.encoding = self.encoding
            self.ftp.connect(self.cfg.get("host"), int(self.cfg.get("port", 21)), timeout=10)
            self.ftp.login(self.cfg.get("username"), self.cfg.get("password"))
            if self.cfg.get("base_path"):
                self.ftp.cwd(self.cfg.get("base_path"))
            self.connectedChanged.emit(True)
            return True
        except Exception:
            self.disconnect()
            self.connectedChanged.emit(False)
            return False

    def disconnect(self) -> None:
        try:
            if self.ftp is not None:
                self.ftp.quit()
        except Exception:
            pass
        self.ftp = None
        self.connectedChanged.emit(False)

    def list_dir(self, path: str) -> list[tuple[str, bool]]:
        # returns list of (name, is_dir)
        result: list[tuple[str, bool]] = []
        if self.ftp is None:
            return result
        cwd = self.pwd()
        try:
            self.ftp.cwd(path)
            # 先尝试使用 LIST 命令（支持详细信息）
            try:
                lines: list[str] = []
                self.ftp.retrlines("LIST", lines.append)
                for line in lines:
                    # POSIX-like LIST parsing
                    parts = line.split(maxsplit=8)
                    if len(parts) < 9:
                        continue
                    flags, name = parts[0], parts[-1]
                    is_dir = flags.startswith("d")
                    result.append((name, is_dir))
            except error_perm:
                # 如果 LIST 命令失败，尝试使用 NLST（仅文件名）
                try:
                    names: list[str] = []
                    self.ftp.retrlines("NLST", names.append)
                    # NLST 只能获取文件名，无法直接判断是文件还是目录
                    # 通过尝试切换目录来判断（但优化性能：只对可能是目录的名字尝试）
                    current_path = self.ftp.pwd()
                    for name in names:
                        if not name.strip():
                            continue
                        is_dir = False
                        # 如果名称看起来像目录（以特定后缀结尾），或者尝试切换判断
                        # 优化：对于常见的文件扩展名，直接判断为文件
                        name_lower = name.lower()
                        common_extensions = [".ts", ".m3u8", ".jpg", ".png", ".mp4", ".avi", ".mkv", ".mov", ".flv", ".webm"]
                        if any(name_lower.endswith(ext) for ext in common_extensions):
                            is_dir = False
                        else:
                            # 尝试切换到该路径，如果成功说明是目录
                            try:
                                self.ftp.cwd(name)
                                self.ftp.cwd(current_path)
                                is_dir = True
                            except Exception:
                                # 切换失败说明是文件
                                is_dir = False
                        result.append((name, is_dir))
                except Exception:
                    # 如果 NLST 也失败，尝试 MLSD（如果支持）
                    try:
                        lines: list[str] = []
                        self.ftp.retrlines("MLSD", lines.append)
                        for line in lines:
                            parts = line.split(None, 1)
                            if len(parts) >= 2:
                                facts, name = parts[0], parts[1]
                                is_dir = "type=dir" in facts or "Type=dir" in facts
                                result.append((name, is_dir))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                self.ftp.cwd(cwd)
            except Exception:
                pass
        return result

    def pwd(self) -> str:
        if self.ftp is None:
            return "/"
        try:
            return self.ftp.pwd()
        except Exception:
            return "/"

    def exists(self, remote_path: str) -> bool:
        if self.ftp is None:
            return False
        parent = os.path.dirname(remote_path).replace("\\", "/") or "/"
        name = os.path.basename(remote_path)
        try:
            for item, _is_dir in self.list_dir(parent):
                if item == name:
                    return True
        except Exception:
            return False
        return False

    def download(self, remote_path: str, local_path: str) -> bool:
        if self.ftp is None:
            return False
        try:
            parent = os.path.dirname(remote_path).replace("\\", "/") or "/"
            name = os.path.basename(remote_path)
            cwd = self.pwd()
            self.ftp.cwd(parent)
            with open(local_path, "wb") as f:
                self.ftp.retrbinary(f"RETR {name}", f.write)
            self.ftp.cwd(cwd)
            return True
        except Exception:
            return False

    def upload(self, local_path: str, remote_path: str) -> bool:
        if self.ftp is None:
            return False
        try:
            parent = os.path.dirname(remote_path).replace("\\", "/") or "/"
            name = os.path.basename(remote_path)
            self._makedirs(parent)
            cwd = self.pwd()
            self.ftp.cwd(parent)
            with open(local_path, "rb") as f:
                self.ftp.storbinary(f"STOR {name}", f)
            self.ftp.cwd(cwd)
            return True
        except Exception:
            return False

    def _makedirs(self, remote_dir: str) -> None:
        if self.ftp is None:
            return
        parts = [p for p in remote_dir.split("/") if p]
        path = "/"
        for p in parts:
            path = f"{path}{p}/"
            try:
                self.ftp.mkd(path)
            except Exception:
                pass

    def build_ftp_url(self, remote_path: str) -> str:
        host = self.cfg.get("host")
        port = int(self.cfg.get("port", 21))
        user = quote(self.cfg.get("username") or "")
        pwd = quote(self.cfg.get("password") or "")
        # Normalize: remove leading/trailing slashes and ensure single slashes
        remote_path = remote_path.strip().replace("//", "/")
        # If path doesn't start with /, it's relative - add base_path
        if not remote_path.startswith("/"):
            base = (self.cfg.get("base_path") or "/").strip().rstrip("/")
            if not base.startswith("/"):
                base = "/" + base
            remote_path = f"{base}/{remote_path}".replace("//", "/")
        # Ensure path starts with /
        if not remote_path.startswith("/"):
            remote_path = "/" + remote_path
        return f"ftp://{user}:{pwd}@{host}:{port}{remote_path}"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, cfg: ConfigManager) -> None:
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("HLS 预览与缩略图截取工具")
        self.resize(1200, 720)

        # Central layout: left tree, center player, right preview
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        hsplit = QtWidgets.QSplitter(QtCore.Qt.Horizontal, central)

        # Left: FTP tree only
        self.tree_ftp = QtWidgets.QTreeWidget()
        self.tree_ftp.setHeaderHidden(True)
        self.tree_ftp.setIndentation(16)  # 减少缩进，使显示更紧凑
        self.tree_ftp.setRootIsDecorated(True)  # 显示根节点展开图标
        self.tree_ftp.itemExpanded.connect(self.on_ftp_expand)
        self.tree_ftp.itemSelectionChanged.connect(self.on_ftp_selection)
        
        # FTP 标题区域
        ftp_header = QtWidgets.QWidget()
        ftp_header_layout = QtWidgets.QVBoxLayout(ftp_header)
        ftp_header_layout.setContentsMargins(6, 8, 6, 4)
        ftp_header_layout.setSpacing(4)
        
        ftp_title = QtWidgets.QLabel("FTP 服务器")
        ftp_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #333; padding: 0px;")
        ftp_header_layout.addWidget(ftp_title)
        
        ftp_container = QtWidgets.QWidget()
        ftp_v = QtWidgets.QVBoxLayout(ftp_container)
        ftp_v.setContentsMargins(6, 4, 6, 8)
        ftp_v.setSpacing(4)
        ftp_v.addWidget(ftp_header)
        ftp_v.addWidget(self.tree_ftp)

        # Center: video area + controls
        center_widget = QtWidgets.QWidget()
        center_layout = QtWidgets.QVBoxLayout(center_widget)
        center_layout.setContentsMargins(12, 12, 12, 12)
        center_layout.setSpacing(8)

        # 视频播放区域
        self.video_frame = QtWidgets.QFrame()
        self.video_frame.setStyleSheet("""
            QFrame {
                background: #000000;
                border: 1px solid #bbb;
                border-radius: 6px;
            }
        """)
        self.video_frame.setMinimumHeight(600)

        # VLC player wrapper
        self.player = HlsPlayer(self.video_frame)
        
        # 进度条
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: none;
                background: #ddd;
                height: 4px;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #666;
                border: 1px solid #999;
                width: 16px;
                margin: -4px 0;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: #555;
                border-color: #777;
            }
            QSlider::sub-page:horizontal {
                background: #888;
                border-radius: 2px;
            }
        """)
        
        # 播放控制区域（进度条、时间、播放倍数）
        controls_container = QtWidgets.QWidget()
        controls_layout = QtWidgets.QVBoxLayout(controls_container)
        controls_layout.setContentsMargins(0, 4, 0, 0)
        controls_layout.setSpacing(6)
        
        # 进度条行
        progress_row = QtWidgets.QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(8)
        
        # 时间标签（左侧）
        self.time_label = QtWidgets.QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("font-size: 11px; color: #666; min-width: 80px;")
        self.time_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        progress_row.addWidget(self.time_label)
        
        # 进度条
        progress_row.addWidget(self.slider, 1)
        
        # 播放倍数控件（右侧）
        speed_container = QtWidgets.QHBoxLayout()
        speed_container.setContentsMargins(0, 0, 0, 0)
        speed_container.setSpacing(4)
        
        speed_label = QtWidgets.QLabel("倍速:")
        speed_label.setStyleSheet("font-size: 11px; color: #666;")
        
        self.speed_combo = QtWidgets.QComboBox()
        self.speed_combo.addItems(["0.25x", "0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "1.75x", "2.0x", "2.5x", "3.0x", "3.5x", "4.0x"])
        self.speed_combo.setCurrentIndex(3)  # 默认 1.0x
        self.speed_combo.setFixedWidth(65)
        self.speed_combo.setStyleSheet("""
            QComboBox {
                background-color: #f5f5f5;
                border: 1px solid #ccc;
                border-radius: 3px;
                padding: 2px 6px;
                font-size: 11px;
            }
            QComboBox:hover {
                background-color: #eee;
                border-color: #aaa;
            }
            QComboBox::drop-down {
                border: none;
                width: 18px;
            }
        """)
        self.speed_combo.currentIndexChanged.connect(self.on_speed_changed)
        
        speed_container.addWidget(speed_label)
        speed_container.addWidget(self.speed_combo)
        progress_row.addLayout(speed_container)
        
        controls_layout.addLayout(progress_row)
        
        center_layout.addWidget(self.video_frame)
        center_layout.addWidget(controls_container)

        # Right: image preview and buttons
        preview_container = QtWidgets.QWidget()
        preview_layout = QtWidgets.QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(12, 12, 12, 12)
        preview_layout.setSpacing(12)
        
        # 标题和搜索区域
        preview_header = QtWidgets.QWidget()
        preview_header_layout = QtWidgets.QVBoxLayout(preview_header)
        preview_header_layout.setContentsMargins(0, 0, 0, 0)
        preview_header_layout.setSpacing(6)
        
        # 标题
        preview_title = QtWidgets.QLabel("图片预览")
        preview_title.setStyleSheet("font-weight: bold; font-size: 13px; color: #333;")
        preview_header_layout.addWidget(preview_title)
        
        # 搜索框（搜索FTP文件夹）
        search_container = QtWidgets.QHBoxLayout()
        search_container.setContentsMargins(0, 0, 0, 0)
        search_container.setSpacing(4)
        
        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("搜索文件夹...")
        self.search_input.setStyleSheet("""
            QLineEdit {
                border: 1px solid #ddd;
                border-radius: 4px;
                padding: 5px 10px;
                font-size: 12px;
                background-color: #fff;
            }
            QLineEdit:focus {
                border-color: #999;
                background-color: #fafafa;
            }
        """)
        self.search_input.textChanged.connect(self.on_search_text_changed)
        
        search_container.addWidget(self.search_input)
        preview_header_layout.addLayout(search_container)
        
        preview_layout.addWidget(preview_header)
        
        self.preview = ImagePreview()
        preview_layout.addWidget(self.preview)
        
        # 控制按钮区域（移到右侧）
        buttons_container = QtWidgets.QWidget()
        buttons_layout = QtWidgets.QVBoxLayout(buttons_container)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)
        
        # 第一行：播放控制
        play_controls = QtWidgets.QHBoxLayout()
        play_controls.setSpacing(8)
        self.btn_play = QtWidgets.QPushButton("播放")
        self.btn_play.setObjectName("btn_play")
        self.btn_pause = QtWidgets.QPushButton("暂停")
        self.btn_pause.setObjectName("btn_pause")
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.setObjectName("btn_stop")
        
        # 按钮快捷键提示将在 _setup_shortcuts 中更新
        play_controls.addWidget(self.btn_play)
        play_controls.addWidget(self.btn_pause)
        play_controls.addWidget(self.btn_stop)
        
        # 第二行：截图和设置
        action_controls = QtWidgets.QHBoxLayout()
        action_controls.setSpacing(8)
        self.btn_snapshot = QtWidgets.QPushButton("截图")
        self.btn_snapshot.setObjectName("btn_snapshot")
        self.btn_snapshot_cover = QtWidgets.QPushButton("截图封面")
        self.btn_snapshot_cover.setObjectName("btn_snapshot_cover")
        self.btn_settings = QtWidgets.QPushButton("设置")
        self.btn_settings.setObjectName("btn_settings")
        
        # 按钮快捷键提示将在 _setup_shortcuts 中更新
        action_controls.addWidget(self.btn_snapshot)
        action_controls.addWidget(self.btn_snapshot_cover)
        action_controls.addWidget(self.btn_settings)
        
        buttons_layout.addLayout(play_controls)
        buttons_layout.addLayout(action_controls)
        
        preview_layout.addWidget(buttons_container)

        hsplit.addWidget(ftp_container)
        hsplit.addWidget(center_widget)
        hsplit.addWidget(preview_container)
        hsplit.setSizes([300, 700, 300])

        layout = QtWidgets.QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(hsplit)

        # Signals
        self.btn_settings.clicked.connect(self.show_settings)
        self.btn_play.clicked.connect(self.player.play)
        self.btn_pause.clicked.connect(self.player.pause)
        self.btn_stop.clicked.connect(self.player.stop)
        self.btn_snapshot.clicked.connect(self.on_snapshot)
        self.btn_snapshot_cover.clicked.connect(self.on_snapshot_cover)
        self.slider.sliderMoved.connect(self.on_slider_moved)
        self.player.positionChanged.connect(self.on_player_position)
        self.player.lengthChanged.connect(self.on_player_length_changed)
        self.player.endReached.connect(self.on_player_end_reached)
        
        # 快捷键
        self.shortcuts = {}
        self._setup_shortcuts()

        # Status bar
        self.statusBar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage("就绪")
        # 下载进度条（初始隐藏）
        self.download_progress = QtWidgets.QProgressBar()
        self.download_progress.setVisible(False)
        self.download_progress.setMaximumWidth(300)
        self.statusBar.addPermanentWidget(self.download_progress)
        # 下载状态标签
        self.download_label = QtWidgets.QLabel()
        self.download_label.setVisible(False)
        self.download_label.setStyleSheet("color: #2196F3; font-weight: bold;")
        self.statusBar.addPermanentWidget(self.download_label)

        # 应用美化样式
        self.apply_beautiful_style()

        # theme
        if self.cfg.get("theme") == "dark":
            self.apply_dark_theme()
        
        # 自动连接 FTP
        self.ftp_retry_timer = QtCore.QTimer(self)
        self.ftp_retry_timer.timeout.connect(self.connect_ftp)
        self.ftp_retry_countdown_timer = QtCore.QTimer(self)
        self.ftp_retry_countdown = 5
        self.ftp_retry_countdown_timer.timeout.connect(self.update_retry_countdown)
        self.ftp_connected = False
        
        # 当前正在使用的缓存目录（播放中）
        self.current_cache_dir = None
        
        # 工作线程引用（避免被垃圾回收）
        self.active_workers: list[QtCore.QThread] = []
        
        # 下载工作线程引用（用于切换时停止）
        self.download_worker: "FtpDownloadWorker | None" = None
        
        # 延迟 500ms 后开始自动连接（等待界面完全加载）
        QtCore.QTimer.singleShot(500, self.connect_ftp)
        
        # 程序启动时使用工作线程清理旧缓存
        max_dirs = int(self.cfg.get("max_cache_dirs", 5))
        cleanup_worker = CacheCleanupWorker(max_dirs, self.current_cache_dir)
        
        def cleanup_startup_worker() -> None:
            if cleanup_worker in self.active_workers:
                self.active_workers.remove(cleanup_worker)
        
        cleanup_worker.finished.connect(cleanup_startup_worker)
        self.active_workers.append(cleanup_worker)
        cleanup_worker.start()  # 异步清理，不等待完成
        
        # 如果设置了启动时最小化，延迟最小化（等待界面完全加载）
        if self.cfg.get("start_minimized", False):
            QtCore.QTimer.singleShot(500, self.showMinimized)

    def apply_beautiful_style(self) -> None:
        """应用美化样式"""
        style = """
        /* 主窗口 */
        QMainWindow {
            background-color: #f5f5f5;
        }
        
        /* 按钮样式 - 简洁实用 */
        QPushButton {
            background-color: #e0e0e0;
            color: #333;
            border: 1px solid #ccc;
            border-radius: 4px;
            padding: 6px 16px;
            font-size: 13px;
            min-width: 70px;
            min-height: 28px;
        }
        QPushButton:hover {
            background-color: #d0d0d0;
            border-color: #999;
        }
        QPushButton:pressed {
            background-color: #c0c0c0;
        }
        QPushButton:disabled {
            background-color: #f0f0f0;
            color: #999;
            border-color: #ddd;
        }
        
        
        /* 标签页 */
        QTabWidget::pane {
            border: 1px solid #ddd;
            border-radius: 4px;
            background: white;
        }
        QTabBar::tab {
            background: #e0e0e0;
            color: #333;
            padding: 8px 20px;
            margin-right: 2px;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
        }
        QTabBar::tab:selected {
            background: white;
            color: #2196F3;
            font-weight: bold;
        }
        QTabBar::tab:hover {
            background: #f0f0f0;
        }
        
        /* FTP 树控件样式 - 简洁舒适 */
        QTreeWidget {
            background: white;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 2px;
            font-size: 12px;
            outline: none;
        }
        QTreeWidget::item {
            padding: 4px 2px;
            border: none;
            min-height: 20px;
            margin: 1px 0px;
        }
        QTreeWidget::item:hover {
            background: #f5f5f5;
        }
        QTreeWidget::item:selected {
            background: #e8e8e8;
            color: #333;
        }
        QTreeWidget::item:selected:hover {
            background: #ddd;
        }
        QTreeWidget::branch {
            background: transparent;
            width: 14px;
        }
        QTreeWidget::branch:has-siblings:!adjoins-item {
            border-image: none;
            border: none;
        }
        QTreeWidget::branch:has-siblings:adjoins-item {
            border-image: none;
            border: none;
        }
        QTreeWidget::branch:!has-children:!has-siblings:adjoins-item {
            border-image: none;
            border: none;
        }
        QTreeWidget::branch:closed:has-children:!has-siblings {
            border-image: none;
            image: none;
        }
        QTreeWidget::branch:open:has-children:!has-siblings {
            border-image: none;
            image: none;
        }
        /* 其他树视图样式 */
        QTreeView {
            background: white;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 4px;
        }
        QTreeView::item {
            padding: 6px 2px;
            border: none;
        }
        QTreeView::item:hover {
            background: #f5f5f5;
        }
        QTreeView::item:selected {
            background: #e8e8e8;
            color: #333;
        }
        
        /* 滑块已在上面单独设置样式 */
        
        /* 状态栏 */
        QStatusBar {
            background: white;
            border-top: 1px solid #ddd;
            color: #333;
        }
        
        /* 进度条 */
        QProgressBar {
            border: 1px solid #ddd;
            border-radius: 4px;
            text-align: center;
            background: #f0f0f0;
        }
        QProgressBar::chunk {
            background: #2196F3;
            border-radius: 3px;
        }
        
        /* 分割线 */
        QSplitter::handle:horizontal {
            background: #e0e0e0;
            width: 3px;
            border: none;
        }
        QSplitter::handle:horizontal:hover {
            background: #2196F3;
            width: 4px;
        }
        
        /* 标签样式 */
        QLabel {
            color: #333;
        }
        """
        self.setStyleSheet(style)
        # 按钮 ID 已在创建时设置

    def apply_dark_theme(self) -> None:
        pal = QtGui.QPalette()
        pal.setColor(QtGui.QPalette.Window, QtGui.QColor(32, 32, 32))
        pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(230, 230, 230))
        pal.setColor(QtGui.QPalette.Base, QtGui.QColor(25, 25, 25))
        pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(40, 40, 40))
        pal.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor(45, 45, 45))
        pal.setColor(QtGui.QPalette.ToolTipText, QtCore.Qt.white)
        pal.setColor(QtGui.QPalette.Text, QtGui.QColor(230, 230, 230))
        pal.setColor(QtGui.QPalette.Button, QtGui.QColor(50, 50, 50))
        pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(230, 230, 230))
        pal.setColor(QtGui.QPalette.BrightText, QtCore.Qt.red)
        pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(66, 150, 250))
        pal.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.white)
        self.setPalette(pal)
        
        # 暗色主题样式 - 优化美观度
        dark_style = """
        QMainWindow {
            background-color: #1e1e1e;
        }
        
        /* 按钮样式 - 现代化设计 */
        QPushButton {
            background-color: #3d3d3d;
            color: #e6e6e6;
            border: 1px solid #4d4d4d;
            border-radius: 5px;
            padding: 6px 16px;
            font-size: 13px;
            min-width: 70px;
            min-height: 28px;
        }
        QPushButton:hover {
            background-color: #4d4d4d;
            border-color: #5d5d5d;
            color: #ffffff;
        }
        QPushButton:pressed {
            background-color: #2d2d2d;
            border-color: #3d3d3d;
        }
        QPushButton:disabled {
            background-color: #2a2a2a;
            color: #666666;
            border-color: #3a3a3a;
        }
        
        /* 标签页样式 */
        QTabWidget::pane {
            background: #2a2a2a;
            border: 1px solid #404040;
            border-radius: 5px;
        }
        QTabBar::tab {
            background: #353535;
            color: #b8b8b8;
            padding: 8px 20px;
            margin-right: 2px;
            border-top-left-radius: 5px;
            border-top-right-radius: 5px;
        }
        QTabBar::tab:selected {
            background: #1e1e1e;
            color: #4296f5;
            font-weight: bold;
            border-bottom: 2px solid #4296f5;
        }
        QTabBar::tab:hover {
            background: #404040;
            color: #e6e6e6;
        }
        
        /* FTP 树控件 - 优化视觉层次 */
        QTreeWidget {
            background: #252525;
            border: 1px solid #3a3a3a;
            border-radius: 5px;
            padding: 3px;
            font-size: 12px;
            color: #e6e6e6;
            selection-background-color: #4296f5;
            selection-color: white;
        }
        QTreeWidget::item {
            padding: 5px 6px;
            border: none;
            min-height: 22px;
            margin: 1px 0px;
            color: #e6e6e6;
            border-radius: 3px;
        }
        QTreeWidget::item:hover {
            background: #353535;
            color: #ffffff;
        }
        QTreeWidget::item:selected {
            background: #4296f5;
            color: white;
        }
        QTreeWidget::item:selected:hover {
            background: #5296f5;
        }
        
        QTreeView {
            background: #252525;
            border: 1px solid #3a3a3a;
            border-radius: 5px;
            padding: 4px;
            color: #e6e6e6;
            selection-background-color: #4296f5;
            selection-color: white;
        }
        QTreeView::item {
            padding: 6px 6px;
            border: none;
            color: #e6e6e6;
            border-radius: 3px;
        }
        QTreeView::item:hover {
            background: #353535;
            color: #ffffff;
        }
        QTreeView::item:selected {
            background: #4296f5;
            color: white;
        }
        
        /* 状态栏 */
        QStatusBar {
            background: #252525;
            border-top: 1px solid #3a3a3a;
            color: #e6e6e6;
        }
        
        /* 进度条 */
        QProgressBar {
            border: 1px solid #3a3a3a;
            border-radius: 5px;
            text-align: center;
            background: #2a2a2a;
            color: #e6e6e6;
        }
        QProgressBar::chunk {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4296f5, stop:1 #5296f5);
            border-radius: 4px;
        }
        
        /* 分割线 */
        QSplitter::handle:horizontal {
            background: #2a2a2a;
            width: 2px;
            border: none;
        }
        QSplitter::handle:horizontal:hover {
            background: #4296f5;
            width: 3px;
        }
        
        /* 标签 */
        QLabel {
            color: #e6e6e6;
        }
        
        /* 输入框 */
        QLineEdit {
            background: #2a2a2a;
            border: 1px solid #404040;
            border-radius: 5px;
            padding: 5px 10px;
            font-size: 12px;
            color: #e6e6e6;
            selection-background-color: #4296f5;
            selection-color: white;
        }
        QLineEdit:focus {
            border: 2px solid #4296f5;
            background: #2f2f2f;
        }
        QLineEdit:hover {
            border-color: #505050;
        }
        
        /* 下拉框 */
        QComboBox {
            background-color: #2a2a2a;
            border: 1px solid #404040;
            border-radius: 5px;
            padding: 3px 8px;
            font-size: 11px;
            color: #e6e6e6;
            min-width: 60px;
        }
        QComboBox:hover {
            background-color: #2f2f2f;
            border-color: #505050;
        }
        QComboBox::drop-down {
            border: none;
            width: 20px;
            background: transparent;
        }
        QComboBox::down-arrow {
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid #888;
            width: 0;
            height: 0;
        }
        QComboBox QAbstractItemView {
            background-color: #2a2a2a;
            border: 1px solid #404040;
            selection-background-color: #4296f5;
            selection-color: white;
            color: #e6e6e6;
        }
        
        /* 滑块 */
        QSlider::groove:horizontal {
            border: none;
            background: #3a3a3a;
            height: 5px;
            border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background: #666;
            border: 2px solid #888;
            width: 18px;
            margin: -6px 0;
            border-radius: 9px;
        }
        QSlider::handle:horizontal:hover {
            background: #777;
            border-color: #999;
        }
        QSlider::sub-page:horizontal {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4296f5, stop:1 #5296f5);
            border-radius: 3px;
        }
        
        /* 视频框架 */
        QFrame {
            background: #000000;
            border: 1px solid #3a3a3a;
            border-radius: 6px;
        }
        
        /* 滚动条 */
        QScrollBar:vertical {
            background: #252525;
            width: 12px;
            border: none;
        }
        QScrollBar::handle:vertical {
            background: #404040;
            min-height: 20px;
            border-radius: 6px;
            margin: 2px;
        }
        QScrollBar::handle:vertical:hover {
            background: #505050;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
        
        QScrollBar:horizontal {
            background: #252525;
            height: 12px;
            border: none;
        }
        QScrollBar::handle:horizontal {
            background: #404040;
            min-width: 20px;
            border-radius: 6px;
            margin: 2px;
        }
        QScrollBar::handle:horizontal:hover {
            background: #505050;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            width: 0px;
        }
        """
        self.setStyleSheet(self.styleSheet() + dark_style)

    def choose_root(self) -> None:
        start = self.cfg.get("root_directory") or str(Path.home())
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, "选择根目录", start)
        if directory:
            self.cfg.set("root_directory", directory)
            self.tree_local.setRootIndex(self.model.index(directory))

    def on_tree_selection(self, current: QtCore.QModelIndex) -> None:  # noqa: ANN001
        path = Path(self.model.filePath(current))
        if not path.exists():
            return
        # 更新右侧预览（cover, thumbnail）
        cover = path / self.cfg.get("cover_filename")
        thumb = path / self.cfg.get("snapshot_filename")
        if not cover.exists():
            # 若当前目录没有，尝试父级目录
            cover = path.parent / self.cfg.get("cover_filename")
        if not thumb.exists():
            thumb = path.parent / self.cfg.get("snapshot_filename")
        self.preview.set_image(self.preview.cover_label, cover)
        self.preview.set_image(self.preview.thumb_label, thumb)

        # 如果点击的是以 _hls 结尾的目录并且包含 m3u8，则自动播放（本地）
        suffix = self.cfg.get("accepted_video_dir_suffix")
        m3u8_name = self.cfg.get("m3u8_filename")
        if path.is_dir() and path.name.endswith(suffix):
            m3u8 = path / m3u8_name
            if m3u8.exists():
                self.player.open(str(m3u8))

    def on_speed_changed(self, index: int) -> None:
        """播放倍数改变时的处理"""
        speeds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0]
        if 0 <= index < len(speeds):
            self.player.set_rate(speeds[index])
            self.statusBar.showMessage(f"播放速率: {speeds[index]}x", 2000)
    
    def increase_speed(self) -> None:
        """加快播放速度"""
        current_rate = self.player.get_rate()
        speeds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0]
        # 找到下一个更快的速度
        next_rate = current_rate
        for speed in speeds:
            if speed > current_rate + 0.01:  # 加上小误差避免浮点比较问题
                next_rate = speed
                break
        # 如果已经是最大速度，保持最大速度
        if next_rate == current_rate:
            next_rate = speeds[-1]
        self.player.set_rate(next_rate)
        # 更新下拉框显示
        if hasattr(self, 'speed_combo'):
            try:
                index = speeds.index(next_rate)
                self.speed_combo.setCurrentIndex(index)
            except ValueError:
                pass
        self.statusBar.showMessage(f"播放速率: {next_rate}x", 2000)
    
    def decrease_speed(self) -> None:
        """减少播放速度"""
        current_rate = self.player.get_rate()
        speeds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0]
        # 找到下一个更慢的速度
        next_rate = current_rate
        for speed in reversed(speeds):
            if speed < current_rate - 0.01:  # 减去小误差避免浮点比较问题
                next_rate = speed
                break
        # 如果已经是最小速度，保持最小速度
        if next_rate == current_rate:
            next_rate = speeds[0]
        self.player.set_rate(next_rate)
        # 更新下拉框显示
        if hasattr(self, 'speed_combo'):
            try:
                index = speeds.index(next_rate)
                self.speed_combo.setCurrentIndex(index)
            except ValueError:
                pass
        self.statusBar.showMessage(f"播放速率: {next_rate}x", 2000)
    
    def reset_speed(self) -> None:
        """恢复原始播放速度（1.0x）"""
        self.player.set_rate(1.0)
        # 更新下拉框显示
        if hasattr(self, 'speed_combo'):
            speeds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0]
            try:
                index = speeds.index(1.0)
                self.speed_combo.setCurrentIndex(index)
            except ValueError:
                pass
        self.statusBar.showMessage("播放速率: 1.0x（已恢复）", 2000)
    
    def on_slider_moved(self, value: int) -> None:
        self.player.set_position(value / 1000.0)
    
    def on_player_end_reached(self) -> None:
        """视频播放结束时的处理"""
        play_on_end = self.cfg.get("play_on_end", "重新播放")
        if play_on_end == "播放下一个视频":
            # 播放下一个视频
            self._play_next_video()
        else:
            # 重新播放当前视频
            self.player.stop()
            QtCore.QTimer.singleShot(300, self.player.play)
            self.statusBar.showMessage("视频播放完成，重新播放", 2000)
    
    def _play_next_video(self) -> None:
        """播放下一个视频"""
        current_item = self._current_ftp_item()
        if current_item is None:
            self.statusBar.showMessage("未找到当前视频，无法播放下一个", 3000)
            return
        
        # 获取当前路径
        current_path = current_item.data(0, QtCore.Qt.UserRole) or ""
        suffix = self.cfg.get("accepted_video_dir_suffix", "_hls")
        
        # 如果不是_hls目录，无法播放下一个
        if not current_path.endswith(suffix):
            self.statusBar.showMessage("当前选择不是视频目录，无法播放下一个", 3000)
            return
        
        # 查找下一个_hls目录
        next_item = self._find_next_hls_item(current_item)
        if next_item is None:
            self.statusBar.showMessage("没有找到下一个视频", 3000)
            return
        
        # 选择并播放下一个视频
        self.tree_ftp.setCurrentItem(next_item)
        self.tree_ftp.scrollToItem(next_item)
        # 触发选择事件，自动开始下载和播放
        # on_ftp_selection 会自动处理
    
    def _find_next_hls_item(self, current_item: QtWidgets.QTreeWidgetItem) -> QtWidgets.QTreeWidgetItem | None:
        """查找下一个_hls目录项"""
        if current_item is None:
            return None
        
        suffix = self.cfg.get("accepted_video_dir_suffix", "_hls")
        
        # 递归查找所有_hls项
        def collect_hls_items(item: QtWidgets.QTreeWidgetItem, items: list) -> None:
            """收集所有_hls目录项"""
            path = item.data(0, QtCore.Qt.UserRole) or ""
            if path.endswith(suffix):
                items.append(item)
            # 递归查找子项
            for i in range(item.childCount()):
                child = item.child(i)
                collect_hls_items(child, items)
        
        # 从根节点开始收集
        all_hls_items = []
        for i in range(self.tree_ftp.topLevelItemCount()):
            root = self.tree_ftp.topLevelItem(i)
            collect_hls_items(root, all_hls_items)
        
        # 找到当前项的位置
        try:
            current_index = all_hls_items.index(current_item)
            # 返回下一个项
            if current_index + 1 < len(all_hls_items):
                return all_hls_items[current_index + 1]
        except ValueError:
            # 当前项不在列表中，返回第一个项
            if all_hls_items:
                return all_hls_items[0]
        
        return None
    
    def on_player_length_changed(self, length_ms: int) -> None:
        """视频长度变化时更新显示"""
        self._update_time_display(length_ms)
    
    def _update_time_display(self, length_ms: int = None) -> None:
        """更新时间显示"""
        if not hasattr(self, "time_label"):
            return
        
        try:
            if length_ms is None:
                length_ms = self.player.get_length()
            
            # 获取当前播放位置
            try:
                pos01 = self.player.get_position()
                if pos01 < 0 or pos01 > 1:
                    pos01 = 0
            except Exception:
                pos01 = 0
            
            current_ms = int(pos01 * length_ms) if length_ms > 0 else 0
            
            def format_time(seconds: int) -> str:
                mins = seconds // 60
                secs = seconds % 60
                return f"{mins:02d}:{secs:02d}"
            
            if length_ms and length_ms > 0:
                length_sec = length_ms // 1000
                current_sec = max(0, current_ms // 1000)
                self.time_label.setText(f"{format_time(current_sec)} / {format_time(length_sec)}")
            else:
                # 如果还没有长度信息，显示当前时间（如果可用）
                try:
                    pos01 = self.player.get_position()
                    if 0 <= pos01 <= 1:
                        # 尝试获取媒体长度
                        length_ms = self.player.get_length()
                        if length_ms > 0:
                            current_ms = int(pos01 * length_ms)
                            current_sec = current_ms // 1000
                            self.time_label.setText(f"{format_time(current_sec)} / --:--")
                        else:
                            self.time_label.setText("00:00 / --:--")
                    else:
                        self.time_label.setText("00:00 / --:--")
                except Exception:
                    self.time_label.setText("00:00 / --:--")
        except Exception:
            if hasattr(self, "time_label"):
                self.time_label.setText("00:00 / --:--")
    
    def on_player_position(self, pos01: float) -> None:
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(int(pos01 * 1000))
            self.slider.blockSignals(False)
        
        # 更新时间显示
        self._update_time_display()

    def _find_active_hls_dir(self) -> Path | None:
        # 仅适用于本地标签
        index = self.tree_local.currentIndex()
        if not index.isValid():
            return None
        path = Path(self.model.filePath(index))
        if path.is_dir() and path.name.endswith(self.cfg.get("accepted_video_dir_suffix")):
            return path
        try:
            for child in path.iterdir():
                if child.is_dir() and child.name.endswith(self.cfg.get("accepted_video_dir_suffix")):
                    return child
        except Exception:  # noqa: BLE001
            pass
        return None

    def _get_current_remote_dir(self) -> str | None:
        """获取当前选中的 FTP 远程目录路径"""
        item = self._current_ftp_item()
        if item is None:
            return None
        remote_dir = item.data(0, QtCore.Qt.UserRole) or ""
        if not remote_dir.endswith(self.cfg.get("accepted_video_dir_suffix")):
            return None
        return remote_dir

    def on_snapshot(self) -> None:
        """截图为 thumbnail.jpg 和 first_frame.jpg"""
        if not hasattr(self, "ftp") or self.ftp is None:
            self.statusBar.showMessage("✗ 请先连接 FTP", 3000)
            return
        
        remote_dir = self._get_current_remote_dir()
        if remote_dir is None:
            self.statusBar.showMessage("✗ 请选择以 _hls 结尾的目录", 3000)
            return
        
        width = int(self.cfg.get("vlc_snapshot_width") or 0)
        height = int(self.cfg.get("vlc_snapshot_height") or 0)
        
        # 先截图到临时文件
        tmp_snap = Path(tempfile.gettempdir()) / "snapshot_tmp.jpg"
        snap_ok = self.player.snapshot(str(tmp_snap), width, height)
        if not snap_ok:
            self.statusBar.showMessage("✗ 截图失败：请确认视频正在播放", 3000)
            return
        
        # 需要替换的文件列表
        files_to_replace = []
        
        # 检查是否存在 thumbnail.jpg
        remote_thumb = f"{remote_dir.rstrip('/')}/{self.cfg.get('snapshot_filename')}"
        if self.ftp.exists(remote_thumb):
            files_to_replace.append((remote_thumb, self.cfg.get("snapshot_filename")))
        
        # 检查是否存在 first_frame.jpg
        remote_first_frame = f"{remote_dir.rstrip('/')}/{self.cfg.get('first_frame_filename')}"
        if self.ftp.exists(remote_first_frame):
            files_to_replace.append((remote_first_frame, self.cfg.get("first_frame_filename")))
        
        if not files_to_replace:
            self.statusBar.showMessage("✗ 未找到需要替换的文件（thumbnail.jpg 或 first_frame.jpg）", 3000)
            return
        
        # 上传所有需要替换的文件
        uploaded_count = 0
        for remote_path, filename in files_to_replace:
            if self.ftp.upload(str(tmp_snap), remote_path):
                uploaded_count += 1
        
        # 下载最新的 thumbnail.jpg 回来预览
        if uploaded_count > 0:
            local_preview = Path(tempfile.gettempdir()) / "thumbnail_preview.jpg"
            if self.ftp.download(remote_thumb, str(local_preview)):
                self.preview.set_image(self.preview.thumb_label, local_preview)
            
            file_names = [f[1] for f in files_to_replace]
            self.statusBar.showMessage(
                f"✓ 截图成功：已替换 {uploaded_count} 个文件 ({', '.join(file_names)})", 5000
            )
        else:
            self.statusBar.showMessage("✗ 上传失败：请检查 FTP 配置与权限", 3000)

    def on_snapshot_cover(self) -> None:
        """截图为封面 cover.jpg（父级目录）"""
        if not hasattr(self, "ftp") or self.ftp is None:
            self.statusBar.showMessage("✗ 请先连接 FTP", 3000)
            return
        
        remote_dir = self._get_current_remote_dir()
        if remote_dir is None:
            self.statusBar.showMessage("✗ 请选择以 _hls 结尾的目录", 3000)
            return
        
        # 封面在父级目录
        parent_dir = str(Path(remote_dir).parent).replace("\\", "/")
        remote_cover = f"{parent_dir}/{self.cfg.get('cover_filename')}"
        
        if not self.ftp.exists(remote_cover):
            self.statusBar.showMessage(f"✗ 未找到封面文件：{self.cfg.get('cover_filename')}", 3000)
            return
        
        width = int(self.cfg.get("vlc_snapshot_width") or 0)
        height = int(self.cfg.get("vlc_snapshot_height") or 0)
        
        # 先截图到临时文件
        tmp_snap = Path(tempfile.gettempdir()) / "snapshot_cover_tmp.jpg"
        snap_ok = self.player.snapshot(str(tmp_snap), width, height)
        if not snap_ok:
            self.statusBar.showMessage("✗ 截图失败：请确认视频正在播放", 3000)
            return
        
        # 上传封面
        if self.ftp.upload(str(tmp_snap), remote_cover):
            # 下载回来预览
            local_preview = Path(tempfile.gettempdir()) / "cover_preview.jpg"
            if self.ftp.download(remote_cover, str(local_preview)):
                self.preview.set_image(self.preview.cover_label, local_preview)
            self.statusBar.showMessage(f"✓ 封面截图成功：已替换 {self.cfg.get('cover_filename')}", 5000)
        else:
            self.statusBar.showMessage("✗ 上传失败：请检查 FTP 配置与权限", 3000)

    # ===== FTP 支持 =====
    def connect_ftp(self) -> None:
        ftp_cfg = self.cfg.get("ftp", {})
        if not ftp_cfg or not ftp_cfg.get("host"):
            self.statusBar.showMessage("✗ FTP 配置缺失：请先在设置中配置 FTP 信息", 5000)
            # 如果配置缺失，停止自动重试
            self.ftp_retry_timer.stop()
            return
        
        # 如果已经连接成功，停止自动重试
        if self.ftp_connected and hasattr(self, "ftp") and self.ftp and self.ftp.ftp:
            return
        
        # 显示连接状态
        if not self.ftp_connected:
            self.statusBar.showMessage("正在连接 FTP...", 0)
        
        # 如果已有连接，先断开
        if hasattr(self, "ftp") and self.ftp:
            self.ftp.disconnect()
        
        # 使用工作线程连接FTP，避免阻塞UI
        def on_ftp_connected(ftp: FtpHelper) -> None:
            self.ftp = ftp
            self.ftp_connected = True
            self.ftp_retry_timer.stop()  # 停止自动重试
            self.ftp_retry_countdown_timer.stop()  # 停止倒计时
            
            self.tree_ftp.clear()
            base = ftp_cfg.get("base_path") or "/"
            root = QtWidgets.QTreeWidgetItem([base])
            root.setData(0, QtCore.Qt.UserRole, base)
            root.setChildIndicatorPolicy(QtWidgets.QTreeWidgetItem.ShowIndicator)
            self.tree_ftp.addTopLevelItem(root)
            # 如果开启了过滤，展开根目录时也会应用过滤
            self.tree_ftp.expandItem(root)
            filter_status = []
            if self.cfg.get("filter_id_dirs", False):
                filter_status.append("ID目录过滤")
            if self.cfg.get("show_only_id_folders", False):
                filter_status.append("仅显示ID文件夹")
            filter_text = f"（已启用: {', '.join(filter_status)}）" if filter_status else ""
            self.statusBar.showMessage(f"✓ FTP 连接成功：{ftp_cfg.get('host')} {filter_text}", 3000)
        
        def on_ftp_failed(error_msg: str) -> None:
            # 连接失败，启动自动重试定时器
            self.ftp_connected = False
            if not self.ftp_retry_timer.isActive():
                self.ftp_retry_timer.start(5000)  # 每5秒重试一次
                # 启动倒计时显示
                self.ftp_retry_countdown = 5
                self.ftp_retry_countdown_timer.start(1000)  # 每秒更新一次
            self.update_retry_countdown()
        
        # 启动FTP连接线程
        connect_worker = FtpConnectWorker(ftp_cfg)
        
        def cleanup_connect_worker() -> None:
            if connect_worker in self.active_workers:
                self.active_workers.remove(connect_worker)
        
        connect_worker.connected.connect(on_ftp_connected)
        connect_worker.failed.connect(on_ftp_failed)
        connect_worker.connected.connect(cleanup_connect_worker)
        connect_worker.failed.connect(cleanup_connect_worker)
        self.active_workers.append(connect_worker)
        connect_worker.start()
    
    def update_retry_countdown(self) -> None:
        """更新重试倒计时显示"""
        if self.ftp_connected:
            self.ftp_retry_countdown_timer.stop()
            return
        
        self.ftp_retry_countdown -= 1
        if self.ftp_retry_countdown > 0:
            self.statusBar.showMessage(f"✗ FTP 连接失败，{self.ftp_retry_countdown}秒后自动重试...", 0)
        else:
            self.ftp_retry_countdown = 5  # 重置倒计时
    
    def on_search_text_changed(self, text: str) -> None:
        """搜索文本改变时的处理"""
        search_text = text.strip().lower()
        if not search_text:
            # 清空搜索，恢复所有项的可见性
            self._restore_all_items_visibility()
            return
        
        # 搜索匹配的项
        self._search_and_highlight(search_text)
    
    def _restore_all_items_visibility(self) -> None:
        """恢复所有项的可见性（清除搜索高亮）"""
        def restore_item(item: QtWidgets.QTreeWidgetItem) -> None:
            item.setHidden(False)
            for i in range(item.childCount()):
                restore_item(item.child(i))
        
        for i in range(self.tree_ftp.topLevelItemCount()):
            restore_item(self.tree_ftp.topLevelItem(i))
    
    def _search_and_highlight(self, search_text: str) -> None:
        """搜索并高亮匹配的项"""
        if not search_text:
            return
        
        if not hasattr(self, "ftp") or self.ftp is None:
            return
        
        # 先隐藏所有项
        def hide_all_items(item: QtWidgets.QTreeWidgetItem) -> None:
            item.setHidden(True)
            for i in range(item.childCount()):
                hide_all_items(item.child(i))
        
        for i in range(self.tree_ftp.topLevelItemCount()):
            hide_all_items(self.tree_ftp.topLevelItem(i))
        
        # 递归搜索匹配的项（需要先展开子项才能搜索）
        def search_item(item: QtWidgets.QTreeWidgetItem, expand_needed: bool = True) -> bool:
            item_name = item.text(0).lower()
            item_path = item.data(0, QtCore.Qt.UserRole) or ""
            
            # 如果项未加载，先尝试加载（展开）
            if expand_needed and not getattr(item, "_loaded", False):
                if hasattr(item, "_loaded") and not item._loaded:  # type: ignore[attr-defined]
                    # 触发展开以加载子项
                    try:
                        self.on_ftp_expand(item)
                    except Exception:
                        pass
            
            # 检查当前项是否匹配（模糊搜索）
            is_match = search_text in item_name or search_text in item_path.lower()
            
            # 递归检查子项
            has_matching_child = False
            for i in range(item.childCount()):
                child = item.child(i)
                if search_item(child, expand_needed=False):  # 子项已经加载，不需要再展开
                    has_matching_child = True
            
            # 如果当前项或子项匹配，显示该项并展开父链
            if is_match or has_matching_child:
                item.setHidden(False)
                # 展开到该项
                parent = item.parent()
                while parent:
                    parent.setExpanded(True)
                    parent.setHidden(False)
                    parent = parent.parent()
                # 展开当前项以便看到匹配的子项
                if has_matching_child:
                    item.setExpanded(True)
                return True
            
            return False
        
        # 搜索所有顶层项
        found_any = False
        for i in range(self.tree_ftp.topLevelItemCount()):
            item = self.tree_ftp.topLevelItem(i)
            if search_item(item):
                found_any = True
        
        # 如果没有找到匹配项，显示提示
        if not found_any:
            self.statusBar.showMessage(f"未找到匹配 '{search_text}' 的文件夹", 2000)
        else:
            self.statusBar.showMessage(f"找到匹配 '{search_text}' 的文件夹", 2000)

    def on_ftp_expand(self, item: QtWidgets.QTreeWidgetItem) -> None:
        # 懒加载当前目录的子项
        if not hasattr(item, "_loaded"):
            item._loaded = False  # type: ignore[attr-defined]
        if item._loaded:  # type: ignore[attr-defined]
            return
        if not hasattr(self, "ftp") or self.ftp is None:
            return
        path = item.data(0, QtCore.Qt.UserRole) or "/"
        
        # 使用工作线程列出目录，避免阻塞UI
        def on_list_finished(children: list) -> None:
            self._populate_tree_item(item, path, children)
        
        def on_list_error(error_msg: str) -> None:
            self.statusBar.showMessage(f"✗ 无法列出目录 {path}: {error_msg}", 3000)
            item._loaded = True  # type: ignore[attr-defined]
        
        # 标记为加载中（防止重复加载）
        item._loaded = False  # type: ignore[attr-defined]
        ftp_cfg = self.cfg.get("ftp", {})
        worker = FtpListWorker(ftp_cfg, path)
        
        def cleanup_worker() -> None:
            if worker in self.active_workers:
                self.active_workers.remove(worker)
        
        worker.finished.connect(on_list_finished)
        worker.error.connect(on_list_error)
        worker.finished.connect(cleanup_worker)
        worker.error.connect(cleanup_worker)
        self.active_workers.append(worker)
        worker.start()
    
    def _populate_tree_item(self, item: QtWidgets.QTreeWidgetItem, path: str, children: list) -> None:
        """填充树形控件的子项（在主线程中执行UI更新）"""
        # 判断是否在根目录或 base_path 目录（只显示 id_ 文件夹）
        show_only_id = False
        if self.cfg.get("show_only_id_folders", False):
            ftp_cfg = self.cfg.get("ftp", {})
            base_path = ftp_cfg.get("base_path", "/")
            base_path_normalized = base_path.rstrip("/") or "/"
            path_normalized = path.rstrip("/") or "/"
            
            # 如果当前路径是根目录或 base_path 目录，只显示 id_ 开头的文件夹
            if path_normalized == "/" or path_normalized == base_path_normalized:
                show_only_id = True
        
        # 判断是否需要过滤（是否是 id_xx 目录且开启了过滤）
        should_filter = False
        if self.cfg.get("filter_id_dirs", False):
            # 检查当前路径是否是 id_xx 格式的目录
            path_normalized = path.rstrip("/")
            if path_normalized:
                # 获取路径的最后一部分（目录名）
                path_parts = [p for p in path_normalized.split("/") if p]
                if path_parts:
                    path_name = path_parts[-1]
                    # 匹配 id_数字_标题 格式（例如：id_11_七月喵子-白丝写真27P2V）
                    if path_name.startswith("id_") and len(path_name) > 3:
                        # 检查 id_ 后面是否跟着数字
                        parts = path_name[3:].split("_", 1)
                        if parts[0].isdigit():
                            should_filter = True
        
        suffix = self.cfg.get("accepted_video_dir_suffix", "_hls")
        
        for name, is_dir in children:
            # 如果开启了只显示 id_ 文件夹且在根目录或 base_path 目录，只显示以 id_ 开头的文件夹
            if show_only_id:
                if not is_dir or not name.startswith("id_"):
                    continue
                # 验证 id_ 后面是否跟着数字（格式：id_数字_...）
                if len(name) <= 3:
                    continue
                id_parts = name[3:].split("_", 1)
                if not id_parts[0].isdigit():
                    continue
            
            # 如果开启了过滤且在 id_xx 目录下，只显示以 _hls 结尾的目录
            if should_filter:
                if not is_dir:  # 过滤掉所有文件（如 cover.jpg）
                    continue
                if not name.endswith(suffix):  # 只显示以 _hls 结尾的目录
                    continue
            
            child_path = f"{path.rstrip('/')}/{name}"
            child = QtWidgets.QTreeWidgetItem([name])
            child.setData(0, QtCore.Qt.UserRole, child_path)
            if is_dir:
                child.setChildIndicatorPolicy(QtWidgets.QTreeWidgetItem.ShowIndicator)
            item.addChild(child)
        item._loaded = True  # type: ignore[attr-defined]

    def on_ftp_selection(self) -> None:
        item = self._current_ftp_item()
        if item is None or not hasattr(self, "ftp") or self.ftp is None:
            return
        
        # 如果正在下载，先停止旧的下载任务
        if hasattr(self, "download_worker") and self.download_worker is not None:
            if self.download_worker.isRunning():
                self.download_worker.terminate()  # 终止下载线程
                self.download_worker.wait(1000)  # 等待最多1秒
                if self.download_worker.isRunning():
                    self.download_worker.terminate()  # 强制终止
                self.download_worker = None
        
        # 停止播放器（异步执行，避免阻塞）
        self.player.stop()
        # 延迟一小段时间确保停止完成
        QtCore.QTimer.singleShot(100, lambda: None)
        
        path = item.data(0, QtCore.Qt.UserRole) or ""
        
        # 使用工作线程下载预览图片，避免阻塞UI
        cover_name = self.cfg.get("cover_filename")
        thumb_name = self.cfg.get("snapshot_filename")
        tmp_dir = Path(tempfile.gettempdir())
        
        # 封面在父级目录
        parent_dir = str(Path(path).parent).replace("\\", "/")
        remote_cover = f"{parent_dir}/{cover_name}"
        local_cover = tmp_dir / f"preview_cover_{hash(remote_cover)}.jpg"
        
        # 缩略图在当前目录（如果是 _hls 目录）
        # 优先显示 first_frame.jpg，如果没有则显示 thumbnail.jpg
        first_frame_name = self.cfg.get("first_frame_filename", "first_frame.jpg")
        remote_first_frame = f"{path.rstrip('/')}/{first_frame_name}"
        local_first_frame = tmp_dir / f"preview_first_frame_{hash(remote_first_frame)}.jpg"
        
        remote_thumb = f"{path.rstrip('/')}/{thumb_name}"
        local_thumb = tmp_dir / f"preview_thumb_{hash(remote_thumb)}.jpg"
        
        def on_cover_downloaded(local_path: str) -> None:
            self.preview.set_image(self.preview.cover_label, Path(local_path))
        
        def on_thumb_downloaded(local_path: str) -> None:
            self.preview.set_image(self.preview.thumb_label, Path(local_path))
        
        # 启动预览图片下载线程
        ftp_cfg = self.cfg.get("ftp", {})
        preview_worker = FtpPreviewWorker(
            ftp_cfg, remote_cover, str(local_cover),
            remote_first_frame, str(local_first_frame),
            remote_thumb, str(local_thumb)
        )
        
        def cleanup_preview_worker() -> None:
            if preview_worker in self.active_workers:
                self.active_workers.remove(preview_worker)
        
        preview_worker.cover_downloaded.connect(on_cover_downloaded)
        preview_worker.thumb_downloaded.connect(on_thumb_downloaded)
        preview_worker.finished.connect(cleanup_preview_worker)
        self.active_workers.append(preview_worker)
        preview_worker.start()

        # 若选择为 *_hls 目录且存在 m3u8，使用后台线程下载整个 HLS 目录到临时目录后播放（更稳定）
        suffix = self.cfg.get("accepted_video_dir_suffix")
        if path.endswith(suffix):
            m3u8_name = self.cfg.get("m3u8_filename")
            remote_m3u8 = f"{path.rstrip('/')}/{m3u8_name}"
            # 在主线程中检查文件是否存在（快速操作）
            try:
                if self.ftp.exists(remote_m3u8):
                    # 如果启用了自动清理，使用工作线程清理旧缓存（避免阻塞UI）
                    if self.cfg.get("auto_clean_cache", True):
                        max_dirs = int(self.cfg.get("max_cache_dirs", 5))
                        cleanup_worker = CacheCleanupWorker(max_dirs, self.current_cache_dir)
                        
                        def cleanup_cache_worker() -> None:
                            if cleanup_worker in self.active_workers:
                                self.active_workers.remove(cleanup_worker)
                        
                        cleanup_worker.finished.connect(cleanup_cache_worker)
                        self.active_workers.append(cleanup_worker)
                        cleanup_worker.start()  # 异步清理，不等待完成
                    
                    # 创建临时本地 HLS 目录
                    hls_dir_name = Path(path).name
                    local_hls_dir = tmp_dir / f"hls_cache_{hash(path)}" / hls_dir_name
                    local_hls_dir.mkdir(parents=True, exist_ok=True)
                    self.current_cache_dir = local_hls_dir  # 记录当前使用的缓存
                    
                    # 在状态栏显示下载进度
                    self.statusBar.showMessage(f"正在从 FTP 下载 HLS 文件到: {local_hls_dir}")
                    self.download_progress.setVisible(True)
                    self.download_progress.setRange(0, 0)  # 不确定进度模式
                    self.download_label.setVisible(True)
                    self.download_label.setText("下载中...")
                    
                    # 启动后台下载线程（传入预览时长设置和多线程选项）
                    preview_duration = int(self.cfg.get("preview_duration", 30))
                    use_multi_thread = bool(self.cfg.get("multi_thread_download", False))
                    # 调试信息
                    if use_multi_thread:
                        self.statusBar.showMessage(f"✓ 多线程下载已启用", 2000)
                    # 为下载线程创建新的FTP连接，避免线程安全问题
                    self.download_worker = FtpDownloadWorker(ftp_cfg, path, local_hls_dir, m3u8_name, preview_duration, use_multi_thread)
                    self.download_worker.progress.connect(self.on_download_progress)
                    self.download_worker.finished.connect(self.on_download_finished)
                    
                    def cleanup_download_worker() -> None:
                        if hasattr(self, 'download_worker') and self.download_worker in self.active_workers:
                            self.active_workers.remove(self.download_worker)
                    
                    self.download_worker.finished.connect(cleanup_download_worker)
                    self.active_workers.append(self.download_worker)
                    self.download_worker.start()
            except Exception as e:
                self.statusBar.showMessage(f"✗ 检查文件失败: {str(e)}", 3000)

    def _current_ftp_item(self) -> QtWidgets.QTreeWidgetItem | None:
        items = self.tree_ftp.selectedItems()
        return items[0] if items else None

    def show_settings(self) -> None:
        """显示设置对话框"""
        dialog = SettingsDialog(self.cfg, self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            settings = dialog.get_settings()
            # 更新 FTP 配置
            ftp_cfg = self.cfg.get("ftp", {})
            ftp_cfg.update(settings["ftp"])
            self.cfg.set("ftp", ftp_cfg)
            # 更新预览时长
            self.cfg.set("preview_duration", settings["preview_duration"])
            # 更新显示过滤设置
            self.cfg.set("filter_id_dirs", settings["filter_id_dirs"])
            self.cfg.set("show_only_id_folders", settings["show_only_id_folders"])
            self.cfg.set("play_on_end", settings["play_on_end"])
            # 更新缓存设置
            self.cfg.set("auto_clean_cache", settings["auto_clean_cache"])
            self.cfg.set("multi_thread_download", settings["multi_thread_download"])
            self.cfg.set("max_cache_dirs", settings["max_cache_dirs"])
            # 更新快捷键设置
            self.cfg.set("shortcuts", settings["shortcuts"])
            # 更新其他设置
            old_theme = self.cfg.get("theme", "light")
            new_theme = settings["theme"]
            self.cfg.set("theme", new_theme)
            self.cfg.set("start_minimized", settings["start_minimized"])
            self.cfg.set("cleanup_on_exit", settings["cleanup_on_exit"])
            self.cfg.set("cleanup_script", settings["cleanup_script"])
            
            # 如果主题改变了，重新应用主题
            if old_theme != new_theme:
                if new_theme == "dark":
                    self.apply_dark_theme()
                else:
                    # 重置为明亮主题（清除暗黑主题样式并重新应用明亮主题）
                    self.setStyleSheet("")  # 清除所有样式
                    self.setPalette(QtWidgets.QApplication.palette())  # 重置调色板
                    self.apply_beautiful_style()  # 重新应用明亮主题样式
            
            # 重新设置快捷键
            self._setup_shortcuts()
            
            # 如果 FTP 配置或显示设置更改了，自动重新连接
            if hasattr(self, "ftp"):
                # 断开当前连接
                self.ftp_connected = False
                self.ftp_retry_timer.stop()
                self.ftp_retry_countdown_timer.stop()
                if self.ftp:
                    self.ftp.disconnect()
                # 重新连接
                QtCore.QTimer.singleShot(500, self.connect_ftp)
                self.statusBar.showMessage("✓ 设置已保存，正在重新连接 FTP...", 2000)
            else:
                self.statusBar.showMessage("✓ 设置已保存", 2000)

    def on_download_progress(self, message: str) -> None:
        """更新下载进度显示"""
        self.statusBar.showMessage(message)
        self.download_label.setText(message)

    def _setup_shortcuts(self) -> None:
        """设置快捷键"""
        # 移除旧的快捷键
        for shortcut in self.shortcuts.values():
            if shortcut:
                shortcut.setEnabled(False)
        self.shortcuts.clear()
        
        # 从配置中读取快捷键
        shortcuts_cfg = self.cfg.get("shortcuts", {})
        default_shortcuts = {
            "play": "Space",
            "pause": "Space",
            "stop": "S",
            "snapshot": "P",
            "snapshot_cover": "C",
            "settings": "F1",
            "speed_up": "E",
            "speed_down": "Q",
            "speed_reset": "W",
        }
        
        # 播放快捷键
        play_seq = shortcuts_cfg.get("play", default_shortcuts["play"])
        if play_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(play_seq), self)
            shortcut.activated.connect(self.player.play)
            self.shortcuts["play"] = shortcut
            self.btn_play.setText(f"播放")
        else:
            self.btn_play.setText("播放")
        
        # 暂停快捷键
        pause_seq = shortcuts_cfg.get("pause", default_shortcuts["pause"])
        if pause_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(pause_seq), self)
            shortcut.activated.connect(self.player.pause)
            self.shortcuts["pause"] = shortcut
            self.btn_pause.setText(f"暂停")
        else:
            self.btn_pause.setText("暂停")
        
        # 停止快捷键
        stop_seq = shortcuts_cfg.get("stop", default_shortcuts["stop"])
        if stop_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(stop_seq), self)
            shortcut.activated.connect(self.player.stop)
            self.shortcuts["stop"] = shortcut
            self.btn_stop.setText(f"停止 ({stop_seq})")
        else:
            self.btn_stop.setText("停止")
        
        # 截图快捷键
        snapshot_seq = shortcuts_cfg.get("snapshot", default_shortcuts["snapshot"])
        if snapshot_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(snapshot_seq), self)
            shortcut.activated.connect(self.on_snapshot)
            self.shortcuts["snapshot"] = shortcut
            self.btn_snapshot.setText(f"截图 ({snapshot_seq})")
        else:
            self.btn_snapshot.setText("截图")
        
        # 截图封面快捷键
        snapshot_cover_seq = shortcuts_cfg.get("snapshot_cover", default_shortcuts["snapshot_cover"])
        if snapshot_cover_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(snapshot_cover_seq), self)
            shortcut.activated.connect(self.on_snapshot_cover)
            self.shortcuts["snapshot_cover"] = shortcut
            self.btn_snapshot_cover.setText(f"截图封面 ({snapshot_cover_seq})")
        else:
            self.btn_snapshot_cover.setText("截图封面")
        
        # 设置快捷键
        settings_seq = shortcuts_cfg.get("settings", default_shortcuts["settings"])
        if settings_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(settings_seq), self)
            shortcut.activated.connect(self.show_settings)
            self.shortcuts["settings"] = shortcut
            self.btn_settings.setText(f"设置 ({settings_seq})")
        else:
            self.btn_settings.setText("设置")
        
        # 播放速度控制快捷键（从设置中读取）
        # 加快播放速度
        speed_up_seq = shortcuts_cfg.get("speed_up", default_shortcuts["speed_up"])
        if speed_up_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(speed_up_seq), self)
            shortcut.activated.connect(self.increase_speed)
            self.shortcuts["speed_up"] = shortcut
        
        # 减慢播放速度
        speed_down_seq = shortcuts_cfg.get("speed_down", default_shortcuts["speed_down"])
        if speed_down_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(speed_down_seq), self)
            shortcut.activated.connect(self.decrease_speed)
            self.shortcuts["speed_down"] = shortcut
        
        # 重置播放速度
        speed_reset_seq = shortcuts_cfg.get("speed_reset", default_shortcuts["speed_reset"])
        if speed_reset_seq:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(speed_reset_seq), self)
            shortcut.activated.connect(self.reset_speed)
            self.shortcuts["speed_reset"] = shortcut
    
    def _cleanup_old_cache(self) -> None:
        """清理旧的缓存目录"""
        if not self.cfg.get("auto_clean_cache", True):
            return
        
        tmp_dir = Path(tempfile.gettempdir())
        max_dirs = int(self.cfg.get("max_cache_dirs", 5))
        
        # 查找所有 hls_cache_xxx 目录
        cache_dirs = []
        try:
            for item in tmp_dir.iterdir():
                if item.is_dir() and item.name.startswith("hls_cache_"):
                    # 获取目录修改时间
                    try:
                        mtime = item.stat().st_mtime
                        cache_dirs.append((mtime, item))
                    except Exception:
                        pass
        except Exception:
            return
        
        # 按修改时间排序（最旧的在前面）
        cache_dirs.sort(key=lambda x: x[0])
        
        # 如果超过最大数量，删除最旧的（但保留当前正在使用的）
        if len(cache_dirs) > max_dirs:
            to_delete = cache_dirs[: len(cache_dirs) - max_dirs]
            for _, cache_dir in to_delete:
                # 不删除当前正在使用的缓存
                if self.current_cache_dir and cache_dir == self.current_cache_dir:
                    continue
                
                try:
                    shutil.rmtree(cache_dir, ignore_errors=True)
                except Exception:
                    pass
    
    def on_download_finished(self, local_dir: str, count: int, success: bool) -> None:
        """下载完成回调"""
        # 隐藏进度条
        self.download_progress.setVisible(False)
        self.download_label.setVisible(False)
        
        if success:
            m3u8_name = self.cfg.get("m3u8_filename")
            local_m3u8 = Path(local_dir) / m3u8_name
            if local_m3u8.exists():
                # 播放本地下载的 m3u8（所有 .ts 也在本地）
                self.player.open(str(local_m3u8))
                self.current_cache_dir = Path(local_dir)  # 更新当前缓存目录
                self.statusBar.showMessage(
                    f"✓ 下载完成：已下载 {count} 个文件，开始播放 | 临时目录：{local_dir}",
                    5000,  # 显示 5 秒
                )
            else:
                self.statusBar.showMessage("✗ 播放失败：m3u8 文件不存在", 5000)
        else:
            self.statusBar.showMessage(f"✗ 下载失败 | 临时目录：{local_dir}", 5000)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    cfg_path = Path(__file__).with_name("Config.json")
    cfg = ConfigManager(cfg_path)
    win = MainWindow(cfg)
    
    # 检查依赖，通过状态栏提示
    if vlc is None:
        win.statusBar.showMessage("⚠ 未检测到 python-vlc，请安装后重试：pip install python-vlc PySide6", 10000)
    
    # 程序关闭时清理缓存和运行清理脚本
    def cleanup_on_exit():
        cleanup_enabled = cfg.get("cleanup_on_exit", True)
        cleanup_script = cfg.get("cleanup_script", "").strip()
        
        # 清理缓存
        if cleanup_enabled:
            tmp_dir = Path(tempfile.gettempdir())
            # 清理所有 hls_cache_xxx 目录（关闭时全部清理）
            try:
                for item in tmp_dir.iterdir():
                    if item.is_dir() and item.name.startswith("hls_cache_"):
                        try:
                            shutil.rmtree(item, ignore_errors=True)
                        except Exception:
                            pass
            except Exception:
                pass
        
        # 运行清理脚本
        if cleanup_script:
            script_path = Path(cleanup_script)
            if script_path.exists() and script_path.suffix.lower() == ".bat":
                try:
                    # 在后台运行批处理脚本（不阻塞关闭）
                    subprocess.Popen(
                        [str(script_path)],
                        shell=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                except Exception:
                    pass
    
    app.aboutToQuit.connect(cleanup_on_exit)
    
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


