#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Atsushi Noda
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import io
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from abc import ABC, abstractmethod

import flet as ft

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * FRAME_MS // 1000  # 640 bytes


def pcm16_to_wav(pcm_data: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return buffer.getvalue()


def platform_name(page: ft.Page) -> str:
    value = getattr(page.platform, "value", str(page.platform))
    return str(value).lower().replace("pageplatform.", "")


class AudioBackend(ABC):
    """OS差を隠す 16 kHz / PCM16 / mono の音声I/O層。"""

    name = "unknown"

    def __init__(self, page: ft.Page):
        self.page = page

    async def initialize(self) -> None:
        pass

    @abstractmethod
    async def ensure_record_permission(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def start_capture(self) -> None:
        """連続マイク入力を開始する。"""
        raise NotImplementedError

    @abstractmethod
    async def read_frame(self) -> bytes:
        """20 ms (=640 bytes) のPCM16 monoを返す。"""
        raise NotImplementedError

    @abstractmethod
    async def stop_capture(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def play(self, pcm_data: bytes) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        try:
            await self.stop_capture()
        except Exception:
            pass


class _FrameQueueMixin:
    """任意サイズで届くPCMを20 msフレームへ整形する。"""

    def _init_frame_queue(self) -> None:
        self._frame_queue: queue.Queue[bytes] = queue.Queue(maxsize=300)
        self._frame_rem = bytearray()
        self._frame_lock = threading.Lock()
        self._capture_active = False

    def _push_pcm(self, data: bytes) -> None:
        if not data:
            return
        with self._frame_lock:
            self._frame_rem.extend(data)
            while len(self._frame_rem) >= FRAME_BYTES:
                frame = bytes(self._frame_rem[:FRAME_BYTES])
                del self._frame_rem[:FRAME_BYTES]
                try:
                    self._frame_queue.put_nowait(frame)
                except queue.Full:
                    # 古いフレームを捨てて遅延の累積を防ぐ。
                    try:
                        self._frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._frame_queue.put_nowait(frame)
                    except queue.Full:
                        pass

    def _clear_frames(self) -> None:
        with self._frame_lock:
            self._frame_rem.clear()
        while True:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    async def read_frame(self) -> bytes:
        while True:
            try:
                return await asyncio.to_thread(self._frame_queue.get, True, 0.25)
            except queue.Empty:
                if not self._capture_active:
                    raise RuntimeError("マイク入力が停止しました")


class AndroidNativeAudioBackend(_FrameQueueMixin, AudioBackend):
    """Android: PyJNIus -> AudioRecord / AudioTrack。"""

    name = "Android / AudioRecord + AudioTrack (PyJNIus)"

    def __init__(self, page: ft.Page):
        super().__init__(page)
        self._init_frame_queue()
        self._capture_stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._capture_error: Exception | None = None

    @staticmethod
    def _get_activity():
        from jnius import autoclass

        activity_host_class_name = os.environ.get("MAIN_ACTIVITY_HOST_CLASS_NAME")
        if not activity_host_class_name:
            raise RuntimeError("MAIN_ACTIVITY_HOST_CLASS_NAME が取得できません")
        activity_host = autoclass(activity_host_class_name)
        return activity_host.mActivity

    async def ensure_record_permission(self) -> bool:
        from jnius import autoclass

        PackageManager = autoclass("android.content.pm.PackageManager")
        permission = "android.permission.RECORD_AUDIO"
        activity = self._get_activity()
        if activity.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED:
            return True
        activity.requestPermissions([permission], 1001)
        return False

    def _capture_worker(self) -> None:
        from jnius import autoclass

        AudioRecord = autoclass("android.media.AudioRecord")
        AudioFormat = autoclass("android.media.AudioFormat")
        AudioSource = autoclass("android.media.MediaRecorder$AudioSource")

        recorder = None
        try:
            min_buffer_size = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            if min_buffer_size <= 0:
                raise RuntimeError(f"AudioRecord.getMinBufferSize()={min_buffer_size}")
            buffer_size = max(min_buffer_size, 4096)
            recorder = AudioRecord(
                AudioSource.MIC,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                buffer_size,
            )
            if recorder.getState() != AudioRecord.STATE_INITIALIZED:
                raise RuntimeError(f"AudioRecord初期化失敗: state={recorder.getState()}")

            recorder.startRecording()
            while not self._capture_stop.is_set():
                chunk = bytearray(buffer_size)
                n = recorder.read(chunk, 0, buffer_size)
                if n < 0:
                    raise RuntimeError(f"AudioRecord.read()={n}")
                if n:
                    self._push_pcm(bytes(chunk[:n]))
        except Exception as exc:
            self._capture_error = exc
        finally:
            if recorder is not None:
                try:
                    if recorder.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING:
                        recorder.stop()
                except Exception:
                    pass
                try:
                    recorder.release()
                except Exception:
                    pass

    async def start_capture(self) -> None:
        await self.stop_capture()
        self._clear_frames()
        self._capture_error = None
        self._capture_stop.clear()
        self._capture_active = True
        self._capture_thread = threading.Thread(target=self._capture_worker, daemon=True)
        self._capture_thread.start()
        await asyncio.sleep(0.12)
        if self._capture_error:
            raise self._capture_error

    async def read_frame(self) -> bytes:
        if self._capture_error:
            raise self._capture_error
        return await super().read_frame()

    async def stop_capture(self) -> None:
        self._capture_active = False
        self._capture_stop.set()
        th = self._capture_thread
        if th and th.is_alive():
            await asyncio.to_thread(th.join, 1.0)
        self._capture_thread = None

    @staticmethod
    def _play_sync(pcm_data: bytes) -> None:
        if not pcm_data:
            return
        from jnius import autoclass

        AudioTrack = autoclass("android.media.AudioTrack")
        AudioFormat = autoclass("android.media.AudioFormat")
        AudioAttributes = autoclass("android.media.AudioAttributes")
        AudioTrackBuilder = autoclass("android.media.AudioTrack$Builder")
        AudioFormatBuilder = autoclass("android.media.AudioFormat$Builder")
        AudioAttributesBuilder = autoclass("android.media.AudioAttributes$Builder")

        attrs = (AudioAttributesBuilder()
                 .setUsage(AudioAttributes.USAGE_MEDIA)
                 .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                 .build())
        fmt = (AudioFormatBuilder()
               .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
               .setSampleRate(SAMPLE_RATE)
               .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
               .build())
        track = (AudioTrackBuilder()
                 .setAudioAttributes(attrs)
                 .setAudioFormat(fmt)
                 .setTransferMode(AudioTrack.MODE_STATIC)
                 .setBufferSizeInBytes(len(pcm_data))
                 .build())
        try:
            state = track.getState()
            if state not in (AudioTrack.STATE_INITIALIZED, AudioTrack.STATE_NO_STATIC_DATA):
                raise RuntimeError(f"AudioTrack生成失敗: state={state}")
            java_bytes = bytearray(pcm_data)
            written = track.write(java_bytes, 0, len(java_bytes), pass_by_reference=False)
            if written < 0:
                raise RuntimeError(f"AudioTrack.write()={written}")
            track.play()
            duration = len(pcm_data) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
            time.sleep(duration + 0.15)
            if track.getPlayState() == AudioTrack.PLAYSTATE_PLAYING:
                track.stop()
        finally:
            track.release()

    async def play(self, pcm_data: bytes) -> None:
        await asyncio.to_thread(self._play_sync, pcm_data)


class LinuxAlsaAudioBackend(AudioBackend):
    """Ubuntu/Linux: ALSA arecord / aplay。"""

    name = "Linux / ALSA arecord + aplay"

    def __init__(self, page: ft.Page):
        super().__init__(page)
        self._proc: subprocess.Popen | None = None

    async def initialize(self) -> None:
        missing = [cmd for cmd in ("arecord", "aplay") if shutil.which(cmd) is None]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} が見つかりません。alsa-utils を確認してください。")

    async def ensure_record_permission(self) -> bool:
        return True

    def _start_capture_sync(self) -> None:
        self._proc = subprocess.Popen(
            ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(SAMPLE_RATE)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    async def start_capture(self) -> None:
        await self.stop_capture()
        await asyncio.to_thread(self._start_capture_sync)

    def _read_frame_sync(self) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("arecord が開始されていません")
        data = bytearray()
        while len(data) < FRAME_BYTES:
            chunk = self._proc.stdout.read(FRAME_BYTES - len(data))
            if not chunk:
                err = b""
                if self._proc.stderr:
                    try:
                        err = self._proc.stderr.read()
                    except Exception:
                        pass
                raise RuntimeError(f"arecord が終了しました: {err.decode(errors='ignore').strip()}")
            data.extend(chunk)
        return bytes(data)

    async def read_frame(self) -> bytes:
        return await asyncio.to_thread(self._read_frame_sync)

    def _stop_capture_sync(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    async def stop_capture(self) -> None:
        await asyncio.to_thread(self._stop_capture_sync)

    @staticmethod
    def _play_sync(pcm_data: bytes) -> None:
        if pcm_data:
            subprocess.run(["aplay", "-q"], input=pcm16_to_wav(pcm_data), check=True)

    async def play(self, pcm_data: bytes) -> None:
        await asyncio.to_thread(self._play_sync, pcm_data)


class WindowsWinMMAudioBackend(_FrameQueueMixin, AudioBackend):
    """Windows: WinMM waveIn microphone capture / winsound playback。"""

    name = "Windows / WinMM waveIn + winsound"

    def __init__(self, page: ft.Page):
        super().__init__(page)
        self._init_frame_queue()
        self._capture_stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._capture_error: Exception | None = None

    async def ensure_record_permission(self) -> bool:
        # WinMMにはFlet/Androidのような権限要求APIはない。
        # Windowsのマイク・プライバシー設定で拒否されている場合は、
        # start_capture() 内の waveInOpen() でエラーとして検出する。
        return True

    @staticmethod
    def _winmm_error_text(winmm, code: int) -> str:
        import ctypes

        buf = ctypes.create_unicode_buffer(256)
        try:
            rc = winmm.waveInGetErrorTextW(code, buf, len(buf))
            if rc == 0 and buf.value:
                return buf.value
        except Exception:
            pass
        return f"MMRESULT={code}"

    def _capture_worker(self) -> None:
        import ctypes
        from ctypes import wintypes

        # WinMM / waveform-audio constants
        WAVE_FORMAT_PCM = 0x0001
        WAVE_MAPPER = 0xFFFFFFFF
        CALLBACK_NULL = 0x00000000
        WHDR_DONE = 0x00000001

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [
                ("wFormatTag", wintypes.WORD),
                ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD),
            ]

        DWORD_PTR = ctypes.c_size_t

        class WAVEHDR(ctypes.Structure):
            _fields_ = [
                ("lpData", ctypes.c_char_p),
                ("dwBufferLength", wintypes.DWORD),
                ("dwBytesRecorded", wintypes.DWORD),
                ("dwUser", DWORD_PTR),
                ("dwFlags", wintypes.DWORD),
                ("dwLoops", wintypes.DWORD),
                ("lpNext", ctypes.c_void_p),
                ("reserved", DWORD_PTR),
            ]

        winmm = ctypes.WinDLL("winmm")
        HWAVEIN = wintypes.HANDLE

        winmm.waveInOpen.argtypes = [
            ctypes.POINTER(HWAVEIN), wintypes.UINT,
            ctypes.POINTER(WAVEFORMATEX), DWORD_PTR, DWORD_PTR, wintypes.DWORD,
        ]
        winmm.waveInOpen.restype = wintypes.UINT
        winmm.waveInPrepareHeader.argtypes = [HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT]
        winmm.waveInPrepareHeader.restype = wintypes.UINT
        winmm.waveInUnprepareHeader.argtypes = [HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT]
        winmm.waveInUnprepareHeader.restype = wintypes.UINT
        winmm.waveInAddBuffer.argtypes = [HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT]
        winmm.waveInAddBuffer.restype = wintypes.UINT
        winmm.waveInStart.argtypes = [HWAVEIN]
        winmm.waveInStart.restype = wintypes.UINT
        winmm.waveInReset.argtypes = [HWAVEIN]
        winmm.waveInReset.restype = wintypes.UINT
        winmm.waveInClose.argtypes = [HWAVEIN]
        winmm.waveInClose.restype = wintypes.UINT
        winmm.waveInGetErrorTextW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        winmm.waveInGetErrorTextW.restype = wintypes.UINT

        hwi = HWAVEIN()
        buffers = []
        headers = []
        prepared = []

        try:
            fmt = WAVEFORMATEX(
                wFormatTag=WAVE_FORMAT_PCM,
                nChannels=CHANNELS,
                nSamplesPerSec=SAMPLE_RATE,
                nAvgBytesPerSec=SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH,
                nBlockAlign=CHANNELS * SAMPLE_WIDTH,
                wBitsPerSample=SAMPLE_WIDTH * 8,
                cbSize=0,
            )

            rc = winmm.waveInOpen(
                ctypes.byref(hwi),
                WAVE_MAPPER,
                ctypes.byref(fmt),
                0,
                0,
                CALLBACK_NULL,
            )
            if rc != 0:
                msg = self._winmm_error_text(winmm, rc)
                raise RuntimeError(
                    f"Windowsマイクを開けません: {msg}. "
                    "Windowsの『設定 > プライバシーとセキュリティ > マイク』も確認してください。"
                )

            # 20 ms PCM16 mono (= 640 bytes) を複数バッファで回す。
            # 上位層のVADフレームサイズを変えず、録音遅延も小さく保つ。
            buffer_count = 8
            for _ in range(buffer_count):
                buf = ctypes.create_string_buffer(FRAME_BYTES)
                hdr = WAVEHDR()
                hdr.lpData = ctypes.cast(buf, ctypes.c_char_p)
                hdr.dwBufferLength = FRAME_BYTES
                hdr.dwBytesRecorded = 0
                hdr.dwFlags = 0
                hdr.dwLoops = 0

                rc = winmm.waveInPrepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
                if rc != 0:
                    raise RuntimeError(f"waveInPrepareHeader失敗: {self._winmm_error_text(winmm, rc)}")
                buffers.append(buf)
                headers.append(hdr)
                prepared.append(hdr)

                rc = winmm.waveInAddBuffer(hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
                if rc != 0:
                    raise RuntimeError(f"waveInAddBuffer失敗: {self._winmm_error_text(winmm, rc)}")

            rc = winmm.waveInStart(hwi)
            if rc != 0:
                raise RuntimeError(f"waveInStart失敗: {self._winmm_error_text(winmm, rc)}")

            while not self._capture_stop.is_set():
                any_done = False
                for buf, hdr in zip(buffers, headers):
                    if hdr.dwFlags & WHDR_DONE:
                        any_done = True
                        n = int(hdr.dwBytesRecorded)
                        if n > 0:
                            self._push_pcm(ctypes.string_at(buf, n))

                        # 同じ20 msバッファを再利用する。
                        hdr.dwBytesRecorded = 0
                        rc = winmm.waveInAddBuffer(hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
                        if rc != 0 and not self._capture_stop.is_set():
                            raise RuntimeError(f"waveInAddBuffer再登録失敗: {self._winmm_error_text(winmm, rc)}")

                if not any_done:
                    time.sleep(0.002)

        except Exception as exc:
            self._capture_error = exc
        finally:
            if hwi:
                try:
                    winmm.waveInReset(hwi)
                except Exception:
                    pass
                for hdr in prepared:
                    try:
                        winmm.waveInUnprepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
                    except Exception:
                        pass
                try:
                    winmm.waveInClose(hwi)
                except Exception:
                    pass
            self._capture_active = False

    async def start_capture(self) -> None:
        await self.stop_capture()
        self._clear_frames()
        self._capture_error = None
        self._capture_stop.clear()
        self._capture_active = True
        self._capture_thread = threading.Thread(target=self._capture_worker, daemon=True)
        self._capture_thread.start()

        # waveInOpen / waveInStart の初期化失敗を呼び出し側へ返す。
        await asyncio.sleep(0.08)
        if self._capture_error:
            self._capture_active = False
            raise self._capture_error
        if not self._capture_thread.is_alive():
            self._capture_active = False
            raise RuntimeError("Windowsマイク入力スレッドが開始直後に終了しました")

    async def read_frame(self) -> bytes:
        if self._capture_error:
            raise self._capture_error
        return await super().read_frame()

    async def stop_capture(self) -> None:
        self._capture_active = False
        self._capture_stop.set()
        th = self._capture_thread
        if th and th.is_alive():
            await asyncio.to_thread(th.join, 1.0)
        self._capture_thread = None

    @staticmethod
    def _play_sync(pcm_data: bytes) -> None:
        """WindowsではFlet Audioを使わず、winsoundでWAVを同期再生する。"""
        if not pcm_data:
            return

        import winsound

        wav_data = pcm16_to_wav(pcm_data)
        fd, temp_path = tempfile.mkstemp(prefix="voicevox_", suffix=".wav")
        os.close(fd)
        try:
            with open(temp_path, "wb") as f:
                f.write(wav_data)
            winsound.PlaySound(temp_path, winsound.SND_FILENAME)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    async def play(self, pcm_data: bytes) -> None:
        await asyncio.to_thread(self._play_sync, pcm_data)


class UnsupportedAudioBackend(AudioBackend):
    def __init__(self, page: ft.Page, detected: str):
        super().__init__(page)
        self.name = f"未対応プラットフォーム: {detected}"
        self.detected = detected

    async def ensure_record_permission(self) -> bool:
        return False
    async def start_capture(self) -> None:
        raise RuntimeError(f"音声入力未対応: {self.detected}")
    async def read_frame(self) -> bytes:
        raise RuntimeError(f"音声入力未対応: {self.detected}")
    async def stop_capture(self) -> None:
        pass
    async def play(self, pcm_data: bytes) -> None:
        raise RuntimeError(f"音声出力未対応: {self.detected}")


def create_audio_backend(page: ft.Page) -> AudioBackend:
    detected = platform_name(page)
    if detected == "android":
        return AndroidNativeAudioBackend(page)
    if detected == "windows":
        return WindowsWinMMAudioBackend(page)
    if detected == "linux":
        return LinuxAlsaAudioBackend(page)
    return UnsupportedAudioBackend(page, detected)
