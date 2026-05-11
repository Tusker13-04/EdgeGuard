// firmware/src/main.cpp
// EdgeGuard — ESP8266 NodeMCU v2
// Accel : Adafruit LIS3DH STEMMA QT  (I2C, address 0x18, DRDY on D3/GPIO0)
// Temp  : DS18B20 waterproof probe    (1-Wire, data pin D4/GPIO2)
// Streams UDP packets to Raspberry Pi at ~400 Hz effective throughput.
// Payload struct must stay in sync with src/udp_receiver.py on the Pi.

#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <Adafruit_LIS3DH.h>
#include <Adafruit_Sensor.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// ── WiFi / network config ──────────────────────────────────────────────────
#define WIFI_SSID   "YOUR_SSID"
#define WIFI_PASS   "YOUR_PASSWORD"
#define HOST_IP     "192.168.1.100"   // Raspberry Pi IP
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
// (burst fires every ~62.5 ms; DS18B20 is requested async and read on
//  the next available burst after conversion completes)
#define FIFO_WATERMARK  25

// ── Payload (must match Pi's struct.unpack format '<LLffff') ──────────────
// timestamp_us : microseconds since boot      (uint32)
// sequence_id  : monotonically increasing     (uint32)
// accel_x/y/z  : m/s²                         (float32)
// board_temp   : °C from DS18B20              (float32)
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
volatile bool      fifo_ready       = false;
float              last_temp_c      = 25.0f;  // safe default until first read
bool               temp_req_pending = false;
uint32_t           temp_req_ms      = 0;

// DS18B20 async conversion time at 12-bit resolution = 750 ms
#define DS18B20_CONV_MS  750

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

    // I2C for LIS3DH (STEMMA QT: SDA=D2/GPIO4, SCL=D1/GPIO5)
    Wire.begin();
    Wire.setClock(400000);  // 400 kHz fast-mode

    // LIS3DH I2C init
    if (!lis.begin(LIS3DH_ADDR)) {
        Serial.println("[EdgeGuard] FATAL: LIS3DH not found. Check STEMMA QT cable and address.");
        while (true) { delay(100); }
    }
    Serial.println("[EdgeGuard] LIS3DH OK (I2C 0x18)");

    // ODR = 400 Hz, range = ±8 g
    lis.setDataRate(LIS3DH_DATARATE_400_HZ);
    lis.setRange(LIS3DH_RANGE_8_G);

    // Enable FIFO stream mode, watermark = FIFO_WATERMARK
    // CTRL_REG5 (0x24): FIFO_EN = 1
    uint8_t ctrl5 = lis.readRegister8(LIS3DH_REG_CTRL5);
    lis.writeRegister8(LIS3DH_REG_CTRL5, ctrl5 | 0x40);
    // FIFO_CTRL_REG (0x2E): FM=01 (stream), FTH=watermark
    lis.writeRegister8(LIS3DH_REG_FIFOCTRL,
        (0x01 << 6) | (FIFO_WATERMARK & 0x1F));
    // CTRL_REG3 (0x22): I1_WTM=1 → route watermark interrupt to INT1 pin
    uint8_t ctrl3 = lis.readRegister8(LIS3DH_REG_CTRL3);
    lis.writeRegister8(LIS3DH_REG_CTRL3, ctrl3 | 0x04);

    // INT1 interrupt
    pinMode(LIS3DH_INT1_PIN, INPUT);
    attachInterrupt(digitalPinToInterrupt(LIS3DH_INT1_PIN),
                    onFifoWatermark, RISING);

    // DS18B20 1-Wire init
    tempSensor.begin();
    tempSensor.setResolution(12);         // 12-bit: 0.0625 °C resolution
    tempSensor.setWaitForConversion(false); // async: never block in loop()
    // Kick off first conversion immediately
    tempSensor.requestTemperatures();
    temp_req_pending = true;
    temp_req_ms      = millis();

    Serial.println("[EdgeGuard] DS18B20 OK (1-Wire GPIO2, 12-bit async)");
    Serial.println("[EdgeGuard] LIS3DH FIFO armed @ 400Hz / watermark=25");
    Serial.println("[EdgeGuard] Ready. Streaming UDP to " HOST_IP);
}

// ── loop ──────────────────────────────────────────────────────────────────
void loop() {
    // ── DS18B20 async read (non-blocking) ───────────────────────────────
    // Check if a pending conversion has completed (750 ms at 12-bit).
    // If so, collect the result and immediately request the next conversion.
    if (temp_req_pending && (millis() - temp_req_ms >= DS18B20_CONV_MS)) {
        float t = tempSensor.getTempCByIndex(0);
        // DEVICE_DISCONNECTED_C = -127; ignore obviously invalid readings
        if (t > -100.0f) {
            last_temp_c = t;
        }
        // Request next conversion immediately (result ready in 750 ms)
        tempSensor.requestTemperatures();
        temp_req_ms = millis();
        // temp_req_pending stays true — we always have a conversion running
    }

    // ── LIS3DH FIFO burst ───────────────────────────────────────────────
    if (!fifo_ready) return;
    fifo_ready = false;

    for (uint8_t i = 0; i < FIFO_WATERMARK; i++) {
        lis.read();  // populates lis.x, lis.y, lis.z (raw int16)

        sensors_event_t event;
        lis.getEvent(&event);  // converts to m/s²

        payload.timestamp_us = micros();
        payload.sequence_id  = seq_counter++;
        payload.accel_x      = event.acceleration.x;
        payload.accel_y      = event.acceleration.y;
        payload.accel_z      = event.acceleration.z;
        payload.board_temp   = last_temp_c;  // DS18B20 last good reading

        udp.beginPacket(HOST_IP, UDP_PORT);
        udp.write((const uint8_t*)&payload, sizeof(SensorPayload));
        udp.endPacket();
    }

    // Yield to allow WiFi stack to run between bursts
    yield();
}
