"""(a) Asterisk-AudioSocket: Python-TCP-Server bekommt PCM (slin 8 kHz) eines Anrufs und sendet PCM zurück.
(b) Session-Timer: 70-s-Gespräch, das Double frischt per re-INVITE/UPDATE auf; minisip muss antworten und Audio muss weiterlaufen."""
import asyncio, json, os, struct, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from minisip import SipUA
import minisip
RES = {}

def tone(f, s): 
    t = np.arange(int(8000*s))/8000; return (np.sin(2*np.pi*f*t)*0.4*32767).astype(np.int16).tobytes()
def peak(pcm):
    x = np.frombuffer(pcm, dtype=np.int16).astype(float); sp = np.abs(np.fft.rfft(x*np.hanning(len(x))))**2
    fr = np.fft.rfftfreq(len(x), 1/8000); i = int(np.argmax(sp[1:]))+1; return round(float(fr[i]),1)

async def audiosocket_server(reader, writer):
    st = {"uuid": None, "rx": b"", "frames": 0, "dtmf": "", "t0": time.monotonic()}
    RES["audiosocket"] = st
    out = tone(1000, 2.0); pos = 0
    try:
        while True:
            hdr = await reader.readexactly(3)
            kind, ln = hdr[0], struct.unpack("!H", hdr[1:])[0]
            payload = await reader.readexactly(ln) if ln else b""
            if kind == 0x01: st["uuid"] = payload.hex()
            elif kind == 0x10:
                st["rx"] += payload; st["frames"] += 1
                chunk = out[pos:pos+320]; pos += 320       # je empfangenem Rahmen einen Rahmen zurück (Takt von Asterisk)
                if chunk: writer.write(b"\x10" + struct.pack("!H", len(chunk)) + chunk)
            elif kind == 0x03: st["dtmf"] += payload.decode()
            elif kind == 0x00: break
    except asyncio.IncompleteReadError:
        pass
    writer.close()

async def main():
    srv = await asyncio.start_server(audiosocket_server, "127.0.0.1", 9092)
    handy = SipUA("621", "geheim621", "127.0.0.1", sip_port=5074, rtp_ports=(7100,7120), local_ip="127.0.0.1", bind_ip="127.0.0.1")
    await handy.start(); assert await handy.register()
    c = await handy.call("603")
    await asyncio.sleep(0.2); c.send_pcm(tone(440, 2.0))
    got, end = b"", time.monotonic()+2.5
    while time.monotonic() < end:
        try: got += await asyncio.wait_for(c.rx.get(), 0.1)
        except asyncio.TimeoutError: pass
    await c.send_dtmf("9#"); await asyncio.sleep(0.6)
    await c.hangup(); await asyncio.sleep(0.5)
    st = RES["audiosocket"]
    RES["audiosocket"] = {"uuid_received": st["uuid"], "frames": st["frames"], "server_hears_hz": peak(st["rx"][8000:8000+32000]),
                          "caller_hears_hz": peak(got[4000:4000+24000]), "dtmf_at_server": st["dtmf"]}
    # (b) Session-Timer
    jarvis = SipUA("620", "geheim620", "127.0.0.1", sip_port=5070, local_ip="127.0.0.1", bind_ip="127.0.0.1")
    seen = []
    orig = jarvis._on_request
    def spy(req):
        if req.method in ("INVITE", "UPDATE"): seen.append((round(time.monotonic()-t0,1), req.method, req.get("Session-Expires")))
        orig(req)
    jarvis._on_request = spy
    await jarvis.start(); assert await jarvis.register()
    t0 = time.monotonic()
    c = await jarvis.call("601")
    dur = float(os.environ.get("CALL_SECONDS", "70"))
    n_before = 0
    while time.monotonic()-t0 < dur and not c.ended.is_set():
        c.send_pcm(tone(1000, 1.0)); await asyncio.sleep(1.0)
    # letzte 2 s prüfen
    while not c.rx.empty(): c.rx.get_nowait()
    c.send_pcm(tone(1000, 2.0)); tail = b""; end = time.monotonic()+2
    while time.monotonic() < end:
        try: tail += await asyncio.wait_for(c.rx.get(), 0.1)
        except asyncio.TimeoutError: pass
    RES["session_timer"] = {"call_seconds": round(time.monotonic()-t0,1), "still_up": c.state == "up", "end_reason": c.end_reason,
                            "refresh_requests_seen": seen, "tail_peak_hz": peak(tail) if len(tail) > 8000 else None,
                            "rtp": {"packets": c.stats.packets, "lost": c.stats.lost, "max_gap_ms": round(c.stats.max_gap_ms,1)}}
    await c.hangup(); await jarvis.register(expires=0); await handy.register(expires=0)
    print(json.dumps(RES, indent=2)); json.dump(RES, open(f"{HERE}/ergebnis_audiosocket_timer.json","w"), indent=2)
asyncio.run(main())
