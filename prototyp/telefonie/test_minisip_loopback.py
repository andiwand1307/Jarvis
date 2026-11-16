"""Loopback-Test für minisip.py gegen das Asterisk-"Fritzbox-Double" (127.0.0.1:5060).

Prüft Registrierung (Digest), ausgehende und ankommende Rufe, Audio in beide Richtungen
(Tonfrequenz per FFT), DTMF, Echo-Laufzeit, RTP-Statistik.
Aufruf: python3 test_minisip_loopback.py
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from minisip import SipUA  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
AST = ["asterisk", "-C", f"{HERE}/fritzdouble/etc/asterisk.conf", "-rx"]
RESULTS: dict = {}


def tone(freq: float, seconds: float, amp: float = 0.4) -> bytes:
    t = np.arange(int(8000 * seconds)) / 8000
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16).tobytes()


def peak_hz(pcm: bytes, rate: int = 8000) -> tuple[float, float]:
    """(dominante Frequenz, Anteil der Energie in ±20 Hz um die Spitze)."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if len(x) < rate // 4:
        return 0.0, 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    i = int(np.argmax(spec[1:])) + 1
    band = (freqs > freqs[i] - 20) & (freqs < freqs[i] + 20)
    return float(freqs[i]), float(spec[band].sum() / spec[1:].sum())


async def collect(call, seconds: float) -> bytes:
    out, end = b"", time.monotonic() + seconds
    while time.monotonic() < end and not call.ended.is_set():
        try:
            out += await asyncio.wait_for(call.rx.get(), 0.1)
        except asyncio.TimeoutError:
            pass
    return out


def wav_pcm(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def stats(call) -> dict:
    s = call.stats
    return {"packets": s.packets, "lost": s.lost, "jitter_ms": round(s.jitter_ms, 2), "max_gap_ms": round(s.max_gap_ms, 1)}


async def main() -> None:
    jarvis = SipUA("620", "geheim620", "127.0.0.1", sip_port=5070, rtp_ports=(7078, 7098), local_ip="127.0.0.1", bind_ip="127.0.0.1")
    await jarvis.start()

    # 0. falsches Kennwort muss scheitern
    bad = SipUA("620", "falsch", "127.0.0.1", sip_port=5072, local_ip="127.0.0.1", bind_ip="127.0.0.1")
    await bad.start()
    RESULTS["register_wrong_password_rejected"] = not await bad.register()

    # 1. Registrierung
    t0 = time.monotonic()
    ok = await jarvis.register()
    RESULTS["register"] = {"ok": ok, "ms": round((time.monotonic() - t0) * 1000, 1), "expires": jarvis.expires}
    contacts = subprocess.run(AST + ["pjsip show contacts"], capture_output=True, text=True).stdout
    RESULTS["register"]["asterisk_sees_contact"] = "620/sip:620@127.0.0.1:5070" in contacts

    # 2. ausgehend zu 600: Gegenstelle spielt 440 Hz
    call = await jarvis.call("600")
    pcm = await collect(call, 2.5)
    f, share = peak_hz(pcm[8000:])           # erste 0,5 s verwerfen
    RESULTS["out_600_receive_440"] = {
        "state": call.state, "codec_pt": call.pt, "peak_hz": round(f, 1), "energy_share": round(share, 3),
        "invite_to_200_ms": round((call.t["answer"] - call.t["invite"]) * 1000, 1),
        "200_to_first_rtp_ms": round((call.t["first_rtp"] - call.t["answer"]) * 1000, 1), **stats(call)}
    await call.hangup()

    # 3. Echo (601): 1000 Hz senden, zurückhören; Laufzeit über einen Tonstoß messen
    call = await jarvis.call("601")
    await asyncio.sleep(0.5)
    await collect(call, 0.0)
    while not call.rx.empty():
        call.rx.get_nowait()
    t_send = time.monotonic()
    call.send_pcm(tone(1000, 2.0))
    got, t_first = b"", None
    end = time.monotonic() + 3
    while time.monotonic() < end:
        try:
            frame = await asyncio.wait_for(call.rx.get(), 0.1)
        except asyncio.TimeoutError:
            continue
        if t_first is None and np.abs(np.frombuffer(frame, dtype=np.int16)).max() > 3000:
            t_first = time.monotonic()
        if t_first is not None:
            got += frame
    f, share = peak_hz(got[: 8000 * 2 * 3 // 2])
    RESULTS["echo_601_send_1000"] = {"peak_hz": round(f, 1), "energy_share": round(share, 3),
                                     "roundtrip_ms": round((t_first - t_send) * 1000, 1) if t_first else None, **stats(call)}
    await call.hangup()

    # 4. Gegenstelle nimmt auf (602): Jarvis sendet 1000 Hz
    rec = f"{HERE}/fritzdouble/spool/rec602-620.wav"
    if os.path.exists(rec):
        os.remove(rec)
    call = await jarvis.call("602")
    await asyncio.sleep(0.3)
    call.send_pcm(tone(1000, 2.5))
    await asyncio.sleep(3.0)
    await call.hangup()
    await asyncio.sleep(0.5)
    pcm, rate = wav_pcm(rec)
    f, share = peak_hz(pcm[rate:rate * 2 * 2 + rate], rate)
    RESULTS["out_602_far_end_records_1000"] = {"peak_hz": round(f, 1), "energy_share": round(share, 3), "wav_rate": rate,
                                               "seconds": round(len(pcm) / 2 / rate, 2)}

    # 5. DTMF-Empfang (605 sendet 1357#) – RFC 4733
    call = await jarvis.call("605")
    digits = ""
    try:
        while True:
            digits += await asyncio.wait_for(call.dtmf.get(), 4)
            if digits.endswith("#"):
                break
    except asyncio.TimeoutError:
        pass
    RESULTS["dtmf_receive"] = {"expected": "1357#", "got": digits}
    await call.hangup()

    # 6. ankommend: Asterisk ruft 620 (wie Fritzbox bei internem/externem Anruf), spielt 440 Hz, nimmt dann auf
    rec = f"{HERE}/fritzdouble/spool/rec606.wav"
    if os.path.exists(rec):
        os.remove(rec)
    t0 = time.monotonic()
    subprocess.Popen(AST + ["channel originate PJSIP/620 extension 606@fritz"], stdout=subprocess.DEVNULL)
    call = await asyncio.wait_for(jarvis.incoming.get(), 10)
    t_ring = time.monotonic()
    await call.answer()
    pcm = await collect(call, 3.0)
    f, share = peak_hz(pcm[8000:8000 + 16000 * 2])
    res = {"from": call.remote_number, "originate_to_invite_ms": round((t_ring - t0) * 1000, 1), "peak_hz": round(f, 1),
           "energy_share": round(share, 3), **stats(call)}
    await collect(call, 1.5)                      # Rest des Tons abwarten, dann nimmt die Gegenseite auf
    call.send_pcm(tone(1000, 2.0))
    await asyncio.sleep(2.5)
    await call.hangup()
    await asyncio.sleep(0.5)
    if os.path.exists(rec):
        pcm, rate = wav_pcm(rec)
        f2, share2 = peak_hz(pcm, rate)
        res["far_end_recorded_peak_hz"] = round(f2, 1)
        res["far_end_energy_share"] = round(share2, 3)
    RESULTS["in_originate_606"] = res

    # 7. "Handy ruft Jarvis": zweite Nebenstelle 621 ruft 620 über das Double (gebrückt), Audio in beide Richtungen
    handy = SipUA("621", "geheim621", "127.0.0.1", sip_port=5074, rtp_ports=(7100, 7120), local_ip="127.0.0.1",
                  bind_ip="127.0.0.1", display="Handy")
    await handy.start()
    assert await handy.register()
    t0 = time.monotonic()
    dial = asyncio.ensure_future(handy.call("620"))
    incoming = await asyncio.wait_for(jarvis.incoming.get(), 10)
    t_ring = time.monotonic()
    await incoming.answer()
    hcall = await dial
    t_up = time.monotonic()
    await asyncio.sleep(0.3)
    hcall.send_pcm(tone(440, 2.0))
    incoming.send_pcm(tone(1000, 2.0))
    a, b = await asyncio.gather(collect(incoming, 2.5), collect(hcall, 2.5))
    fa, sa = peak_hz(a[4000:4000 + 24000])
    fb, sb = peak_hz(b[4000:4000 + 24000])
    await incoming.send_dtmf("42#")
    got = ""
    try:
        while not got.endswith("#"):
            got += await asyncio.wait_for(hcall.dtmf.get(), 3)
    except asyncio.TimeoutError:
        pass
    RESULTS["bridged_621_calls_620"] = {
        "caller_seen_by_jarvis": incoming.remote_number, "dial_to_ring_ms": round((t_ring - t0) * 1000, 1),
        "answer_to_connected_ms": round((t_up - t_ring) * 1000, 1),
        "jarvis_hears_hz": round(fa, 1), "jarvis_share": round(sa, 3), "handy_hears_hz": round(fb, 1), "handy_share": round(sb, 3),
        "dtmf_jarvis_to_handy": got, "jarvis_rtp": stats(incoming), "handy_rtp": stats(hcall)}
    await hcall.hangup()                         # Handy legt auf → Jarvis muss BYE bekommen
    try:
        await asyncio.wait_for(incoming.ended.wait(), 3)
    except asyncio.TimeoutError:
        pass
    RESULTS["bridged_621_calls_620"]["jarvis_end_reason"] = incoming.end_reason

    # 8. Abmelden
    RESULTS["unregister_ok"] = await jarvis.register(expires=0)
    await handy.register(expires=0)
    print(json.dumps(RESULTS, indent=2, ensure_ascii=False))
    with open(f"{HERE}/ergebnis_minisip.json", "w") as fh:
        json.dump(RESULTS, fh, indent=2, ensure_ascii=False)


asyncio.run(main())
