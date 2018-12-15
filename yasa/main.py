"""
YASA main functions
"""
import numpy as np
import pandas as pd
from numba import jit
from scipy import signal
from scipy.fftpack import next_fast_len
from mne.filter import filter_data, resample
from scipy.interpolate import interp1d, interp2d

__all__ = ['spindles_detect', 'stft_power', 'moving_transform',
           'get_bool_vector']


#############################################################################
# NUMBA JIT UTILITY FUNCTIONS
#############################################################################


@jit('float64(float64[:], float64[:])', nopython=True)
def _corr(x, y):
    """Fast Pearson correlation."""
    mx, my = x.mean(), y.mean()
    xm, ym = x - mx, y - my
    r_num = (xm * ym).sum()
    r_den = np.sqrt((xm**2).sum()) * np.sqrt((ym**2).sum())
    return r_num / r_den


@jit('float64(float64[:], float64[:])', nopython=True)
def _covar(x, y):
    """Fast Covariance."""
    n = x.size
    mx, my = x.mean(), y.mean()
    xm, ym = x - mx, y - my
    cov = (xm * ym).sum()
    return cov / (n - 1)


@jit('float64(float64[:])', nopython=True)
def _rms(x):
    """Fast root mean square."""
    return np.sqrt((x**2).mean())

#############################################################################
# HELPER FUNCTIONS
#############################################################################


def moving_transform(x, y=None, sf=100, window=.3, step=.1, method='corr',
                     interp=False):
    """Moving transformation of one or two time-series.

    Parameters
    ----------
    x : array_like
        Single-channel data
    y : array_like, optional
        Second single-channel data (only used if method in ['corr', 'covar']).
    sf : float
        Sampling frequency.
    window : int
        Window size in seconds.
    step : int
        Step in seconds.
        A step of 0.1 second (100 ms) is usually a good default.
        If step == 0, overlap at every sample (slowest)
        If step == nperseg, no overlap (fastest)
        Higher values = higher precision = slower computation.
    method : str
        Transformation to use.
        Available methods are::

            'rms' : root mean square of x
            'corr' : Correlation between x and y
            'covar' : Covariance between x and y
    interp : boolean
        If True, a cubic interpolation is performed to ensure that the output
        is the same size as the input (= pointwise power).

    Returns
    -------
    t : np.array
        Time vector
    out : np.array
        Transformed signal
    """
    # Safety checks
    assert method in ['covar', 'corr', 'rms']
    x = np.asarray(x, dtype=np.float64)
    if y is not None:
        y = np.asarray(y, dtype=np.float64)
        assert x.size == y.size

    if step == 0:
        step = 1 / sf

    halfdur = window / 2
    n = x.size
    total_dur = n / sf
    last = n - 1
    idx = np.arange(0, total_dur, step)
    out = np.zeros(idx.size)

    # Define beginning, end and time (centered) vector
    beg = ((idx - halfdur) * sf).astype(int)
    beg[beg < 0] = 0
    end = ((idx + halfdur) * sf).astype(int)
    end[end > last] = last
    t = np.vstack((end, beg)).mean(0) / sf

    if method == 'covar':
        def func(x, y):
            return _covar(x, y)

    elif method == 'corr':
        def func(x, y):
            return _corr(x, y)

    else:
        def func(x):
            return _rms(x)

    # Now loop over successive epochs
    if method in ['covar', 'corr']:
        for i in range(idx.size):
            out[i] = func(x[beg[i]:end[i]], y[beg[i]:end[i]])
    else:
        for i in range(idx.size):
            out[i] = func(x[beg[i]:end[i]])

    # Finally interpolate
    if interp and step != 1 / sf:
        f = interp1d(t, out, kind='cubic',
                     bounds_error=False,
                     fill_value=0)
        t = np.arange(n) / sf
        out = f(t)

    return t, out


def stft_power(data, sf, window=4, step=.1, band=(0.5, 30), interp=True,
               norm=False):
    """Compute the pointwise power via STFT and interpolation.

    Parameters
    ----------
    data : array_like
        Single-channel data
    window : int
        Window size in seconds for STFT.
        2 or 4 seconds are usually a good default.
        Higher values = higher frequency resolution.
    step : int
        Step in seconds for the STFT.
        A step of 0.1 second (100 ms) is usually a good default.
        If step == 0, overlap at every sample (slowest)
        If step == nperseg, no overlap (fastest)
        Higher values = higher precision = slower computation.
    band : tuple or None
        Broad band frequency of interest.
        Default is 0.5 to 30 Hz.
    interp : boolean
        If True, a cubic interpolation is performed to ensure that the output
        is the same size as the input (= pointwise power).
    norm : bool
        If True, return bandwise normalized band power, i.e. for each time
        point, the sum of power in all the frequency bins equals 1.

    Returns
    -------
    f : ndarray
        Frequency vector
    t : ndarray
        Time vector
    Sxx : ndarray
        Power in the specified frequency bins of shape (f, t)
    """
    # Safety check
    data = np.asarray(data)
    assert step <= window
    assert band[0] < band[1]

    step = 1 / sf if step == 0 else step

    # Define STFT parameters
    nperseg = int(window * sf)
    noverlap = int(nperseg - (step * sf))

    # Compute STFT and remove the last epoch
    f, t, Sxx = signal.stft(data, sf, nperseg=nperseg, noverlap=noverlap,
                            detrend='constant', padded=True)

    # Let's keep only the frequency of interest
    if band is not None:
        idx_band = np.logical_and(f >= band[0], f <= band[1])
        f = f[idx_band]
        Sxx = Sxx[idx_band, :]

    # Compute power
    Sxx = np.square(np.abs(Sxx))

    # Interpolate
    if interp:
        func = interp2d(t, f, Sxx, kind='cubic')
        t = np.arange(data.size) / sf
        Sxx = func(t, f)

    if norm:
        sum_pow = Sxx.sum(0).reshape(1, -1)
        np.divide(Sxx, sum_pow, out=Sxx)
    return f, t, Sxx


def _events_distance_fill(index, min_distance_ms, sf):
    """Merge events that are too close in time.

    Parameters
    ----------
    index : array_like
        Indices of supra-threshold events.
    min_distance_ms : int
        Minimum distance (ms) between two events to consider them as two
        distinct events
    sf : float
        Sampling frequency of the data (Hz)

    Returns
    -------
    f_index : array_like
        Filled (corrected) Indices of supra-threshold events

    Notes
    -----
    Original code from the Visbrain package.
    """
    # Convert min_distance_ms
    min_distance = min_distance_ms / 1000. * sf
    idx_diff = np.diff(index)
    condition = idx_diff > 1
    idx_distance = np.where(condition)[0]
    distance = idx_diff[condition]
    bad = idx_distance[np.where(distance < min_distance)[0]]
    # Fill gap between events separated with less than min_distance_ms
    if len(bad) > 0:
        fill = np.hstack([np.arange(index[j] + 1, index[j + 1])
                          for i, j in enumerate(bad)])
        f_index = np.sort(np.append(index, fill))
        return f_index
    else:
        return index


def _index_to_events(x):
    """Convert a 2D (start, end) array into a continuous one.

    Parameters
    ----------
    x : array_like
        2D array of indicies.

    Returns
    -------
    index : array_like
        Continuous array of indices.

    Notes
    -----
    Original code from the Visbrain package.
    """
    index = np.array([])
    for k in range(x.shape[0]):
        index = np.append(index, np.arange(x[k, 0], x[k, 1] + 1))
    return index.astype(int)


def get_bool_vector(data, sf, sp):
    """Return a Boolean vector given the original data and sf and
    a YASA's detection dataframe.

    Parameters
    ----------
    data : array_like
        Single-channel EEG data.
    sf : float
        Sampling frequency of the data.
    sp : pandas DataFrame
        YASA's detection dataframe returned by the spindles_detect function.

    Returns
    -------
    bool_vector : array
        Array of bool indicating for each sample in data if this sample is
        part of a spindle (True) or not (False).
    """
    data = np.asarray(data)
    assert isinstance(sp, pd.DataFrame)
    assert 'Start' in sp.keys()
    assert 'End' in sp.keys()
    idx_sp = _index_to_events(sp[['Start', 'End']].values * sf)
    bool_spindles = np.zeros(data.size, dtype=int)
    bool_spindles[idx_sp] = 1
    return bool_spindles

#############################################################################
# MAIN FUNCTIONS
#############################################################################


def spindles_detect(data, sf, freq_sp=(11, 16), duration=(0.3, 2.5),
                    freq_broad=(0.5, 30), min_distance=500,
                    thresh={'abs_pow': 1.25, 'rel_pow': 0.20, 'rms': 95,
                    'corr': 0.69}):
    """Spindles detection using a custom algorithm based on
    Lacourse et al. 2018.

    This script will be more precise if applied only on artefact-free
    NREM epochs. However, it should also work relatively well with full-night
    recordings.

    Parameters
    ----------
    data : array_like
        Single-channel data
    sf : float
        Sampling frequency of the data in Hz.
    freq_sp : tuple or list
        Spindles frequency range. Default is 11 to 16 Hz.
    freq_broad : tuple or list
        Broad band frequency of interest.
        Default is 0.5 to 30 Hz.
    min_distance : int
        If two spindles are closer than min_distance (in ms), they are merged
        into a single spindles. Default is 500 ms.
    thresh : dict
        Detection thresholds::

            'abs_pow' : Absolute log10(power) of the sigma-filtered signal.
            'rel_pow' : Relative power (= power ratio freq_sp / freq_broad).
            'rms' : Percentile of the sigma-filtered moving RMS signal.
            'corr' : Pearson correlation coefficient.

    Returns
    -------
    sp_params : pd.DataFrame
        Pandas DataFrame::

            'Start' : start time of each detected spindles (in seconds)
            'End' : end time (in seconds)
            'Duration' : Duration (in seconds)
            'Amplitude' : amplitude, in uV
            'RMS' : Root-mean-square, in uV
            'AbsPower' : Mean absolute power (log10 uV^2)
            'RelPower' : Mean relative power (% uV^2)
            'Frequency' : Median frequency, in Hz
            'Oscillations' : Number of oscillations (peaks)
            'Symmetry' : Symmetry index, from 0 to 1
            'Confidence' : Detection confidence ('high' or 'medium')
    """
    # Safety check
    data = np.asarray(data)
    assert freq_sp[0] < freq_sp[1]
    assert freq_broad[0] < freq_broad[1]

    # Downsample to 100 Hz
    if sf >= 200:
        fac = 100 / sf
        data = resample(data, up=fac, down=1.0, npad='auto', axis=-1,
                        window='boxcar', n_jobs=1, pad='reflect_limited',
                        verbose=False)
        sf = 100

    # Bandpass filter
    data = filter_data(data, sf, freq_broad[0], freq_broad[1], method='fir',
                       verbose=0)

    # If freq_sp is not too narrow, we add and remove 0.5 Hz to adjust the
    # FIR filter transition band
    trans = 1 if freq_sp[1] - freq_sp[0] > 3 else 0
    data_sigma = filter_data(data, sf, freq_sp[0] + trans, freq_sp[1] - trans,
                             method='fir', verbose=0)

    # Compute the pointwise relative power using interpolated STFT
    f, _, Sxx = stft_power(data, sf, window=2, step=.05, band=freq_broad)
    idx_sigma = np.logical_and(f >= freq_sp[0], f <= freq_sp[1])
    rel_pow = Sxx[idx_sigma].sum(0) / Sxx.sum(0)

    # Now we apply moving RMS and correlation on the sigma-filtered signal
    _, mcorr = moving_transform(data_sigma, data, sf, .3, .1, method='corr',
                                interp=True)
    _, mrms = moving_transform(data_sigma, data, sf, .3, .1, method='rms',
                               interp=True)
    # We compute the absolute power using the mean-square, as in Lacourse 2018
    # Note that we could also use the STFT above (Sxx[idx_sigma].sum(0)).
    mms = np.square(mrms)
    mms[mms <= 0] = 0.000000001
    abs_pow_log = np.log10(mms)

    # Hilbert power (to define the instantaneous frequency)
    n = data_sigma.size
    nfast = next_fast_len(n)
    phase_sigma = np.angle(signal.hilbert(data_sigma, N=nfast)[:n])
    # inst_freq = sf / 2pi * 1st-derivative of the phase of the analytic signal
    inst_freq = (sf / (2 * np.pi) * np.diff(phase_sigma))

    # Let's define the thresholds
    idx_abs_pow = (abs_pow_log >= thresh['abs_pow']).astype(int)
    idx_rel_pow = (rel_pow >= thresh['rel_pow']).astype(int)
    idx_mcorr = (mcorr >= thresh['corr']).astype(int)
    idx_mrms = (mrms >= np.percentile(mrms, thresh['rms'])).astype(int)
    idx_sum = (idx_abs_pow + idx_rel_pow + idx_mcorr + idx_mrms).astype(int)

    # For debugging
    # print('Abs pow', np.sum(idx_abs_pow))
    # print('Rel pow', np.sum(idx_rel_pow))
    # print('Corr', np.sum(idx_mcorr))
    # print('RMS %', np.sum(idx_mrms))

    # Find indices that matches at least 3 out of four 4 criteria
    where_sp = np.where(idx_sum >= 3)[0]

    # If no events are found, return an empty dataframe
    if not len(where_sp):
        print('No spindle were found in data. Returning None.')
        return None

    # Merge events that are too close
    if min_distance is not None and min_distance > 0:
        where_sp = _events_distance_fill(where_sp, min_distance, sf)

    # Extract start and end of each spindles
    sp = np.split(where_sp, np.where(np.diff(where_sp) != 1)[0] + 1)

    # Extract start, end, and duration of each spindle
    idx_start_end = np.array([[k[0], k[-1]] for k in sp]) / sf
    sp_start, sp_end = idx_start_end.T
    sp_dur = sp_end - sp_start

    # Find events with bad duration
    good_dur = np.logical_and(sp_dur > duration[0], sp_dur < duration[1])

    # If no events of good duration are found, return an empty dataframe
    if all(~good_dur):
        print('No spindle were found in data. Returning None.')
        return None

    # Extract the peak-to-peak amplitude and frequency
    n_sp = len(sp)
    sp_amp = np.zeros(n_sp)
    sp_freq = np.zeros(n_sp)
    sp_rms = np.zeros(n_sp)
    sp_osc = np.zeros(n_sp)
    sp_sym = np.zeros(n_sp)
    sp_abs = np.zeros(n_sp)
    sp_rel = np.zeros(n_sp)
    sp_conf = ["" for x in range(n_sp)]

    # Number of oscillations (= number of peaks separated by at least 60 ms)
    distance = 60 * sf / 1000

    for i in np.arange(len(sp))[good_dur]:
        # Important: detrend the signal to avoid wrong peak-to-peak amplitude
        sp_det = signal.detrend(data[sp[i]])
        sp_amp[i] = np.ptp(sp_det)  # Peak-to-peak amplitude
        sp_rms[i] = _rms(sp_det)  # Root mean square
        sp_abs[i] = np.mean(abs_pow_log[sp[i]])  # Mean absolute power
        sp_rel[i] = np.mean(rel_pow[sp[i]])  # Mean relative power
        sp_freq[i] = np.median(inst_freq[sp[i]])  # Median frequency
        thresh = np.percentile(idx_sum[sp[i]], 75)
        sp_conf[i] = 'high' if thresh == 4 else 'medium'

        # Number of oscillations
        peaks, peaks_params = signal.find_peaks(sp_det, distance=distance,
                                                prominence=(None, None))
        sp_osc[i] = len(peaks)

        # Symmetry index
        sp_sym[i] = peaks[peaks_params['prominences'].argmax()] / sp_det.size

    # Create a dictionnary
    sp_params = {'Start': sp_start,
                 'End': sp_end,
                 'Duration': sp_dur,
                 'Amplitude': sp_amp,
                 'RMS': sp_rms,
                 'AbsPower': sp_abs,
                 'RelPower': sp_rel,
                 'Frequency': sp_freq,
                 'Oscillations': sp_osc,
                 'Symmetry': sp_sym,
                 'Confidence': sp_conf}

    return pd.DataFrame.from_dict(sp_params)[good_dur]
