// firmware/src/main.cpp
// EdgeGuard — ESP8266 NodeMCU v2
// Accel : Adafruit LIS3DH STEMMA QT  (I2C, address 0x18, DRDY on D3/GPIO0)
// Temp  : DS18B20 waterproof probe    (1-Wire, data pin D4/GPIO2)
// Streams UDP packets to Raspberry Pi at ~400 Hz effective throughput.
// Payload struct must stay in sync with src/udp_receiver.py on the Pi.
//
// WiFi credentials:
//   Copy firmware/src/secrets.h.example → firmware/src/secrets.h
//   Fill in WIFI_SSID, WIFI_PASS, HOST_IP.
//   secrets.h is gitignored — never commit real credentials.

#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <Adafruit_LIS3DH.h>
#include <Adafruit_Sensor.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "secrets.h"   // defines WIFI_SSID, WIFI_PASS, HOST_IP

// ── Network config (values come from secrets.h) ───────────────────────────
#define UDP_PORT    4444

// ── LIS3DH I2C config ─────────────────────────────────────────────────────
// Connected via STEMMA QT cable: SDA=D2/GPIO4, SCL=D1/GPIO5
// SDO pin floating → I2C address 0x18 (default)
// CS pin not connected (STEMMA QT does not wire CS) → I2C mode enforced
#define LIS3DH_ADDR   0x18

// ── LIS3DH DRDY interrupt ─────────────────────────────────────────────────
// INT1 = D3 / GPIO0 (INPUT, active-high from LIS3DH CTRL_REG3)
#define LIS3DH_INT1_PIN  0

// ── DS18B20 1-Wire pin ────────────────────────────────────────────────────
// Data wire = D4 / GPIO2 (3.3V with 4.7kΩ pull-up to 3.3V)
#define ONE_WIRE_PIN  2

// ── Sampling config ───────────────────────────────────────────────────────
// LIS3DH ODR = 400 Hz  → one sample every 2.5 ms
// FIFO watermark = 25 samples → ISR fires every ~62.5 ms
// DS18B20 conversion time at 12-bit = 750 ms → read once per FIFO burst
#define FIFO_WATERMARK  25

// ── I2C clock-stretch timeout (ESP8266 specific) ──────────────────────────
// Default is 230 μs; set to 2000 μs to detect a hung LIS3DH without
// blocking the loop() indefinitely.
#define I2C_CLOCK_STRETCH_LIMIT_US  2000

// ── Payload (must match Pi's struct.unpack format '<LLffff') ──────────────
struct __attribute__((packed)) SensorPayload {
    uint32_t timestamp_us;
    uint32_t sequence_id;
    float    accel_x;
    float    accel_y;
    float    accel_z;
    float    board_temp;
};
static_assert(sizeof(SensorPayload) == 24, "Payload size mismatch — sync with Pi");

// ── Globals ────────────────────────────────────────────────────────────────
Adafruit_LIS3DH    lis;
OneWire            oneWire(ONE_WIRE_PIN);
DallasTemperature  tempSensor(&oneWire);
WiFiUDP            udp;
SensorPayload      payload;
uint32_t           seq_counter      = 0;
// FIX: use volatile uint8_t as atomic flag instead of volatile bool.
// noInterrupts()/interrupts() guards in loop() ensure test-and-clear is atomic.
volatile uint8_t   fifo_ready       = 0;
float              last_temp_c      = 25.0f;
bool               temp_req_pending = false;
uint32_t           temp_req_ms      = 0;
uint32_t           fifo_overflows   = 0;  // diagnostic counter

#define DS18B20_CONV_MS  750

// ── FIFO watermark ISR ────────────────────────────────────────────────────
// Marked ICACHE_RAM_ATTR so it runs from IRAM, not flash (avoids cache miss
// latency on ESP8266 when flash is busy with WiFi stack).
ICACHE_RAM_ATTR void onFifoWatermark() {
    fifo_ready = 1;
}

// ── setup ─────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    Serial.println("[EdgeGuard] Booting...");

    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.print("[EdgeGuard] Connecting WiFi");
    while (WiFi.status() != WL_CONNECTED) {
        delay(300);
        Serial.print(".");
    }
    Serial.println();
    Serial.print("[EdgeGuard] IP: ");
    Serial.println(WiFi.localIP());
    udp.begin(UDP_PORT);

    Wire.begin();
    Wire.setClock(400000);
    // FIX: set I2C clock-stretch timeout to detect hung LIS3DH.
    // Without this, a stuck SCL line hangs loop() forever with no recovery.
    Wire.setClockStretchLimit(I2C_CLOCK_STRETCH_LIMIT_US);

    if (!lis.begin(LIS3DH_ADDR)) {
        Serial.println("[EdgeGuard] FATAL: LIS3DH not found.");
        while (true) { delay(100); }
    }
    Serial.println("[EdgeGuard] LIS3DH OK (I2C 0x18)");

    lis.setDataRate(LIS3DH_DATARATE_400_HZ);
    lis.setRange(LIS3DH_RANGE_8_G);

    // Enable FIFO stream mode with watermark interrupt
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

    Serial.println("[EdgeGuard] DS18B20 OK (1-Wire GPIO2, 12-bit async)");
    Serial.println("[EdgeGuard] LIS3DH FIFO armed @ 400Hz / watermark=25");
    Serial.println("[EdgeGuard] Ready. Streaming UDP to " HOST_IP);
}

// ── loop ──────────────────────────────────────────────────────────────────
void loop() {
    // ── Async temperature read (non-blocking) ──────────────────────────────
    if (temp_req_pending && (millis() - temp_req_ms >= DS18B20_CONV_MS)) {
        float t = tempSensor.getTempCByIndex(0);
        if (t > -100.0f) last_temp_c = t;
        // FIX: was missing — temp_req_pending was never cleared, so the
        // condition re-evaluated every loop() call and called requestTemperatures()
        // on every iteration after the first 750 ms, thrashing the 1-Wire bus.
        temp_req_pending = false;
        tempSensor.requestTemperatures();
        temp_req_pending = true;
        temp_req_ms      = millis();
    }

    // ── FIX: Atomic test-and-clear of fifo_ready ISR flag ─────────────────
    // A non-atomic read-then-clear allows the ISR to fire between the test
    // and the clear, silently losing the second interrupt (missed batch).
    // noInterrupts()/interrupts() provide the required critical section on
    // single-core Xtensa LX106.
    noInterrupts();
    uint8_t batch_ready = fifo_ready;
    fifo_ready = 0;
    interrupts();

    if (!batch_ready) return;

    // ── FIX: FIFO overflow check before draining ───────────────────────────
    // LIS3DH FIFO_SRC_REG bit 6 (OVR) is set if a sample was overwritten
    // before being read. Log and reset FIFO to avoid reading stale data.
    uint8_t fifo_src = lis.readRegister8(LIS3DH_REG_FIFOSRC);
    if (fifo_src & 0x40) {
        fifo_overflows++;
        Serial.print("[EdgeGuard] WARN: FIFO overflow #");
        Serial.println(fifo_overflows);
        // Reset FIFO: bypass mode → stream mode
        lis.writeRegister8(LIS3DH_REG_FIFOCTRL, 0x00);
        lis.writeRegister8(LIS3DH_REG_FIFOCTRL,
            (0x01 << 6) | (FIFO_WATERMARK & 0x1F));
        return;  // Discard this batch — data integrity cannot be guaranteed
    }

    // ── Drain FIFO ────────────────────────────────────────────────────────
    for (uint8_t i = 0; i < FIFO_WATERMARK; i++) {
        // FIX: REMOVED standalone lis.read() that was here before.
        // getEvent() calls lis.read() internally as its first operation.
        // Having both caused 2 FIFO pops per iteration:
        //   - first  pop: data discarded (lis.read() result unused)
        //   - second pop: data used      (getEvent() result)
        // Net effect: every odd sample was silently dropped, true throughput
        // was ~200 Hz instead of 400 Hz, and sequence gaps corrupted telemetry.
        sensors_event_t event;
        lis.getEvent(&event);  // ← single FIFO pop; read() called internally

        payload.timestamp_us = micros();
        payload.sequence_id  = seq_counter++;
        payload.accel_x      = event.acceleration.x;
        payload.accel_y      = event.acceleration.y;
        payload.accel_z      = event.acceleration.z;
        payload.board_temp   = last_temp_c;

        udp.beginPacket(HOST_IP, UDP_PORT);
        udp.write((const uint8_t*)&payload, sizeof(SensorPayload));
        udp.endPacket();
    }

    yield();
}
