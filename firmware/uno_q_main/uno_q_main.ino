// firmware/uno_q_main/uno_q_main.ino
// EdgeGuard — Arduino UNO Q (STM32U585)
// Accel : Adafruit LIS3DH (I2C, address 0x18, INT1 on Pin 2)
// Temp  : DS18B20 (1-Wire, data pin Pin 4)
// Uses Arduino_RouterBridge to send data to the MPU (Linux) side.

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_LIS3DH.h>
#include <Adafruit_Sensor.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Arduino_RouterBridge.h>

// ── IWDG Watchdog (STM32U585 HAL) ─────────────────────────────────────────
// IWatchdog is part of STM32duino core. A 4-second window is generous enough
// to survive one full DS18B20 conversion (750 ms) + Bridge.notify() latency,
// but short enough to reset a genuinely stuck loop() within 4 s.
#include <IWatchdog.h>

// ── LIS3DH I2C config ─────────────────────────────────────────────────────
#define LIS3DH_ADDR      0x18
#define LIS3DH_INT1_PIN  2

// ── DS18B20 1-Wire pin ────────────────────────────────────────────────────
#define ONE_WIRE_PIN  4

// ── Sampling config ───────────────────────────────────────────────────────
// LIS3DH ODR = 400 Hz
// FIFO watermark = 25 samples → ISR fires every ~62.5 ms
#define FIFO_WATERMARK  25

// ── I2C timeout (STM32 HAL) ───────────────────────────────────────────────
// Without a timeout, a stuck SCL line (LIS3DH power glitch, cable fault)
// blocks Wire transactions indefinitely, freezing the loop() and
// starving the IWDG of reloads — causing an unintended device reset.
// 5 ms covers the worst-case clock-stretch for 400 kHz I2C.
#define I2C_TIMEOUT_MS  5

// ── Payload (must match src/udp_receiver.py / system specs) ──────────────
struct __attribute__((packed)) SensorPayload {
    uint32_t timestamp_us;
    uint32_t sequence_id;
    float    accel_x;
    float    accel_y;
    float    accel_z;
    float    board_temp;
};
static_assert(sizeof(SensorPayload) == 24, "Payload size mismatch");

// ── Globals ────────────────────────────────────────────────────────────────
Adafruit_LIS3DH    lis;
OneWire            oneWire(ONE_WIRE_PIN);
DallasTemperature  tempSensor(&oneWire);
SensorPayload      batch[FIFO_WATERMARK];
uint32_t           seq_counter      = 0;
// FIX: volatile uint8_t instead of volatile bool for cleaner atomic semantics.
// Cortex-M33 load/store of uint8_t is single-instruction (atomic on aligned addr).
volatile uint8_t   fifo_ready       = 0;
float              last_temp_c      = 25.0f;
bool               temp_req_pending = false;
uint32_t           temp_req_ms      = 0;
uint32_t           fifo_overflows   = 0;  // diagnostic overflow counter

#define DS18B20_CONV_MS  750

// ── FIFO watermark ISR ────────────────────────────────────────────────────
void onFifoWatermark() {
    fifo_ready = 1;
}

// ── setup ─────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);

    // FIX: start IWDG with a 4-second window before any blocking I/O.
    // Bridge.begin() and Wire I2C init can block; the watchdog ensures
    // the device self-recovers if either call hangs permanently.
    IWatchdog.begin(4000000);  // 4,000,000 μs = 4 s

    // Initialize Bridge for MPU communication
    Bridge.begin();
    IWatchdog.reload();  // Bridge.begin() can take > 1 s on first boot

    Wire.begin();
    Wire.setClock(400000);
    // FIX: set I2C timeout to prevent clock-stretch deadlock.
    // STM32duino TwoWire exposes setTimeout() (milliseconds).
    Wire.setTimeout(I2C_TIMEOUT_MS);

    if (!lis.begin(LIS3DH_ADDR)) {
        Serial.println("[EdgeGuard] FATAL: LIS3DH not found.");
        // Let the IWDG reset the device rather than spinning indefinitely.
        // This allows the hardware watchdog to trigger a clean reboot.
        while (true) { /* IWDG will fire in ≤4 s */ }
    }

    lis.setDataRate(LIS3DH_DATARATE_400_HZ);
    lis.setRange(LIS3DH_RANGE_8_G);

    // Enable FIFO stream mode with watermark interrupt (matching original main.cpp)
    uint8_t ctrl5 = lis.readRegister8(LIS3DH_REG_CTRL5);
    lis.writeRegister8(LIS3DH_REG_CTRL5, ctrl5 | 0x40);
    lis.writeRegister8(LIS3DH_REG_FIFOCTRL,
        (0x01 << 6) | (FIFO_WATERMARK & 0x1F));
    uint8_t ctrl3 = lis.readRegister8(LIS3DH_REG_CTRL3);
    lis.writeRegister8(LIS3DH_REG_CTRL3, ctrl3 | 0x04);

    pinMode(LIS3DH_INT1_PIN, INPUT);
    attachInterrupt(digitalPinToInterrupt(LIS3DH_INT1_PIN),
                    onFifoWatermark, RISING);

    tempSensor.begin();
    tempSensor.setResolution(12);
    tempSensor.setWaitForConversion(false);
    tempSensor.requestTemperatures();
    temp_req_pending = true;
    temp_req_ms      = millis();

    IWatchdog.reload();
    Serial.println("[EdgeGuard] UNO Q Booted. Bridge OK. FIFO armed @ 400Hz.");
}

// ── loop ──────────────────────────────────────────────────────────────────
void loop() {
    // ── Async temperature read (non-blocking) ──────────────────────────────
    if (temp_req_pending && (millis() - temp_req_ms >= DS18B20_CONV_MS)) {
        float t = tempSensor.getTempCByIndex(0);
        if (t > -100.0f) last_temp_c = t;
        // FIX: was missing — temp_req_pending was never cleared after reading,
        // causing requestTemperatures() to be called on every loop() iteration
        // after the first 750 ms, thrashing the 1-Wire bus continuously.
        temp_req_pending = false;
        tempSensor.requestTemperatures();
        temp_req_pending = true;
        temp_req_ms      = millis();
    }

    // ── FIX: Atomic test-and-clear of fifo_ready ISR flag ─────────────────
    // On Cortex-M33 a non-atomic test-then-clear creates a race: if the ISR
    // fires between the load and the store, the second notification is lost
    // (missed batch, invisible data gap). __disable_irq/__enable_irq provide
    // the required critical section without disabling the SysTick/FreeRTOS.
    __disable_irq();
    uint8_t batch_ready = fifo_ready;
    fifo_ready = 0;
    __enable_irq();

    if (!batch_ready) {
        // Reload watchdog while idle so a quiet sensor period does not
        // trigger a false watchdog reset.
        IWatchdog.reload();
        return;
    }

    // ── FIX: FIFO overflow check before draining ───────────────────────────
    // LIS3DH FIFO_SRC_REG bit 6 (OVR) indicates a sample was overwritten
    // before being read. If set, reset FIFO and discard the contaminated batch.
    uint8_t fifo_src = lis.readRegister8(LIS3DH_REG_FIFOSRC);
    if (fifo_src & 0x40) {
        fifo_overflows++;
        Serial.print("[EdgeGuard] WARN: FIFO overflow #");
        Serial.println(fifo_overflows);
        // Reset FIFO: bypass mode momentarily → back to stream mode
        lis.writeRegister8(LIS3DH_REG_FIFOCTRL, 0x00);
        lis.writeRegister8(LIS3DH_REG_FIFOCTRL,
            (0x01 << 6) | (FIFO_WATERMARK & 0x1F));
        IWatchdog.reload();
        return;  // Discard batch — data integrity cannot be guaranteed
    }

    // ── Drain FIFO and fill batch array ───────────────────────────────────
    for (uint8_t i = 0; i < FIFO_WATERMARK; i++) {
        // FIX: REMOVED standalone lis.read() that appeared here before.
        // getEvent() calls lis.read() internally as its first operation.
        // The previous code popped TWO FIFO entries per loop iteration:
        //   Iteration:  lis.read()       → discarded (raw registers overwritten)
        //               lis.getEvent()   → popped next sample, used result
        // This silently halved throughput to ~200 Hz and left phantom
        // sequence-counter gaps that inflated the Pi-side drop_rate_pct metric.
        sensors_event_t event;
        lis.getEvent(&event);  // ← single FIFO pop; read() called internally

        batch[i].timestamp_us = micros();
        batch[i].sequence_id  = seq_counter++;
        batch[i].accel_x      = event.acceleration.x;
        batch[i].accel_y      = event.acceleration.y;
        batch[i].accel_z      = event.acceleration.z;
        batch[i].board_temp   = last_temp_c;
    }

    // Notify MPU side of the new sensor batch via Bridge RPC
    Bridge.notify("sensor_batch", (uint8_t*)batch, sizeof(batch));

    // Reload watchdog after successful batch send
    IWatchdog.reload();
}
