#!/usr/bin/env python3
"""Stank: the Pipeline theme, a funk organ groove synthesized from scratch (numpy + scipy,
seeded, no samples), and the cue blocks the bot scores a reel from.

    uv run --python 3.12 --with numpy --with scipy python assets/styles/pipeline/music/synth.py --out DIR

writes DIR/theme.wav (the whole tune, 63 s) and DIR/blocks/*.wav:

    pickup    half a bar: snare fill, bass walk-up and an organ smear into a downbeat
    riff-1..4 the four bars of the vamp, Gm7 | C7 | Gm7 | D7#9, each leading to the next
    sting-1,3 bars 1 and 3 again with the bass turned home, for when G follows instead
    hold      a band hit on Gm7 with the chord held on the Leslie (the bot trims and fades it)
    outro     the three hits, the fall, and the final G7#9 spinning down

Every block starts exactly on its first beat and carries its own ring-out past its nominal
length, so blocks overlap-add into a seamless cue (bot/clipbot/score.py, docs/styles.md).
The full tune fixes the mix gains (`calibrate`), and every block goes through that same chain,
so a cue assembled from blocks sits at one level.

Band: swung drum kit (kick, snare with ghost notes, hats, tambourine, crash), a plucked
electric-bass voice, and a tonewheel organ (drawbar additive synthesis, key click, percussion)
through tube-style overdrive and a two-rotor Leslie with speed changes.

Form of the tune at 104 BPM, G dorian: pickup | A (8 bars) | B (8 bars) | A (8 bars) | outro.
"""
import argparse
import zlib
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

SR = 48000
BPM = 104
S16 = 60 / BPM / 4
BAR = 16 * S16
SWING = 0.17  # odd sixteenths arrive this fraction of a sixteenth late
TAIL = 2.4  # seconds of ring-out rendered after a block's nominal end
BLOCK_TRIM_DB = -3.5  # blocks sit this far under the tune so overlapping tails cannot clip
RNG = np.random.default_rng(1971)  # the drum sounds, the tune's performance and the room


# ---------- helpers ----------
def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def tt(seconds):
    return np.arange(max(1, int(seconds * SR))) / SR


def put(buf, t, sig, gain=1.0):
    i = int(round(t * SR))
    if i >= len(buf) or i < 0:
        return
    j = min(len(buf), i + len(sig))
    buf[i:j] += gain * sig[: j - i]


def filt(x, kind, freq, order=2):
    sos = signal.butter(order, freq, btype=kind, fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def peak(x, level=1.0):
    return x * (level / max(1e-9, np.abs(x).max()))


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


# ---------- drum one-shots ----------
def make_kick():
    t = tt(0.2)
    f = 50 + 120 * np.exp(-t * 42)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 19)
    click = filt(RNG.standard_normal(len(t)), "high", 2500) * np.exp(-t * 480) * 0.22
    return peak(np.tanh(1.4 * (body + click)))


def make_snare():
    t = tt(0.28)
    noise = filt(RNG.standard_normal(len(t)), "band", [1300, 7800]) * np.exp(-t * 23)
    tone = np.sin(2 * np.pi * 192 * t) * np.exp(-t * 33) * 0.75 + np.sin(2 * np.pi * 338 * t) * np.exp(-t * 46) * 0.35
    return peak(np.tanh(1.5 * (0.95 * noise + tone)))


def make_hat(decay, seconds):
    t = tt(seconds)
    return peak(filt(RNG.standard_normal(len(t)), "high", 7200, 4) * np.exp(-t * decay))


def make_tamb():
    t = tt(0.13)
    jingle = 1 + 0.6 * np.sin(2 * np.pi * 58 * t)
    return peak(filt(RNG.standard_normal(len(t)), "band", [4800, 11500]) * np.exp(-t * 38) * jingle)


def make_crash():
    t = tt(1.9)
    return peak(filt(RNG.standard_normal(len(t)), "high", 3600, 2) * np.exp(-t * 2.9))


KICK, SNARE, HAT, OPEN_HAT, TAMB, CRASH = make_kick(), make_snare(), make_hat(85, 0.07), make_hat(11, 0.34), make_tamb(), make_crash()


def bass_note(m, steps, vel):
    t = tt(steps * S16 + 0.03)
    f = hz(m)
    sig = 0.4 * np.sin(2 * np.pi * f * t)
    for k in range(1, 16):
        if f * k > 4500:
            break
        sig += np.sin(2 * np.pi * f * k * t) / k * np.exp(-t * (1.0 + 1.1 * k))  # bright partials die first: a pluck
    env = np.minimum(1, t / 0.004) * np.minimum(1, (t[-1] - t) / 0.028)
    return np.tanh(2.2 * sig * env) * vel


RATIOS = [0.5, 1.5, 1, 2, 3, 4, 5, 6, 8]  # 16', 5 1/3', 8', 4', 2 2/3', 2', 1 3/5', 1 1/3', 1'


def drawbars(setting):
    return [0.0 if d == 0 else 10 ** ((d - 8) * 3 / 20) for d in setting]


LEAD = drawbars([8, 8, 8, 6, 0, 0, 0, 2, 4])
COMP = drawbars([0, 0, 8, 7, 5, 0, 0, 0, 3])
FULL = drawbars([8, 6, 8, 8, 5, 4, 0, 3, 5])

GM7, C7, D79, BB7, EB7 = "Gm7", "C7", "D7#9", "Bb7", "Eb7"
ROOT = {GM7: 31, C7: 36, D79: 38, BB7: 34, EB7: 39}
LH = {GM7: [53, 58, 62], C7: [52, 58, 62], D79: [54, 60, 65], BB7: [56, 62, 65], EB7: [55, 61, 65]}
RH = {GM7: [70, 74, 77], C7: [70, 76, 79], D79: [66, 72, 77], BB7: [68, 74, 77], EB7: [67, 73, 77]}
FINAL_CHORD = [55, 62, 65, 71, 74, 82]  # G7#9
FILLS = {
    BB7: [(8, 77, None), (9, 80, None), (10, 82, None), (12, 80, None), (13, 77, None), (14, 74, 73)],
    C7: [(8, 79, None), (9, 82, None), (10, 84, None), (12, 82, None), (13, 79, None), (14, 76, 75)],
    EB7: [(8, 82, None), (9, 85, None), (10, 87, None), (12, 85, None), (13, 82, None), (14, 79, 78)],
}


class Take:
    """One performance: the band's buses for `bars` bars plus ring-out, and its own randomness."""

    def __init__(self, bars, rng):
        self.rng = rng
        self.n = int((bars * BAR + TAIL) * SR)
        self.kick, self.snare, self.hat, self.tamb, self.crash = (np.zeros(self.n) for _ in range(5))
        self.bass_bus, self.organ_bus = np.zeros(self.n), np.zeros(self.n)
        self.fast_spans = []

    # ----- time and feel -----
    def when(self, bar, step, human=0.0):
        t = (bar * 16 + step) * S16
        if int(np.floor(step)) % 2 == 1:
            t += SWING * S16
        if human:
            t += self.rng.normal(0, human)
        return max(t, 0.0)

    def v(self, x, spread=0.08):
        return x * (1 + self.rng.uniform(-spread, spread))

    def fast(self, bar, a, b):
        self.fast_spans.append((self.when(bar, a), self.when(bar, b)))

    # ----- voices -----
    def organ_note(self, m, seconds, vel, reg, perc=0.0):
        t = tt(seconds + 0.02)
        f = hz(m)
        sig = np.zeros(len(t))
        for ratio, amp in zip(RATIOS, reg):
            if amp == 0:
                continue
            fr = f * ratio
            while fr > 6000:  # tonewheel foldback
                fr /= 2
            sig += amp * np.sin(2 * np.pi * fr * t + self.rng.uniform(0, 2 * np.pi))
        if perc:
            sig += perc * np.sin(2 * np.pi * f * 3 * t) * np.exp(-t * 15)
        sig *= np.minimum(1, t / 0.004) * np.minimum(1, (t[-1] - t) / 0.014)
        n = int(0.004 * SR)
        sig[:n] += 0.5 * self.rng.standard_normal(n) * np.linspace(1, 0, n)  # key click
        return sig * vel * 0.2

    def lead(self, bar, step, m, steps, vel=1.0, grace=None, perc=0.55):
        t = self.when(bar, step, 0.003)
        if grace is not None:
            put(self.organ_bus, max(0, t - 0.045), self.organ_note(grace, 0.04, 0.7 * vel, LEAD))
        put(self.organ_bus, t, self.organ_note(m, steps * S16, self.v(vel), LEAD, perc))

    def chord(self, bar, step, notes, steps, vel=0.8, reg=COMP):
        t = self.when(bar, step, 0.003)
        for m in notes:
            put(self.organ_bus, t + self.rng.uniform(0, 0.006), self.organ_note(m, steps * S16, self.v(vel), reg))

    def gliss(self, bar, step, lo, hi, vel=0.4):
        """Palm smear: a fast chromatic run that lands on the next downbeat."""
        notes = list(range(lo, hi)) if hi > lo else list(range(lo, hi, -1))
        t0 = self.when(bar, step)
        span = self.when(bar + 1, 0) - t0 if step >= 12 else 2.2 * S16
        for i, m in enumerate(notes):
            note = self.organ_note(m, span / len(notes) + 0.01, vel * (0.6 + 0.4 * i / len(notes)), LEAD)
            put(self.organ_bus, t0 + i * span / len(notes), note)

    def bass(self, bar, step, m, steps, vel=0.9):
        put(self.bass_bus, self.when(bar, step, 0.0025), bass_note(m, steps, self.v(vel)))

    # ----- parts -----
    def drum_bar(self, bar, fill=False, tamb=False, parity=None):
        odd = (bar % 2 == 1) if parity is None else bool(parity)
        kicks = {0: 1.0, 3: 0.72, 7: 0.85, 10: 0.95}
        if not odd:
            kicks[13] = 0.7
        for step in range(16):
            if fill and step >= 12:
                put(self.snare, self.when(bar, step), SNARE, self.v(0.55 + 0.15 * (step - 12)))
                if step in (12, 14):
                    put(self.kick, self.when(bar, step), KICK, 0.8)
                continue
            if step in kicks:
                put(self.kick, self.when(bar, step, 0.002), KICK, self.v(kicks[step], 0.05))
            if step in (4, 12):
                put(self.snare, self.when(bar, step, 0.002), SNARE, self.v(1.0, 0.05))
            if step in (6, 9, 15):
                put(self.snare, self.when(bar, step, 0.004), SNARE, self.v(0.2, 0.25))  # ghost notes
            accent = [0.85, 0.3, 0.6, 0.38][step % 4]
            if step == 14 and odd:
                put(self.hat, self.when(bar, step, 0.003), OPEN_HAT, self.v(0.7))
            else:
                put(self.hat, self.when(bar, step, 0.003), HAT, self.v(accent, 0.15))
            if tamb:
                level = [0.45, 0.2, 0.32, 0.2][step % 4] * (1.7 if step in (4, 12) else 1)
                put(self.tamb, self.when(bar, step, 0.004), TAMB, self.v(level, 0.15))

    def pickup_drums(self, bar):
        for step, vel in [(8, 0.5), (10, 0.6), (12, 0.7), (13, 0.5), (14, 0.85), (15, 0.95)]:
            put(self.snare, self.when(bar, step), SNARE, vel)
        for step in (8, 12):
            put(self.kick, self.when(bar, step), KICK, 0.9)
        for step in range(8, 16, 2):
            put(self.hat, self.when(bar, step), HAT, 0.6)

    def walk_up(self, bar):
        for step, m in [(12, 38), (13, 40), (14, 41), (15, 42)]:
            self.bass(bar, step, m, 0.9, 0.85)

    def bass_bar(self, bar, name, nxt, even):
        r = ROOT[name]
        third = r + (3 if name == GM7 else 4)
        self.bass(bar, 0, r, 2.4, 1.0)
        self.bass(bar, 3, r, 0.7, 0.6)
        self.bass(bar, 6, r + 10, 0.8, 0.8)
        self.bass(bar, 7, r + 12, 1.4, 0.95)
        self.bass(bar, 10, r + 7, 0.9, 0.8)
        self.bass(bar, 12, r + 10, 0.9, 0.75)
        if even:
            self.bass(bar, 13, r + 12, 0.6, 0.6)
        self.bass(bar, 14, third, 0.9, 0.8)
        self.bass(bar, 15, nxt - 1, 0.9, 0.85)  # chromatic approach to the next root

    def outro_hits(self, bar):
        for step in (0, 3, 6):
            put(self.kick, self.when(bar, step), KICK, 1.0)
            put(self.snare, self.when(bar, step), SNARE, 0.95)
        put(self.crash, self.when(bar, 0), CRASH, 0.9)
        for step, vel in [(10, 0.5), (11, 0.55), (12, 0.65), (13, 0.75), (14, 0.9), (15, 1.0)]:
            put(self.snare, self.when(bar, step), SNARE, vel)

    def outro_bass(self, bar, final_steps):
        for step in (0, 3, 6):
            self.bass(bar, step, 31, 1.0, 1.0)
        for step, m in [(8, 43), (10, 41), (12, 38), (13, 36), (14, 34)]:
            self.bass(bar, step, m, 1.0 if step >= 12 else 1.8, 0.85)
        self.bass(bar + 1, 0, 31, final_steps, 1.0)

    def outro_organ(self, bar, final_steps):
        for step in (0, 3, 6):
            self.chord(bar, step, RH[GM7] + LH[GM7], 0.9, 0.9, FULL)
        self.gliss(bar, 8, 79, 60, 0.45)
        self.chord(bar + 1, 0, FINAL_CHORD, final_steps, 0.9, FULL)  # G7#9, let it spin
        self.fast(bar + 1, 0, 7)

    def riff_1(self, bar):  # over Gm7: climb to the high G and fall back
        self.lead(bar, 2, 74, 0.9, grace=73)
        self.lead(bar, 3, 77, 0.9)
        self.lead(bar, 4, 79, 2.6)
        self.fast(bar, 4, 7)
        self.lead(bar, 7, 77, 0.8)
        self.lead(bar, 8, 74, 1.4)
        self.lead(bar, 10, 72, 0.9)
        self.lead(bar, 11, 70, 0.9)
        self.lead(bar, 12, 67, 2.5)
        self.lead(bar, 15, 70, 0.8, 0.85)

    def riff_2(self, bar):  # over C7: stabs, then the crushed third up to the flat seven
        self.lead(bar, 0, 72, 1.6, grace=71)
        self.chord(bar, 3, [64, 70, 74], 0.7, 0.75)
        self.chord(bar, 6, [64, 70, 74], 0.7, 0.8)
        self.lead(bar, 8, 76, 0.9, grace=75)
        self.lead(bar, 9, 79, 0.9)
        self.lead(bar, 10, 82, 1.8)
        self.fast(bar, 10, 12.5)
        self.lead(bar, 12, 79, 0.9)
        self.lead(bar, 13, 76, 0.9, grace=75)
        self.lead(bar, 14, 72, 1.8)

    def riff_3(self, bar, high=False):  # over Gm7: running sixteenths down the blues scale
        if high:
            for step, m in [(0, 82), (1, 79), (2, 77), (3, 79)]:
                self.lead(bar, step, m, 0.8)
            self.lead(bar, 4, 82, 1.6)
            self.fast(bar, 4, 6)
            self.lead(bar, 6, 84, 0.8)
            self.lead(bar, 7, 82, 0.8)
            self.lead(bar, 8, 79, 2.4)
            for step, m in [(11, 77), (12, 74), (13, 73)]:
                self.lead(bar, step, m, 0.8)
            self.lead(bar, 14, 74, 1.8)
            return
        for step, m in [(0, 79), (1, 77), (2, 74), (3, 77), (4, 74), (5, 72), (6, 70), (7, 72)]:
            self.lead(bar, step, m, 0.8)
        self.lead(bar, 8, 67, 2.4)
        for step, m in [(11, 70), (12, 72), (13, 73)]:
            self.lead(bar, step, m, 0.8)
        self.lead(bar, 14, 74, 1.8)

    def riff_4(self, bar):  # over D7#9: lean on the chord, then smear into the next bar
        self.chord(bar, 0, RH[D79], 2.6, 0.9, FULL)
        self.chord(bar, 4, RH[D79], 0.7, 0.8, FULL)
        self.chord(bar, 7, RH[D79], 0.7, 0.85, FULL)
        self.chord(bar, 10, RH[D79], 2.8, 0.95, FULL)
        self.fast(bar, 10, 14)
        self.gliss(bar, 14, 66, 79)

    def organ_bar(self, bar, which, name, busy, high=False):
        """One bar of the A vamp: the riff plus the left hand comping under it."""
        if which == 0:
            self.riff_1(bar)
        elif which == 1:
            self.riff_2(bar)
        elif which == 2:
            self.riff_3(bar, high=high)
        else:
            self.riff_4(bar)
        if which in (0, 2):
            for step in ([2, 5, 11, 14] if busy else [5, 14]):
                self.chord(bar, step, LH[name], 1.2 if step == 14 else 0.6, 0.5)
        elif which == 1:
            self.chord(bar, 14, LH[name], 1.2, 0.5)

    # ----- signal chain -----
    def stems(self):
        org = filt(self.organ_bus, "high", 85)
        org = np.tanh(2.3 * org) / np.tanh(2.3)  # tube overdrive: chords growl, single notes sing
        org = filt(org, "low", 5200)
        target = np.full(self.n, 0.85)  # rotor speed in Hz: chorale, with tremolo on the marked spans
        for a, b in self.fast_spans:
            target[int(a * SR): int(b * SR)] = 6.7
        alpha = 1 - np.exp(-1 / (0.55 * SR))
        speed = signal.lfilter([alpha], [1, alpha - 1], target)
        phase = 2 * np.pi * np.cumsum(speed) / SR
        horn = filt(org, "high", 800)
        drum = org - horn
        idx = np.arange(self.n, dtype=np.float64)

        def rotor(x, ph, am, depth_ms):
            delay = depth_ms / 1000 * SR * 0.5 * (1 + np.sin(ph)) + 1
            return np.interp(idx - delay, idx, x) * (1 + am * np.sin(ph + 0.9))

        return {
            "kick": self.kick, "snare": self.snare, "hat": self.hat, "tamb": self.tamb, "crash": self.crash,
            "bass": filt(self.bass_bus, "low", 3200),
            "organ_l": rotor(horn, phase, 0.4, 0.5) + rotor(drum, phase * 0.86, 0.2, 0.16),
            "organ_r": rotor(horn, phase + 2.2, 0.4, 0.5) + rotor(drum, phase * 0.86 + 2.2, 0.2, 0.16),
        }


def room():
    t = tt(0.75)
    ir = RNG.standard_normal((2, len(t))) * np.exp(-t / 0.2)
    ir = filt(ir, "low", 5200)
    ir[:, : int(0.009 * SR)] = 0
    return ir / np.sqrt(np.sum(ir**2, axis=1, keepdims=True))


def calibrate(stems):
    """Stem gains from the full tune; every block is then mixed with the same numbers."""
    def top(x):
        return max(1e-9, float(np.abs(x).max()))

    return {
        "kick": 0.8 / top(stems["kick"]), "snare": 0.74 / top(stems["snare"]), "hat": 0.36 / top(stems["hat"]),
        "tamb": 0.22 / top(stems["tamb"]), "crash": 0.3 / top(stems["crash"]),
        "bass": 0.15 / rms(stems["bass"]),
        "organ": 0.22 / rms(np.concatenate([stems["organ_l"], stems["organ_r"]])),
    }


def premix(stems, gains, ir):
    kick, snare, hat = stems["kick"] * gains["kick"], stems["snare"] * gains["snare"], stems["hat"] * gains["hat"]
    tamb, crash, bass = stems["tamb"] * gains["tamb"], stems["crash"] * gains["crash"], stems["bass"] * gains["bass"]
    organ_l, organ_r = stems["organ_l"] * gains["organ"], stems["organ_r"] * gains["organ"]
    left = kick + snare + 0.75 * hat + 1.2 * tamb + crash + bass + organ_l
    right = kick + snare + 1.2 * hat + 0.75 * tamb + 0.9 * crash + bass + organ_r
    send_l = 0.5 * snare + 0.3 * organ_l + 0.5 * tamb + 0.4 * crash
    send_r = 0.5 * snare + 0.3 * organ_r + 0.5 * tamb + 0.4 * crash
    n = len(left)
    left = left + 0.32 * signal.fftconvolve(send_l, ir[0])[:n]
    right = right + 0.32 * signal.fftconvolve(send_r, ir[1])[:n]
    return filt(np.stack([left, right]), "high", 28)


# ---------- the tune ----------
CHORDS = [GM7] + [GM7, C7, GM7, D79] * 2 + [BB7, BB7, C7, C7, EB7, EB7, D79, D79] + [GM7, C7, GM7, D79] * 2 + [GM7, GM7]
BARS = len(CHORDS)


def compose_tune(take):
    """The whole tune. The order of these calls is the performance: do not reorder them."""
    take.pickup_drums(0)
    for bar in range(1, 25):
        if bar == 16:  # stop-time break: two hits, air, then a fill
            for step in (0, 3):
                put(take.kick, take.when(bar, step), KICK, 1.0)
                put(take.snare, take.when(bar, step), SNARE, 0.9)
            put(take.crash, take.when(bar, 0), CRASH, 0.8)
            for step, vel in [(12, 0.6), (13, 0.7), (14, 0.85), (15, 1.0)]:
                put(take.snare, take.when(bar, step), SNARE, vel)
            put(take.kick, take.when(bar, 14), KICK, 0.8)
            continue
        take.drum_bar(bar, fill=bar in (8, 24), tamb=bar >= 9)
    for bar in (1, 9, 17):
        put(take.crash, take.when(bar, 0), CRASH, 1.0)
    take.outro_hits(25)
    put(take.kick, take.when(26, 0), KICK, 1.0)
    put(take.crash, take.when(26, 0), CRASH, 1.0)

    take.walk_up(0)
    for bar in range(1, 25):
        r = ROOT[CHORDS[bar]]
        if bar == 16:
            take.bass(bar, 0, r, 1.5, 1.0)
            take.bass(bar, 3, r, 1.0, 0.9)
            take.walk_up(bar)
            continue
        take.bass_bar(bar, CHORDS[bar], ROOT[CHORDS[bar + 1]], even=bar % 2 == 0)
    take.outro_bass(25, 13)

    take.gliss(0, 12, 62, 74, 0.45)  # smear into the first bar
    for i, bar in enumerate(range(1, 9)):
        take.organ_bar(bar, i % 4, CHORDS[bar], busy=i >= 4)
    for bar in range(9, 17):  # B: big held chords on the fast rotor, a lick every second bar
        name = CHORDS[bar]
        take.fast(bar, 0, 16)
        if bar == 16:
            take.chord(bar, 0, RH[name], 1.4, 0.95, FULL)
            take.chord(bar, 3, RH[name], 1.0, 0.95, FULL)
            for step, m in [(6, 86), (7, 84), (8, 81), (9, 79), (10, 77), (11, 74), (12, 72), (13, 70)]:
                take.lead(bar, step, m, 0.85)
            take.gliss(bar, 14, 66, 79)
        elif (bar - 9) % 2 == 0:
            take.chord(bar, 0, RH[name], 6, 0.85, FULL)
            take.chord(bar, 7, RH[name], 0.8, 0.8, FULL)
            take.chord(bar, 10, RH[name], 5.4, 0.9, FULL)
        else:
            take.chord(bar, 0, RH[name], 3, 0.85, FULL)
            take.chord(bar, 4, RH[name], 0.7, 0.8, FULL)
            take.chord(bar, 6, RH[name], 0.7, 0.8, FULL)
            for step, m, grace in FILLS[name]:
                take.lead(bar, step, m, 1.7 if step in (10, 14) else 0.85, grace=grace)
    for i, bar in enumerate(range(17, 25)):
        take.organ_bar(bar, i % 4, CHORDS[bar], busy=True, high=i >= 4)
    take.outro_organ(25, 13)


def render_tune():
    """(theme audio, gains, room, master): the tune, and the chain every block reuses."""
    take = Take(BARS, RNG)
    take.n = int((BARS * 16 * S16 + 2.2) * SR)  # the tune's own length, kept from the first version
    for name in ("kick", "snare", "hat", "tamb", "crash", "bass_bus", "organ_bus"):
        setattr(take, name, np.zeros(take.n))
    compose_tune(take)
    stems = take.stems()
    ir = room()
    gains = calibrate(stems)
    mix = premix(stems, gains, ir)
    pre = 1.25 / np.abs(mix).max()  # glue: the loudest hits lean into the limiter
    mix = np.tanh(mix * pre)
    end = int((take.when(BARS, 0) + 1.6) * SR)
    mix = mix[:, int((take.when(0, 8) - 0.06) * SR):end]  # start just before the pickup fill
    fade = int(1.3 * SR)
    mix[:, -fade:] *= np.linspace(1, 0, fade) ** 1.5
    post = 0.89 / np.abs(mix).max()
    return mix * post, gains, ir, (pre, post)


# ---------- cue blocks ----------
def block_take(name, bars):
    return Take(bars, np.random.default_rng(zlib.crc32(name.encode())))


def compose_bar(take, which, name, nxt, parity, crash=False, high=False):
    take.drum_bar(0, tamb=True, parity=parity)
    if crash:
        put(take.crash, take.when(0, 0), CRASH, 1.0)
    take.bass_bar(0, name, nxt, even=not parity)
    take.organ_bar(0, which, name, busy=True, high=high)


def compose_block(name):
    """(take, start seconds, nominal seconds) for one named block."""
    G, C, D = ROOT[GM7], ROOT[C7], ROOT[D79]
    if name == "pickup":
        take = block_take(name, 1)
        take.pickup_drums(0)
        take.walk_up(0)
        take.gliss(0, 12, 62, 74, 0.45)
        return take, take.when(0, 8), 8 * S16
    bars = {
        "riff-1": (0, GM7, C, 1, True, False), "riff-2": (1, C7, G, 0, False, False),
        "riff-3": (2, GM7, D, 1, False, False), "riff-4": (3, D79, G, 0, False, False),
        "sting-1": (0, GM7, G, 1, True, False), "sting-3": (2, GM7, G, 1, False, True),
    }
    if name in bars:
        which, chord, nxt, parity, crash, high = bars[name]
        take = block_take(name, 1)
        compose_bar(take, which, chord, nxt, parity, crash, high)
        return take, 0.0, BAR
    if name == "hold":
        take = block_take(name, 3)
        put(take.kick, 0.0, KICK, 1.0)
        put(take.snare, 0.0, SNARE, 0.9)
        put(take.crash, 0.0, CRASH, 1.0)
        take.bass(0, 0, G, 14, 1.0)
        take.chord(0, 0, LH[GM7] + RH[GM7], 40, 0.9, FULL)
        take.fast(0, 0, 9)
        return take, 0.0, 40 * S16
    if name == "outro":
        take = block_take(name, 4)
        take.outro_hits(0)
        put(take.kick, take.when(1, 0), KICK, 1.0)
        put(take.crash, take.when(1, 0), CRASH, 1.0)
        take.outro_bass(0, 20)
        take.outro_organ(0, 22)
        return take, 0.0, BAR + 22 * S16
    raise ValueError(name)


BLOCKS = ("pickup", "riff-1", "riff-2", "riff-3", "riff-4", "sting-1", "sting-3", "hold", "outro")


def render_block(name, gains, ir, master):
    """(audio, nominal seconds): one block through the tune's chain, with its ring-out."""
    take, start, nominal = compose_block(name)
    pre, post = master
    mix = np.tanh(premix(take.stems(), gains, ir) * pre) * post * 10 ** (BLOCK_TRIM_DB / 20)
    first = int(round(start * SR))
    ring = 0.6 if name == "outro" else TAIL - 0.1
    mix = mix[:, first: first + int((nominal + ring) * SR)]
    if name == "outro":  # the final chord fades out under its own ring: this is how the reel ends
        fade = int(2.0 * SR)
        mix[:, -fade:] *= np.linspace(1, 0, fade) ** 1.5
    else:
        edge = int(0.05 * SR)
        mix[:, -edge:] *= np.linspace(1, 0, edge)
    return mix, nominal


def write_wav(path, audio):
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(path, SR, (np.clip(audio.T, -1, 1) * 32767).astype(np.int16))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, help="directory for theme.wav and blocks/")
    args = parser.parse_args()
    out = Path(args.out)
    theme, gains, ir, master = render_tune()
    write_wav(out / "theme.wav", theme)
    print(f"theme.wav   {theme.shape[1] / SR:7.3f} s  peak {20 * np.log10(np.abs(theme).max()):5.1f} dBFS")
    for name in BLOCKS:
        audio, nominal = render_block(name, gains, ir, master)
        write_wav(out / "blocks" / f"{name}.wav", audio)
        print(f"{name:10}  nominal {nominal:6.3f} s  file {audio.shape[1] / SR:6.3f} s  "
              f"peak {20 * np.log10(np.abs(audio).max()):5.1f} dBFS")
    print(f"bar {BAR:.6f} s at {BPM} BPM")


if __name__ == "__main__":
    main()
