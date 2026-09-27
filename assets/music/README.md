# assets/music/

Music beds for the reel. Kyle asked for music at the intro, outro and
transitions, in the public domain and on theme for Pipeline Entrepreneurs (a
Kansas City founder fellowship: optimistic, builder-minded, a little playful).
Both files are 1920s Kansas City jazz by Bennie Moten's Kansas City Orchestra,
the band that made Kansas City a jazz town, recorded in November 1924 for OKeh
and now in the US public domain (see [`LICENSE`](LICENSE) for the provenance,
the legal reasoning and the exact processing). They are short, loop-friendly
excerpts, loudness-normalised to about -16 LUFS with a one-second fade at each
end, 128 kbps MP3, well under 2 MB each.

| File | Recording | Feel | Length |
|------|-----------|------|--------|
| `bed.mp3` | "South" (1924), Moten's signature tune and the Kansas City anthem | medium-up stomp, steady, brassy | 89 s |
| `bed-alt.mp3` | "Goofy Dust" (1924), same session | fast rag, playful, cleanest transfer | 71 s |

A plan references a bed through the v1.2 reel extension
(`contract/README.md`, "v1.2 additions"): set
`output.reel.music.path = "assets/music/bed.mp3"` (repo-root-relative POSIX
path) and, optionally, `under` (`cards`, the default: music plays only under the
intro, opening, chapter, closing and outro cards, faded at the run edges; or
`all`: under everything, ducked below speech by `duck_db`), `gain_db`,
`fade_seconds` and `loop`. `contract/examples/reel-with-music.json` is a
complete example. Swap in `bed-alt.mp3` by changing the path only. Anything
else added here must be public domain or licensed for the project and must get
an entry in `LICENSE`.
