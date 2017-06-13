#vim:fileencoding=utf-8
from __future__ import division, print_function
import numpy as np
from .segmentaxis import segment_axis
from .theory import cal_mqam_symbols
from .signal_quality import cal_S0
from .dsp_cython import unwrap_discont
from .dsp_cython import bps as _bps_idx_pyx
import numba
try:
    import arrayfire as af
except ImportError:
    af = None

SYMBOLS_16QAM = cal_mqam_symbols(16)
NMAX = 4*1024**3

def viterbiviterbi_gen(N, E, M):
    """
    Viterbi-Viterbi blind phase recovery for an M-PSK signal

    Parameters
    ----------
    N : int
        number of samples to average over
    E : array_like
        the electric field of the signal
    M : int
        order of the M-PSK

    Returns
    -------
    Eout : array_like
        Field with compensated phases
    """
    E = E.flatten()
    L = len(E)
    phi = np.angle(E)
    E_raised = np.exp(1.j * phi)**M
    sa = segment_axis(E_raised, N, N - 1)
    phase_est = np.sum(sa, axis=1)
    phase_est = np.unwrap(np.angle(phase_est))
    if N % 2:
        E = E[(N - 1) // 2:L - (N - 1) // 2]
    else:
        E = E[N // 2 - 1:L - (N // 2)]
    #if M == 4: # QPSK needs pi/4 shift
    # need a shift by pi/M for constellation points to not be on the axis
    phase_est = phase_est - np.pi
    return E * np.exp(-1.j * phase_est / M)

def _bps_py(E, Mtestangles, symbols, N):
    """
    Blind phase search using Python. This is slow compared to the cython and arrayfire methods and should not be used.
    """
    angles = np.linspace(-np.pi/4, np.pi/4, Mtestangles, endpoint=False)
    EE = E[:,np.newaxis]*np.exp(1.j*angles)
    idx = np.zeros(len(E)-2*N, dtype=np.int)
    dist = (abs(EE[:2*N, :, np.newaxis]-symbols)**2).min(axis=2)
    idx[0] = dist.sum(axis=0).argmin(axis=0)
    for i in range(1,len(idx)):
        tmp = (abs(EE[N+i:i+3*N,:,np.newaxis]-symbols)**2).min(axis=1).reshape(1,Mtestangles)
        dist = np.concatenate([dist[1:], tmp])#(abs(EE[N+i,:,np.newaxis]-symbols)**2).min(axis=1)])
        idx[i] = dist.sum(axis=0).argmin(axis=0)
    ph = np.unwrap(angles[idx]*4)/4
    En = E[N:-N]*np.exp(1.j*ph)
    return En, ph

def bps(E, Mtestangles, symbols, N, method="pyx", **kwargs):
    """
    Perform a blind phase search phase recovery after _[1]

    Parameters
    ----------

    E           : array_like
        input signal (single polarisation)

    Mtestangles : int
        number of test angles to try

    symbols     : array_like
        the symbols of the modulation format

    N           : int
        block length to use for averaging

    method      : string, optional
        implementation method to use has to be "af" for arrayfire (uses OpenCL) or "pyx" for a cython-OpenMP based parallel search.

    **kwargs    :
        arguments to be passed to the search function

    Returns
    -------
    Eout    : array_like
        phase compensated field
    ph      : array_like
        unwrapped angle from phase recovery

    References
    ----------
    ..[1] Timo Pfau et al, Hardware-Efficient Coherent Digital Receiver Concept With Feedforward Carrier Recovery for M-QAM Constellations, Journal of Lightwave Technology 27, pp 989-999 (2009)
    """
    if method.lower() == "pyx":
        bps_fct = _bps_idx_pyx
    elif method.lower() == "af":
        if af == None:
            raise RuntimeError("Arrayfire was not imported so cannot use it")
        bps_fct = _bps_idx_af
    else:
        raise("Method needs to be 'pyx' or 'af'")
    angles = np.linspace(-np.pi/4, np.pi/4, Mtestangles, endpoint=False).reshape(1,-1)
    idx =  bps_fct(E, angles, symbols, N, **kwargs)
    ph = select_angles(angles, idx)
    # ignore the phases outside the averaging window
    ph[N:-N] = unwrap_discont(ph[N:-N], 10*np.pi/2/Mtestangles, np.pi/2)
    Eout = E*np.exp(1.j*ph)
    return Eout, ph

def movavg_af(X, N, axis=0):
    """
    Calculate moving average over N samples using arrayfire
    """
    cs = af.accum(X, dim=axis)
    return cs[N:] - cs[:-N]

def _bps_idx_af(E, angles, symbols, N, precision=16):
    global NMAX
    if precision == 16:
        prec_dtype = np.complex128
    elif precision == 8:
        prec_dtype = np.complex64
    else:
        raise ValueError("Precision has to be either 16 for double complex or 8 for single complex")
    Ntestangles = angles.shape[1]
    Nmax = NMAX//Ntestangles//symbols.shape[0]//16
    L = E.shape[0]
    EE = E[:,np.newaxis]*np.exp(1.j*angles)
    syms  = af.np_to_af_array(symbols.astype(prec_dtype).reshape(1,1,-1))
    idxnd = np.zeros(L, dtype=np.int32)
    if L <= Nmax+N:
        Eaf = af.np_to_af_array(EE.astype(prec_dtype))
        tmp = af.min(af.abs(af.broadcast(lambda x,y: x-y, Eaf[0:L,:], syms))**2, dim=2)
        cs = movavg_af(tmp, 2*N, axis=0)
        val, idx = af.imin(cs, dim=1)
        idxnd[N:-N] = np.array(idx)
    else:
        K = L//Nmax
        R = L%Nmax
        if R < N:
            R = R+Nmax
            K -= 1
        Eaf = af.np_to_af_array(EE[0:Nmax+N].astype(prec_dtype))
        tmp = af.min(af.abs(af.broadcast(lambda x,y: x-y, Eaf, syms))**2, dim=2)
        tt = np.array(tmp)
        cs = movavg_af(tmp, 2*N, axis=0)
        val, idx = af.imin(cs, dim=1)
        idxnd[N:Nmax] = np.array(idx)
        for i in range(1,K):
            Eaf = af.np_to_af_array(EE[i*Nmax-N:(i+1)*Nmax+N].astype(np.complex128))
            tmp = af.min(af.abs(af.broadcast(lambda x,y: x-y, Eaf, syms))**2, dim=2)
            cs = movavg_af(tmp, 2*N, axis=0)
            val, idx = af.imin(cs, dim=1)
            idxnd[i*Nmax:(i+1)*Nmax] = np.array(idx)
        Eaf = af.np_to_af_array(EE[K*Nmax-N:K*Nmax+R].astype(np.complex128))
        tmp = af.min(af.abs(af.broadcast(lambda x,y: x-y, Eaf, syms))**2, dim=2)
        cs = movavg_af(tmp, 2*N, axis=0)
        val, idx = af.imin(cs, dim=1)
        idxnd[K*Nmax:-N] = np.array(idx)
    return idxnd

def bps_af(E, testangles, symbols, N, **kwargs):
    """
    Arrayfire based blind phase search. See bps for parameters
    """
    return bps(E, testangles, symbols, N, method="af", **kwargs)


def bps_pyx(E, testangles, symbols, N, **kwargs):
    """
    Cython based blind phase search. See bps for parameters
    """
    return bps(E, testangles, symbols, N, method="pyx", **kwargs)


@numba.jit(nopython=True)
def select_angles(angles, idx):
    if angles.shape[0] > 1:
        L = angles.shape[0]
        anglesn = np.zeros(L, dtype=np.float64)
        for i in range(L):
            anglesn[i] = angles[i, idx[i]]
        return anglesn
    else:
        L = idx.shape[0]
        anglesn = np.zeros(L, dtype=np.float64)
        for i in range(L):
            anglesn[i] = angles[0, idx[i]]
        return anglesn

def bps_twostage(E, Mtestangles, symbols, N , B=4, method="pyx", **kwargs):
    """
    Perform a blind phase search phase recovery using two stages after _[1]

    Parameters
    ----------

    E           : array_like
        input signal (single polarisation)

    Mtestangles : int
        number of initial test angles to try

    symbols     : array_like
        the symbols of the modulation format

    N           : int
        block length to use for averaging

    B           : int, optional
        number of second stage test angles

    method      : string, optional
        implementation method to use has to be "af" for arrayfire (uses OpenCL) or "pyx" for a Cython-OpenMP based parallel search.

    **kwargs    :
        arguments to be passed to the search function

    Returns
    -------
    Eout    : array_like
        phase compensated field
    ph      : array_like
        unwrapped angle from phase recovery

    References
    ----------
    ..[1] Qunbi Zhuge and Chen Chen and David V. Plant, Low Computation Complexity Two-Stage Feedforward Carrier Recovery Algorithm for M-QAM, Optical Fiber Communication Conference (OFC, 2011)
    """
    if method.lower() == "pyx":
        bps_fct = _bps_idx_pyx
    elif method.lower() == "af":
        bps_fct = _bps_idx_af
    else:
        raise("Method needs to be 'pyx' or 'af'")
    angles = np.linspace(-np.pi/4, np.pi/4, Mtestangles, endpoint=False).reshape(1,-1)
    idx = bps_fct(E, angles, symbols, N, **kwargs)
    ph = select_angles(angles, idx)
    b = np.linspace(-B/2, B/2, B)
    phn = ph[:,np.newaxis] + b[np.newaxis,:]/(B*Mtestangles)*np.pi/2
    idx2 = bps_fct(E, phn, symbols, N, **kwargs)
    phf = select_angles(phn, idx2)
    angles_adj = np.unwrap(phf*4, discont=np.pi*4/4)/4
    En = E*np.exp(1.j*angles_adj)
    return En, angles_adj

def viterbiviterbi_qpsk(N, E):
    """
    Viterbi-Viterbi blind phase recovery for QPSK signal

    Parameters
    ----------
    N : int
        number of samples to average over
    E : array_like
        the electric field of the signal

    Returns
    -------
    Eout : array_like
        Field with compensated phases
    """
    return viterbiviterbi_gen(N, E, 4)


def viterbiviterbi_bpsk(N, E):
    """
    Viterbi-Viterbi for BPSK signal

    Parameters
    ----------
    N : int
        number of samples to average over
    E : array_like
        the electric field of the signal

    Returns
    -------
    Eout : array_like
        Field with compensated phases
    """
    return viterbiviterbi_gen(N, E, 2)


def __findmax_16QAM(rk, ci, vk):
    mkk = np.real(rk * np.conj(ci) * np.conj(vk) - abs(ci)**2 / 2)
    pk = np.argmax(mkk)
    return ci[pk]


def ML_phase_16QAM(X, Y, pix, piy, cfactor):
    """
    Maximum-likelihood phase recovery for 16-QAM signal
    using pilots for starting the estimator on a dual-pol 16 QAM signal.

    Parameters
    ----------
    X : array_like
        X polarisation of the input signal field
    Y : array_like
        Y polarisation of the input signal field
    pix : array_like
        Known pilot data (X polarisation)
    piy : array_like
        Known pilot data (Y polarisation)

    Returns
    -------
    RecoveredX : array_like
        Phase recovered signal field (X polarisation)
    RecoveredY : array_like
        Phase recovered signal field (Y polarisation)
    """
    N = len(X)
    cfactor = len(pix)
    pilotX = np.zeros(N, dtype=np.complex)
    pilotY = np.zeros(N, dtype=np.complex)
    pilotX[:cfactor] = pix
    pilotY[:cfactor] = piy
    pcoeX = np.zeros(N, dtype=np.complex)
    pcoeY = np.zeros(N, dtype=np.complex)
    pcoeX[:cfactor] = np.angle(np.conj(pilotX[:cfactor]) * X[:cfactor])
    pcoeY[:cfactor] = np.angle(np.conj(pilotY[:cfactor]) * Y[:cfactor])
    for k in range(cfactor, N):
        pcoeX[k] = np.angle(
            np.sum(np.conj(pilotX[k - cfactor:k]) * X[k - cfactor:k]))
        pcoeY[k] = np.angle(
            np.sum(np.conj(pilotY[k - cfactor:k]) * Y[k - cfactor:k]))
        pilotX[k] = __findmax_16QAM(X[k], SYMBOLS_16QAM,\
                    np.sum(np.conj(pilotX[k-cfactor:k])*X[k-cfactor:k])/\
                    np.sum(np.abs(pilotX[k-cfactor:k])**2))
        pilotY[k] = __findmax_16QAM(Y[k], SYMBOLS_16QAM,
                    np.sum(np.conj(pilotY[k-cfactor:k])*Y[k-cfactor:k])/\
                    np.sum(np.abs(pilotY[k-cfactor:k])**2))
    return X * np.exp(-1.j * pcoeX), Y * np.exp(-1.j * pcoeY)


def partition_16QAM(E):
    r"""Partition a 16-QAM signal into the inner and outer circles.

    Separates a 16-QAM signal into the inner and outer rings, which have
    different phase orientations. Detailed in _[1].

    Parameters
    ----------
        E : array_like
            electric field of the signal

    Returns
    -------
        class1_mask : array_like
            A mask designating the class 1 symbols which are the smallest and
            largest rings.
        class2_mask : array_like
            A mask designating the class 2 symbols which lie on the middle ring

    References
    ----------
    .. [1] R. Muller and D. D. A. Mello, “Phase-offset estimation for
       joint-polarization phase-recovery in DP-16-QAM systems,” Photonics
       Technol. Lett. …, vol. 22, no. 20, pp. 1515–1517, 2010.
    """

    S0 = cal_S0(E, 1.32)
    inner = (np.sqrt(S0 / 5) + np.sqrt(S0)) / 2.
    outer = (np.sqrt(9 * S0 / 5) + np.sqrt(S0)) / 2.
    Ea = abs(E)
    class1_mask = (Ea < inner) | (Ea > outer)
    class2_mask = ~class1_mask
    return class1_mask, class2_mask


def ff_Phase_recovery_16QAM(E, Nangles, Nsymbols):
    phi = np.linspace(0, np.pi, Nangles)
    N = len(E)
    d = (abs(E[:, np.newaxis, np.newaxis] * np.exp(1.j * phi)[:, np.newaxis] -
             SYMBOLS_16QAM)**2).min(axis=2)
    phinew = np.zeros(N - Nsymbols, dtype=np.float)
    for k in range(Nsymbols, N - Nsymbols):
        phinew[k] = phi[np.sum(d[k - Nsymbols:k + Nsymbols], axis=0).argmin()]
    return E[Nsymbols:] * np.exp(1.j * phinew)


def QPSK_partition_phase_16QAM(Nblock, E):
    r"""16-QAM blind phase recovery using QPSK partitioning.

    A blind phase estimator for 16-QAM signals based on partitioning the signal
    into 3 rings, which are then phase estimated using traditional V-V phase
    estimation after Fatadin et al _[1].

    Parameters
    ----------
        Nblock : int
            number of samples in an averaging block
        E : array_like
            electric field of the signal

    Returns
    -------
        E_rec : array_like
            electric field of the signal with recovered phase.

    References
    ----------
    .. [1] I. Fatadin, D. Ives, and S. Savory, “Laser linewidth tolerance
       for 16-QAM coherent optical systems using QPSK partitioning,”
       Photonics Technol. Lett. IEEE, vol. 22, no. 9, pp. 631–633, May 2010.

    """
    dphi = np.pi / 4 + np.arctan(1 / 3)
    L = len(E)
    # partition QPSK signal into qpsk constellation and non-qpsk const
    c1_m, c2_m = partition_16QAM(E)
    Sx = np.zeros(len(E), dtype=np.complex128)
    Sx[c2_m] = (E[c2_m] * np.exp(1.j * dphi))**4
    So = np.zeros(len(E), dtype=np.complex128)
    So[c2_m] = (E[c2_m] * np.exp(1.j * -dphi))**4
    S1 = np.zeros(len(E), dtype=np.complex128)
    S1[c1_m] = (E[c1_m])**4
    E_n = np.zeros(len(E), dtype=np.complex128)
    phi_est = np.zeros(len(E), dtype=np.float64)
    for i in range(0, L, Nblock):
        S1_sum = np.sum(S1[i:i + Nblock])
        Sx_tmp = np.min(
            [S1_sum - Sx[i:i + Nblock], S1_sum - So[i:i + Nblock]],
            axis=0)[c2_m[i:i + Nblock]]
        phi_est[i:i + Nblock] = np.angle(S1_sum + Sx_tmp.sum())
    phi_est = np.unwrap(phi_est) / 4 - np.pi / 4
    return (E * np.exp(-1.j * phi_est))[:(L // Nblock) * Nblock]


def find_freq_offset(sig, os=1, average_over_modes = False, fft_size = 4096):
    """
    Find the frequency offset by searching in the spectrum of the signal
    raised to 4. Doing so eliminates the modulation for QPSK but the method also
    works for higher order M-QAM.

    Parameters
    ----------
        sig : array_line
            signal array with N modes
        os: int
            oversampling ratio (Samples per symbols in sig)
        average_over_modes : bool
            Using the field in all modes for estimation

        fft_size: array
            Size of FFT used to estimate. Should be power of 2, otherwise the
            next higher power of 2 will be used.

    Returns
    -------
        freq_offset : int
            found frequency offset

    """
    if not((np.log2(fft_size)%2 == 0) | (np.log2(fft_size)%2 == 1)):
        fft_size = 2**(np.ceil(np.log2(fft_size)))

    # Fix number of stuff
    sig = np.atleast_2d(sig)
    npols = sig.shape[0]

    # Find offset for all modes
    freq_sig = np.zeros([npols,fft_size])
    for l in range(npols):
        freq_sig[l,:] = np.abs(np.fft.fft(sig[l,:]**4,fft_size))**2

    # If selected, sum over all
    if average_over_modes:
        freq_sig[:,:] = np.sum(freq_sig,axis = 0)

    # Extract corresponding FO
    freq_offset = np.zeros([npols,1])
    freq_vector = np.fft.fftfreq(fft_size,1/os)/4
    for k in range(npols):
        max_freq_bin = np.argmax(freq_sig[k,:])
        freq_offset[k,0] = freq_vector[max_freq_bin]

    return freq_offset

def comp_freq_offset(sig, freq_offset, os=1 ):
    """
    Compensate for frequency offset in signal

    Parameters
    ----------
        sig : array_line
            signal array with N modes
        freq_offset: array_like
            frequency offset to compensate for if 1D apply to all modes
        os: int
            oversampling ratio (Samples per symbols in sig)


    Returns
    -------
        comp_signal : array with N modes
            input signal with removed frequency offset

    """
    # Fix number of stuff
    sig = np.atleast_2d(sig)
    freq_offset = np.atleast_2d(freq_offset)
    npols = sig.shape[0]

    # Output Vector
    comp_signal = np.zeros([1,np.shape(sig)[1]],dtype=complex)

    # Fix output
    sig_len = len(sig[0,:])
    lin_phase = np.arange(1,sig_len + 1,dtype = float)
    for l in range(npols):
        lin_phase = 2 * np.pi * freq_offset[l] /  os
        comp_signal[l] = sig[l] * np.exp(-1j * lin_phase)

    return comp_signal
