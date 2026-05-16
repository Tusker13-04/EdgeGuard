// firmware/uno_q_main/config.h
// Hardware configuration and pinout for EdgeGuard on Arduino UNO Q

#ifndef EDGEGUARD_CONFIG_H
#define EDGEGUARD_CONFIG_H

// ── I2C Bus config ────────────────────────────────────────────────────────
// On UNO Q, the Qwiic connector is on I2C4 (Wire1).
#define LIS3DH_WIRE      Wire1
#define LIS3DH_ADDR      0x18
#define LIS3DH_INT1_PIN  2
#define I2C_TIMEOUT_MS   5

// ── DS18B20 1-Wire config ─────────────────────────────────────────────────
#define ONE_WIRE_PIN     4
#define DS18B20_CONV_MS  750

// ── Sampling config ───────────────────────────────────────────────────────
// LIS3DH ODR = 400 Hz
// Hardware FIFO is 32 samples deep. We trigger an interrupt every 25 samples.
#define SAMPLES_PER_IRQ  25

// We accumulate 4 IRQs (100 samples) before sending a batch to the MPU.
#define FIFO_WATERMARK   100

// ── Watchdog ──────────────────────────────────────────────────────────────
#define IWDG_TIMEOUT_US  4000000  // 4 seconds

#endif // EDGEGUARD_CONFIG_H
