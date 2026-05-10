# Future Ideas — Parked

These are engineering ideas that are valid but **not required to win**.
Do not implement unless the demo is stable, docs are finished, and the video is done.

---

## Thermal-Accelerometer Drift Compensation

**Concept:**
The LIS3DH zero-g level drifts ~0.5 mg/°C (Table 4, ST datasheet).
The embedded temperature sensor (1°C resolution, `OUT_ADC3`) could be used to
apply a first-order baseline correction to raw accelerometer readings before
feeding them into the ML window.

**Why it was parked:**
At 1°C granularity and a small DC motor producing large vibration deltas,
the drift correction signal is below the noise floor of what matters for
anomaly detection. Adds complexity without improving demo reliability.

**Implementation sketch (if ever needed):**
```python
T_ref   = 25.0        # °C at calibration
TCOff   = 0.5e-3      # g/°C from LIS3DH datasheet Table 4
bias    = (current_T - T_ref) * TCOff
accel_x_corrected = accel_x_raw - bias
```

**Prerequisite before picking this up:**
- Demo is fully stable end-to-end
- Dashboard is polished and live
- Video is recorded
- README is complete

---

## Thermal-Vibration Cross-Correlation Feature

**Concept:**
Correlate rolling RMS vibration with rolling mean board temperature across
multiple inference windows. A rising correlation is a secondary fault signature.

**Why it was parked:**
Same reason — adds ML feature engineering overhead with marginal demo value.
The primary vibration anomaly score already tells the story cleanly.

---

## Multi-Window Covariance Matrix

**Concept:**
Compute per-window covariance between accel axes and temperature to build
a richer feature vector for the LSTM.

**Why it was parked:**
Overengineering. A 3-axis RMS + spectral feature vector already separates
normal vs imbalance with high confidence on small motors.
