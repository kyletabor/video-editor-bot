#!/usr/bin/env python3
"""
SIGNAL -- a 90 s looping music bed for "Art of Intelligence" title cards.

Pure numpy + scipy.  Every sound is synthesised here; the file is deterministic
(one seeded Generator drives every random choice, including the reverb noise).

Design in one paragraph
-----------------------
96 BPM (so 90.0 s is exactly 36 bars and the loop seam falls on a bar line).
Two plucked patterns of different lengths phase against each other on an
eighth-note grid: pattern A (marimba-like, 9 steps, slightly left) and pattern
B (FM glass pluck, 8 steps, a sixteenth later, slightly right).  Under them: a
warm sine sub on chord roots, a very soft muted low pluck pulse, and an airy
detuned-saw pad.  Dotted-eighth delay + convolution reverb on sends.
Everything is rendered CIRCULARLY (notes, pad crossfades, delay taps, FFT
reverb all wrap around the 90 s buffer), so the bed is already "running" at
t=0 and the end flows straight back into the start; only the 1 s fade-in and
1 s fade-out requested for the cut-in/loop are non-circular.

Run:  uv run --python 3.12 --with numpy --with scipy python synth.py
"""
import os
import sys
import json
import shutil
import subprocess
import numpy as np
import scipy.signal as sps
import scipy.fft as sfft
from scipy.io import wavfile

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SR = 48000
DUR = 90.0
N = int(SR * DUR)                    # 4,320,000 samples
BPM = 96
BEAT = SR * 60 // BPM                # 30000 samples
EIGHTH = BEAT // 2                   # 15000
SIXTEENTH = BEAT // 4                # 7500
BAR = 4 * BEAT                       # 120000
NBARS = N // BAR                     # 36
assert N == NBARS * BAR
SEED = 20261001
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.environ.get(
    "FFMPEG",
    "/tmp/claude-1000/-home-orangepi-projects/cf151889-93dd-4cf8-81d0-beaf400298fe/"
    "scratchpad/render-main/.tools/ffmpeg/ffmpeg")

TARGET_LUFS = -16.0
TP_CEIL_DB = -2.0                    # WAV true-peak ceiling (MP3 encode adds overshoot)

rng = np.random.default_rng(SEED)    # the ONLY source of randomness


def hz(m):
    return 440.0 * 2.0 ** ((np.asarray(m, dtype=float) - 69.0) / 12.0)


def db(x):
    return 10.0 ** (x / 20.0)


# ----------------------------------------------------------------------------
# Harmony.  36 bars: Am9 | Fmaj9 | Cmaj9 | G6 | (repeat) | Fmaj9 (2) + G6 (2)
# The last 4 bars (F -> G) lift straight back into Am9 at the loop seam.
# ----------------------------------------------------------------------------
CHORDS = {
    #          sub root  pitch classes (for plucks)   pad voicing (MIDI)
    "Am9":   dict(root=45, pcs=[9, 0, 4, 7, 11], pad=[57, 60, 64, 67, 71]),
    "Fmaj9": dict(root=41, pcs=[5, 9, 0, 4, 7],  pad=[53, 57, 60, 64, 67]),
    "Cmaj9": dict(root=48, pcs=[0, 4, 7, 11, 2], pad=[60, 64, 67, 71, 74]),
    "G6":    dict(root=43, pcs=[7, 11, 2, 4, 9], pad=[55, 59, 62, 64, 69]),
}
SCORE = [("Am9", 4), ("Fmaj9", 4), ("Cmaj9", 4), ("G6", 4),
         ("Am9", 4), ("Fmaj9", 4), ("Cmaj9", 4), ("G6", 4),
         ("Fmaj9", 2), ("G6", 2)]
# blocks where the glass pluck adds the Lydian #11 colour (B natural over F)
LYDIAN_BLOCKS = {5, 8}

blocks = []          # (name, start_sample, end_sample, block_index)
_bar = 0
for _i, (_n, _b) in enumerate(SCORE):
    blocks.append((_n, _bar * BAR, (_bar + _b) * BAR, _i))
    _bar += _b
assert _bar == NBARS


def block_at(sample):
    s = sample % N
    for name, a, b, i in blocks:
        if a <= s < b:
            return name, i
    raise ValueError


def pool(pcs, lo, hi):
    return [m for m in range(lo, hi + 1) if (m % 12) in pcs]


# ----------------------------------------------------------------------------
# Pluck patterns.  Index = position in the current chord's note pool, so the
# same shape follows the harmony.  None = rest.
#   A: 9 steps (4.5 beats)    B: 8 steps (1 bar)     both divide 288 eighths
#   -> they drift one eighth per repeat and realign every 9 bars (72 eighths),
#      and the whole 36-bar loop contains exactly 4 realignments.
# ----------------------------------------------------------------------------
A_BASE = [0, 2, 4, 2, 4, 5, 4, 2, 3]
A_VARS = [{}, {5: 6, 8: 1}, {3: None, 7: 3}, {2: 5, 6: 6, 8: None}]
B_BASE = [5, None, 3, 4, None, 6, 5, 3]
B_VARS = [{}, {7: 6}, {2: 4, 5: 7}, {3: 5, 0: 6}, {6: None, 1: 4}]
A_LEN, B_LEN = len(A_BASE), len(B_BASE)
assert (N // EIGHTH) % A_LEN == 0 and (N // EIGHTH) % B_LEN == 0

ACC8 = [1.00, 0.70, 0.84, 0.70, 0.93, 0.70, 0.84, 0.70]      # metric accent (per eighth in bar)


def pattern_note(base, vars_, step, rep):
    v = vars_[rep % len(vars_)]
    return v[step] if step in v else base[step]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def pan_gains(p):
    th = (p + 1.0) * np.pi / 4.0
    return float(np.cos(th)), float(np.sin(th))


def add_circ(buf, sig, start, gl=1.0, gr=1.0):
    """Add mono sig into stereo buf (2,N) at sample `start`, wrapping around."""
    n = len(sig)
    start = int(start) % N
    end = start + n
    if end <= N:
        buf[0, start:end] += gl * sig
        buf[1, start:end] += gr * sig
    else:
        k = N - start
        buf[0, start:] += gl * sig[:k]
        buf[1, start:] += gr * sig[:k]
        buf[0, :n - k] += gl * sig[k:]
        buf[1, :n - k] += gr * sig[k:]


def attack_ramp(n, ms):
    a = max(2, int(SR * ms / 1000))
    r = np.ones(n)
    r[:a] = 0.5 * (1 - np.cos(np.pi * np.arange(a) / a))
    return r


def tail_fade(n, ms):
    a = int(SR * ms / 1000)
    r = np.ones(n)
    r[-a:] = 0.5 * (1 + np.cos(np.pi * np.arange(a) / a))
    return r


def circ_sos(sos, x, pad=2 * SR):
    """Apply an IIR (SOS) circularly: wrap-pad, filter, drop the settle region."""
    xx = np.concatenate([x[..., -pad:], x], axis=-1)
    return sps.sosfilt(sos, xx, axis=-1)[..., pad:]


def circ_conv(x, h):
    return sfft.irfft(sfft.rfft(x, N) * sfft.rfft(h, N), N)


def rms_db(x):
    return 10 * np.log10(np.mean(x.astype(np.float64) ** 2) + 1e-20)


# ----------------------------------------------------------------------------
# Instruments
# ----------------------------------------------------------------------------
def marimba(f, vel, dur=1.5):
    """Additive marimba-like bar: partials at 1 : 4 : ~10, short exponential decays."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    kt = (f / 440.0) ** -0.25                       # higher notes ring a little shorter
    out = np.zeros(n)
    bright = 0.35 + 0.65 * vel
    for ratio, amp, tau, ph in ((1.0, 1.00, 0.40, 0.0),
                                (3.99, 0.38 * bright, 0.12, 0.6),
                                (9.90, 0.10 * bright, 0.04, 1.3)):
        fr = f * ratio
        if fr > 9000:
            continue
        out += amp * np.sin(2 * np.pi * fr * t + ph) * np.exp(-t / (tau * kt))
    return vel * out * attack_ramp(n, 3.0) * tail_fade(n, 150)


def glass(f, vel, dur=1.6):
    """FM pluck, carrier:modulator 1:3, index decays fast -> bright blip into a soft sine."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    key = np.clip((700.0 / f) ** 0.5, 0.45, 1.0)
    idx = (1.0 + 1.7 * vel) * key * np.exp(-t / 0.075)
    mod = np.sin(2 * np.pi * 3.0 * f * t)
    car = np.sin(2 * np.pi * f * t + idx * mod)
    # a faint octave shimmer gives the "glass"
    car += 0.12 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t / 0.18)
    return vel * car * np.exp(-t / 0.40) * attack_ramp(n, 2.5) * tail_fade(n, 150)


def muted_low(f, vel, dur=0.7):
    """Muted low pluck: fundamental + soft 2nd/3rd, very short decay."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = (np.sin(2 * np.pi * f * t) * np.exp(-t / 0.16)
           + 0.30 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t / 0.08)
           + 0.08 * np.sin(2 * np.pi * 3 * f * t) * np.exp(-t / 0.04))
    return vel * out * attack_ramp(n, 4.0) * tail_fade(n, 60)


_SAWTAB = {}


def saw_table(f0, size=4096, maxf=6000.0):
    h = max(1, int(maxf / f0))
    if h not in _SAWTAB:
        x = np.arange(size) / size
        tab = np.zeros(size)
        for k in range(1, h + 1):
            tab += np.sin(2 * np.pi * k * x) / k
        _SAWTAB[h] = tab * (2 / np.pi)
    return _SAWTAB[h]


def saw_osc(f0, cents, phase0, lfo_cents, lfo_hz, lfo_ph, n):
    """Band-limited saw by wavetable lookup, detuned with slow drift."""
    t = np.arange(n) / SR
    cc = cents + lfo_cents * np.sin(2 * np.pi * lfo_hz * t + lfo_ph)
    inc = f0 * 2.0 ** (cc / 1200.0) / SR
    ph = phase0 + np.cumsum(inc)
    tab = saw_table(f0)
    size = len(tab)
    pos = (ph % 1.0) * size
    i0 = pos.astype(np.int64)
    fr = pos - i0
    return tab[i0 % size] * (1 - fr) + tab[(i0 + 1) % size] * fr


# ----------------------------------------------------------------------------
# Render the stems (all circular)
# ----------------------------------------------------------------------------
def render_plucks():
    A = np.zeros((2, N)); B = np.zeros((2, N)); P = np.zeros((2, N))
    gA = pan_gains(-0.26)
    gB = pan_gains(+0.26)
    n_steps = N // EIGHTH
    for k in range(n_steps):
        t0 = k * EIGHTH
        name, bi = block_at(t0)
        # --- pattern A, on the eighth grid ---
        step, rep = k % A_LEN, k // A_LEN
        idx = pattern_note(A_BASE, A_VARS, step, rep)
        if idx is not None:
            pl = pool(CHORDS[name]["pcs"], 57, 79)
            m = pl[min(max(idx, 0), len(pl) - 1)]
            vel = ACC8[k % 8] * (1 + rng.uniform(-0.12, 0.12))
            jit = int(rng.uniform(-1.5, 1.5) * SR / 1000)
            add_circ(A, marimba(hz(m), vel), t0 + jit, *gA)
        # --- pattern B, a sixteenth later (interlocks between A's notes) ---
        tb = t0 + SIXTEENTH
        nameb, bib = block_at(tb)
        stepb, repb = k % B_LEN, k // B_LEN
        idxb = pattern_note(B_BASE, B_VARS, stepb, repb)
        if idxb is not None:
            pcs = list(CHORDS[nameb]["pcs"])
            if bib in LYDIAN_BLOCKS:
                pcs = pcs + [11]
            pl = pool(pcs, 72, 93)
            m = pl[min(max(idxb, 0), len(pl) - 1)]
            vel = (0.55 + 0.45 * ACC8[(k + 1) % 8]) * (1 + rng.uniform(-0.12, 0.12))
            jit = int(rng.uniform(-1.5, 1.5) * SR / 1000)
            add_circ(B, glass(hz(m), vel), tb + jit, *gB)
    # --- soft muted low pluck pulse on the beats (root, one octave over the sub) ---
    ACC4 = [1.0, 0.55, 0.80, 0.55]
    for b in range(N // BEAT):
        t0 = b * BEAT
        name, _ = block_at(t0)
        vel = ACC4[b % 4] * (1 + rng.uniform(-0.08, 0.08))
        add_circ(P, muted_low(hz(CHORDS[name]["root"] + 12), vel), t0, 1.0, 1.0)
    return A, B, P


def render_sub():
    """Warm sine sub on the chord roots; one continuous sine per block, gentle
    re-swell on each bar line, 120 ms equal-power crossfades between roots."""
    S = np.zeros(N)
    xf = int(0.12 * SR)
    bar_t = (np.arange(N) % BAR) / SR
    swell = 0.5 * (1 - np.cos(np.pi * np.clip(bar_t / 0.05, 0, 1)))
    swell = np.where(bar_t < 0.05, swell, np.exp(-(bar_t - 0.05) / 0.55))
    env = 0.78 + 0.22 * swell
    for name, a, b, bi in blocks:
        f = float(hz(CHORDS[name]["root"]))
        lo, hi = a - xf // 2, b + xf // 2
        t = (np.arange(lo, hi) - a) / SR
        sig = (np.sin(2 * np.pi * f * t)
               + 0.30 * np.sin(2 * np.pi * 2 * f * t + 0.4)
               + 0.30 * np.sin(2 * np.pi * 0.5 * f * t))
        w = np.ones(hi - lo)
        u = np.linspace(0, 1, xf, endpoint=False)
        w[:xf] = np.sin(0.5 * np.pi * u)
        w[-xf:] = np.cos(0.5 * np.pi * u)
        idx = np.arange(lo, hi) % N
        S[idx] += sig * w * env[idx]
    return S


def render_pad():
    """Airy pad: 3 detuned band-limited saws per note + faint octave sines,
    low-passed (cutoff breathes slowly), 1.6 s equal-power crossfades."""
    PL = np.zeros((2, N)); PR = np.zeros((2, N))
    ov = int(1.6 * SR)
    sos_lo = sps.butter(2, 550, fs=SR, output="sos")
    sos_hi = sps.butter(2, 1700, fs=SR, output="sos")
    sos_hp = sps.butter(2, 180, "highpass", fs=SR, output="sos")
    voice_pan = [-0.38, 0.0, 0.38]
    voice_cents = [-9.0, 0.0, 8.0]
    for name, a, b, bi in blocks:
        lo, hi = a - ov // 2, b + ov // 2
        n = hi - lo
        notes = CHORDS[name]["pad"]
        L = np.zeros(n); R = np.zeros(n)
        for ni, m in enumerate(notes):
            f0 = float(hz(m))
            for vi in range(3):
                osc = saw_osc(f0, voice_cents[vi] + rng.uniform(-1.5, 1.5), rng.uniform(0, 1),
                              3.0, rng.choice([1, 2, 3, 4, 5]) / DUR * 4, rng.uniform(0, 6.28), n)
                gl, gr = pan_gains(voice_pan[vi])
                amp = 0.55 if vi == 1 else 0.40
                L += amp * gl * osc
                R += amp * gr * osc
        # faint octave-up sines on the top two notes = "air"
        t = (np.arange(lo, hi)) / SR
        for m in notes[-2:]:
            f = float(hz(m + 12))
            ph = rng.uniform(0, 6.28)
            vib = 1 + 0.0012 * np.sin(2 * np.pi * (3 / DUR) * t + ph)
            s = 0.30 * np.sin(2 * np.pi * f * vib * t + ph)
            L += 0.75 * s
            R += 0.75 * s
        # slow breathing filter: crossfade between dark and brighter lowpass
        tt = (np.arange(lo, hi) % N) / SR
        m_lfo = 0.5 + 0.5 * np.sin(2 * np.pi * (4 / DUR) * tt + 0.7)
        L = sps.sosfilt(sos_lo, L) * (1 - m_lfo) + sps.sosfilt(sos_hi, L) * m_lfo
        R = sps.sosfilt(sos_lo, R) * (1 - m_lfo) + sps.sosfilt(sos_hi, R) * m_lfo
        L = sps.sosfilt(sos_hp, L)               # keep the pad out of the sub / low-mud range
        R = sps.sosfilt(sos_hp, R)
        w = np.ones(n)
        u = np.linspace(0, 1, ov, endpoint=False)
        w[:ov] = np.sin(0.5 * np.pi * u)
        w[-ov:] = np.cos(0.5 * np.pi * u)
        idx = np.arange(lo, hi) % N
        PL[0, idx] += L * w
        PR[0, idx] += R * w
    return np.stack([PL[0], PR[0]])


# ----------------------------------------------------------------------------
# Effects
# ----------------------------------------------------------------------------
def make_reverb_ir():
    """Stereo impulse response: seeded noise, exponentially decaying, with
    frequency-dependent RT60 (low 3.4 s / mid 2.5 s / high 1.2 s), 14 ms pre-delay."""
    n = int(3.8 * SR)
    t = np.arange(n) / SR
    bands = [(sps.butter(2, 350, "lowpass", fs=SR, output="sos"), 3.4),
             (sps.butter(2, [350, 3000], "bandpass", fs=SR, output="sos"), 2.5),
             (sps.butter(2, 3000, "highpass", fs=SR, output="sos"), 1.2)]
    irs = []
    for ch in range(2):
        h = np.zeros(n)
        for sos, rt in bands:
            h += sps.sosfilt(sos, rng.standard_normal(n)) * np.exp(-6.9078 * t / rt)
        h = sps.sosfilt(sps.butter(2, 7500, "lowpass", fs=SR, output="sos"), h)
        h *= np.clip(t / 0.012, 0, 1) ** 2             # soft onset
        h = np.concatenate([np.zeros(int(0.014 * SR)), h])[:n]
        h *= tail_fade(n, 300)
        h /= np.sqrt(np.sum(h ** 2))
        irs.append(h)
    return irs


def delay_bus(send, fb=0.30, taps=8):
    """Dotted-eighth (3 sixteenths) delay, circular, low feedback, darker each
    repeat, gentle ping-pong (channels swap on each repeat)."""
    D = 3 * SIXTEENTH
    sos_lp = sps.butter(2, 3800, "lowpass", fs=SR, output="sos")
    sos_hp = sps.butter(2, 220, "highpass", fs=SR, output="sos")
    cur = circ_sos(sos_hp, send)
    out = np.zeros_like(send)
    g = 1.0
    for k in range(taps):
        cur = np.roll(circ_sos(sos_lp, cur), D, axis=-1)[::-1]
        out += g * cur
        g *= fb
    return out


# ----------------------------------------------------------------------------
# Loudness (ITU-R BS.1770 / EBU R128), true peak
# ----------------------------------------------------------------------------
_K1 = ([1.53512485958697, -2.69169618940638, 1.19839281085285],
       [1.0, -1.69065929318241, 0.73248077421585])
_K2 = ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])


def integrated_lufs(x):
    y = sps.lfilter(*_K1, x, axis=-1)
    y = sps.lfilter(*_K2, y, axis=-1)
    p = np.sum(y.astype(np.float64) ** 2, axis=0)
    cs = np.concatenate([[0.0], np.cumsum(p)])
    bl, hop = int(0.4 * SR), int(0.1 * SR)
    starts = np.arange(0, len(p) - bl + 1, hop)
    z = (cs[starts + bl] - cs[starts]) / bl
    lk = -0.691 + 10 * np.log10(z + 1e-20)
    keep = lk > -70
    gr = -0.691 + 10 * np.log10(np.mean(z[keep])) - 10
    keep &= lk > gr
    return -0.691 + 10 * np.log10(np.mean(z[keep]))


def true_peak_db(x):
    up = sps.resample_poly(x, 4, 1, axis=-1)
    return 20 * np.log10(np.max(np.abs(up)) + 1e-12)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def build_mix():
    print("rendering plucks ...", flush=True)
    A, B, P = render_plucks()
    print("rendering sub ...", flush=True)
    sub = render_sub()
    print("rendering pad ...", flush=True)
    pad = render_pad()

    # ---- stem balance (RMS dBFS per stem, pre-master) ----
    def level(x, target):
        return x * db(target - rms_db(x))
    A = level(A, -21.5)
    B = level(B, -25.0)
    P = level(P, -38.0)
    pad = level(pad, -27.5)
    sub = np.stack([level(sub, -30.5)] * 2)

    plucks = A + B
    # ---- sends ----
    print("delay ...", flush=True)
    dly = delay_bus(plucks * 1.0)
    dly = db(rms_db(plucks) - 11.0 - rms_db(dly)) * dly

    print("reverb ...", flush=True)
    irL, irR = make_reverb_ir()
    rev_in = 0.5 * (plucks[0] + plucks[1]) * 0.9 + 0.5 * (pad[0] + pad[1]) * 0.55 \
        + 0.5 * (P[0] + P[1]) * 0.4
    rev_in = circ_sos(sps.butter(2, 140, "highpass", fs=SR, output="sos"), rev_in)
    wet = np.stack([circ_conv(rev_in, irL), circ_conv(rev_in, irR)])
    dry = A + B + P + sub + pad
    wet = db(rms_db(dry) - 11.0 - rms_db(wet)) * wet

    mix = dry + dly + wet

    return mix


def master(mix):
    """Master bus: 30 Hz HPF, gentle air roll-off, soft tanh saturation (low drive)."""
    print("master ...", flush=True)
    mix = circ_sos(sps.butter(4, 30, "highpass", fs=SR, output="sos"), mix)
    mix = circ_sos(sps.butter(2, 9500, "lowpass", fs=SR, output="sos"), mix)
    drive = 1.25
    pk = np.max(np.abs(mix))
    x = mix / pk * 0.55                                    # fixed working level
    x = np.tanh(drive * x) / np.tanh(drive * 0.55) * 0.55  # peaks nudged, quiet parts +1.2 dB
    return x


def finalize(x):
    """1 s fade-in / 1 s fade-out (the only non-circular step), then ONE linear
    gain to -16 LUFS integrated; true-peak ceiling enforced."""
    x = x.copy()
    fl = SR
    ramp = 0.5 * (1 - np.cos(np.pi * np.arange(fl) / fl))
    x[:, :fl] *= ramp
    x[:, -fl:] *= ramp[::-1]
    gain = db(TARGET_LUFS - integrated_lufs(x))
    y = x * gain
    tp = true_peak_db(y)
    if tp > TP_CEIL_DB:
        print(f"WARNING: true peak {tp:.2f} dBTP above ceiling; lowering gain (LUFS will undershoot)")
        y *= db(TP_CEIL_DB - tp)
    print(f"final: {integrated_lufs(y):.2f} LUFS, TP {true_peak_db(y):.2f} dBTP", flush=True)
    return y


MP3_GAIN_DB = 0.3      # LAME lowers integrated loudness ~0.3 LU; compensate so the MP3 also lands on -16


def self_check(y):
    """Quick in-script numbers (ffmpeg ebur128/volumedetect are the reference)."""
    mono = 0.5 * (y[0] + y[1])
    st_rms = rms_db(y)
    mono_rms = rms_db(mono)
    f, p = sps.welch(mono, SR, nperseg=16384)
    df = f[1] - f[0]
    tot = p.sum() * df
    fr = lambda a, b: 100 * p[(f >= a) & (f < b)].sum() * df / tot
    print(f"  mono vs stereo mean level : {mono_rms - st_rms:+.2f} dB")
    print(f"  DC offset L/R             : {y[0].mean():.1e} {y[1].mean():.1e}")
    print("  energy <80 / 80-4k / 4-8k / >8k Hz : %.1f / %.1f / %.1f / %.2f %%"
          % (fr(0, 80), fr(80, 4000), fr(4000, 8000), fr(8000, 24000)))


def ffmpeg(*args):
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def write_outputs(y):
    r = np.random.default_rng(SEED + 1)
    tpdf = (r.random(y.shape) - r.random(y.shape)) / 32768.0       # +-1 LSB TPDF dither
    q = np.clip(np.round((y + tpdf) * 32767.0), -32768, 32767).astype(np.int16)
    wav = os.path.join(OUT_DIR, "signal.wav")
    wavfile.write(wav, SR, q.T)                                     # 48 kHz, 16-bit, stereo
    if not os.path.exists(FFMPEG):
        print("ffmpeg not found; skipping mp3 / images", file=sys.stderr)
        return wav
    ffmpeg("-i", wav, "-af", f"volume={MP3_GAIN_DB}dB", "-codec:a", "libmp3lame", "-b:a", "192k",
           os.path.join(OUT_DIR, "signal.mp3"))
    # full-length spectrogram exactly as specified in the brief (linear 0-24 kHz axis) ...
    ffmpeg("-i", wav, "-lavfi", "showspectrumpic=s=1600x800:legend=1",
           os.path.join(OUT_DIR, "spectrogram.png"))
    # ... plus a 10 s log-frequency zoom where the note-onset grid is readable
    ffmpeg("-ss", "20", "-t", "10", "-i", wav, "-lavfi",
           "showspectrumpic=s=1600x800:legend=1:fscale=log:start=40:stop=12000:drange=80",
           os.path.join(OUT_DIR, "spectrogram_10s.png"))
    ffmpeg("-i", wav, "-lavfi", "showwavespic=s=1600x400:filter=peak",
           os.path.join(OUT_DIR, "waveform.png"))
    return wav


if __name__ == "__main__":
    mix = build_mix()
    y = finalize(master(mix))
    self_check(y)
    write_outputs(y)
    print("done", flush=True)
