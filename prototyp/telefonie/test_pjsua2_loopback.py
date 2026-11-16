"""Loopback-Test: pjsua2 (PJSIP 2.17, selbst gebaut) als Jarvis-Nebenstelle 620 am Asterisk-"Fritzbox-Double".

PCM-Kopplung über pj.AudioMediaPort (onFrameReceived/onFrameRequested, 20 ms, 16 bit mono).
Gegenstelle "Handy" (621) ist der unabhängige Mini-Stack aus minisip.py.

Aufruf:
  LD_LIBRARY_PATH=/opt/pj/lib PYTHONPATH=<build>/pjmod python3 test_pjsua2_loopback.py
"""
import asyncio
import json
import os
import sys
import threading
import time
import wave

import numpy as np
import pjsua2 as pj

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from minisip import SipUA  # noqa: E402

RATE = int(os.environ.get("PJ_RATE", "8000"))     # 16000 für G.722-Test
RES: dict = {}


def tone(freq, seconds, rate=RATE, amp=0.4):
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16).tobytes()


def peak_hz(pcm, rate=RATE):
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if len(x) < rate // 4:
        return 0.0, 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    i = int(np.argmax(spec[1:])) + 1
    band = (freqs > freqs[i] - 20) & (freqs < freqs[i] + 20)
    return round(float(freqs[i]), 1), round(float(spec[band].sum() / spec[1:].sum()), 3)


class PcmPort(pj.AudioMediaPort):
    """Brücke zwischen der pjsua-Konferenzbrücke und Python: empfangene Rahmen in rx, zu sendende aus tx."""

    def __init__(self):
        super().__init__()
        self.rx = bytearray()
        self.tx = bytearray()
        self.lock = threading.Lock()
        self.frames_rx = self.frames_tx = 0
        self.first_rx = 0.0
        self.frame_bytes = RATE // 50 * 2

    def onFrameRequested(self, frame):
        frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
        with self.lock:
            chunk = bytes(self.tx[:self.frame_bytes])
            del self.tx[:self.frame_bytes]
        chunk = chunk.ljust(self.frame_bytes, b"\x00")
        frame.buf = pj.ByteVector(chunk)
        self.frames_tx += 1

    def onFrameReceived(self, frame):
        if not self.first_rx:
            self.first_rx = time.monotonic()
        with self.lock:
            self.rx += bytes(frame.buf)
        self.frames_rx += 1


class Call(pj.Call):
    def __init__(self, acc, call_id=pj.PJSUA_INVALID_ID):
        super().__init__(acc, call_id)
        self.port = None
        self.up = threading.Event()
        self.done = threading.Event()
        self.dtmf = ""
        self.t_confirmed = 0.0

    def onCallState(self, prm):
        ci = self.getInfo()
        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.t_confirmed = time.monotonic()
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.done.set()

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        for i, mi in enumerate(ci.media):
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                aud = self.getAudioMedia(i)
                if self.port is None:
                    self.port = PcmPort()
                    fmt = pj.MediaFormatAudio()
                    fmt.init(pj.PJMEDIA_FORMAT_PCM, RATE, 1, 20000, 16)
                    self.port.createPort("jarvis-pcm", fmt)
                aud.startTransmit(self.port)
                self.port.startTransmit(aud)
                self.up.set()

    def onDtmfDigit(self, prm):
        self.dtmf += prm.digit


class Account(pj.Account):
    def __init__(self):
        super().__init__()
        self.reg = threading.Event()
        self.incoming = []
        self.incoming_ev = threading.Event()
        self.reg_code = 0

    def onRegState(self, prm):
        self.reg_code = prm.code
        if prm.code // 100 == 2:
            self.reg.set()

    def onIncomingCall(self, prm):
        call = Call(self, prm.callId)
        self.incoming.append(call)
        self.incoming_ev.set()


def codec_of(call):
    try:
        si = call.getStreamInfo(0)
        return f"{si.codecName}/{si.codecClockRate}"
    except Exception as exc:  # noqa: BLE001
        return f"? ({exc})"


def take(port, seconds):
    time.sleep(seconds)
    with port.lock:
        data = bytes(port.rx)
        port.rx.clear()
    return data


def main():
    ep = pj.Endpoint()
    ep.libCreate()
    cfg = pj.EpConfig()
    cfg.logConfig.level = 3
    cfg.logConfig.consoleLevel = 0
    cfg.logConfig.filename = f"{HERE}/pjsua2.log"
    cfg.uaConfig.userAgent = "jarvis-pjsua2"
    cfg.medConfig.clockRate = RATE
    cfg.medConfig.sndClockRate = RATE
    cfg.medConfig.noVad = True
    cfg.medConfig.ecTailLen = 0
    ep.libInit(cfg)
    tcfg = pj.TransportConfig()
    tcfg.port = 5080
    tcfg.boundAddress = "127.0.0.1"
    ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)
    ep.libStart()
    ep.audDevManager().setNullDev()          # kein Soundgerät im Container
    if RATE == 16000:
        ep.codecSetPriority("G722/16000/1", 255)
    else:
        ep.codecSetPriority("PCMA/8000/1", 255)
        ep.codecSetPriority("G722/16000/1", 0)

    acfg = pj.AccountConfig()
    acfg.idUri = "sip:620@127.0.0.1"
    acfg.regConfig.registrarUri = "sip:127.0.0.1:5060"
    acfg.regConfig.timeoutSec = 300
    acfg.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", "620", 0, "geheim620"))
    acfg.mediaConfig.transportConfig.port = 7200
    acfg.mediaConfig.transportConfig.portRange = 20
    acc = Account()
    t0 = time.monotonic()
    acc.create(acfg)
    RES["register"] = {"ok": acc.reg.wait(5), "code": acc.reg_code, "ms": round((time.monotonic() - t0) * 1000, 1)}

    def dial(number):
        call = Call(acc)
        prm = pj.CallOpParam(True)
        t = time.monotonic()
        call.makeCall(f"sip:{number}@127.0.0.1", prm)
        ok = call.up.wait(8)
        return call, ok, t

    def hangup(call):
        try:
            call.hangup(pj.CallOpParam())
        except pj.Error:
            pass
        call.done.wait(3)

    # ausgehend: Gegenstelle spielt Ton (600: 440 Hz schmalbandig; 607: 3000 Hz breitbandig)
    target, expect = ("607", 3000) if RATE == 16000 else ("600", 440)
    call, ok, t = dial(target)
    pcm = take(call.port, 2.5)
    RES[f"out_{target}_receive_{expect}"] = {
        "media_up": ok, "codec": codec_of(call), "peak": peak_hz(pcm[RATE:]),
        "makecall_to_first_frame_ms": round((call.port.first_rx - t) * 1000, 1), "frames_rx": call.port.frames_rx}
    hangup(call)

    # ausgehend: Gegenstelle nimmt auf, Jarvis sendet 1000 Hz
    rec = f"{HERE}/fritzdouble/spool/rec602-620.wav"
    if os.path.exists(rec):
        os.remove(rec)
    call, ok, t = dial("602")
    time.sleep(0.3)
    with call.port.lock:
        call.port.tx += tone(1000, 2.5)
    time.sleep(3.0)
    hangup(call)
    time.sleep(0.5)
    with wave.open(rec, "rb") as w:
        data, r = w.readframes(w.getnframes()), w.getframerate()
    RES["out_602_far_end_records_1000"] = {"peak": peak_hz(data[r:5 * r], r), "wav_rate": r}

    # ankommend: "Handy" 621 (minisip, eigener Thread mit asyncio) ruft 620 über das Double
    handy_res = {}

    async def handy_side():
        ua = SipUA("621", "geheim621", "127.0.0.1", sip_port=5074, rtp_ports=(7100, 7120), local_ip="127.0.0.1",
                   bind_ip="127.0.0.1", display="Handy")
        await ua.start()
        assert await ua.register()
        t = time.monotonic()
        c = await ua.call("620")
        handy_res["dial_to_up_ms"] = round((time.monotonic() - t) * 1000, 1)
        handy_res["state"] = c.state
        await asyncio.sleep(0.4)
        c.send_pcm(tone(440, 2.0, 8000))
        got, end = b"", time.monotonic() + 2.6
        while time.monotonic() < end:
            try:
                got += await asyncio.wait_for(c.rx.get(), 0.1)
            except asyncio.TimeoutError:
                pass
        handy_res["handy_hears"] = peak_hz(got[4000:4000 + 24000], 8000)
        handy_res["handy_rtp"] = {"packets": c.stats.packets, "lost": c.stats.lost, "jitter_ms": round(c.stats.jitter_ms, 2),
                                  "max_gap_ms": round(c.stats.max_gap_ms, 1)}
        await c.send_dtmf("4711#")
        await asyncio.sleep(0.8)
        await c.hangup()
        await ua.register(expires=0)

    th = threading.Thread(target=lambda: asyncio.run(handy_side()))
    th.start()
    assert acc.incoming_ev.wait(8), "kein ankommender Ruf"
    inc = acc.incoming[0]
    ci = inc.getInfo()
    prm = pj.CallOpParam()
    prm.statusCode = 200
    inc.answer(prm)
    inc.up.wait(5)
    time.sleep(0.4)
    with inc.port.lock:
        inc.port.rx.clear()
        inc.port.tx += tone(1000, 2.0)
    pcm = take(inc.port, 2.4)
    inc.done.wait(8)
    th.join(10)
    RES["in_621_calls_620"] = {"remote_uri": ci.remoteUri, "codec": codec_of(inc) if not inc.done.is_set() else "(beendet)",
                               "jarvis_hears": peak_hz(pcm[RATE // 2:RATE // 2 + 3 * RATE]), "dtmf_received": inc.dtmf,
                               "remote_hangup_detected": inc.done.is_set(), **handy_res}

    acc.shutdown()
    ep.libDestroy()
    print(json.dumps(RES, indent=2, ensure_ascii=False))
    with open(f"{HERE}/ergebnis_pjsua2_{RATE}.json", "w") as fh:
        json.dump(RES, fh, indent=2, ensure_ascii=False)


main()
