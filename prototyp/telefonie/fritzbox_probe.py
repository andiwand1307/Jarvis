"""Vor-Ort-Probe an der echten Fritzbox (nur numpy nötig). Im Heimnetz ausführen, z. B. auf einem Rechner im LAN:

  python3 fritzbox_probe.py --user jarvis-telefon --password '…' --wait        # anmelden, ankommenden Ruf annehmen, Echo
  python3 fritzbox_probe.py --user … --password '…' --call '**610' --seconds 8   # ausgehend (intern **6xx oder externe Nummer)

Gibt den SIP-Verlauf aus (Zeilen >>> / <<<), die Art der Digest-Abfrage, den ausgehandelten Codec, Anrufernummer,
DTMF-Ziffern und die RTP-Statistik. Bei --wait hört der Anrufer sich selbst (Echo) nach einem 1-s-Ton.
"""
import argparse, asyncio, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from minisip import SipUA

def tone(f, s):
    t = np.arange(int(8000 * s)) / 8000
    return (np.sin(2 * np.pi * f * t) * 0.3 * 32767).astype(np.int16).tobytes()

async def talk(call, seconds):
    call.send_pcm(tone(440, 1.0))
    end = time.monotonic() + seconds
    while time.monotonic() < end and not call.ended.is_set():
        try:
            call.send_pcm(await asyncio.wait_for(call.rx.get(), 0.2))      # Echo
        except asyncio.TimeoutError:
            pass
        while not call.dtmf.empty():
            print("DTMF:", call.dtmf.get_nowait())
    s = call.stats
    print(f"Codec-PT {call.pt} (8=PCMA, 0=PCMU), RTP-Ziel {call.rtp_remote}, Pakete {s.packets}, verloren {s.lost}, "
          f"Jitter {s.jitter_ms:.1f} ms, größte Lücke {s.max_gap_ms:.0f} ms, Ende: {call.end_reason or 'läuft'}")
    await call.hangup()

async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="fritz.box"); p.add_argument("--user", required=True); p.add_argument("--password", required=True)
    p.add_argument("--call"); p.add_argument("--wait", action="store_true"); p.add_argument("--seconds", type=float, default=15)
    p.add_argument("--local-ip", help="im Docker-Bridge-Netz: LAN-Adresse des Hosts"); p.add_argument("--sip-port", type=int, default=5070)
    a = p.parse_args()
    ua = SipUA(a.user, a.password, a.host, sip_port=a.sip_port, local_ip=a.local_ip, trace=True)
    await ua.start()
    t = time.monotonic(); ok = await ua.register()
    print(f"Registrierung: {'ok' if ok else 'FEHLGESCHLAGEN'} in {(time.monotonic()-t)*1000:.0f} ms, Laufzeit {ua.expires} s, eigene Adresse {ua.local_ip}")
    if not ok:
        return
    if a.call:
        call = await ua.call(a.call)
        print("Zustand:", call.state, call.end_reason, {k: round(v - call.t['invite'], 3) for k, v in call.t.items()})
        if call.state == "up":
            await talk(call, a.seconds)
    if a.wait:
        print("Warte auf Anruf (Strg+C beendet) …")
        call = await ua.incoming.get()
        print("Anruf von:", call.remote_number, "| From:", call.invite.get("From"), "| To:", call.invite.get("To"))
        print("Kopfzeilen:", [(k, v) for k, v in call.invite.headers if k.lower() in ("supported", "require", "session-expires", "allow", "user-agent", "p-asserted-identity")])
        await call.answer(); await talk(call, a.seconds)
    await ua.register(expires=0)
asyncio.run(main())
