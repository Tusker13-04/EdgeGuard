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

// ── LIS3DH I2C config ─────────────────────────────────────────────────────
#define LIS3DH_ADDR   0x18
#define LIS3DH_INT1_PIN  2

// ── DS18B20 1-Wire pin ────────────────────────────────────────────────────
#define ONE_WIRE_PIN  4

// ── Sampling config ───────────────────────────────────────────────────────
// LIS3DH ODR = 400 Hz
// FIFO watermark = 25 samples → ISR fires every ~62.5 ms
#define FIFO_WATERMARK  25

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
volatile bool      fifo_ready       = false;
float              last_temp_c      = 25.0f;
bool               temp_req_pending = false;
uint32_t           temp_req_ms      = 0;

#define DS18B20_CONV_MS  750

// ── FIFO watermark ISR ────────────────────────────────────────────────────
void onFifoWatermark() {
    fifo_ready = true;
}

// ── setup ─────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    
    // Initialize Bridge for MPU communication
    Bridge.begin();

    Wire.begin();
    Wire.setClock(400000);

    if (!lis.begin(LIS3DH_ADDR)) {
        Serial.println("[EdgeGuard] FATAL: LIS3DH not found.");
        while (true) { delay(100); }
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

    Serial.println("[EdgeGuard] UNO Q Booted. Bridge OK. FIFO armed @ 400Hz.");
}

// ── loop ──────────────────────────────────────────────────────────────────
void loop() {
    // Async Temperature Read (non-blocking)
    if (temp_req_pending && (millis() - temp_req_ms >= DS18B20_CONV_MS)) {
        float t = tempSensor.getTempCByIndex(0);
        if (t > -100.0f) last_temp_c = t;
        tempSensor.requestTemperatures();
        temp_req_ms = millis();
    }

    if (!fifo_ready) return;
    fifo_ready = false;

    // Read batch from LIS3DH FIFO
    for (uint8_t i = 0; i < FIFO_WATERMARK; i++) {
        lis.read();
        sensors_event_t event;
        lis.getEvent(&event);

        batch[i].timestamp_us = micros();
        batch[i].sequence_id  = seq_counter++;
        batch[i].accel_x      = event.acceleration.x;
        batch[i].accel_y      = event.acceleration.y;
        batch[i].accel_z      = event.acceleration.z;
        batch[i].board_temp   = last_temp_c;
    }

    // Notify MPU side of the new sensor batch via Bridge RPC
    Bridge.notify("sensor_batch", (uint8_t*)batch, sizeof(batch));
}
