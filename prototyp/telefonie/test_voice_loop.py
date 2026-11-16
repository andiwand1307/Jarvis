"""Ende-zu-Ende-Test der Gesprächsführung über eine echte SIP/RTP-Strecke (Handy 621 → Double → Jarvis 620).

Echt: SIP, RTP/G.711, Silero-VAD, Satzschnitt, Barge-in, Sprachsynthese (espeak-ng als Platzhalter-TTS).
Attrappe: STT (feste Wartezeit) und Claude (fester Zeitpunkt des ersten Tokens, feste Tokenrate) –
die Zeiten sind Parameter, nicht gemessen.

Aufruf: python3 test_voice_loop.py [stt_ms] [llm_first_token_ms] [tokens_per_s]
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from minisip import SipUA  # noqa: E402
from voice_loop import PhoneSession, SentenceSplitter  # noqa: E402

STT_MS = float(sys.argv[1]) if len(sys.argv) > 1 else 300
LLM_TTFT_MS = float(sys.argv[2]) if len(sys.argv) > 2 else 800
TOK_PER_S = float(sys.argv[3]) if len(sys.argv) > 3 else 60
METRICS: dict = {"params": {"stt_ms_simulated": STT_MS, "llm_first_token_ms_simulated": LLM_TTFT_MS, "tokens_per_s_simulated": TOK_PER_S}}


def espeak(text: str) -> bytes:
    """Deutsch, 8 kHz PCM16."""
    p1 = subprocess.run(["espeak-ng", "-v", "de", "-s", "165", "--stdout", text], capture_output=True, check=True)
    p2 = subprocess.run(["sox", "-t", "wav", "-", "-r", "8000", "-c", "1", "-b", "16", "-e", "signed", "-t", "raw", "-"],
                        input=p1.stdout, capture_output=True, check=True)
    return p2.stdout


class FakeStt:
    async def transcribe(self, pcm16k: bytes) -> str:
        await asyncio.sleep(STT_MS / 1000)
        return "Wie wird das Wetter morgen in München?"


class EspeakTts:
    synth_ms: list[float] = []

    async def synthesize(self, text: str):
        t = time.monotonic()
        pcm = await asyncio.get_running_loop().run_in_executor(None, espeak, text)
        self.synth_ms.append((time.monotonic() - t) * 1000)
        yield pcm


ANSWER = ("Morgen wird es in München überwiegend sonnig. Die Temperaturen steigen auf etwa zwölf Grad, z. B. am Nachmittag. "
          "Am Abend ziehen von Westen Wolken auf, es bleibt aber trocken. Soll ich dir den Regenschirm trotzdem auf die Liste setzen?")


def make_agent(with_tool: bool):
    async def agent(user_text: str):
        if with_tool:
            await asyncio.sleep(LLM_TTFT_MS / 1000)
            yield {"type": "tool", "data": {"name": "ha_get_state", "label": "Lese Wetter …", "status": "running"}}
            await asyncio.sleep(1.5)                       # Werkzeuglaufzeit + zweiter Modellaufruf
            yield {"type": "tool", "data": {"name": "ha_get_state", "status": "done"}}
        await asyncio.sleep(LLM_TTFT_MS / 1000)
        for word in ANSWER.split(" "):                    # ~1,5 Tokens je Wort
            yield {"type": "text", "data": {"delta": word + " "}}
            await asyncio.sleep(1.5 / TOK_PER_S)
        yield {"type": "done", "data": {}}
    return agent


def rms(frame: bytes) -> float:
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
    return float(np.sqrt((x ** 2).mean())) if len(x) else 0.0


async def scenario(name: str, handy: SipUA, jarvis: SipUA, with_tool: bool, barge: bool) -> None:
    m: dict = {}
    dial = asyncio.ensure_future(handy.call("620"))
    inc = await asyncio.wait_for(jarvis.incoming.get(), 5)
    await inc.answer()
    hcall = await dial
    sess = PhoneSession(inc, FakeStt(), EspeakTts(), make_agent(with_tool), on_metric=lambda k, v: m.__setitem__(k, round(v, 1)))
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0.5)

    question = espeak("Hallo Jarvis, wie wird das Wetter morgen in München?")
    t_q_start = time.monotonic()
    hcall.send_pcm(question)
    t_q_end = t_q_start + len(question) / 16000           # Ende der Äußerung auf der Leitung
    m["question_seconds"] = round(len(question) / 16000, 2)

    # Handy hört zu: wann kommt der erste Ton von Jarvis an?
    heard_at, silent_at, t_barge = None, None, None
    quiet = 0
    end = time.monotonic() + 25
    while time.monotonic() < end:
        try:
            frame = await asyncio.wait_for(hcall.rx.get(), 0.1)
        except asyncio.TimeoutError:
            continue
        now = time.monotonic()
        loud = rms(frame) > 300
        if heard_at is None and loud and now > t_q_end:
            heard_at = now
            if barge:                                     # 1,5 s nach Antwortbeginn ins Wort fallen
                async def interrupt():
                    nonlocal t_barge
                    await asyncio.sleep(1.5)
                    t_barge = time.monotonic()
                    hcall.send_pcm(espeak("Stopp, danke, das reicht mir schon."))
                asyncio.ensure_future(interrupt())
        if heard_at is not None:
            quiet = 0 if loud else quiet + 1
            if barge and t_barge and quiet >= 10 and silent_at is None:      # 200 ms Stille nach dem Dazwischenreden
                silent_at = now - 0.2
                break
            if not barge and quiet >= 75:                                     # 1,5 s Stille = Antwort fertig
                break
    marks = {what.split(" ")[0]: t for t, what in sess.log}
    m["vad_end_after_true_end_ms"] = round((marks["speech_end"] - t_q_end) * 1000, 1) if "speech_end" in marks else None
    m["true_end_to_heard_at_handy_ms"] = round((heard_at - t_q_end) * 1000, 1) if heard_at else None
    m["filler_spoken"] = any(w.startswith("filler") for _, w in sess.log)
    if barge and t_barge:
        b = [t for t, w in sess.log if w.startswith("barge_in")]
        m["barge_speech_onset_to_detect_ms"] = round((b[0] - t_barge) * 1000, 1) if b else None
        m["barge_speech_onset_to_silence_at_handy_ms"] = round((silent_at - t_barge) * 1000, 1) if silent_at else None
        m["barge_dropped"] = [w for _, w in sess.log if w.startswith("barge_in")]
    m["tts_synth_ms_per_sentence"] = [round(x) for x in EspeakTts.synth_ms]
    EspeakTts.synth_ms = []
    m["vad_ms_per_32ms_chunk"] = round(sess.detector.vad_seconds / max(1, sess.detector.vad_chunks) * 1000, 3)
    METRICS[name] = m
    await hcall.hangup()
    await asyncio.sleep(0.3)
    task.cancel()


async def main() -> None:
    sp = SentenceSplitter()
    METRICS["sentence_split_check"] = sum((sp.feed(w + " ") for w in ANSWER.split(" ")), []) + sp.flush()
    jarvis = SipUA("620", "geheim620", "127.0.0.1", sip_port=5070, local_ip="127.0.0.1", bind_ip="127.0.0.1")
    handy = SipUA("621", "geheim621", "127.0.0.1", sip_port=5074, rtp_ports=(7100, 7120), local_ip="127.0.0.1", bind_ip="127.0.0.1")
    await jarvis.start()
    await handy.start()
    assert await jarvis.register() and await handy.register()
    await scenario("einfache_antwort", handy, jarvis, with_tool=False, barge=False)
    await scenario("mit_werkzeug_fuellsignal", handy, jarvis, with_tool=True, barge=False)
    await scenario("ins_wort_fallen", handy, jarvis, with_tool=False, barge=True)
    await jarvis.register(expires=0)
    await handy.register(expires=0)
    print(json.dumps(METRICS, indent=2, ensure_ascii=False))
    with open(f"{HERE}/ergebnis_voice_loop.json", "w") as fh:
        json.dump(METRICS, fh, indent=2, ensure_ascii=False)


asyncio.run(main())
