// firmware/uno_q_main/uno_q_main.ino
// EdgeGuard — Arduino UNO Q (STM32U585)
//
// Optimized for 4GB UNO Q using:
// 1. 32-byte aligned buffers for DMA/serialization efficiency.
// 2. High-priority dedicated thread for jitter-free 400Hz sampling.
// 3. 100-sample batching to maximize internal UART throughput.

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_LIS3DH.h>
#include <Adafruit_Sensor.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Arduino_RouterBridge.h>
#include <IWatchdog.h>
#include "config.h"

// ── Payload (must match src/schema.py) ─────────────────────────────
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

// FIX: Aligned to 32 bytes for cache/DMA efficiency during MessagePack serialization
__attribute__((aligned(32))) SensorPayload batch[FIFO_WATERMARK];

uint32_t           seq_counter      = 0;
float              last_temp_c      = 25.0f;
bool               temp_req_pending = false;
uint32_t           temp_req_ms      = 0;

// Binary semaphore for thread synchronization
// On UNO Q / Zephyr, we can use a Semaphore for thread-safe signaling
struct k_sem       fifo_sem;

// ── FIFO watermark ISR ────────────────────────────────────────────────────
void onFifoWatermark() {
    // Release semaphore from ISR
    k_sem_give(&fifo_sem);
}

// ── Acquisition Thread ────────────────────────────────────────────────────
// Stack size and priority (lower number = higher priority)
#define ACQ_STACK_SIZE 2048
#define ACQ_PRIORITY   5

void acq_thread_func(void *p1, void *p2, void *p3) {
    uint8_t batch_offset = 0;

    while (true) {
        // Wait for ISR to signal that hardware FIFO has SAMPLES_PER_IRQ (25) samples
        k_sem_take(&fifo_sem, K_FOREVER);

        uint8_t fifo_src = lis.readRegister8(LIS3DH_REG_FIFOSRC);
        if (fifo_src & 0x40) {
            // Reset FIFO on overflow and discard current partial batch
            lis.writeRegister8(LIS3DH_REG_FIFOCTRL, 0x00);
            lis.writeRegister8(LIS3DH_REG_FIFOCTRL, (0x01 << 6) | (SAMPLES_PER_IRQ & 0x1F));
            batch_offset = 0;
            continue;
        }

        for (uint8_t i = 0; i < SAMPLES_PER_IRQ; i++) {
            sensors_event_t event{};
            uint8_t idx = batch_offset + i;

            if (!lis.getEvent(&event)) {
                batch[idx].timestamp_us = micros();
                batch[idx].sequence_id  = seq_counter++;
                batch[idx].accel_x = batch[idx].accel_y = batch[idx].accel_z = batch[idx].board_temp = NAN;
                continue;
            }

            batch[idx].timestamp_us = micros();
            batch[idx].sequence_id  = seq_counter++;
            batch[idx].accel_x      = event.acceleration.x;
            batch[idx].accel_y      = event.acceleration.y;
            batch[idx].accel_z      = event.acceleration.z;
            batch[idx].board_temp   = last_temp_c;
        }

        batch_offset += SAMPLES_PER_IRQ;

        // Only notify MPU when we have accumulated a full batch (e.g. 100 samples)
        if (batch_offset >= FIFO_WATERMARK) {
            Bridge.notify("sensor_batch", (uint8_t*)batch, sizeof(batch));
            batch_offset = 0;
        }
    }
}

K_THREAD_STACK_DEFINE(acq_stack, ACQ_STACK_SIZE);
struct k_thread acq_thread_data;

// ── setup ─────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);

    IWatchdog.begin(IWDG_TIMEOUT_US);

    Bridge.begin();
    IWatchdog.reload();

    LIS3DH_WIRE.begin();
    LIS3DH_WIRE.setClock(400000);
    LIS3DH_WIRE.setTimeout(I2C_TIMEOUT_MS);

    if (!lis.begin(LIS3DH_ADDR, &LIS3DH_WIRE)) {
        Serial.println("[EdgeGuard] FATAL: LIS3DH not found.");
        while (true) { /* IWDG will reset */ }
    }

    lis.setDataRate(LIS3DH_DATARATE_400_HZ);
    lis.setRange(LIS3DH_RANGE_8_G);

    // FIFO setup
    uint8_t ctrl5 = lis.readRegister8(LIS3DH_REG_CTRL5);
    lis.writeRegister8(LIS3DH_REG_CTRL5, ctrl5 | 0x40);
    // Note: LIS3DH supports 32 levels. We trigger an interrupt every SAMPLES_PER_IRQ (25) samples.
    // The acquisition thread accumulates multiple IRQs into a larger 100-sample Bridge batch.
    lis.writeRegister8(LIS3DH_REG_FIFOCTRL, (0x01 << 6) | (SAMPLES_PER_IRQ & 0x1F));

    uint8_t ctrl3 = lis.readRegister8(LIS3DH_REG_CTRL3);
    lis.writeRegister8(LIS3DH_REG_CTRL3, ctrl3 | 0x04);

    pinMode(LIS3DH_INT1_PIN, INPUT);
    attachInterrupt(digitalPinToInterrupt(LIS3DH_INT1_PIN), onFifoWatermark, RISING);

    // Init semaphore
    k_sem_init(&fifo_sem, 0, 1);

    // Start acquisition thread
    k_thread_create(&acq_thread_data, acq_stack,
                    K_THREAD_STACK_SIZEOF(acq_stack),
                    acq_thread_func, NULL, NULL, NULL,
                    ACQ_PRIORITY, 0, K_NO_WAIT);

    delay(100); 
    tempSensor.begin();
    tempSensor.setResolution(12);
    tempSensor.setWaitForConversion(false);
    tempSensor.requestTemperatures();
    temp_req_pending = true;
    temp_req_ms      = millis();

    IWatchdog.reload();
    Serial.println("[EdgeGuard] UNO Q Booted. High-throughput acquisition thread running.");
}

// ── loop ──────────────────────────────────────────────────────────────────
void loop() {
    // loop() handles lower priority tasks like temperature updates
    if (temp_req_pending && (millis() - temp_req_ms >= DS18B20_CONV_MS)) {
        float t = tempSensor.getTempCByIndex(0);
        if (t > -100.0f) last_temp_c = t;
        temp_req_pending = false;
        tempSensor.requestTemperatures();
        temp_req_pending = true;
        temp_req_ms      = millis();
    }
    
    // Periodically reload watchdog
    IWatchdog.reload();
    delay(10);
}
