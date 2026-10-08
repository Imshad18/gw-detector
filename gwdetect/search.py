"""Matched-filter search for binary black hole mergers in LIGO/Virgo strain.

Steps: estimate the noise spectrum, correlate the data against a bank of waveform templates in
each detector, require coincidence within the light travel time between sites, measure the
background with time slides, check the signal shape with a chi-squared test, and estimate the
chirp mass with inspiral-only templates.
"""
import numpy as np
from scipy.ndimage import maximum_filter1d
from scipy.signal import butter, sosfiltfilt, welch
from scipy.signal.windows import tukey

from . import waveform as W

F_LOW = 20.0
EDGE_S = 8.0          # ignore filter output this close to the segment edges
POOL_S = 0.010        # coincidence bin; also the H1-L1 light travel time
TRAVEL_S = {"H1": 0.010, "L1": 0.010, "V1": 0.027}
COINC_SNR = 4.0


class Detector:
    """Noise estimate and frequency-domain data for one detector."""

    def __init__(self, name, t0, dt, x, center, seg=64.0):
        self.name, self.dt = name, dt
        fs = 1 / dt
        # remove the huge sub-15 Hz noise first so gating doesn't create edge artifacts
        x = sosfiltfilt(butter(8, 15, "highpass", fs=fs, output="sos"), x)
        self.psd_f, self.psd = welch(x, fs=fs, nperseg=int(4 * fs), average="median")
        x, self.gates = gate(x, dt, self.psd_f, self.psd, t0)
        if self.gates:
            self.psd_f, self.psd = welch(x, fs=fs, nperseg=int(4 * fs), average="median")
        i0 = int(round((center - seg / 2 - t0) / dt))
        self.seg = x[i0:i0 + int(seg * fs)]
        self.t0 = t0 + i0 * dt
        self.n = len(self.seg)
        self.f = np.fft.rfftfreq(self.n, dt)
        self.df = self.f[1]
        self.S = np.interp(self.f, self.psd_f, self.psd)
        self.S[self.f < F_LOW * 0.8] = np.inf
        self.d = np.fft.rfft(self.seg * tukey(self.n, 0.1)) * dt
        self.edge = int(EDGE_S / dt)
        self.veto = np.zeros(self.n, bool)
        for g in self.gates:
            a = int((g["t"] - self.t0 - g["width_s"] / 2 - VETO_S) / dt)
            b = int((g["t"] - self.t0 + g["width_s"] / 2 + VETO_S) / dt)
            self.veto[max(0, a):max(0, min(self.n, b))] = True

    def filter(self, h):
        """Complex SNR time series z(t)/sigma and the template norm sigma."""
        w = np.where(np.isfinite(self.S), 1 / self.S, 0.0)
        sigma = np.sqrt(4 * np.sum(np.abs(h) ** 2 * w) * self.df)
        if sigma == 0:
            return None, 0.0
        q = np.zeros(self.n, complex)
        q[:len(self.f)] = self.d * np.conj(h) * w
        z = np.fft.ifft(q) * self.n * self.df * 4
        return z / sigma, sigma

    def whiten(self, xf, band=(30, 350)):
        w = np.where(np.isfinite(self.S), 1 / np.sqrt(self.S), 0.0)
        w[(self.f < band[0]) | (self.f > band[1])] = 0
        return np.fft.irfft(xf * w, self.n) / self.dt * np.sqrt(2 * self.dt)


GATE_SIGMA = 10.0     # whitened-amplitude threshold for removing glitches
GATE_HALF_S = 0.5
VETO_S = 2.0          # ignore filter output this close to a gate


def gate(x, dt, psd_f, psd, t0):
    """Zero out short loud glitches (with a smooth taper), as LIGO search pipelines do.

    Binary-merger signals are far below this level in whitened data; instrumental glitches are not.
    """
    n = len(x)
    f = np.fft.rfftfreq(n, dt)
    S = np.interp(f, psd_f, psd)
    band = (f > 20) & (f < 1000)
    xf = np.fft.rfft(x * tukey(n, 0.05))
    w = np.fft.irfft(np.where(band, xf / np.sqrt(S), 0), n)
    sd = 1.4826 * np.median(np.abs(w - np.median(w)))
    loud = np.flatnonzero(np.abs(w) > GATE_SIGMA * sd)
    if len(loud) == 0:
        return x, []
    half = int(GATE_HALF_S / dt)
    taper = int(0.1 / dt)
    win = np.ones(n)
    gates = []
    # merge loud samples into gate intervals
    starts = [loud[0]]
    ends = []
    for a, b in zip(loud[:-1], loud[1:]):
        if b - a > half:
            ends.append(a)
            starts.append(b)
    ends.append(loud[-1])
    for a, b in zip(starts, ends):
        lo, hi = max(0, a - half), min(n, b + half)
        win[lo:hi] = 0
        ramp = 0.5 * (1 - np.cos(np.linspace(0, np.pi, taper)))
        l0 = max(0, lo - taper)
        win[l0:lo] *= ramp[-(lo - l0):] if lo > l0 else 1
        h1 = min(n, hi + taper)
        win[hi:h1] *= ramp[::-1][:h1 - hi]
        gates.append({"t": float(t0 + (a + b) / 2 * dt), "width_s": float((hi - lo) * dt),
                      "peak_sigma": float(np.abs(w[a:b + 1]).max() / sd)})
    return x * win, gates


def template_bank(mmin=5.0, mmax=100.0, n=34):
    m = np.geomspace(mmin, mmax, n)
    return [(a, b) for a in m for b in m if b <= a and a / b <= 8]


def _pool(rho, k):
    m = len(rho) // k * k
    return rho[:m].reshape(-1, k).max(1)


def search(dets, bank, progress=None):
    """Network SNR for every template; keeps pooled SNR series for the time-slide background."""
    k = int(round(POOL_S / dets[0].dt))
    lo, hi = dets[0].edge, dets[0].n - dets[0].edge
    pooled = {d.name: [] for d in dets}
    net_best = np.zeros(len(bank))
    peak = np.zeros(len(bank), int)
    for ti, (m1, m2) in enumerate(bank):
        if progress and ti % 25 == 0:
            progress(ti / len(bank), f"Template {ti + 1}/{len(bank)}: {m1:.1f} + {m2:.1f} M☉")
        series = []
        for d in dets:
            z, sig = d.filter(W.template(d.f, m1, m2, f_low=F_LOW))
            rho = np.zeros(d.n) if z is None else np.abs(z)
            rho[:lo] = 0
            rho[hi:] = 0
            rho[d.veto] = 0
            series.append(rho)
            pooled[d.name].append(_pool(rho, k))
        ref = series[0] ** 2
        count = (series[0] >= COINC_SNR).astype(int)
        for d, rho in zip(dets[1:], series[1:]):
            win = 2 * int(TRAVEL_S[d.name] / d.dt) + 1
            mx = maximum_filter1d(rho, win)
            ref = ref + mx ** 2
            count += mx >= COINC_SNR
        net = np.sqrt(ref)
        if len(dets) > 1:
            net[count < 2] = 0  # require a coincident trigger in at least two detectors
        peak[ti] = int(np.argmax(net))
        net_best[ti] = net[peak[ti]]
    for name in pooled:
        pooled[name] = np.array(pooled[name], dtype=np.float32)
    return net_best, peak, pooled


def background(pooled, names, n_slides=400, min_shift_s=0.2):
    """Max network SNR over the bank for many unphysical time shifts between detectors."""
    H = pooled[names[0]] ** 2
    nb = H.shape[1]
    step = int(min_shift_s / POOL_S)
    out = []
    rng = np.random.default_rng(1)
    for s in range(1, n_slides + 1):
        tot = H.copy()
        count = (pooled[names[0]] >= COINC_SNR).astype(np.int8)
        for j, name in enumerate(names[1:]):
            shift = (s * step * (j + 1) + (rng.integers(0, step) if j else 0)) % nb
            L = pooled[name]
            # neighbouring bins approximate the coincidence window
            Lm = np.roll(np.maximum(L, np.maximum(np.roll(L, 1, 1), np.roll(L, -1, 1))), shift, axis=1)
            tot += Lm ** 2
            count += Lm >= COINC_SNR
        tot[count < 2] = 0
        out.append(float(np.sqrt(tot.max())))
    return np.array(out)


def chisq(det, h, t_idx, p=8):
    """Allen chi-squared: does the SNR build up across frequency the way the template predicts?"""
    w = np.where(np.isfinite(det.S), 1 / det.S, 0.0)
    power = np.abs(h) ** 2 * w
    c = np.cumsum(power)
    if c[-1] == 0:
        return np.nan, np.nan
    edges = np.searchsorted(c, np.linspace(0, c[-1], p + 1)[1:-1])
    edges = np.concatenate([[0], edges, [len(h)]])
    sigma = np.sqrt(4 * c[-1] * det.df)
    phase = np.exp(2j * np.pi * det.f * (t_idx * det.dt))
    integrand = det.d * np.conj(h) * w * phase * 4 * det.df
    zi = np.array([integrand[a:b].sum() for a, b in zip(edges[:-1], edges[1:])])
    z = zi.sum()
    chi = p * np.sum(np.abs(zi - z / p) ** 2) / sigma ** 2
    return float(chi / (2 * p - 2)), float(np.abs(z) / sigma)


# Templates never match a real signal perfectly, so chi-squared grows with SNR^2 even for true
# signals. Glitches exceed this allowance by a wide margin.
MISMATCH = 0.06


def chi_allowance(rho):
    return 1 + MISMATCH * rho ** 2


def reweighted(rho, chi_r):
    """Down-weight SNR when chi-squared exceeds what template mismatch can explain."""
    if not np.isfinite(chi_r):
        return rho
    x = chi_r / chi_allowance(rho)
    return rho if x <= 1 else rho / ((1 + x ** 3) / 2) ** (1 / 6)


def chirp_mass_scan(dets, t_peak, mc_center, progress=None):
    """Inspiral-only templates (cut at ISCO) give a less biased chirp mass than the detection bank."""
    mcs = np.geomspace(mc_center * 0.6, mc_center * 1.9, 140)
    qs = np.linspace(0.35, 1.0, 6)
    grid = np.zeros((len(qs), len(mcs)))
    win = 0.02
    for qi, q in enumerate(qs):
        if progress:
            progress(qi / len(qs), f"Chirp-mass scan, mass ratio {q:.2f}")
        for mi, mc in enumerate(mcs):
            # m1, m2 from chirp mass and mass ratio
            m1 = mc * (1 + q) ** 0.2 / q ** 0.6
            m2 = q * m1
            tot = 0.0
            for d in dets:
                h = np.zeros(len(d.f), complex)
                b = (d.f >= F_LOW) & (d.f <= W.f_isco(m1, m2))
                h[b] = d.f[b] ** (-7 / 6) * np.exp(-1j * W.taylorf2_phase(d.f[b], m1, m2))
                z, _ = d.filter(h)
                if z is None:
                    continue
                i = int((t_peak - d.t0) / d.dt)
                j = int(win / d.dt)
                tot += np.abs(z[i - j:i + j]).max() ** 2
            grid[qi, mi] = np.sqrt(tot)
    best = np.unravel_index(np.argmax(grid), grid.shape)
    prof = grid.max(0)
    ok = prof ** 2 >= prof.max() ** 2 - 1  # 1-sigma interval of the profile likelihood
    idx = np.flatnonzero(ok)
    lo, hi = max(0, idx.min() - 1), min(len(mcs) - 1, idx.max() + 1)  # grid resolution
    return {"mc": float(mcs[best[1]]), "q": float(qs[best[0]]),
            "mc_lo": float(mcs[lo]), "mc_hi": float(mcs[hi]),
            "profile": {"mc": mcs.round(3).tolist(), "snr": prof.round(3).tolist()}}


def qscan(det, t_rel, span=1.0, fmin=20, fmax=512, nf=70, nt=360, Q=8.0):
    """Constant-Q energy map of whitened data around t_rel (seconds into the segment)."""
    w = np.where(np.isfinite(det.S), 1 / np.sqrt(det.S), 0.0)
    xw = det.d * w
    freqs = np.geomspace(fmin, fmax, nf)
    i0 = int((t_rel - span) / det.dt)
    i1 = int((t_rel + span) / det.dt)
    idx = np.linspace(i0, i1, nt).astype(int)
    rows = []
    for fc in freqs:
        bw = fc / Q
        g = np.exp(-0.5 * ((det.f - fc) / bw) ** 2)
        q = np.zeros(det.n, complex)
        q[:len(det.f)] = xw * g
        e = np.abs(np.fft.ifft(q)) ** 2
        seg = e[max(0, i0 - det.n // 8):i1 + det.n // 8]
        rows.append(e[idx] / np.median(seg))
    return freqs, (idx * det.dt - t_rel), np.array(rows)
