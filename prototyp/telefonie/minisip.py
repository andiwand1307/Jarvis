"""Minimaler SIP-User-Agent (asyncio, UDP) mit RTP/G.711 – Prototyp für Jarvis-Telefonie.

Umfang: REGISTER mit Digest-Auth (mit/ohne qop), ausgehender und ankommender INVITE,
BYE/CANCEL, Antworten auf re-INVITE/UPDATE/OPTIONS (Session-Timer-Auffrischung der Gegenseite),
RTP mit PCMA/PCMU in 20-ms-Rahmen, DTMF-Empfang und -Versand nach RFC 4733 (telephone-event).

Nicht enthalten: TCP/TLS, SRTP, Jitterpuffer (Gegenstelle ist die Fritzbox im LAN), G.722,
Route-Sets, Weiterleitungen (3xx), Halten.

Abhängigkeit: nur numpy (Python 3.13 hat `audioop` nicht mehr).
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
import socket
import struct
import time
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------- G.711 ----


def _alaw2lin(a: int) -> int:
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if a & 0x80 else -t


def _ulaw2lin(u: int) -> int:
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if u & 0x80 else (t - 0x84)


def _lin2alaw(s: int) -> int:
    s >>= 3
    if s >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        s = -s - 1
    for seg, end in enumerate((0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)):
        if s <= end:
            aval = seg << 4
            aval |= (s >> 1) & 0x0F if seg < 2 else (s >> seg) & 0x0F
            return aval ^ mask
    return 0x7F ^ mask


def _lin2ulaw(s: int) -> int:
    s >>= 2
    if s < 0:
        s = -s
        mask = 0x7F
    else:
        mask = 0xFF
    s = min(s, 8159) + (0x84 >> 2)
    for seg, end in enumerate((0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)):
        if s <= end:
            return ((seg << 4) | ((s >> (seg + 1)) & 0x0F)) ^ mask
    return 0x7F ^ mask


_DEC = {8: np.array([_alaw2lin(i) for i in range(256)], dtype=np.int16),
        0: np.array([_ulaw2lin(i) for i in range(256)], dtype=np.int16)}
_ENC = {8: np.array([_lin2alaw(i - 32768) for i in range(65536)], dtype=np.uint8),
        0: np.array([_lin2ulaw(i - 32768) for i in range(65536)], dtype=np.uint8)}


def g711_decode(pt: int, payload: bytes) -> bytes:
    return _DEC[pt][np.frombuffer(payload, dtype=np.uint8)].tobytes()


def g711_encode(pt: int, pcm16: bytes) -> bytes:
    idx = np.frombuffer(pcm16, dtype=np.int16).astype(np.int32) + 32768
    return _ENC[pt][idx].tobytes()


# ----------------------------------------------------------------- SIP -----

CRLF = "\r\n"
DTMF_CHARS = "0123456789*#ABCD"


def _rand(n: int = 10) -> str:
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=n))


@dataclass
class Msg:
    start: str
    headers: list[tuple[str, str]]
    body: str = ""

    @property
    def is_response(self) -> bool:
        return self.start.startswith("SIP/2.0")

    @property
    def status(self) -> int:
        return int(self.start.split()[1])

    @property
    def method(self) -> str:
        return self.start.split()[0]

    def get(self, name: str, default: str = "") -> str:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return default

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    @property
    def cseq_method(self) -> str:
        return self.get("CSeq").split()[-1]


_COMPACT = {"v": "Via", "f": "From", "t": "To", "i": "Call-ID", "m": "Contact", "l": "Content-Length",
            "c": "Content-Type", "k": "Supported"}


def parse(data: bytes) -> Msg | None:
    try:
        text = data.decode("utf-8", "replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        headers = []
        for line in lines[1:]:
            if line[:1] in " \t" and headers:
                headers[-1] = (headers[-1][0], headers[-1][1] + " " + line.strip())
                continue
            k, _, v = line.partition(":")
            k = k.strip()
            headers.append((_COMPACT.get(k.lower(), k), v.strip()))
        if not lines[0].strip():
            return None
        return Msg(lines[0], headers, body)
    except Exception:  # noqa: BLE001
        return None


def _tag(value: str) -> str:
    m = re.search(r";tag=([^;>\s]+)", value)
    return m.group(1) if m else ""


def _uri(value: str) -> str:
    m = re.search(r"<([^>]+)>", value)
    return m.group(1) if m else value.split(";")[0].strip()


def digest(challenge: str, user: str, password: str, method: str, uri: str) -> str:
    p = dict(re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', challenge) and
             [(k, a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', challenge)])
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
    ha1, ha2 = md5(f"{user}:{realm}:{password}"), md5(f"{method}:{uri}")
    out = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", algorithm=MD5'
    if "auth" in p.get("qop", ""):
        cnonce, nc = _rand(8), "00000001"
        resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        out += f', qop=auth, nc={nc}, cnonce="{cnonce}"'
    else:
        resp = md5(f"{ha1}:{nonce}:{ha2}")
    out += f', response="{resp}"'
    if "opaque" in p:
        out += f', opaque="{p["opaque"]}"'
    return out


@dataclass
class RtpStats:
    packets: int = 0
    lost: int = 0
    first_at: float = 0.0
    jitter_ms: float = 0.0      # RFC-3550-Schätzer
    max_gap_ms: float = 0.0
    _last_seq: int = -1
    _last_arr: float = 0.0
    _last_ts: int = 0


class Call(asyncio.DatagramProtocol):
    """Ein Gespräch: Dialogzustand + RTP. `rx` liefert PCM16/8 kHz in 20-ms-Blöcken."""

    def __init__(self, ua: "SipUA", call_id: str, direction: str):
        self.ua, self.call_id, self.direction = ua, call_id, direction
        self.local_tag, self.remote_tag = _rand(), ""
        self.local_uri = self.remote_uri = self.remote_target = ""
        self.cseq = random.randint(100, 9999)
        self.state = "new"            # new → ringing → up → ended
        self.answered = asyncio.Event()
        self.ended = asyncio.Event()
        self.end_reason = ""
        self.rx: asyncio.Queue[bytes] = asyncio.Queue()
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()
        self.stats = RtpStats()
        self.remote_number = ""
        self.t: dict[str, float] = {}
        # RTP
        self.pt, self.dtmf_pt = 8, 101
        self.rtp_remote: tuple[str, int] | None = None
        self.rtp_transport: asyncio.DatagramTransport | None = None
        self.rtp_port = 0
        self._tx: asyncio.Queue[bytes] = asyncio.Queue()
        self._tx_buf = b""
        self._tx_task: asyncio.Task | None = None
        self._seq, self._ts, self._ssrc = random.randint(0, 0xFFFF), random.randint(0, 2**31), random.getrandbits(32)
        self._dtmf_last = (-1, False)
        self.invite: Msg | None = None       # ankommender INVITE
        self._last_sdp = ""

    # --- RTP ---------------------------------------------------------------
    async def open_rtp(self) -> None:
        loop = asyncio.get_running_loop()
        lo, hi = self.ua.rtp_ports
        for port in range(lo, hi, 2):
            try:
                self.rtp_transport, _ = await loop.create_datagram_endpoint(
                    lambda: self, local_addr=(self.ua.bind_ip, port))
                self.rtp_port = port
                return
            except OSError:
                continue
        raise OSError("kein freier RTP-Port")

    def datagram_received(self, data: bytes, addr) -> None:  # RTP eingehend
        if len(data) < 12 or data[0] >> 6 != 2:
            return
        now = time.monotonic()
        pt, seq = data[1] & 0x7F, struct.unpack("!H", data[2:4])[0]
        ts = struct.unpack("!I", data[4:8])[0]
        cc = data[0] & 0x0F
        payload = data[12 + 4 * cc:]
        st = self.stats
        if pt in (0, 8):
            if not st.first_at:
                st.first_at = now
                self.t["first_rtp"] = now
            elif st._last_seq >= 0:
                gap = (seq - st._last_seq) & 0xFFFF
                if 1 < gap < 1000:
                    st.lost += gap - 1
                d = abs((now - st._last_arr) * 1000 - ((ts - st._last_ts) & 0xFFFFFFFF) / 8)
                st.jitter_ms += (d - st.jitter_ms) / 16
                st.max_gap_ms = max(st.max_gap_ms, (now - st._last_arr) * 1000)
            st._last_seq, st._last_arr, st._last_ts = seq, now, ts
            st.packets += 1
            self.rx.put_nowait(g711_decode(pt, payload))
        elif pt == self.dtmf_pt and len(payload) >= 4:
            ev, end = payload[0], bool(payload[1] & 0x80)
            if end and self._dtmf_last != (ts, True) and ev < len(DTMF_CHARS):
                self.dtmf.put_nowait(DTMF_CHARS[ev])
            if end:
                self._dtmf_last = (ts, True)

    def send_pcm(self, pcm16: bytes) -> None:
        """PCM16 mono 8 kHz in beliebiger Stückelung; wird in 20-ms-Takt gesendet."""
        self._tx.put_nowait(pcm16)

    def clear_tx(self) -> int:
        """Barge-in: alles noch nicht Gesendete verwerfen. Liefert verworfene Millisekunden."""
        n = len(self._tx_buf)
        self._tx_buf = b""
        while not self._tx.empty():
            n += len(self._tx.get_nowait())
        return n // 16

    def tx_pending_ms(self) -> int:
        return (len(self._tx_buf) + sum(len(x) for x in list(self._tx._queue))) // 16  # type: ignore[attr-defined]

    async def send_dtmf(self, digits: str, ms: int = 160) -> None:
        for ch in digits:
            ev, ts = DTMF_CHARS.index(ch), self._ts
            for i in range(ms // 20 + 3):
                end = i >= ms // 20
                dur = min((i + 1) * 160, ms * 8)
                hdr = struct.pack("!BBHII", 0x80, self.dtmf_pt | (0x80 if i == 0 else 0), self._seq, ts, self._ssrc)
                self._seq = (self._seq + 1) & 0xFFFF
                if self.rtp_transport and self.rtp_remote:
                    self.rtp_transport.sendto(hdr + struct.pack("!BBH", ev, (0x80 if end else 0) | 10, dur), self.rtp_remote)
                if not end:
                    await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)

    async def _tx_loop(self) -> None:
        """Sendet alle 20 ms einen Rahmen (Stille, wenn nichts ansteht), driftfrei über die Monotonuhr."""
        frame, silence = 320, b"\x00" * 320
        t0, n = time.monotonic(), 0
        while self.state == "up":
            while len(self._tx_buf) < frame and not self._tx.empty():
                self._tx_buf += self._tx.get_nowait()
            if len(self._tx_buf) >= frame:
                chunk, self._tx_buf = self._tx_buf[:frame], self._tx_buf[frame:]
            else:
                chunk = silence
            hdr = struct.pack("!BBHII", 0x80, self.pt, self._seq, self._ts, self._ssrc)
            self._seq, self._ts = (self._seq + 1) & 0xFFFF, (self._ts + 160) & 0xFFFFFFFF
            if self.rtp_transport and self.rtp_remote:
                self.rtp_transport.sendto(hdr + g711_encode(self.pt, chunk), self.rtp_remote)
            n += 1
            delay = t0 + n * 0.02 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:                      # Aussetzer: Takt neu aufsetzen
                t0, n = time.monotonic(), 0

    # --- SDP ---------------------------------------------------------------
    def sdp(self) -> str:
        ip = self.ua.local_ip
        pts = [self.pt] + ([0] if self.pt == 8 and self.direction == "out" and self.state != "up" else [])
        lines = ["v=0", f"o=jarvis {self._ssrc} {int(time.time())} IN IP4 {ip}", "s=jarvis", f"c=IN IP4 {ip}", "t=0 0",
                 f"m=audio {self.rtp_port} RTP/AVP {' '.join(map(str, pts))} {self.dtmf_pt}"]
        names = {8: "PCMA", 0: "PCMU"}
        lines += [f"a=rtpmap:{p} {names[p]}/8000" for p in pts]
        lines += [f"a=rtpmap:{self.dtmf_pt} telephone-event/8000", f"a=fmtp:{self.dtmf_pt} 0-16", "a=ptime:20", "a=sendrecv"]
        return CRLF.join(lines) + CRLF

    def apply_sdp(self, sdp: str) -> bool:
        ip = re.search(r"^c=IN IP4 (\S+)", sdp, re.M)
        m = re.search(r"^m=audio (\d+) RTP/AVP ([\d ]+)", sdp, re.M)
        if not (ip and m):
            return False
        pts = [int(x) for x in m.group(2).split()]
        codec = next((p for p in pts if p in (8, 0)), None)
        if codec is None:
            return False
        self.pt = codec
        te = re.search(r"^a=rtpmap:(\d+) telephone-event/8000", sdp, re.M)
        if te:
            self.dtmf_pt = int(te.group(1))
        self.rtp_remote = (ip.group(1), int(m.group(1)))
        self._last_sdp = sdp
        return True

    def _start_media(self) -> None:
        self.state = "up"
        self.t["up"] = time.monotonic()
        self.answered.set()
        if not self._tx_task:
            self._tx_task = asyncio.ensure_future(self._tx_loop())

    # --- Steuerung ----------------------------------------------------------
    async def answer(self) -> None:
        assert self.invite is not None
        self.ua._respond(self.invite, 200, "OK", self, body=self.sdp())
        self._start_media()

    async def hangup(self) -> None:
        if self.state == "ended":
            return
        if self.state == "up":
            self.ua._request("BYE", self)
        elif self.direction == "out":
            self.ua._cancel(self)
        elif self.invite is not None:
            self.ua._respond(self.invite, 486, "Busy Here", self)
        self._end("local")

    def _end(self, reason: str) -> None:
        if self.state == "ended":
            return
        self.state, self.end_reason = "ended", reason
        self.ended.set()
        self.answered.set()
        if self.rtp_transport:
            self.rtp_transport.close()
        self.ua.calls.pop(self.call_id, None)


class SipUA(asyncio.DatagramProtocol):
    def __init__(self, user: str, password: str, registrar: str, registrar_port: int = 5060,
                 bind_ip: str = "0.0.0.0", local_ip: str | None = None, sip_port: int = 5070,
                 rtp_ports: tuple[int, int] = (7078, 7110), display: str = "Jarvis", trace: bool = False):
        self.user, self.password, self.display = user, password, display
        self.registrar = (registrar, registrar_port)
        self.domain = registrar
        self.bind_ip, self.sip_port, self.rtp_ports = bind_ip, sip_port, rtp_ports
        self.local_ip = local_ip or self._guess_ip()
        self.transport: asyncio.DatagramTransport | None = None
        self.calls: dict[str, Call] = {}
        self.incoming: asyncio.Queue[Call] = asyncio.Queue()
        self._waiters: dict[str, asyncio.Future] = {}     # branch → Antwort
        self._reg_call_id, self._reg_cseq, self._reg_tag = _rand(16), 1, _rand()
        self.registered = False
        self.expires = 300
        self.trace = trace
        self._reg_task: asyncio.Task | None = None
        self._invite_branch: dict[str, tuple[str, str]] = {}  # call_id → (branch, request-uri)

    def _guess_ip(self) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((socket.gethostbyname(self.registrar[0]), self.registrar[1]))
            return s.getsockname()[0]
        finally:
            s.close()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(self.bind_ip, self.sip_port))

    # --- Senden ---------------------------------------------------------------
    def _send(self, text: str, addr=None) -> None:
        if self.trace:
            print(f">>> {text.splitlines()[0]}")
        self.transport.sendto(text.encode(), addr or self.registrar)

    def _contact(self) -> str:
        return f"<sip:{self.user}@{self.local_ip}:{self.sip_port}>"

    def _build(self, method: str, uri: str, headers: list[tuple[str, str]], body: str = "", branch: str | None = None) -> tuple[str, str]:
        branch = branch or "z9hG4bK" + _rand(14)
        lines = [f"{method} {uri} SIP/2.0", f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};rport;branch={branch}",
                 "Max-Forwards: 70"]
        lines += [f"{k}: {v}" for k, v in headers]
        lines += ["User-Agent: jarvis-minisip/0.1", f"Content-Length: {len(body.encode())}"]
        return CRLF.join(lines) + CRLF + CRLF + body, branch

    async def _transact(self, text: str, branch: str, timeout: float = 8.0, final: bool = True) -> Msg | None:
        """Sendet mit Wiederholung (0,5/1/2 s) und wartet auf die (End-)Antwort."""
        fut = asyncio.get_running_loop().create_future()
        self._waiters[branch] = fut
        try:
            deadline, wait = time.monotonic() + timeout, 0.5
            while True:
                self._send(text)
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), min(wait, max(0.01, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        return None
                    wait = min(wait * 2, 4)
        finally:
            self._waiters.pop(branch, None)

    # --- Registrierung ----------------------------------------------------------
    async def register(self, expires: int = 300) -> bool:
        uri = f"sip:{self.domain}"
        auth = None
        for _ in range(3):
            self._reg_cseq += 1
            h = [("From", f'"{self.display}" <sip:{self.user}@{self.domain}>;tag={self._reg_tag}'),
                 ("To", f"<sip:{self.user}@{self.domain}>"), ("Call-ID", self._reg_call_id),
                 ("CSeq", f"{self._reg_cseq} REGISTER"), ("Contact", f"{self._contact()};expires={expires}"),
                 ("Expires", str(expires)), ("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, UPDATE, INFO")]
            if auth:
                h.append(auth)
            text, branch = self._build("REGISTER", uri, h)
            resp = await self._transact(text, branch)
            if resp is None:
                self.registered = False
                return False
            if resp.status in (401, 407) and not auth:
                hdr = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
                name = "Authorization" if resp.status == 401 else "Proxy-Authorization"
                auth = (name, digest(resp.get(hdr), self.user, self.password, "REGISTER", uri))
                continue
            if resp.status == 423:     # Intervall zu kurz
                expires = int(resp.get("Min-Expires", "300"))
                continue
            self.registered = resp.status == 200 and expires > 0
            if resp.status == 200:
                m = re.search(r"expires=(\d+)", " ".join(resp.all("Contact")))
                self.expires = int(m.group(1)) if m else int(resp.get("Expires", str(expires)) or expires)
            return resp.status == 200
        return False

    async def keep_registered(self) -> None:
        """Dauerschleife: neu registrieren bei 80 % der Laufzeit, bei Fehlern mit Wartezeit erneut."""
        backoff = 2
        while True:
            ok = await self.register()
            if ok:
                backoff = 2
                await asyncio.sleep(max(20, self.expires * 0.8))
            else:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # --- Ausgehender Anruf --------------------------------------------------------
    async def call(self, number: str, timeout: float = 30.0) -> Call:
        call = Call(self, _rand(20) + "@" + self.local_ip, "out")
        self.calls[call.call_id] = call
        call.remote_number = number
        call.local_uri, call.remote_uri = f"sip:{self.user}@{self.domain}", f"sip:{number}@{self.domain}"
        call.remote_target = call.remote_uri
        await call.open_rtp()
        call.t["invite"] = time.monotonic()
        auth = None
        for _ in range(2):
            call.cseq += 1
            h = [("From", f'"{self.display}" <{call.local_uri}>;tag={call.local_tag}'), ("To", f"<{call.remote_uri}>"),
                 ("Call-ID", call.call_id), ("CSeq", f"{call.cseq} INVITE"), ("Contact", self._contact()),
                 ("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, UPDATE, INFO"), ("Content-Type", "application/sdp")]
            if auth:
                h.append(auth)
            text, branch = self._build("INVITE", call.remote_uri, h, call.sdp())
            self._invite_branch[call.call_id] = (branch, call.remote_uri)
            resp = await self._transact(text, branch, timeout=timeout)
            if resp is None:
                call._end("timeout")
                return call
            if resp.status >= 300:
                self._ack(call, resp, branch)          # ACK für Nicht-2xx: gleiche Transaktion
            if resp.status in (401, 407) and not auth:
                hdr = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
                name = "Authorization" if resp.status == 401 else "Proxy-Authorization"
                auth = (name, digest(resp.get(hdr), self.user, self.password, "INVITE", call.remote_uri))
                continue
            if resp.status == 200:
                call.t["answer"] = time.monotonic()
                call.remote_tag = _tag(resp.get("To"))
                call.remote_target = _uri(resp.get("Contact")) or call.remote_uri
                self._ack(call, resp, None)
                if not call.apply_sdp(resp.body):
                    await call.hangup()
                    return call
                call._start_media()
            else:
                call._end(f"{resp.status} {resp.start.split(' ', 2)[-1]}")
            return call
        call._end("auth failed")
        return call

    def _ack(self, call: Call, resp: Msg, branch: str | None) -> None:
        uri = call.remote_target if branch is None else self._invite_branch[call.call_id][1]
        h = [("From", f"<{call.local_uri}>;tag={call.local_tag}"), ("To", resp.get("To")),
             ("Call-ID", call.call_id), ("CSeq", f"{resp.get('CSeq').split()[0]} ACK")]
        text, _ = self._build("ACK", uri, h, branch=branch)
        self._send(text)

    def _cancel(self, call: Call) -> None:
        branch, uri = self._invite_branch.get(call.call_id, ("", ""))
        if not branch:
            return
        h = [("From", f"<{call.local_uri}>;tag={call.local_tag}"), ("To", f"<{call.remote_uri}>"),
             ("Call-ID", call.call_id), ("CSeq", f"{call.cseq} CANCEL")]
        text, _ = self._build("CANCEL", uri, h, branch=branch)
        self._send(text)

    def _request(self, method: str, call: Call, body: str = "", ctype: str = "") -> None:
        """Anfrage innerhalb des Dialogs (BYE, …); Antwort wird nicht abgewartet."""
        call.cseq += 1
        to = f"<{call.remote_uri}>" + (f";tag={call.remote_tag}" if call.remote_tag else "")
        h = [("From", f"<{call.local_uri}>;tag={call.local_tag}"), ("To", to), ("Call-ID", call.call_id),
             ("CSeq", f"{call.cseq} {method}"), ("Contact", self._contact())]
        if ctype:
            h.append(("Content-Type", ctype))
        text, _ = self._build(method, call.remote_target, h, body)
        self._send(text)

    def _respond(self, req: Msg, code: int, reason: str, call: Call | None = None, body: str = "", extra=()) -> None:
        to = req.get("To")
        if call is not None and ";tag=" not in to:
            to += f";tag={call.local_tag}"
        lines = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in req.all("Via")]
        lines += [f"Record-Route: {v}" for v in req.all("Record-Route")]
        lines += [f"From: {req.get('From')}", f"To: {to}", f"Call-ID: {req.get('Call-ID')}", f"CSeq: {req.get('CSeq')}"]
        if code < 300 and req.method in ("INVITE", "UPDATE"):
            lines.append(f"Contact: {self._contact()}")
        lines += [f"{k}: {v}" for k, v in extra]
        if body:
            lines.append("Content-Type: application/sdp")
        lines += ["User-Agent: jarvis-minisip/0.1", f"Content-Length: {len(body.encode())}"]
        via = req.get("Via")
        m = re.search(r"SIP/2.0/UDP ([^;:\s]+)(?::(\d+))?", via)
        addr = (m.group(1), int(m.group(2) or 5060)) if m else self.registrar
        self._send(CRLF.join(lines) + CRLF + CRLF + body, self._src if getattr(self, "_src", None) else addr)

    # --- Empfang ---------------------------------------------------------------------
    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 8:        # Keepalive (CRLF)
            return
        msg = parse(data)
        if msg is None:
            return
        self._src = addr
        if self.trace:
            print(f"<<< {msg.start}")
        if msg.is_response:
            self._on_response(msg)
        else:
            self._on_request(msg)

    def _on_response(self, msg: Msg) -> None:
        m = re.search(r"branch=([^;,\s]+)", msg.get("Via"))
        branch = m.group(1) if m else ""
        call = self.calls.get(msg.get("Call-ID"))
        if msg.status < 200:
            if call and msg.status in (180, 183) and call.state == "new":
                call.state = "ringing"
                call.t["ringing"] = time.monotonic()
            return
        fut = self._waiters.get(branch)
        if fut and not fut.done():
            fut.set_result(msg)
        elif call and msg.status == 200 and msg.cseq_method == "INVITE" and call.direction == "out":
            self._ack(call, msg, None)       # wiederholtes 200: ACK erneut senden

    def _on_request(self, req: Msg) -> None:
        method = req.method
        call = self.calls.get(req.get("Call-ID"))
        if method == "INVITE":
            if call is None:
                self._new_incoming(req)
            elif call.state == "up":                         # re-INVITE (Session-Timer, Codecwechsel)
                if req.body:
                    call.apply_sdp(req.body)
                self._respond(req, 200, "OK", call, body=call.sdp())
            elif call.invite is not None:                    # Wiederholung
                self._respond(req, 180, "Ringing", call)
        elif method == "ACK":
            pass
        elif method == "BYE":
            self._respond(req, 200, "OK", call)
            if call:
                call._end("remote")
        elif method == "CANCEL":
            self._respond(req, 200, "OK", call)
            if call and call.invite is not None and call.state != "up":
                self._respond(call.invite, 487, "Request Terminated", call)
                call._end("cancelled")
        elif method == "UPDATE":
            if call and req.body:
                call.apply_sdp(req.body)
            self._respond(req, 200, "OK", call, body=call.sdp() if call and req.body else "")
        elif method in ("OPTIONS", "INFO", "NOTIFY", "MESSAGE"):
            self._respond(req, 200, "OK", call, extra=[("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, UPDATE, INFO")])
        else:
            self._respond(req, 405, "Method Not Allowed", call)

    def _new_incoming(self, req: Msg) -> None:
        call = Call(self, req.get("Call-ID"), "in")
        call.invite = req
        call.t["invite"] = time.monotonic()
        call.remote_tag = _tag(req.get("From"))
        call.local_uri, call.remote_uri = _uri(req.get("To")), _uri(req.get("From"))
        call.remote_target = _uri(req.get("Contact")) or call.remote_uri
        m = re.match(r"sips?:([^@;]+)@", call.remote_uri)
        call.remote_number = m.group(1) if m else ""
        self.calls[call.call_id] = call
        self._respond(req, 100, "Trying")

        async def setup() -> None:
            await call.open_rtp()
            if not call.apply_sdp(req.body):
                self._respond(req, 488, "Not Acceptable Here", call)
                call._end("no codec")
                return
            call.state = "ringing"
            self._respond(req, 180, "Ringing", call)
            self.incoming.put_nowait(call)
        asyncio.ensure_future(setup())
