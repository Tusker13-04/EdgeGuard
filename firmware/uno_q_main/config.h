#ifndef CONFIG_H
#define CONFIG_H

// --- Sampling & FIFO -----------------------------------------------
#define FIFO_WATERMARK       100
#define SAMPLES_PER_IRQ       25
#define IWDG_TIMEOUT_US  4000000

// --- Phase 1: Reflex Layer -----------------------------------------
// Pin toggled immediately when EI model fires anomaly (us-latency reflex)
#define REFLEX_ALERT_PIN      D2

// Raw RMS fallback threshold (used when EI model is absent)
// Units: mg (milli-g)
#define REFLEX_THRESHOLD    2000

// --- Phase 3: Remote Tuning (Bridge command string) ----------------
// MPU sends Bridge.put(REMOTE_TUNE_CMD, "{\"threshold\":1800}")
// MCU's onCommand handler parses and hot-patches REFLEX_THRESHOLD at runtime
#define REMOTE_TUNE_CMD  "remote_tune"

#endif // CONFIG_H
