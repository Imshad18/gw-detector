# GW Detector

Re-detects gravitational-wave events from the raw LIGO and Virgo strain data published by GWOSC,
using a matched-filter search written from scratch in NumPy.

![screenshot](shots/result.png)

## Usage

```bash
./run.sh        # Linux / macOS / WSL
run.bat         # Windows
```

Open http://localhost:8002 and pick an event, or enter any GPS time to search data with no known event.

## How it works

1. Reads 128 s of 4 kHz strain around the event from each detector (HTTP range reads of the GWOSC
   files, so only the needed slice is downloaded).
2. High-pass filter, noise spectrum estimate, and gating of loud instrumental glitches.
3. Matched filter against ~530 templates (5–100 M☉): TaylorF2 3.5PN inspiral with a phenomenological
   merger and ringdown.
4. Coincidence: SNR ≥ 4 in at least two detectors within the light travel time between sites.
5. Background from 400 time slides; χ² signal-consistency test.
6. Chirp mass from inspiral-only templates; source-frame value uses the published redshift.

Output: whitened strain with the best-fit waveform, constant-Q time-frequency maps, SNR time series,
background distribution, template bank map, noise spectra, audio of the signal, and a comparison with
the published parameters.

## Tested on

| Event | Result | Network SNR | Chirp mass (det. frame) | Published |
|---|---|---|---|---|
| GW150914 | Detected | 17.3 | 29.0 M☉ | 30.7 M☉ |
| GW151226 | Detected | 11.0 | 9.5 M☉ | 9.8 M☉ |
| GW170814 | Detected (L1 glitch gated) | 12.8 | 26.3 M☉ | 27.2 M☉ |
| GPS 1126260462 (no event) | No significant signal | 6.5 | | |

## Limitations

- Non-spinning templates with an approximate merger: SNR is lower than in the published analyses
  and quiet, high-spin or very unequal-mass events can be missed.
- Binary neutron star signals are longer than the 64 s analysis window and are not covered.
- Background from 400 time slides limits the false-alarm probability to about 1 in 400.
