# FFmpeg experiment record

Executed 2026-09-25 on Windows using PowerShell 7.6.5 and
FFmpeg/ffprobe `9.0.2-full_build-www.gyan.dev`. See the
[research recommendation](ffmpeg-local-pipeline.md) for scope and limitations.
All media was generated in a temporary directory; no project assets were changed.

## Measured results

| Experiment | Observation |
|---|---|
| Source A/V | 12.000000 s; 360 decoded frames; H.264, 640 x 360, 30 fps, yuv420p; mono AAC at 48 kHz |
| Source without audio | Same video; no audio stream |
| Keep `[1.25, 3.75)` and `[7.5, 10.0)`, A/V | 5.000000 s; 150 decoded frames; AAC duration 5.000000 s |
| Same ranges, video only | 5.000000 s; 150 decoded frames; no audio stream |
| Audio extraction | 12.000000 s; mono 16 kHz PCM signed 16-bit little-endian WAV |
| Complete decode of the five files | Exit 0; empty error logs |
| Black/white/black scene changes | Detected at 2 s and 4 s with threshold 10; score 85.547 |
| Synthetic silent interval | Expected `[2, 4]`; measured start 2, end 4.000021, duration 2.000021 s |
| First cut's original video timestamps | 75 retained frames from 1.266667 through 3.733333 s |
| Whisper capability discovery | Build has `--enable-whisper`; filter and its help are present; no model inference tested |

These are measurements on synthetic input, not performance benchmarks. The
continuous tone does not establish speech synchronization or transcription
accuracy. Frame counts/durations do not establish subjective visual quality.

## Reproduce

Use a new, empty temporary directory with `ffmpeg` and `ffprobe` on PATH.
The commands are individual PowerShell lines; `-y` overwrites the named experiment
outputs if rerun. `NUL` is the Windows null destination; use `-` with the null
muxer on other platforms. Check each process's exit status before proceeding.

### Inspect capabilities

```powershell
ffmpeg -hide_banner -version
ffprobe -hide_banner -version
ffmpeg -hide_banner -buildconf
ffmpeg -hide_banner -h filter=whisper
```

### Generate 12 seconds of video and tone, then a video-only copy

```powershell
ffmpeg -hide_banner -nostdin -y -f lavfi -i 'testsrc2=size=640x360:rate=30:duration=12' -f lavfi -i 'sine=frequency=440:sample_rate=48000:duration=12' -map 0:v:0 -map 1:a:0 -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart source-av.mp4
ffmpeg -hide_banner -nostdin -y -i source-av.mp4 -map 0:v:0 -an -c:v copy -movflags +faststart source-v.mp4
```

### Trim and join two ranges, preserving audio

```powershell
ffmpeg -hide_banner -nostdin -y -i source-av.mp4 -filter_complex '[0:v:0]split=2[v0][v1];[0:a:0]asplit=2[a0][a1];[v0]trim=start=1.25:end=3.75,setpts=PTS-STARTPTS[v0t];[a0]atrim=start=1.25:end=3.75,asetpts=PTS-STARTPTS[a0t];[v1]trim=start=7.5:end=10.0,setpts=PTS-STARTPTS[v1t];[a1]atrim=start=7.5:end=10.0,asetpts=PTS-STARTPTS[a1t];[v0t][a0t][v1t][a1t]concat=n=2:v=1:a=1[v][a]' -map '[v]' -map '[a]' -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart trim-av.mp4
```

### Use an audio-free graph for the source with no audio

```powershell
ffmpeg -hide_banner -nostdin -y -i source-v.mp4 -filter_complex '[0:v:0]split=2[v0][v1];[v0]trim=start=1.25:end=3.75,setpts=PTS-STARTPTS[v0t];[v1]trim=start=7.5:end=10.0,setpts=PTS-STARTPTS[v1t];[v0t][v1t]concat=n=2:v=1:a=0[v]' -map '[v]' -an -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -movflags +faststart trim-v.mp4
```

### Extract analysis audio

```powershell
ffmpeg -hide_banner -nostdin -y -i source-av.mp4 -map 0:a:0 -vn -ac 1 -ar 16000 -c:a pcm_s16le speech.wav
```

### Detect synthetic boundaries and inspect source frame timing

```powershell
ffmpeg -hide_banner -nostdin -f lavfi -i 'color=black:s=640x360:r=30:d=2' -f lavfi -i 'color=white:s=640x360:r=30:d=2' -f lavfi -i 'color=black:s=640x360:r=30:d=2' -filter_complex '[0:v][1:v][2:v]concat=n=3:v=1:a=0,scdet=threshold=10,metadata=mode=print[v]' -map '[v]' -an -f null NUL
ffmpeg -hide_banner -nostdin -f lavfi -i 'aevalsrc=if(between(t\,2\,4)\,0\,0.25*sin(2*PI*440*t)):s=48000:d=6' -af 'silencedetect=noise=-50dB:d=0.3' -f null NUL
ffmpeg -hide_banner -nostdin -i source-av.mp4 -vf 'trim=start=1.25:end=3.75,showinfo' -an -f null NUL
```

The thresholds were chosen to demonstrate known synthetic transitions. They are
not recommended universal settings for highlight selection.
Filter references: [scene detection](https://ffmpeg.org/ffmpeg-filters.html#scdet),
[silence detection](https://ffmpeg.org/ffmpeg-filters.html#silencedetect),
[video trim](https://ffmpeg.org/ffmpeg-filters.html#trim) and
[audio trim](https://ffmpeg.org/ffmpeg-filters.html#atrim).

### Probe and fully decode every artifact

```powershell
foreach ($mediaName in @('source-av.mp4','source-v.mp4','trim-av.mp4','trim-v.mp4','speech.wav')) {
    ffprobe -v error -count_frames -show_entries 'format=duration,size:stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,time_base,start_time,duration,nb_frames,nb_read_frames,sample_rate,channels' -of json $mediaName
    if ($LASTEXITCODE -ne 0) { throw "Probe failed: $mediaName" }
    ffmpeg -hide_banner -nostdin -v error -xerror -i $mediaName -map '0:v?' -map '0:a?' -f null NUL
    if ($LASTEXITCODE -ne 0) { throw "Full decode failed: $mediaName" }
}
```

Compare the printed metadata with the measured table; exit 0 alone is not a
duration or content assertion. The examples intentionally do not download a
transcription model or exercise an automatic highlight selector.
