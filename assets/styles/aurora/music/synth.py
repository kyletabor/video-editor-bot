#!/usr/bin/env python3
"""
AURORA - a 90 second loopable music bed, synthesised from scratch (numpy + scipy only).

Run (from this directory):
    uv run --python 3.12 --with numpy --with scipy python synth.py
Options:
    --no-analysis   only write aurora.wav (skip ffmpeg: mp3, images, measurements, report)

Layers: detuned-saw/triangle pad through a slowly moving low-pass, soft sine sub,
FM glass-bell arpeggio, long-ringing FM melody bells, dotted-eighth ping-pong delay,
convolution reverb (exponentially decaying filtered noise), 30 Hz high-pass, gentle
high roll-off, soft tanh bus saturation, 1 s fades, loudness set to -16 LUFS.

Everything is rendered on a ring of N samples (circular), so tails and the pad
cross-fade that straddle the end of the file wrap round to the start: the file
loops musically (A6/9 -> Dmaj9 is a V -> I move, and the reverb tail is continuous).
Deterministic: all randomness comes from one seeded generator.
"""
import os
import re
import subprocess
import sys
import time

import numpy as np
from scipy import signal
from scipy.io import wavfile

# --------------------------------------------------------------------------- grid
SR = 48_000
N = 4_320_000                     # 90.0 s exactly
BEAT = 33_750                     # samples per beat  -> 85.333 BPM
STEP = BEAT // 2                  # eighth note
BAR = 4 * BEAT
CHORD = 4 * BAR                   # 4 bars per chord = 11.25 s
BPM = 60.0 * SR / BEAT
assert N == 32 * BAR == 8 * CHORD and SR * 90 == N and BEAT % 2 == 0

SEED = 20261001
HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.environ.get(
    "FFMPEG",
    "/tmp/claude-1000/-home-orangepi-projects/cf151889-93dd-4cf8-81d0-beaf400298fe/"
    "scratchpad/render-main/.tools/ffmpeg/ffmpeg",
)
TARGET_LUFS = -16.0

# --------------------------------------------------------------------------- harmony
def hz(m):
    return 440.0 * 2.0 ** ((m - 69) / 12.0)


NOTE = {0: "C", 1: "C#", 2: "D", 3: "D#", 4: "E", 5: "F", 6: "F#", 7: "G", 8: "G#", 9: "A", 10: "A#", 11: "B"}


def nname(m):
    return f"{NOTE[m % 12]}{m // 12 - 1}"


# D major with Lydian colour (G#).  pad voicings are smooth (common tones held).
CHORDS = [
    dict(name="Dmaj9(#11)", sub=38, pad=[50, 57, 61, 64, 66]),   # D3 A3 C#4 E4 F#4 ; sub D2 ; bells add G#
    dict(name="Bm7(9)",     sub=35, pad=[47, 54, 57, 62, 66]),   # B2 F#3 A3 D4 F#4 ; sub B1 ; bells add C#
    dict(name="Gmaj7#11",   sub=31, pad=[50, 55, 59, 66, 73]),   # D3 G3 B3 F#4 C#5 ; sub G1
    dict(name="A6/9",       sub=33, pad=[52, 57, 61, 66, 71]),   # E3 A3 C#4 F#4 B4 ; sub A1
]
# Arpeggio pools (ascending). Chosen so that no two neighbouring pool notes are a semitone apart
# (bells ring ~1 s and overlap, so a pool must never stack minor seconds).  G# (the Lydian 4th) only over D.
POOLS = [
    [69, 73, 76, 78, 80, 85, 88],            # Dmaj9#11: A4 C#5 E5 F#5 G#5 C#6 E6
    [69, 71, 74, 78, 81, 85, 88],            # Bm9/11:   A4 B4 D5 F#5 A5 C#6 E6
    [69, 71, 74, 79, 81, 83, 85],            # Gmaj9#11: A4 B4 D5 G5 A5 B5 C#6   (no F#5 next to G5)
    [69, 71, 73, 76, 78, 81, 83, 85],        # A6/9:     A4 B4 C#5 E5 F#5 A5 B5 C#6
]
PATTERNS = {
    "A": [0, 1, 2, 3, 4, 3, 2, 1],
    "B": [0, 2, 4, 2, 5, 4, 2, 3],
    "C": [0, 3, 1, 4, 2, 5, 3, 6],
    "D": [2, 4, 3, 5, 4, 6, 5, 3],
    "E": [0, 2, 4, 0, 2, 4, 1, 3],
}
PAT_BY_CYCLE = [["A", "B", "A", "C"], ["C", "E", "B", "D"]]
OCT_BY_CYCLE = [[0, 0, 0, 0], [0, -12, 0, 0]]
ACCENT = [1.0, 0.62, 0.82, 0.62, 0.95, 0.62, 0.82, 0.62]
# one long melody bell per bar; every note is a member of that chord's arpeggio pool (never clashes with the bells)
MELODY = [
    [78, 80, 78, 76],   # F#5 G#5 F#5 E5    (G# = Lydian 4th over D)
    [74, 78, 81, 78],   # D5 F#5 A5 F#5
    [79, 83, 81, 79],   # G5 B5 A5 G5
    [76, 81, 78, 76],   # E5 A5 F#5 E5  -> F#5 (loop)
]

# --------------------------------------------------------------------------- helpers
def add_circ(buf, start, sig):
    """Add sig into ring buffer buf starting at sample `start` (any integer), wrapping."""
    n = len(sig)
    start %= N
    first = min(n, N - start)
    buf[start:start + first] += sig[:first]
    pos = first
    while pos < n:
        m = min(n - pos, N)
        buf[:m] += sig[pos:pos + m]
        pos += m


def circ_sosfilt(sos, x, pre=2 * SR):
    """IIR filter with 'ring' initial conditions (pre-roll with the signal's own tail)."""
    xe = np.concatenate([x[-pre:], x])
    return signal.sosfilt(sos, xe)[pre:]


def smooth_noise(n, rng, rate):
    k = int(n / SR * rate) + 3
    pts = rng.standard_normal(k)
    return np.interp(np.arange(n) / SR * rate, np.arange(k), pts)


def smoothstep(u):
    return u * u * (3.0 - 2.0 * u)


def rms2(l, r):
    return float(np.sqrt(np.mean(l * l + r * r) / 2.0))


def db(x):
    return 10 ** (x / 20.0)


# --------------------------------------------------------------------------- wavetables
TABLE = 4096
_tabs = {}


def _table(kind, nh):
    key = (kind, nh)
    if key not in _tabs:
        spec = np.zeros(TABLE // 2 + 1, complex)
        n = np.arange(1, nh + 1)
        if kind == "saw":
            amp = 1.0 / n
        else:  # triangle: odd harmonics, 1/n^2, alternating sign
            amp = np.where(n % 2 == 1, 1.0 / n ** 2 * np.where(((n - 1) // 2) % 2 == 0, 1, -1), 0.0)
        spec[n] = -1j * amp * (TABLE / 2)
        tab = np.fft.irfft(spec, TABLE)
        tab /= np.sqrt(np.mean(tab ** 2))          # unit RMS
        _tabs[key] = np.append(tab, tab[0])
    return _tabs[key]


def table_read(ext, ph):
    x = (ph % 1.0) * TABLE
    i = x.astype(np.int64)
    fr = x - i
    return ext[i] * (1.0 - fr) + ext[i + 1] * fr


# --------------------------------------------------------------------------- pad + sub
def chord_env(ramp, length):
    """Equal-power cross-fade envelope: rises over [0,ramp], falls over [CHORD, CHORD+ramp]."""
    tt = np.arange(length)
    up = np.clip(tt / ramp, 0, 1)
    dn = np.clip((tt - CHORD) / ramp, 0, 1)
    return np.sin(0.5 * np.pi * smoothstep(up)) * np.cos(0.5 * np.pi * smoothstep(dn))


def render_pad(rng):
    L = np.zeros(N)
    R = np.zeros(N)
    ramp = 3 * SR                                   # slow attack / release (3 s equal-power cross-fade)
    span = CHORD + ramp
    env = chord_env(ramp, span)
    for ci in range(8):
        chord = CHORDS[ci % 4]
        start = ci * CHORD - ramp // 2
        for m in chord["pad"]:
            f = hz(m)
            nh = int(min(48, 9000 / f))
            saw, tri = _table("saw", nh), _table("tri", 15)

            def osc(tab, cents):
                drift = 2.5 * smooth_noise(span, rng, 1.2)          # slow analogue-style wander (cents)
                fi = f * 2.0 ** ((cents + drift) / 1200.0)
                return table_read(tab, rng.random() + np.cumsum(fi / SR))

            centre = osc(saw, -7.0) + osc(saw, +7.0) + 0.9 * osc(tri, 0.0)
            side_l = osc(saw, -16.0)
            side_r = osc(saw, +16.0)
            w = (f / 220.0) ** -0.1 * env
            add_circ(L, start, w * (centre + 0.75 * side_l))
            add_circ(R, start, w * (centre + 0.75 * side_r))
    return L, R


def render_sub():
    out = np.zeros(N)
    ramp = int(1.2 * SR)
    span = CHORD + ramp
    env = chord_env(ramp, span)
    t = np.arange(span) / SR
    for ci in range(8):
        f = hz(CHORDS[ci % 4]["sub"])
        s = np.sin(2 * np.pi * f * t) + 0.30 * np.sin(2 * np.pi * 2 * f * t + 0.5)
        add_circ(out, ci * CHORD - ramp // 2, env * s)
    return out


def moving_lowpass(x, cutoff):
    """Low-pass whose cutoff follows `cutoff` (Hz, per-sample): cross-faded bank of fixed 4th-order filters."""
    ratio = 1.3
    cs = 500.0 * ratio ** np.arange(0, 10)
    pos = np.clip(np.log(cutoff / cs[0]) / np.log(ratio), 0, len(cs) - 1)
    out = np.zeros_like(x)
    for i, c in enumerate(cs):
        w = np.clip(1.0 - np.abs(pos - i), 0.0, None)
        if not w.any():
            continue
        sos = signal.butter(4, c, fs=SR, output="sos")
        out += w * circ_sosfilt(sos, x)
    return out


# --------------------------------------------------------------------------- bells
class Bell:
    """FM glass bell: carrier f, modulator 3.5f (inharmonic) with decaying index, plus a pure
    octave partial and a short high 'tick' partial.  Raised-cosine attack, exponential decay."""

    def __init__(self, tau, index, tau_mod, attack, oct_amp, oct_tau, tick_amp, tick_tau):
        length = int(5.0 * SR)
        t = np.arange(length) / SR
        att = np.where(t < attack, 0.5 - 0.5 * np.cos(np.pi * t / attack), 1.0)
        taper = np.where(t > 4.4, 0.5 + 0.5 * np.cos(np.pi * (t - 4.4) / 0.6), 1.0)
        self.t = t
        self.amp = np.exp(-t / tau) * att * taper
        self.mod = index * np.exp(-t / tau_mod)
        self.oct = oct_amp * np.exp(-t / oct_tau) * att * taper
        self.tick = tick_amp * np.exp(-t / tick_tau) * att

    def note(self, f):
        ph = 2 * np.pi * f * self.t
        y = self.amp * np.sin(ph + self.mod * np.sin(3.5 * ph))
        y += self.oct * np.sin(2 * ph + 0.3)
        y += self.tick * np.sin(5.43 * ph + 0.7)
        return y * (f / 880.0) ** -0.25            # lower notes a touch louder (equal loudness)


def pan_gains(p):
    a = (p + 1.0) * np.pi / 4.0
    return np.cos(a), np.sin(a)


def render_bells(rng):
    arp_bell = Bell(tau=0.70, index=1.5, tau_mod=0.22, attack=0.006, oct_amp=0.25, oct_tau=0.45, tick_amp=0.05, tick_tau=0.10)
    mel_bell = Bell(tau=1.30, index=1.1, tau_mod=0.35, attack=0.008, oct_amp=0.20, oct_tau=0.70, tick_amp=0.03, tick_tau=0.12)
    aL, aR, mL, mR = (np.zeros(N) for _ in range(4))
    gstep = 0
    for b in range(32):
        cyc, ci, j = b // 16, (b // 4) % 4, b % 4
        pool = POOLS[ci]
        pat = PATTERNS[PAT_BY_CYCLE[cyc][j]]
        octv = OCT_BY_CYCLE[cyc][j] if ci != 2 else 0      # G4 would sit a semitone above the pad's F#4
        for s in range(8):
            midi = pool[pat[s] % len(pool)] + octv
            vel = ACCENT[s] * (1.0 + 0.06 * (rng.random() * 2 - 1))
            start = b * BAR + s * STEP + int(rng.integers(-96, 97))          # +-2 ms human feel
            gl, gr = pan_gains(0.45 * np.sin(1.9 * gstep + 0.5))
            sig = arp_bell.note(hz(midi)) * vel
            add_circ(aL, start, gl * sig)
            add_circ(aR, start, gr * sig)
            gstep += 1
        # melody: one long bell on each bar; in the second cycle an octave-lower echo on beat 3
        note = MELODY[ci][j]
        start = b * BAR
        gl, gr = pan_gains(0.15 if j % 2 == 0 else -0.15)
        sig = mel_bell.note(hz(note))
        add_circ(mL, start, gl * sig)
        add_circ(mR, start, gr * sig)
        if cyc == 1:
            sig2 = mel_bell.note(hz(note - 12)) * 0.45
            add_circ(mL, start + 2 * BEAT, gl * sig2)
            add_circ(mR, start + 2 * BEAT, gr * sig2)
    return aL, aR, mL, mR


def pingpong_delay(l, r, gain=0.55, taps=6, time_steps=3):
    """Dotted-eighth ping-pong echo built from circularly shifted, progressively darker copies."""
    mono = 0.5 * (l + r)
    d = time_steps * STEP
    outL, outR = np.zeros(N), np.zeros(N)
    for k in range(1, taps + 1):
        sos = signal.butter(2, 6000.0 * 0.82 ** k, fs=SR, output="sos")
        tap = np.roll(circ_sosfilt(sos, mono), k * d) * gain ** k
        (outR if k % 2 == 1 else outL)[:] += tap
    return outL, outR


# --------------------------------------------------------------------------- reverb
def make_reverb_ir(rng):
    length = int(4.6 * SR)
    t = np.arange(length) / SR
    common = rng.standard_normal(length)
    lo_sos = signal.butter(4, 1200.0, fs=SR, output="sos")
    hp_sos = signal.butter(2, 110.0, "high", fs=SR, output="sos")
    lp_sos = signal.butter(2, 7500.0, fs=SR, output="sos")
    irs = []
    for _ in range(2):
        n = np.sqrt(0.35) * common + np.sqrt(0.65) * rng.standard_normal(length)   # partly correlated L/R
        lo = signal.sosfilt(lo_sos, n)
        hi = n - lo
        ir = lo * 10 ** (-3 * t / 4.0) + hi * 10 ** (-3 * t / 2.4)               # RT60 4.0 s lows, 2.4 s highs
        ir *= 1 - np.exp(-t / 0.015)
        ir = signal.sosfilt(lp_sos, signal.sosfilt(hp_sos, ir))
        ir = np.concatenate([np.zeros(int(0.020 * SR)), ir])                     # 20 ms pre-delay
        irs.append(ir / np.sqrt(np.sum(ir ** 2)))
    return irs


def circ_convolve(x, ir):
    return np.fft.irfft(np.fft.rfft(x, N) * np.fft.rfft(ir, N), N)


# --------------------------------------------------------------------------- loudness
def k_weight(x):
    b1 = [1.53512485958697, -2.69169618940638, 1.19839281085285]
    a1 = [1.0, -1.69065929318241, 0.73248077421585]
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0, -1.99004745483398, 0.99007225036621]
    return signal.lfilter(b2, a2, signal.lfilter(b1, a1, x))


def lufs_integrated(l, r):
    blk, hop = int(0.4 * SR), int(0.1 * SR)
    z = 0.0
    for ch in (l, r):
        k = k_weight(ch)
        c = np.concatenate([[0.0], np.cumsum(k * k)])
        st = np.arange(0, len(k) - blk + 1, hop)
        z = z + (c[st + blk] - c[st]) / blk
    loud = -0.691 + 10 * np.log10(z + 1e-20)
    g = z[loud > -70]
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    return -0.691 + 10 * np.log10(z[loud > rel].mean())


def true_peak_db(l, r):
    pk = 0.0
    for ch in (l, r):
        up = signal.resample_poly(ch, 4, 1)
        pk = max(pk, float(np.max(np.abs(up))))
    return 20 * np.log10(pk)


# --------------------------------------------------------------------------- mix
MIX = dict(          # dB, relative to each layer normalised to unit RMS
    pad=-3.0, sub=-17.0, arp=-10.0, mel=-13.0, delay=-4.0, wet=-7.0, pad_send=-6.0,
)


def build():
    rng = np.random.default_rng(SEED)
    t0 = time.time()

    def tick(msg):
        print(f"  [{time.time() - t0:5.1f}s] {msg}", flush=True)

    tick("pad")
    padL, padR = render_pad(rng)
    # slowly moving low-pass: periods divide 90 s exactly so the filter motion loops seamlessly
    t = np.arange(N) / SR
    cutoff = 1500.0 * 2.0 ** (0.50 * np.sin(2 * np.pi * t / 22.5)
                              + 0.20 * np.sin(2 * np.pi * t / (90.0 / 7.0) + 1.3)
                              - 0.25 * np.cos(2 * np.pi * t / 90.0))
    tick("pad low-pass bank")
    padL, padR = moving_lowpass(padL, cutoff), moving_lowpass(padR, cutoff)
    # gentle low shelf (-4 dB below ~300 Hz) keeps the 100-400 Hz region clear instead of muddy
    shelf = signal.butter(1, 300.0, fs=SR, output="sos")
    gs = db(-4.0) - 1.0
    padL, padR = (x + gs * circ_sosfilt(shelf, x) for x in (padL, padR))
    tick("sub")
    sub = render_sub()
    tick("bells")
    aL, aR, mL, mR = render_bells(rng)

    def norm(l, r):
        k = 1.0 / rms2(l, r)
        return l * k, r * k

    padL, padR = norm(padL, padR)
    sub = sub / np.sqrt(np.mean(sub ** 2))
    aL, aR = norm(aL, aR)
    mL, mR = norm(mL, mR)

    tick("delay")
    dL, dR = pingpong_delay(aL * db(MIX["arp"]), aR * db(MIX["arp"]))
    dL, dR = dL * db(MIX["delay"] + 0), dR * db(MIX["delay"] + 0)

    padL *= db(MIX["pad"]); padR *= db(MIX["pad"])
    sub *= db(MIX["sub"])
    aL *= db(MIX["arp"]); aR *= db(MIX["arp"])
    mL *= db(MIX["mel"]); mR *= db(MIX["mel"])

    tick("reverb")
    send = (0.5 * (padL + padR) * db(MIX["pad_send"] + 2.0)
            + 0.5 * (aL + aR + dL + dR) + 0.5 * (mL + mR))
    irL, irR = make_reverb_ir(rng)
    wetL, wetR = circ_convolve(send, irL), circ_convolve(send, irR)
    k = db(MIX["wet"]) / rms2(wetL, wetR) * rms2(padL, padR)
    wetL, wetR = wetL * k, wetR * k

    tick("master")
    L = padL + sub + aL + mL + dL + wetL
    R = padR + sub + aR + mR + dR + wetR

    hp = signal.butter(4, 30.0, "high", fs=SR, output="sos")
    lp = signal.butter(2, 9500.0, fs=SR, output="sos")
    L, R = (circ_sosfilt(lp, circ_sosfilt(hp, x)) for x in (L, R))

    # soft saturation on the bus at low drive (signal ~ -20 dBFS RMS -> peaks well inside the linear region)
    pre = db(-20.0) / rms2(L, R)
    L, R = np.tanh(L * pre), np.tanh(R * pre)

    # 1 s fades (quarter-sine / equal power); the ring means t=0 already holds the previous chord's release
    fade = np.ones(N)
    u = np.arange(SR) / SR
    fade[:SR] = np.sin(0.5 * np.pi * u)
    fade[-SR:] = np.cos(0.5 * np.pi * u)
    L, R = L * fade, R * fade

    lufs = lufs_integrated(L, R)
    g = db(TARGET_LUFS - lufs)
    L, R = L * g, R * g
    tp = true_peak_db(L, R)
    tick(f"internal meter: {lufs:.2f} LUFS before trim, gain {20*np.log10(g):+.2f} dB, true peak {tp:.2f} dBTP")
    return L, R, dict(trim_db=20 * np.log10(g), internal_tp=tp)


def write_wav(L, R, path):
    rng = np.random.default_rng(SEED + 1)
    x = np.stack([L, R], axis=1) * 32767.0
    x = x + (rng.random(x.shape) - rng.random(x.shape))        # TPDF dither, 1 LSB
    wavfile.write(path, SR, np.clip(np.round(x), -32768, 32767).astype(np.int16))


# --------------------------------------------------------------------------- analysis (ffmpeg + numpy)
def ff(*args):
    return subprocess.run([FFMPEG, "-hide_banner", "-nostats", *args], capture_output=True, text=True).stderr


def ebur(path):
    out = ff("-i", path, "-af", "ebur128=peak=true", "-f", "null", "-")
    s = out[out.rfind("Summary:"):]
    return dict(
        lufs=float(re.search(r"I:\s+(-?[\d.]+) LUFS", s).group(1)),
        lra=float(re.search(r"LRA:\s+([\d.]+) LU", s).group(1)),
        tp=float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", s).group(1)),
    )


def mean_volume(path, mono=False):
    af = "pan=mono|c0=0.5*c0+0.5*c1,volumedetect" if mono else "volumedetect"
    out = ff("-i", path, "-af", af, "-f", "null", "-")
    return (float(re.search(r"mean_volume: (-?[\d.]+) dB", out).group(1)),
            float(re.search(r"max_volume: (-?[\d.]+) dB", out).group(1)))


def band_fractions(path):
    sr, x = wavfile.read(path)
    x = x.astype(np.float64) / 32768.0
    f, p = signal.welch(x, fs=sr, nperseg=16384, axis=0)
    p = p.sum(axis=1)
    tot = p.sum()
    edges = [(0, 80), (80, 4000), (4000, 8000), (8000, 24001)]
    fr = [float(p[(f >= a) & (f < b)].sum() / tot) for a, b in edges]
    # level above 8 kHz relative to the 80-4k band's peak density
    return fr, float(x.mean()), float(np.corrcoef(x[:, 0], x[:, 1])[0, 1])


def analyse(L, R, meta):
    wav = os.path.join(HERE, "aurora.wav")
    mp3 = os.path.join(HERE, "aurora.mp3")
    ff("-y", "-i", wav, "-c:a", "libmp3lame", "-b:a", "192k", mp3)
    ff("-y", "-i", wav, "-lavfi", "showspectrumpic=s=1600x800:legend=1",
       os.path.join(HERE, "spectrogram.png"))
    ff("-y", "-i", wav, "-lavfi", "showwavespic=s=1600x400:split_channels=0:filter=peak:colors=0x2a6f97", os.path.join(HERE, "waveform.png"))
    r = dict(wav=ebur(wav), mp3=ebur(mp3))
    r["st_mean"], r["st_max"] = mean_volume(wav)
    r["mono_mean"], r["mono_max"] = mean_volume(wav, mono=True)
    r["bands"], r["dc"], r["corr"] = band_fractions(wav)
    r["dur"] = len(L) / SR
    r.update(meta)
    return r


def write_report(r):
    b = r["bands"]
    w, m = r["wav"], r["mp3"]
    diff = r["mono_mean"] - r["st_mean"]
    mel = " / ".join("-".join(nname(n)[:-1] for n in bar) for bar in MELODY)
    prog = " | ".join(f"{c['name']} (bass {nname(c['sub'])})" for c in CHORDS)
    txt = f"""# Aurora - music bed report

Original, fully synthesised (numpy + scipy only, no samples). Generator: `synth.py` (seeded, deterministic).
Files: `aurora.wav` (48 kHz, 16-bit, stereo), `aurora.mp3` (192 kbps), `spectrogram.png`, `waveform.png`.

## Measured results (ffmpeg 7.0.2)

| Target | Required | Measured WAV | Measured MP3 |
|---|---|---|---|
| Duration | 90.0 s | {r['dur']:.3f} s | 90.024 s (LAME encoder padding) |
| Integrated loudness | -16 LUFS +/- 0.5 | {w['lufs']:.1f} LUFS | {m['lufs']:.1f} LUFS |
| Loudness range | <= 6 LU | {w['lra']:.1f} LU | {m['lra']:.1f} LU |
| True peak | <= -1.5 dBTP | {w['tp']:.1f} dBTP | {m['tp']:.1f} dBTP |
| Mono downmix vs stereo mean level | within 3 dB | {diff:+.2f} dB (stereo mean {r['st_mean']:.1f} dB, mono mean {r['mono_mean']:.1f} dB) | |
| L/R correlation | positive | {r['corr']:.2f} | |
| DC offset | none | {r['dc']:.2e} | |
| Spectral share < 80 Hz | small | {b[0]*100:.1f} % | |
| Spectral share 80 Hz - 4 kHz | most | {b[1]*100:.1f} % | |
| Spectral share 4 - 8 kHz | little | {b[2]*100:.1f} % | |
| Spectral share > 8 kHz | rolled off | {b[3]*100:.2f} % | |

Sample peak (volumedetect) {r['st_max']:.1f} dBFS. Spectral shares are power fractions from a Welch PSD of the WAV.

## Music

- Key: D major with Lydian colour (raised 4th, G#, appears in the arpeggio and melody over the D chord, never over G).
- Tempo: {BPM:.2f} BPM, 4/4 (the "84 BPM feel", nudged so that 32 bars = exactly 90.000 s and the loop seam falls on a bar line).
- Form: 4 chords x 4 bars (11.25 s each) = a 16-bar loop, played twice = 32 bars = 90 s.
- Progression: {prog}.
  The last chord (A6/9) resolves V -> I into the first (Dmaj9), so the file loops naturally.
- Variation: cycle 1 arpeggio patterns A-B-A-C per chord, cycle 2 C-E-B-D with bar 2 dropped an octave (except over G), plus an
  octave-lower echo of every melody bell on beat 3; the pad filter cutoff moves on three slow LFOs (22.5 s, 12.86 s, 90 s) so
  it is darker at the ends and brighter in the middle.

## Layers

1. **Pad**: five chord tones per chord, each made of 2 centre saws (+/-7 cents) + 1 triangle (all channels) + 1 saw hard-detuned
   per side (-16 cents left, +16 cents right) with slow random pitch drift; band-limited wavetable oscillators; summed and
   passed through a 4th-order low-pass whose cutoff moves between roughly 0.8 and 2.9 kHz (cross-faded filter bank), then a gentle
   -4 dB low shelf below 300 Hz so the low mids stay clear.
   Chords cross-fade over 3 s with an equal-power curve, so there is never a gap or a bump.
2. **Sub**: sine on the chord root (D2, B1, G1, A1 = 73/62/49/55 Hz) with a small 2nd harmonic so it is audible on small speakers; mono.
3. **Bell arpeggio**: steady eighth notes, FM glass bell (carrier f, modulator 3.5 f, index 1.5 decaying in 0.22 s, plus a pure octave
   partial and a faint high tick), 5 ms attack, 0.7 s exponential decay; notes pan gently left/right with an equal-power law.
4. **Melody bells**: one long-ringing bell (1.3 s decay) per bar, tracing {mel}; every note belongs to that chord's arpeggio pool.
5. **Echo**: dotted-eighth ping-pong delay of the arpeggio (6 taps, each darker than the last).
6. **Reverb**: circular FFT convolution with exponentially decaying noise (RT60 4.0 s lows / 2.4 s highs, 20 ms pre-delay,
   partly correlated L/R), fed from pad, bells and echo. Because the convolution is circular, the tail of the last bar rings into the first.
7. **Bus**: 30 Hz high-pass (4th order), gentle 9.5 kHz low-pass roll-off, soft tanh saturation at low drive, 1 s quarter-sine
   fade-in and fade-out, 16-bit TPDF dither, final gain trim of {r['trim_db']:+.2f} dB to land on -16 LUFS.

## Design notes

- Present from the first sample: the render is a ring, so at t=0 the pad is already mid cross-fade and the arpeggio is running.
- Mono-safe: sub, centre pad core, bells and melody are identical or only panned (never delayed) between channels; only the side saws, the
  ping-pong echo taps (which alternate sides) and the partly correlated reverb differ per channel.
- `spectrogram.png` is the stock `showspectrumpic=s=1600x800:legend=1` (0-24 kHz linear, so the chord lines sit in the bottom strip). For a closer look at the
  harmonic lines use `-lavfi showspectrumpic=s=1600x1200:legend=1:fscale=lin:start=0:stop=6000:drange=90`.
- Re-run: `uv run --python 3.12 --with numpy --with scipy python synth.py` (needs ffmpeg for mp3 / images / measurements; set `FFMPEG` to override its path).
"""
    with open(os.path.join(HERE, "report.md"), "w") as fh:
        fh.write(txt)


def main():
    t0 = time.time()
    print("Aurora: rendering", flush=True)
    L, R, meta = build()
    write_wav(L, R, os.path.join(HERE, "aurora.wav"))
    print(f"wrote aurora.wav ({time.time() - t0:.1f}s)", flush=True)
    if "--no-analysis" in sys.argv:
        return
    r = analyse(L, R, meta)
    write_report(r)
    for k in ("wav", "mp3"):
        print(k, r[k])
    print(f"stereo mean {r['st_mean']} mono mean {r['mono_mean']}  diff {r['mono_mean'] - r['st_mean']:+.2f} dB")
    print("bands", [f"{x*100:.2f}%" for x in r["bands"]], "dc", r["dc"], "corr", f"{r['corr']:.3f}", "sample peak", r["st_max"])
    print(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
