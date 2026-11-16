"""Gesprächsführung am Telefon – Prototyp der Steuerlogik, unabhängig vom SIP-Stack.

Bausteine:
  TurnDetector      Sprechpausenerkennung (Silero-VAD über pysilero-vad, 16 kHz, 32-ms-Blöcke)
  SentenceSplitter  schneidet den Claude-Textstrom in vorlesbare Sätze
  PhoneSession      Zustände HÖREN → DENKEN → SPRECHEN, Ins-Wort-Fallen (Barge-in), Füllsignal

STT, Agent und TTS sind Schnittstellen (Protocol); im Test stehen Attrappen bzw. espeak-ng dahinter.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import AsyncIterator, Awaitable, Callable, Protocol

import numpy as np
from pysilero_vad import SileroVoiceActivityDetector


def up8to16(pcm8: bytes) -> bytes:
    """8 kHz → 16 kHz (linear interpoliert; für VAD/STT ausreichend)."""
    x = np.frombuffer(pcm8, dtype=np.int16).astype(np.float32)
    if len(x) == 0:
        return b""
    y = np.empty(len(x) * 2, dtype=np.float32)
    y[0::2] = x
    y[1::2] = (x + np.append(x[1:], x[-1])) / 2
    return y.astype(np.int16).tobytes()


class TurnDetector:
    """Liefert 'start' (Sprache begonnen) und 'end' (Sprechpause lang genug) aus 8-kHz-Rahmen."""

    def __init__(self, start_ms: int = 96, end_silence_ms: int = 600, threshold: float = 0.5):
        self.vad = SileroVoiceActivityDetector()
        self.chunk = self.vad.chunk_bytes()            # 512 Samples = 32 ms bei 16 kHz
        self.start_chunks = max(1, start_ms // 32)
        self.end_chunks = max(1, end_silence_ms // 32)
        self.threshold = threshold
        self.buf = b""
        self.speaking = False
        self._run = 0
        self._silence = 0
        self.utterance = bytearray()                   # 16 kHz PCM der laufenden Äußerung
        self._pre: list[bytes] = []                    # Vorlauf, damit der Wortanfang nicht fehlt
        self.vad_seconds = 0.0
        self.vad_chunks = 0

    def reset(self) -> None:
        self.speaking, self._run, self._silence = False, 0, 0
        self.utterance = bytearray()
        self._pre = []

    def feed(self, pcm8: bytes) -> list[str]:
        events = []
        self.buf += up8to16(pcm8)
        while len(self.buf) >= self.chunk:
            chunk, self.buf = self.buf[:self.chunk], self.buf[self.chunk:]
            t = time.perf_counter()
            prob = self.vad.process_chunk(chunk)
            self.vad_seconds += time.perf_counter() - t
            self.vad_chunks += 1
            voiced = prob >= self.threshold
            if not self.speaking:
                self._pre = (self._pre + [chunk])[-10:]
                self._run = self._run + 1 if voiced else 0
                if self._run >= self.start_chunks:
                    self.speaking, self._silence = True, 0
                    self.utterance = bytearray(b"".join(self._pre))
                    events.append("start")
            else:
                self.utterance += chunk
                self._silence = 0 if voiced else self._silence + 1
                if self._silence >= self.end_chunks:
                    self.speaking, self._run = False, 0
                    events.append("end")
        return events


_ABBR = re.compile(r"(?:(?<![A-Za-zÄÖÜäöüß])[A-Za-zÄÖÜäöü]|\b(?:Nr|bzw|ca|ggf|evtl|inkl|usw|etc|Dr|Hr|Str|Tel|Mo|Di|Mi|Do|Fr|Sa|So)|\d)\.$")


class SentenceSplitter:
    """Sammelt Text-Deltas und gibt ganze Sätze ab. Der erste Satz darf kurz sein (schneller erster Ton)."""

    def __init__(self, min_chars: int = 12):
        self.buf = ""
        self.min_chars = min_chars

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out = []
        while True:
            m = None
            for cand in re.finditer(r"[.!?:;…](?=[\s\"”)]|$)|\n", self.buf):
                head = self.buf[:cand.end()]
                if cand.end() == len(self.buf) and cand.group() != "\n":
                    break                               # Satzende erst sicher, wenn danach etwas kommt
                if len(head.strip()) < self.min_chars or _ABBR.search(head.rstrip()):
                    continue
                m = cand
                break
            if not m:
                return out
            sentence, self.buf = self.buf[:m.end()].strip(), self.buf[m.end():]
            if sentence:
                out.append(sentence)

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


def speakable(text: str) -> str:
    """Entfernt, was nicht vorgelesen werden soll (Reste von Markdown, Links)."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"[*_#`>|]+", "", text).strip()


class Stt(Protocol):
    async def transcribe(self, pcm16k: bytes) -> str: ...


class Tts(Protocol):
    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...       # PCM16 8 kHz, in Stücken


AgentFn = Callable[[str], AsyncIterator[dict]]     # liefert Ereignisse wie Agent.run(): text/tool/pending/error/done


class PhoneSession:
    """Ein Telefongespräch. `call` braucht: rx (Queue mit 20-ms-PCM), send_pcm(), clear_tx(), tx_pending_ms(), ended."""

    def __init__(self, call, stt: Stt, tts: Tts, agent: AgentFn, *, end_silence_ms: int = 600,
                 barge_in: bool = True, filler_after_s: float = 1.2, filler_text: str = "Einen Moment.",
                 on_metric: Callable[[str, float], None] | None = None):
        self.call, self.stt, self.tts, self.agent = call, stt, tts, agent
        self.detector = TurnDetector(end_silence_ms=end_silence_ms)
        self.barge_in = barge_in
        self.filler_after_s, self.filler_text = filler_after_s, filler_text
        self.state = "listening"
        self._reply: asyncio.Task | None = None
        self.metric = on_metric or (lambda name, value: None)
        self.log: list[tuple[float, str]] = []

    def _mark(self, what: str) -> None:
        self.log.append((time.monotonic(), what))

    async def say(self, text: str) -> None:
        async for pcm in self.tts.synthesize(speakable(text)):
            self.call.send_pcm(pcm)

    async def run(self, greeting: str | None = None) -> None:
        if greeting:
            self.state = "speaking"
            self._reply = asyncio.ensure_future(self._speak_only(greeting))
        while not self.call.ended.is_set():
            try:
                frame = await asyncio.wait_for(self.call.rx.get(), 0.2)
            except asyncio.TimeoutError:
                continue
            for ev in self.detector.feed(frame):
                if ev == "start":
                    self._mark("speech_start")
                    if self.state in ("speaking", "thinking") and self.barge_in:
                        await self._interrupt()
                elif ev == "end":
                    self._mark("speech_end")
                    if self.state == "listening":
                        audio = bytes(self.detector.utterance)
                        self.state = "thinking"
                        self._reply = asyncio.ensure_future(self._respond(audio))
            if self.state == "speaking" and self._reply and self._reply.done() and self.call.tx_pending_ms() == 0:
                self.state = "listening"
        if self._reply:
            self._reply.cancel()

    async def _interrupt(self) -> None:
        dropped = self.call.clear_tx()
        if self._reply and not self._reply.done():
            self._reply.cancel()            # bricht Agentenlauf-Verbrauch und TTS ab (Agent selbst: Agent.cancel(cid))
        self._mark(f"barge_in dropped_ms={dropped}")
        self.state = "listening"

    async def _speak_only(self, text: str) -> None:
        await self.say(text)

    async def _respond(self, audio16k: bytes) -> None:
        t0 = time.monotonic()
        text = await self.stt.transcribe(audio16k)
        self.metric("stt_ms", (time.monotonic() - t0) * 1000)
        self._mark(f"stt_done {text!r}")
        if not text.strip():
            self.state = "listening"
            return
        splitter = SentenceSplitter()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        first_audio = False

        async def speaker() -> None:
            nonlocal first_audio
            while (sentence := await queue.get()) is not None:
                async for pcm in self.tts.synthesize(speakable(sentence)):
                    if not first_audio:
                        first_audio = True
                        self.state = "speaking"
                        self.metric("speech_end_to_first_audio_ms", (time.monotonic() - t0) * 1000)
                        self._mark("first_tts_audio")
                    self.call.send_pcm(pcm)

        async def filler() -> None:
            await asyncio.sleep(self.filler_after_s)
            if not first_audio:
                self._mark("filler")
                queue.put_nowait(self.filler_text)

        spk, fil = asyncio.ensure_future(speaker()), asyncio.ensure_future(filler())
        try:
            got_text = False
            async for ev in self.agent(text):
                kind, data = ev["type"], ev.get("data", {})
                if kind == "text":
                    if not got_text:
                        got_text = True
                        self.metric("llm_first_text_ms", (time.monotonic() - t0) * 1000)
                    for s in splitter.feed(data["delta"]):
                        queue.put_nowait(s)
                elif kind == "tool" and data.get("status") == "running" and not first_audio:
                    fil.cancel()
                    self._mark("filler(tool)")
                    queue.put_nowait(self.filler_text)       # Füllsignal bei Werkzeugaufruf
                elif kind == "pending":
                    queue.put_nowait("Das habe ich zur Freigabe vorgemerkt. Bitte bestätige es in der Oberfläche.")
                elif kind == "error":
                    queue.put_nowait("Da ist etwas schiefgegangen.")
            for s in splitter.flush():
                queue.put_nowait(s)
            queue.put_nowait(None)
            await spk
        except asyncio.CancelledError:
            spk.cancel()
            raise
        finally:
            fil.cancel()
