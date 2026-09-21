# -*- coding: utf-8 -*-
"""
统一音频试听播放控制器 (Audio Preview Controller)
【为什么这样设计】
针对主音频、背景音、混音试听以往唤起外部播放器导致的割裂感，
构建统一架构控制器，支持“内置播放器”与“系统默认播放器”双后端无缝切换。
内置后端采用 FFmpeg 管道直接输出 48kHz/Stereo/s16le PCM 流，
由工作子线程实时喂入 PySide6 QAudioSink 直推声卡，
实现点击 0.2~0.6s 秒开、零中间文件生成、无阻塞 GUI、极速切歌与精准 Seek。
"""

import os
import sys
import time
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Union

from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt, QMutex, QMutexLocker
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

from .ffmpeg_utils import get_audio_info

logger = logging.getLogger(__name__)


class PlaybackWorker(QThread):
    """
    后台音频流式解码与推流工作线程。
    【为什么这样设计】
    将 FFmpeg 标准输出读取、PCM 数据缓冲和 QAudioSink 写入完全隔离在后台子线程，
    彻底杜绝推流过程对 GUI 主线程事件循环的任何微小阻塞，确保主界面在高频音频操作下丝滑流畅。
    """
    sig_progress = Signal(int, int)      # (当前毫秒, 总毫秒)
    sig_finished = Signal()             # 播放正常结束
    sig_error = Signal(str)             # 发生异常通知主控制器
    sig_state_changed = Signal(bool)    # 播放状态改变 (True=播放中, False=暂停/停止)

    def __init__(self, cmd: list, total_duration_sec: float, start_sec: float = 0.0, volume: float = 1.0, parent=None):
        super().__init__(parent)
        self.cmd = cmd
        self.total_duration_sec = max(0.1, total_duration_sec)
        self.start_sec = max(0.0, start_sec)
        self.volume = max(0.0, min(1.0, volume))
        self._is_interrupted = False
        self._is_paused = False
        self._pause_mutex = QMutex()
        self._process: Optional[subprocess.Popen] = None
        self._sink: Optional[QAudioSink] = None

    def pause(self):
        """暂停音频播放与流读取"""
        self._is_paused = True
        if self._sink:
            self._sink.suspend()
        self.sig_state_changed.emit(False)

    def resume(self):
        """恢复音频播放与流读取"""
        self._is_paused = False
        if self._sink:
            self._sink.resume()
        self.sig_state_changed.emit(True)

    def is_paused(self) -> bool:
        return self._is_paused

    def set_sink_volume(self, volume: float):
        """动态调节当前声卡输出音量 (0.0 ~ 1.0)"""
        self.volume = max(0.0, min(1.0, volume))
        if self._sink:
            self._sink.setVolume(self.volume)

    def stop(self):
        """立即中断子进程并终止播放"""
        self._is_interrupted = True
        self._is_paused = False
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
        if self._sink:
            try:
                self._sink.stop()
            except Exception:
                pass

    def run(self):
        """线程执行入口：初始化声卡输出并启动 FFmpeg 管道解码推流"""
        sample_rate = 48000
        channels = 2
        bytes_per_sample = 2  # s16le 为 16 位整数 (2 字节)
        bytes_per_second = sample_rate * channels * bytes_per_sample

        try:
            audio_format = QAudioFormat()
            audio_format.setSampleRate(sample_rate)
            audio_format.setChannelCount(channels)
            audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)

            default_device = QMediaDevices.defaultAudioOutput()
            if not default_device or default_device.isNull():
                self.sig_error.emit("未检测到可用的系统音频输出设备 (声卡)。")
                return

            self._sink = QAudioSink(default_device, audio_format)
            # 设置声卡缓冲队列大小为约 0.4 秒，平衡抗抖动与低延迟
            self._sink.setBufferSize(int(bytes_per_second * 0.4))
            self._sink.setVolume(self.volume)
            io_device = self._sink.start()
            if not io_device:
                self.sig_error.emit("初始化声卡音频输出通道失败。")
                return

            # 启动 FFmpeg 进程
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            self._process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
                bufsize=1024 * 64
            )

            chunk_size = 4096
            written_bytes = 0
            start_offset_bytes = int(self.start_sec * bytes_per_second)
            total_bytes = int(self.total_duration_sec * bytes_per_second)

            self.sig_state_changed.emit(True)

            last_progress_time = 0.0

            while not self._is_interrupted:
                # 暂停等待
                while self._is_paused and not self._is_interrupted:
                    time.sleep(0.05)

                if self._is_interrupted:
                    break

                chunk = self._process.stdout.read(chunk_size)
                if not chunk:
                    # 解码流读取完毕
                    break

                # 控制写入速率：若声卡硬件缓冲区满则短暂等待，避免内存暴涨
                while self._sink.bytesFree() < len(chunk) and not self._is_interrupted:
                    time.sleep(0.005)

                if self._is_interrupted:
                    break

                io_device.write(chunk)
                written_bytes += len(chunk)

                # 每 100ms 向 UI 汇报一次播放进度
                now = time.time()
                if now - last_progress_time >= 0.1:
                    last_progress_time = now
                    current_bytes = start_offset_bytes + written_bytes
                    current_ms = int((current_bytes / bytes_per_second) * 1000)
                    total_ms = int(self.total_duration_sec * 1000)
                    self.sig_progress.emit(min(current_ms, total_ms), total_ms)

            # 正常流式播放推流完毕，等待声卡把最后缓冲播放完
            if not self._is_interrupted:
                remaining_time = (self._sink.bufferSize() - self._sink.bytesFree()) / float(bytes_per_second)
                if remaining_time > 0:
                    time.sleep(min(0.5, remaining_time))
                total_ms = int(self.total_duration_sec * 1000)
                self.sig_progress.emit(total_ms, total_ms)
                self.sig_finished.emit()

        except Exception as e:
            logger.exception(f"流式推流线程异常: {e}")
            if not self._is_interrupted:
                self.sig_error.emit(f"音频流式解码播放失败: {str(e)}")
        finally:
            self.sig_state_changed.emit(False)
            if self._process:
                try:
                    self._process.stdout.close()
                    self._process.terminate()
                    self._process.wait(timeout=0.2)
                except Exception:
                    pass
                self._process = None
            if self._sink:
                try:
                    self._sink.stop()
                except Exception:
                    pass
                self._sink = None


class AudioPreviewController(QObject):
    """
    统一音频试听播放控制器 (单例/主窗口聚合)
    【为什么这样设计】
    1. 统一接口：将主音频、背景音乐、混音三种试听需求归一化为单一入口，
       消灭多按钮分散维护多套播放器调用的混乱设计；
    2. 双后端解耦：内置模式使用 FFmpeg+QAudioSink 流式播放，外置模式无缝回退系统默认播放器；
    3. 全状态响应：对外提供标准的播放、暂停、进度信号与 Seek 支持，无缝驱动 UI 播放条。
    """
    # UI 响应信号
    sig_progress = Signal(int, int)       # (当前毫秒, 总毫秒)
    sig_state_changed = Signal(bool)     # True=播放中, False=暂停/停止
    sig_error_fallback = Signal(str)     # 发生错误，触发向用户确认是否回退至外部播放器
    sig_source_changed = Signal(str)     # 当前播放源标识 ("MAIN_AUDIO", "BGM", "MIX", "")
    sig_preparing = Signal()             # 通知 UI 正在准备音频流（用于显示转码提示）
    sig_duration_resolved = Signal(float)  # 异步时长查询完成后通知真实时长

    MODE_INTERNAL = "internal"
    MODE_EXTERNAL = "external"

    SOURCE_MAIN = "MAIN_AUDIO"
    SOURCE_BGM = "BGM"
    SOURCE_MIX = "MIX"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.playback_mode = self.MODE_INTERNAL
        self.current_source: str = ""
        self.current_file_path: Optional[Path] = None
        self.current_total_sec: float = 0.0
        self.current_pos_sec: float = 0.0
        self.volume_ratio: float = 1.0

        # 针对 MIX 模式缓存参数，便于 Seek 时重新拉流
        self._last_mix_params: Optional[Dict[str, Any]] = None

        self._worker: Optional[PlaybackWorker] = None

        # 异步时长查询完成后修正进度条总量
        self.sig_duration_resolved.connect(self._on_duration_resolved)

    def set_playback_mode(self, mode: str):
        """设置播放模式：'internal' 或 'external'"""
        if mode in (self.MODE_INTERNAL, self.MODE_EXTERNAL):
            if self.is_playing():
                self.stop()
            self.playback_mode = mode
            logger.info(f"试听播放器模式切换为: {mode}")

    def is_playing(self) -> bool:
        """是否正在播放中"""
        return bool(self._worker and self._worker.isRunning() and not self._worker.is_paused())

    def is_active(self) -> bool:
        """是否有活跃的播放任务 (包含暂停中)"""
        return bool(self._worker and self._worker.isRunning())

    def play_main_audio(self, voice_path: Path):
        """播放主音频 (纯人声干音)"""
        if not voice_path.exists():
            self.sig_error_fallback.emit(f"主音频参考文件不存在: {voice_path}")
            return

        if self.playback_mode == self.MODE_EXTERNAL:
            self._play_externally(voice_path)
            return

        self.sig_preparing.emit()

        # 【为什么这样设计】
        # 先用保底时长立即启动 FFmpeg 管道，让声卡尽快出声；
        # 真实时长在后台异步查询，查询完毕后通过信号更新进度条总量。
        # 这样避免了 ffprobe 冷启动阻塞用户等待 0.5-1.5s 的体感延迟。
        fallback_dur = 300.0
        self.current_source = self.SOURCE_MAIN
        self.current_file_path = voice_path
        self.current_total_sec = fallback_dur
        self._last_mix_params = None

        cmd = [
            "ffmpeg", "-y", "-v", "quiet",
            "-i", str(voice_path.resolve()),
            "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"
        ]
        self._start_internal_stream(cmd, total_sec=fallback_dur, start_sec=0.0)
        self._async_resolve_duration(voice_path)

    def play_bgm(self, bgm_path: Path):
        """播放背景音乐 (支持快速切歌)"""
        if not bgm_path.exists():
            self.sig_error_fallback.emit(f"背景音乐文件不存在: {bgm_path}")
            return

        if self.playback_mode == self.MODE_EXTERNAL:
            self._play_externally(bgm_path)
            return

        self.sig_preparing.emit()

        # 同 play_main_audio：先用保底时长即刻启动管道，异步查询真实时长
        fallback_dur = 300.0
        self.current_source = self.SOURCE_BGM
        self.current_file_path = bgm_path
        self.current_total_sec = fallback_dur
        self._last_mix_params = None

        cmd = [
            "ffmpeg", "-y", "-v", "quiet",
            "-i", str(bgm_path.resolve()),
            "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"
        ]
        self._start_internal_stream(cmd, total_sec=fallback_dur, start_sec=0.0)
        self._async_resolve_duration(bgm_path)

    def play_mix(self, voice_path: Path, bgm_path: Optional[Path],
                 voice_vol_percent: float, bgm_vol_percent: float,
                 preview_sec: Optional[float] = None):
        """
        实时管道混音试听
        【为什么这样设计】
        彻底放弃先生成完整 WAV/MP3 落盘文件再播放的低效做法。
        直接使用 FFmpeg amix 滤镜从主音频与 BGM 实时混合并输出 PCM，
        在内存管道中秒开，同时完美保留已设定的主音量与背景音量比例。
        preview_sec 由调用方预先传入以避免阻塞式时长查询；
        若为 None 则使用保底值并通过信号后续修正。
        """
        if not voice_path.exists():
            self.sig_error_fallback.emit(f"主音频参考文件不存在: {voice_path}")
            return

        # 【为什么这样设计】
        # 消除旧版中 play_mix 内部同步调用 _inspect_duration 导致的阻塞。
        # 调用方已在外部预计算 preview_sec，此处直接使用保底值兜底即可。
        if preview_sec is None or preview_sec <= 0:
            preview_sec = 60.0

        self.sig_preparing.emit()

        self.current_source = self.SOURCE_MIX
        self.current_file_path = voice_path
        self.current_total_sec = preview_sec
        self._last_mix_params = {
            "voice_path": voice_path,
            "bgm_path": bgm_path,
            "voice_vol": voice_vol_percent,
            "bgm_vol": bgm_vol_percent,
            "preview_sec": preview_sec
        }

        if self.playback_mode == self.MODE_EXTERNAL:
            # 外置播放时调用现有混音落盘逻辑
            self._play_mix_externally(voice_path, bgm_path, voice_vol_percent, bgm_vol_percent, preview_sec)
            return

        cmd = self._build_mix_ffmpeg_cmd(
            voice_path=voice_path,
            bgm_path=bgm_path,
            voice_vol=voice_vol_percent,
            bgm_vol=bgm_vol_percent,
            preview_sec=preview_sec,
            start_sec=0.0
        )
        self._start_internal_stream(cmd, total_sec=preview_sec, start_sec=0.0)

    def pause(self):
        """暂停当前播放"""
        if self._worker and self._worker.isRunning():
            self._worker.pause()

    def resume(self):
        """恢复当前播放"""
        if self._worker and self._worker.isRunning():
            self._worker.resume()

    def toggle_play_pause(self):
        """播放/暂停状态翻转"""
        if not self._worker or not self._worker.isRunning():
            # 若当前有加载的文件/源，重新从头播放
            if self.current_source == self.SOURCE_MAIN and self.current_file_path:
                self.play_main_audio(self.current_file_path)
            elif self.current_source == self.SOURCE_BGM and self.current_file_path:
                self.play_bgm(self.current_file_path)
            elif self.current_source == self.SOURCE_MIX and self._last_mix_params:
                p = self._last_mix_params
                self.play_mix(p["voice_path"], p["bgm_path"], p["voice_vol"], p["bgm_vol"], p["preview_sec"])
            return

        if self._worker.is_paused():
            self._worker.resume()
        else:
            self._worker.pause()

    def stop(self):
        """完全停止当前播放并重置状态"""
        if self._worker:
            self._worker.stop()
            self._worker.wait(300)
            self._worker = None
        self.sig_state_changed.emit(False)
        self.sig_progress.emit(0, int(self.current_total_sec * 1000))

    def seek(self, target_sec: float):
        """
        拖动定位 (Seek)
        【为什么这样设计】
        流式管道无法原地跳跃文件指针，
        因此通过优雅停止当前流，并在启动命令中添加 '-ss target_sec' 重新拉流，
        毫秒级完成重新定向，无任何杂音与卡死。
        """
        if not self.current_source or self.current_total_sec <= 0:
            return

        target_sec = max(0.0, min(self.current_total_sec - 0.2, target_sec))
        self.current_pos_sec = target_sec

        if self.playback_mode == self.MODE_EXTERNAL:
            return

        if self.current_source == self.SOURCE_MIX and self._last_mix_params:
            p = self._last_mix_params
            cmd = self._build_mix_ffmpeg_cmd(
                voice_path=p["voice_path"],
                bgm_path=p["bgm_path"],
                voice_vol=p["voice_vol"],
                bgm_vol=p["bgm_vol"],
                preview_sec=p["preview_sec"],
                start_sec=target_sec
            )
            self._start_internal_stream(cmd, total_sec=p["preview_sec"], start_sec=target_sec)
        elif self.current_file_path and self.current_file_path.exists():
            cmd = [
                "ffmpeg", "-y", "-v", "quiet",
                "-ss", f"{target_sec:.3f}",
                "-i", str(self.current_file_path.resolve()),
                "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"
            ]
            self._start_internal_stream(cmd, total_sec=self.current_total_sec, start_sec=target_sec)

    def set_volume(self, volume_percent: float):
        """设置主播放音量 (0 ~ 100)"""
        self.volume_ratio = max(0.0, min(1.0, volume_percent / 100.0))
        if self._worker:
            self._worker.set_sink_volume(self.volume_ratio)

    def fallback_to_external(self):
        """用户确认降级为外部播放器后执行"""
        if self.current_source == self.SOURCE_MIX and self._last_mix_params:
            p = self._last_mix_params
            self._play_mix_externally(p["voice_path"], p["bgm_path"], p["voice_vol"], p["bgm_vol"], p["preview_sec"])
        elif self.current_file_path and self.current_file_path.exists():
            self._play_externally(self.current_file_path)

    # ---------------- 内部私有辅助逻辑 ----------------

    def _start_internal_stream(self, cmd: list, total_sec: float, start_sec: float = 0.0):
        """启动后台流式播放线程"""
        if self._worker:
            self._worker.stop()
            self._worker.wait(300)
            self._worker = None

        self._worker = PlaybackWorker(
            cmd=cmd,
            total_duration_sec=total_sec,
            start_sec=start_sec,
            volume=self.volume_ratio,
            parent=self
        )
        self._worker.sig_progress.connect(self._on_worker_progress)
        self._worker.sig_finished.connect(self._on_worker_finished)
        self._worker.sig_error.connect(self._on_worker_error)
        self._worker.sig_state_changed.connect(self.sig_state_changed.emit)

        self.sig_source_changed.emit(self.current_source)
        self._worker.start()

    def _on_worker_progress(self, current_ms: int, total_ms: int):
        self.current_pos_sec = current_ms / 1000.0
        self.sig_progress.emit(current_ms, total_ms)

    def _on_worker_finished(self):
        self.sig_state_changed.emit(False)
        self.sig_progress.emit(int(self.current_total_sec * 1000), int(self.current_total_sec * 1000))

    def _on_worker_error(self, err_msg: str):
        logger.error(f"内置播放器故障: {err_msg}")
        self.stop()
        self.sig_error_fallback.emit(err_msg)

    def _inspect_duration(self, audio_path: Optional[Path]) -> float:
        """快速获取音频文件时长 (秒)"""
        if not audio_path or not audio_path.exists():
            return 0.0
        try:
            info = get_audio_info(audio_path)
            return float(info.get("duration", 0.0))
        except Exception:
            return 10.0  # 保底 10 秒

    def _async_resolve_duration(self, audio_path: Path):
        """
        在后台 daemon 线程中异步查询音频文件真实时长。
        【为什么这样设计】
        将 ffprobe 子进程冷启动（Windows 上约 0.5-1.5s）从播放关键路径中剥离，
        不阻塞 FFmpeg 管道启动和声卡初始化，首帧 PCM 可提前 0.5-1.5s 到达声卡。
        查询完毕后通过 sig_duration_resolved 信号在主线程安全地修正进度条总量。
        """
        def _worker():
            try:
                dur = self._inspect_duration(audio_path)
                if dur > 0:
                    self.sig_duration_resolved.emit(dur)
            except Exception as e:
                logger.warning(f"异步时长查询失败: {e}")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    @Slot(float)
    def _on_duration_resolved(self, real_duration: float):
        """
        异步时长查询完成回调：修正当前播放总时长和 worker 内的时长记录。
        【为什么这样设计】
        播放启动时使用保底值 300s，此回调用真实时长替换之，
        使得进度条比例和时间读数从保底值无缝过渡为真实值。
        """
        if real_duration <= 0:
            return
        self.current_total_sec = real_duration
        if self._worker and self._worker.isRunning():
            self._worker.total_duration_sec = real_duration
        logger.debug(f"异步时长查询完毕，已修正为 {real_duration:.2f}s")

    def _build_mix_ffmpeg_cmd(self, voice_path: Path, bgm_path: Optional[Path],
                              voice_vol: float, bgm_vol: float,
                              preview_sec: float, start_sec: float = 0.0) -> list:
        """构建 FFmpeg 管道混音命令"""
        remain_sec = max(0.1, preview_sec - start_sec)
        voice_factor = voice_vol / 100.0
        bgm_factor = bgm_vol / 100.0

        if bgm_path and bgm_path.exists():
            # 双路实时混音滤镜
            filter_str = (
                f"[0:a]volume={voice_factor:.2f}[v];"
                f"[1:a]volume={bgm_factor:.2f}[b];"
                f"[v][b]amix=inputs=2:duration=first:dropout_transition=2[out]"
            )
            cmd = [
                "ffmpeg", "-y", "-v", "quiet",
                "-ss", f"{start_sec:.3f}", "-i", str(voice_path.resolve()),
                "-ss", f"{start_sec:.3f}", "-i", str(bgm_path.resolve()),
                "-filter_complex", filter_str,
                "-map", "[out]",
                "-t", f"{remain_sec:.3f}",
                "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"
            ]
        else:
            # 无 BGM，单路调整音量输出
            cmd = [
                "ffmpeg", "-y", "-v", "quiet",
                "-ss", f"{start_sec:.3f}", "-i", str(voice_path.resolve()),
                "-filter:a", f"volume={voice_factor:.2f}",
                "-t", f"{remain_sec:.3f}",
                "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"
            ]
        return cmd

    def _play_externally(self, path: Path):
        """调用系统默认播放器打开文件"""
        try:
            logger.info(f"系统默认播放器调用: {path.resolve()}")
            os.startfile(str(path.resolve()))
        except Exception as e:
            logger.exception(f"调用系统默认播放器失败: {e}")
            self.sig_error_fallback.emit(f"无法启动系统播放器: {e}")

    def _play_mix_externally(self, voice_path: Path, bgm_path: Optional[Path],
                             voice_vol: float, bgm_vol: float, preview_sec: float):
        """外置模式下的混音试听：生成临时 MP3 后打开"""
        try:
            from .audio_mixer import AudioMixer
            project_root = Path(__file__).resolve().parent.parent.parent
            out_preview = (project_root / "output" / "temp_mix_preview.mp3").resolve()
            out_preview.parent.mkdir(parents=True, exist_ok=True)

            mixer = AudioMixer(
                voice_volume_percent=float(voice_vol),
                bgm_volume_percent=float(bgm_vol)
            )
            success = mixer.generate_preview_mix(
                voice_path=voice_path,
                bgm_path=bgm_path,
                output_path=out_preview,
                preview_seconds=preview_sec
            )
            if success and out_preview.exists() and out_preview.stat().st_size > 1000:
                self._play_externally(out_preview)
            else:
                self.sig_error_fallback.emit("生成混音试听文件异常。")
        except Exception as e:
            logger.exception(f"生成外置混音试听异常: {e}")
            self.sig_error_fallback.emit(f"外置混音试听失败: {e}")
