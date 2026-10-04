# SPDX-License-Identifier: MIT
"""Helios-Mesh: reference implementation (single file, Python 3.10+, no dependencies).

Sections: energy, navigation, radio, frames, routing, dtn, storage, node.
"""
import hashlib
import heapq
import hmac
import itertools
import math
import struct
from dataclasses import dataclass, field

__version__ = "0.2.0"


# ============ energy ============
MODES = ("sleep", "survival", "eco", "full")
# upward thresholds: sleep->survival, survival->eco, eco->full
UP = (0.20, 0.35, 0.65)
# downward thresholds (5% hysteresis)
DOWN = (0.15, 0.30, 0.60)

POLICY = {
    "full":     {"radios": {"lora", "uhf", "hf", "sat"}, "replicate": True,  "compute": True},
    "eco":      {"radios": {"lora", "uhf"},               "replicate": False, "compute": False},
    "survival": {"radios": {"lora"},                      "replicate": False, "compute": False},
    "sleep":    {"radios": set(),                         "replicate": False, "compute": False},
}


@dataclass
class EnergyScheduler:
    mode: str = "eco"

    def update(self, soc: float) -> str:
        i = MODES.index(self.mode)
        while i < 3 and soc >= UP[i]:
            i += 1
        while i > 0 and soc < DOWN[i - 1]:
            i -= 1
        self.mode = MODES[i]
        return self.mode

    def policy(self) -> dict:
        return POLICY[self.mode]


def daily_harvest_wh(panel_w: float, peak_sun_hours: float, efficiency: float = 0.75) -> float:
    return panel_w * peak_sun_hours * efficiency


def autonomy_days(battery_wh: float, load_wh_day: float, dod: float = 0.8) -> float:
    return battery_wh * dod / load_wh_day


# ============ navigation ============
POLARIS_P_DEG = 0.7


def latitude_from_polaris(altitude_deg, lha_deg, p_deg=POLARIS_P_DEG):
    """φ ≈ h − p·cos(LHA) (first order)."""
    return altitude_deg - p_deg * math.cos(math.radians(lha_deg))


def julian_date(y, mo, d, h=0, mi=0, s=0):
    if mo <= 2:
        y, mo = y - 1, mo + 12
    a = y // 100
    b = 2 - a + a // 4
    day = d + (h + (mi + s / 60) / 60) / 24
    return int(365.25 * (y + 4716)) + int(30.6001 * (mo + 1)) + day + b - 1524.5


def gmst_deg(jd):
    """Greenwich mean sidereal time in degrees."""
    t = (jd - 2451545.0) / 36525
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t
    return g % 360


def longitude_from_transit(gmst_at_transit_deg, star_ra_deg):
    """Star on the meridian: LST = RA, so longitude = RA - GMST (east positive)."""
    lon = (star_ra_deg - gmst_at_transit_deg + 180) % 360 - 180
    return lon


def time_error_to_distance_m(seconds, lat_deg=0.0):
    return seconds * 463.8 * math.cos(math.radians(lat_deg))


def gnss_spoof_suspect(gnss_lat, star_lat, tolerance_km=30.0):
    """Compare GNSS latitude with the astronomical one (1 deg ~ 111.2 km)."""
    return abs(gnss_lat - star_lat) * 111.2 > tolerance_km


# ============ radio ============
@dataclass(frozen=True)
class Channel:
    name: str
    range_km: float
    bps: float
    tx_power_w: float


CHANNELS = {
    "lora": Channel("lora", 15, 5_000, 0.1),
    "uhf":  Channel("uhf", 60, 50_000, 5.0),
    "hf":   Channel("hf", 3_000, 1_000, 40.0),
    "sat":  Channel("sat", 20_000, 9_600, 5.0),
}


def tx_time_s(ch: Channel, size_bytes: int) -> float:
    return size_bytes * 8 / ch.bps


def hf_band_ok(hour_utc: int, band_mhz: float) -> bool:
    """Rough rule: daytime uses higher bands (>=14 MHz), night lower (<=10 MHz)."""
    day = 6 <= hour_utc < 18
    return band_mhz >= 14 if day else band_mhz <= 10


def choose_channel(size_bytes: int, distance_km: float, allowed: set,
                   sat_window: bool = False, urgent: bool = False) -> str | None:
    """Minimize transmit time among channels that reach the target."""
    best, best_t = None, float("inf")
    for name in allowed:
        ch = CHANNELS[name]
        if ch.range_km < distance_km:
            continue
        if name == "sat" and not sat_window:
            continue
        t = tx_time_s(ch, size_bytes)
        if not urgent:
            t += ch.tx_power_w * 0.01  # small energy penalty
        if t < best_t:
            best, best_t = name, t
    return best


# ============ frames ============
HEADER = ">BB4s4sH"       # ver_flags, ttl, src, dst, seq
HLEN = struct.calcsize(HEADER)
TAG = 8
MAX_PAYLOAD = 200


def _tag(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()[:TAG]


def pack(key: bytes, src: bytes, dst: bytes, seq: int, payload: bytes,
         ttl: int = 8, flags: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    head = struct.pack(HEADER, (1 << 4) | (flags & 0x0F), ttl, src, dst, seq)
    body = head + payload
    return body + _tag(key, body)


def unpack(key: bytes, frame: bytes) -> dict:
    body, tag = frame[:-TAG], frame[-TAG:]
    if not hmac.compare_digest(tag, _tag(key, body)):
        raise ValueError("bad auth tag")
    vf, ttl, src, dst, seq = struct.unpack(HEADER, body[:HLEN])
    return {"version": vf >> 4, "flags": vf & 0x0F, "ttl": ttl, "src": src,
            "dst": dst, "seq": seq, "payload": body[HLEN:]}


def forward(frame: bytes, key: bytes) -> bytes | None:
    """Decrement TTL and re-sign; None if TTL is exhausted."""
    f = unpack(key, frame)
    if f["ttl"] <= 1:
        return None
    return pack(key, f["src"], f["dst"], f["seq"], f["payload"], f["ttl"] - 1, f["flags"])


# ============ routing ============
K = 4.0
MIN_SOC = 0.15


@dataclass
class Router:
    soc: dict = field(default_factory=dict)     # node -> state of charge 0..1
    links: dict = field(default_factory=dict)   # node -> [(neighbor, etx, channel)]

    def add_node(self, n, soc=1.0):
        self.soc[n] = soc
        self.links.setdefault(n, [])

    def add_link(self, u, v, etx, channel):
        self.links[u].append((v, etx, channel))
        self.links[v].append((u, etx, channel))

    def cost(self, etx, soc_rx):
        return etx * (1 + K * (1 - soc_rx))

    def route(self, src, dst):
        pq, best, prev = [(0.0, src)], {src: 0.0}, {}
        while pq:
            d, u = heapq.heappop(pq)
            if u == dst:
                break
            if d > best.get(u, float("inf")):
                continue
            for v, etx, ch in self.links[u]:
                if v != dst and self.soc[v] < MIN_SOC:
                    continue
                nd = d + self.cost(etx, self.soc[v])
                if nd < best.get(v, float("inf")):
                    best[v], prev[v] = nd, (u, ch)
                    heapq.heappush(pq, (nd, v))
        if dst != src and dst not in prev:
            return None
        path, hops, n = [dst], [], dst
        while n != src:
            u, ch = prev[n]
            path.append(u)
            hops.append(ch)
            n = u
        return path[::-1], hops[::-1], best[dst]


# ============ dtn ============
@dataclass(order=True)
class Bundle:
    priority: int
    seq: int = field(compare=True)
    dst: str = field(compare=False, default="")
    payload: bytes = field(compare=False, default=b"")
    expires: float = field(compare=False, default=float("inf"))


class DTNQueue:
    def __init__(self):
        self._h, self._c = [], itertools.count()

    def push(self, dst, payload, priority=5, expires=float("inf")):
        heapq.heappush(self._h, Bundle(priority, next(self._c), dst, payload, expires))

    def expire(self, now):
        keep = [b for b in self._h if b.expires > now]
        dropped = len(self._h) - len(keep)
        self._h = keep
        heapq.heapify(self._h)
        return dropped

    def pop_ready(self, neighbors, now=0.0):
        """Return the highest-priority bundle whose destination is reachable via neighbors."""
        self.expire(now)
        skipped, out = [], None
        while self._h:
            b = heapq.heappop(self._h)
            if b.dst in neighbors:
                out = b
                break
            skipped.append(b)
        for b in skipped:
            heapq.heappush(self._h, b)
        return out

    def __len__(self):
        return len(self._h)


# ============ storage ============
# --- GF(256), polynomial 0x11d ---
EXP, LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gmul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def ginv(a):
    return EXP[255 - LOG[a]]


def cid(data: bytes) -> str:
    return "h1-" + hashlib.sha256(data).hexdigest()


def _matrix(k, m):
    """Systematic (k+m) x k matrix: identity + Cauchy."""
    rows = [[1 if i == j else 0 for j in range(k)] for i in range(k)]
    for i in range(m):
        rows.append([ginv((k + i) ^ j) for j in range(k)])
    return rows


def _invert(mat):
    n = len(mat)
    a = [row[:] + [1 if i == j else 0 for j in range(n)] for i, row in enumerate(mat)]
    for c in range(n):
        p = next(r for r in range(c, n) if a[r][c])
        a[c], a[p] = a[p], a[c]
        iv = ginv(a[c][c])
        a[c] = [gmul(iv, v) for v in a[c]]
        for r in range(n):
            if r != c and a[r][c]:
                f = a[r][c]
                a[r] = [v ^ gmul(f, w) for v, w in zip(a[r], a[c])]
    return [row[n:] for row in a]


def encode(data: bytes, k: int = 8, m: int = 4):
    """Return (shards: list[bytes], original_len)."""
    n = len(data)
    size = -(-n // k) or 1
    padded = data.ljust(size * k, b"\0")
    chunks = [padded[i * size:(i + 1) * size] for i in range(k)]
    shards = [bytes(c) for c in chunks]
    mat = _matrix(k, m)
    for row in mat[k:]:
        out = bytearray(size)
        for coef, ch in zip(row, chunks):
            if coef:
                for t in range(size):
                    out[t] ^= gmul(coef, ch[t])
        shards.append(bytes(out))
    return shards, n


def decode(available: dict, n: int, k: int = 8, m: int = 4) -> bytes:
    """available: {shard_index: bytes}; any k shards are enough."""
    if len(available) < k:
        raise ValueError(f"need {k} shards, have {len(available)}")
    idx = sorted(available)[:k]
    size = len(available[idx[0]])
    mat = _matrix(k, m)
    inv = _invert([mat[i] for i in idx])
    chunks = []
    for row in inv:
        out = bytearray(size)
        for coef, i in zip(row, idx):
            if coef:
                sh = available[i]
                for t in range(size):
                    out[t] ^= gmul(coef, sh[t])
        chunks.append(bytes(out))
    return b"".join(chunks)[:n]


# ============ node ============
@dataclass
class Node:
    node_id: str
    soc: float = 1.0
    shards: dict = field(default_factory=dict)   # (cid, idx) -> bytes
    queue: DTNQueue = field(default_factory=DTNQueue)
    energy: EnergyScheduler = field(default_factory=EnergyScheduler)

    def tick(self, soc):
        self.soc = soc
        return self.energy.update(soc)

    def store_shard(self, key, data):
        if not self.energy.policy()["replicate"] and self.energy.mode != "eco":
            return False
        self.shards[key] = data
        return True

    def pick_channel(self, size, distance_km, **kw):
        return choose_channel(size, distance_km, self.energy.policy()["radios"], **kw)


def publish(data: bytes, holders: list, k=8, m=4):
    """Encode an object and spread shards over nodes round-robin. Return (cid, n)."""
    c = cid(data)
    shards, n = encode(data, k, m)
    for i, sh in enumerate(shards):
        holders[i % len(holders)].shards[(c, i)] = sh
    return c, n


def fetch(c, n, holders, k=8, m=4):
    got = {}
    for h in holders:
        for (cc, i), sh in h.shards.items():
            if cc == c:
                got[i] = sh
    data = decode(got, n, k, m)
    if cid(data) != c:
        raise ValueError("CID mismatch")
    return data
