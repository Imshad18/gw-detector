"""Frequency-domain compact-binary waveforms (non-spinning).

TaylorF2 inspiral (3.5PN phase, Newtonian amplitude), extended through merger and
ringdown with the phenomenological amplitude of IMRPhenomA-style models: the inspiral
amplitude ~ f^-7/6 is joined to a merger ~ f^-2/3 and a Lorentzian ringdown. This is
not a full IMR model, but it captures most of the matched-filter SNR for heavy binaries.
"""
import numpy as np

MSUN_S = 4.925491025543576e-06   # G*Msun/c^3 in seconds
MPC_S = 3.085677581491367e22 / 299792458.0  # 1 Mpc in light-seconds
EULER = 0.5772156649015329


def chirp_mass(m1, m2):
    return (m1 * m2) ** 0.6 / (m1 + m2) ** 0.2


def f_isco(m1, m2):
    return 1.0 / (6 ** 1.5 * np.pi * (m1 + m2) * MSUN_S)


def ringdown(m1, m2):
    """Rough final-black-hole ringdown frequency and width (non-spinning fits)."""
    M = (m1 + m2) * MSUN_S
    eta = m1 * m2 / (m1 + m2) ** 2
    # IMRPhenomA-style fits for merger / ringdown frequencies (in units of 1/(pi M))
    f_merg = (0.29740 * eta ** 2 + 0.044810 * eta + 0.095560) / (np.pi * M)
    f_ring = (0.59411 * eta ** 2 + 0.089794 * eta + 0.19111) / (np.pi * M)
    sigma = (0.50801 * eta ** 2 + 0.077515 * eta + 0.022369) / (np.pi * M)
    f_cut = (0.84845 * eta ** 2 + 0.12848 * eta + 0.27299) / (np.pi * M)
    return f_merg, f_ring, sigma, f_cut


def taylorf2_phase(f, m1, m2):
    M = (m1 + m2) * MSUN_S
    eta = m1 * m2 / (m1 + m2) ** 2
    v = (np.pi * M * f) ** (1 / 3)
    v_isco = 1 / np.sqrt(6)
    p = np.zeros((8,) + np.shape(f))
    p[0] = 1
    p[2] = 3715 / 756 + 55 / 9 * eta
    p[3] = -16 * np.pi
    p[4] = 15293365 / 508032 + 27145 / 504 * eta + 3085 / 72 * eta ** 2
    p[5] = np.pi * (38645 / 756 - 65 / 9 * eta) * (1 + 3 * np.log(v / v_isco))
    p[6] = (11583231236531 / 4694215680 - 640 / 3 * np.pi ** 2 - 6848 / 21 * EULER
            - 6848 / 21 * np.log(4 * v)
            + (-15737765635 / 3048192 + 2255 / 12 * np.pi ** 2) * eta
            + 76055 / 1728 * eta ** 2 - 127825 / 1296 * eta ** 3)
    p[7] = np.pi * (77096675 / 254016 + 378515 / 1512 * eta - 74045 / 756 * eta ** 2)
    series = sum(p[k] * v ** k for k in range(8))
    return 3 / (128 * eta * v ** 5) * series - np.pi / 4


def template(f, m1, m2, dist_mpc=1.0, f_low=20.0):
    """Optimally-oriented strain h(f) at distance dist_mpc. Zero outside [f_low, f_cut]."""
    M = (m1 + m2) * MSUN_S
    eta = m1 * m2 / (m1 + m2) ** 2
    Mc = chirp_mass(m1, m2) * MSUN_S
    f_merg, f_ring, sigma, f_cut = ringdown(m1, m2)
    h = np.zeros(len(f), complex)
    band = (f >= f_low) & (f <= f_cut)
    fb = f[band]
    # Newtonian inspiral amplitude, optimally oriented
    A0 = np.sqrt(5 / 24) * np.pi ** (-2 / 3) * Mc ** (5 / 6) / (dist_mpc * MPC_S)
    amp = np.where(fb < f_merg, fb ** (-7 / 6), 0.0)
    w_m = f_merg ** (-1 / 2)
    mid = (fb >= f_merg) & (fb < f_ring)
    amp[mid] = w_m * fb[mid] ** (-2 / 3)
    w_r = w_m * f_ring ** (-2 / 3) * (np.pi * sigma / 2)
    rd = fb >= f_ring
    amp[rd] = w_r * (sigma / (2 * np.pi)) / ((fb[rd] - f_ring) ** 2 + sigma ** 2 / 4)
    # Phase: TaylorF2 below f_merg, continued linearly-in-frequency-derivative above
    psi = taylorf2_phase(np.minimum(fb, f_merg), m1, m2)
    hi = fb > f_merg
    if hi.any():
        df = 1e-3 * f_merg
        dpsi = (taylorf2_phase(f_merg, m1, m2) - taylorf2_phase(f_merg - df, m1, m2)) / df
        psi[hi] += dpsi * (fb[hi] - f_merg)
    h[band] = A0 * amp * np.exp(-1j * psi)
    return h
