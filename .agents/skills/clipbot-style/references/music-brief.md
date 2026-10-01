# Brief for one music agent

Fill in the bracketed parts. One agent per candidate; give each a different DIRECTION.

---

Compose an original [LENGTH, e.g. 60 to 90 second] piece of music by writing a Python
synthesizer (numpy + scipy only). There are no soundfonts or synth tools here; every sound is
generated in code. It is [WHAT IT IS FOR, e.g. the theme under the title cards of a summary
video for a series about ...]. The feel: [THE USER'S OWN WORDS. Ask for a groove, a hook and
some dirt. "Calm and quietly confident" gets elevator music].

YOUR DIRECTION, "[NAME]": [instruments, key, tempo, the hook, the form in bars].

HOW IT IS USED: under title cards for 3 to 7 seconds at a time, and at the start and end of a
video. So it needs a strong first bar, an ending that actually ends, and steady energy.

SYNTHESIS QUALITY (raw oscillators sound cheap): drums from shaped noise and pitch-swept
sines with velocity variation, ghost notes and swing; bass with a plucked envelope whose
bright partials die first; keys and organs from several partials with attack transients;
overdrive (tanh) where a real instrument would have it; movement (a Leslie, a filter sweep,
chorus) on anything sustained; a short room reverb; humanized timing and velocity from a
seeded generator; soft clipping on the bus, never hard clipping.

TECHNICAL TARGETS (measure and meet all of them):
- Stereo, 48 kHz. Integrated loudness [-14 for a theme, -16 for a bed] LUFS within 1 LU. True
  peak at or below -1 dBTP.
- Mono downmix within 3 dB of the stereo level.
- Energy across the spectrum: a bass you can also hear on a laptop (harmonics above 150 Hz),
  mids for the melody, hats or air above 6 kHz. Check each band's level.
- Deterministic: the same seed gives the same file.

TOOLS: `uv run --python 3.12 --with numpy --with scipy python synth.py`; ffmpeg for
`ebur128=peak=true`, `volumedetect`, and `showspectrumpic=s=1600x700:legend=1:fscale=log`.
You cannot listen. Verify by measurement and by opening the spectrogram: harmonic lines for
the melody, a regular grid for the drums, no full-height stripes outside drum hits, no wall of
noise, and the form visible as sections.

OUTPUT in [DIRECTORY], nothing outside it, no git: synth.py, the .wav, an .mp3, the
spectrogram.

RETURN, under 200 words: paths, the measured numbers, the form bar by bar, what a listener
hears in three sentences, and anything you could not meet.
