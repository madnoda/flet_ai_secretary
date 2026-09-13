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

import array
import math
from collections import deque
from dataclasses import dataclass

from audio_backend import FRAME_MS, SAMPLE_RATE, AudioBackend

START_VOICE_FRAMES = 5      # 100 ms
END_SILENCE_FRAMES = 40     # 800 ms
PRE_ROLL_FRAMES = 10        # 200 ms
MAX_UTTER_SECONDS = 20


class EnergyVad:
    """webrtcvad が使えない環境用の純Pythonフォールバック。"""

    name = "Adaptive energy VAD"

    def __init__(self):
        self.noise_rms = 180.0

    @staticmethod
    def _rms(frame: bytes) -> float:
        samples = array.array("h")
        samples.frombytes(frame)
        if not samples:
            return 0.0
        return math.sqrt(sum(float(x) * float(x) for x in samples) / len(samples))

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        rms = self._rms(frame)
        threshold = max(450.0, self.noise_rms * 3.0)
        speech = rms >= threshold
        if not speech:
            # 周囲雑音へゆっくり追従。発話中はnoise floorを上げない。
            self.noise_rms = self.noise_rms * 0.97 + rms * 0.03
        return speech


class WebRtcVadAdapter:
    name = "WebRTC VAD mode 2"

    def __init__(self):
        import webrtcvad
        self._vad = webrtcvad.Vad(2)

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return self._vad.is_speech(frame, sample_rate)


def create_vad():
    try:
        return WebRtcVadAdapter()
    except Exception:
        return EnergyVad()


@dataclass
class VadResult:
    pcm: bytes
    detector_name: str

    @property
    def seconds(self) -> float:
        return len(self.pcm) / (SAMPLE_RATE * 2)


class VadRecorder:
    """ESP32版と同じ20msフレームの発話区切りロジック。"""

    def __init__(self):
        self.detector = create_vad()
        self.detector_name = getattr(self.detector, "name", type(self.detector).__name__)

    async def record_utterance(self, audio: AudioBackend, status_cb=None) -> VadResult:
        prebuf = deque(maxlen=PRE_ROLL_FRAMES)
        voiced_count = 0
        silence_count = 0
        recording = False
        utter = bytearray()
        max_frames = int(MAX_UTTER_SECONDS * 1000 / FRAME_MS)
        frames_seen = 0

        await audio.start_capture()
        try:
            if status_cb:
                status_cb("waiting", self.detector_name)

            while True:
                frame = await audio.read_frame()
                is_speech = self.detector.is_speech(frame, SAMPLE_RATE)

                if not recording:
                    prebuf.append(frame)
                    if is_speech:
                        voiced_count += 1
                        if voiced_count >= START_VOICE_FRAMES:
                            recording = True
                            silence_count = 0
                            utter.clear()
                            for f in prebuf:
                                utter.extend(f)
                            prebuf.clear()
                            frames_seen = 0
                            if status_cb:
                                status_cb("recording", self.detector_name)
                    else:
                        voiced_count = 0
                else:
                    utter.extend(frame)
                    frames_seen += 1
                    if is_speech:
                        silence_count = 0
                    else:
                        silence_count += 1
                        if silence_count >= END_SILENCE_FRAMES:
                            return VadResult(bytes(utter), self.detector_name)

                    if frames_seen >= max_frames:
                        return VadResult(bytes(utter), self.detector_name)
        finally:
            await audio.stop_capture()
