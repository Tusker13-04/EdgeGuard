// firmware/src/main.cpp
// EdgeGuard — ESP8266 NodeMCU v2
// Sensor : Adafruit LIS3DH (SPI, CS=D8/GPIO15)
// INT1   : D3 / GPIO0  (FIFO watermark, active-high)
// Streams UDP packets to Raspberry Pi at ~400 Hz effective throughput
// Payload struct must stay in sync with src/udp_receiver.py on the Pi.

#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <SPI.h>
#include <Adafruit_LIS3DH.h>
#include <Adafruit_Sensor.h>

// ── WiFi / network config ──────────────────────────────────────────────────
#define WIFI_SSID   "YOUR_SSID"
#define WIFI_PASS   "YOUR_PASSWORD"
#define HOST_IP     "192.168.1.100"   // Raspberry Pi IP
#define UDP_PORT    4444

// ── LIS3DH SPI pins (NodeMCU v2) ──────────────────────────────────────────
// MOSI = D7 / GPIO13
// MISO = D6 / GPIO12
// SCK  = D5 / GPIO14
// CS   = D8 / GPIO15
#define LIS3DH_CS_PIN  15

// ── FIFO watermark interrupt ───────────────────────────────────────────────
// INT1 = D3 / GPIO0  (INPUT, active-high from LIS3DH)
#define LIS3DH_INT1_PIN 0

// ── Sampling config ───────────────────────────────────────────────────────
// LIS3DH ODR = 400 Hz  → one sample every 2.5 ms
// FIFO watermark = 25 samples → ISR fires every ~62.5 ms
// Effective UDP packet rate ≈ 16 packets/sec carrying 25 samples each
#define FIFO_WATERMARK   25

// ── Payload (must match Pi's struct.unpack format '<LLffff') ──────────────
// timestamp_us : microseconds since boot (uint32)
// sequence_id  : monotonically increasing packet counter (uint32)
// accel_x/y/z  : m/s² (float32)
// board_temp   : °C   (float32, from LIS3DH ADC3, 1°C resolution)
struct __attribute__((packed)) SensorPayload {
    uint32_t timestamp_us;
    uint32_t sequence_id;
    float    accel_x;
    float    accel_y;
    float    accel_z;
    float    board_temp;
};
// sizeof(SensorPayload) == 24 bytes
static_assert(sizeof(SensorPayload) == 24, "Payload size mismatch — sync with Pi");

// ── Globals ────────────────────────────────────────────────────────────────
Adafruit_LIS3DH lis = Adafruit_LIS3DH(LIS3DH_CS_PIN);
WiFiUDP         udp;
SensorPayload   payload;
uint32_t        seq_counter = 0;
volatile bool   fifo_ready  = false;

// ── Temperature conversion ────────────────────────────────────────────────
// LIS3DH embedded temp: 16-bit signed output from OUT_ADC3_L/H
// Sensitivity = 1 LSB/°C (left-justified in 10-bit field → divide by 64 for
// 10-bit value, then offset from 25°C)
// Ref: LIS3DH datasheet §3.7, Table 5
float lis3dh_read_temp_celsius(Adafruit_LIS3DH &sensor) {
    int16_t raw = 0;
    // Adafruit driver exposes readADC(3) which returns the raw ADC3 value
    raw = sensor.readADC(3);
    // raw is a 10-bit signed value scaled to 16-bit (left-aligned)
    // Divide by 64 to recover 10-bit, then by 4 to get °C offset (4 LSB/°C
    // at 10-bit), then add 25°C reference.
    // Simplified from datasheet: each LSB of the 10-bit value = 1°C
    return 25.0f + (float)(raw >> 6) / 4.0f;
}

// ── FIFO watermark ISR ────────────────────────────────────────────────────
ICACHE_RAM_ATTR void onFifoWatermark() {
    fifo_ready = true;
}

// ── setup ─────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    Serial.println("[EdgeGuard] Booting...");

    // WiFi
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

    // LIS3DH SPI init
    if (!lis.begin_SPI(LIS3DH_CS_PIN)) {
        Serial.println("[EdgeGuard] FATAL: LIS3DH not found. Check wiring.");
        while (true) { delay(100); }
    }
    Serial.println("[EdgeGuard] LIS3DH OK");

    // ODR = 400 Hz, range = ±8g (good for industrial vibration)
    lis.setDataRate(LIS3DH_DATARATE_400_HZ);
    lis.setRange(LIS3DH_RANGE_8_G);

    // Enable ADC and embedded temperature sensor
    // Write 0xC0 to TEMP_CFG_REG (0x1F): ADC_EN=1, TEMP_EN=1
    lis.writeRegister8(LIS3DH_REG_TEMPCFG, 0xC0);

    // Enable FIFO stream mode with watermark = FIFO_WATERMARK
    // CTRL_REG5 (0x24): FIFO_EN = 1
    uint8_t ctrl5 = lis.readRegister8(LIS3DH_REG_CTRL5);
    lis.writeRegister8(LIS3DH_REG_CTRL5, ctrl5 | 0x40);
    // FIFO_CTRL_REG (0x2E): FM=01 (stream), FTH=watermark
    lis.writeRegister8(LIS3DH_REG_FIFOCTRL,
        (0x01 << 6) | (FIFO_WATERMARK & 0x1F));
    // CTRL_REG3 (0x22): I1_WTM=1 → route watermark interrupt to INT1 pin
    uint8_t ctrl3 = lis.readRegister8(LIS3DH_REG_CTRL3);
    lis.writeRegister8(LIS3DH_REG_CTRL3, ctrl3 | 0x04);

    // Attach ISR to INT1
    pinMode(LIS3DH_INT1_PIN, INPUT);
    attachInterrupt(digitalPinToInterrupt(LIS3DH_INT1_PIN),
                    onFifoWatermark, RISING);

    Serial.println("[EdgeGuard] LIS3DH FIFO armed @ 400Hz / watermark=25");
    Serial.println("[EdgeGuard] Ready. Streaming UDP to " HOST_IP);
}

// ── loop ──────────────────────────────────────────────────────────────────
void loop() {
    if (!fifo_ready) return;
    fifo_ready = false;

    // Read temperature once per FIFO burst (shared across all 25 samples)
    float board_temp = lis3dh_read_temp_celsius(lis);

    // Burst-read all samples from FIFO
    for (uint8_t i = 0; i < FIFO_WATERMARK; i++) {
        lis.read();  // populates lis.x, lis.y, lis.z (raw int16)

        sensors_event_t event;
        lis.getEvent(&event);  // converts to m/s²

        payload.timestamp_us = micros();
        payload.sequence_id  = seq_counter++;
        payload.accel_x      = event.acceleration.x;
        payload.accel_y      = event.acceleration.y;
        payload.accel_z      = event.acceleration.z;
        payload.board_temp   = board_temp;

        udp.beginPacket(HOST_IP, UDP_PORT);
        udp.write((const uint8_t*)&payload, sizeof(SensorPayload));
        udp.endPacket();
    }

    // Yield to allow WiFi stack to run between bursts
    yield();
}
