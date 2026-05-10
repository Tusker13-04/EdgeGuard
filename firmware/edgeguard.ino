/*
 * EdgeGuard — firmware/edgeguard.ino
 * ====================================
 * ESP8266 NodeMCU / D1 Mini sensor acquisition node.
 *
 * Responsibilities
 * ----------------
 *  1. Reads MPU-6050 via I²C at the highest stable rate the bus supports
 *     (~250–500 Hz depending on I²C clock and processing overhead).
 *  2. Reads NTC thermistor voltage on the ADC pin.
 *  3. Packs sensor data into a 28-byte binary struct.
 *  4. Sends the struct as a UDP datagram to the Raspberry Pi.
 *  5. Tracks actual achieved sampling rate and exposes it via Serial.
 *
 * Hardware wiring (ESP8266 NodeMCU)
 * ----------------------------------
 *  MPU-6050 VCC  → 3V3
 *  MPU-6050 GND  → GND
 *  MPU-6050 SCL  → D1 (GPIO 5)
 *  MPU-6050 SDA  → D2 (GPIO 4)
 *  MPU-6050 AD0  → GND  (I²C address 0x68)
 *  NTC thermistor → A0 (voltage divider with 10 kΩ resistor to 3V3)
 *  CT sensor (opt) → not used in base config; current defaults to 0
 *
 * Dependencies (install via Arduino Library Manager)
 * ---------------------------------------------------
 *  - ESP8266WiFi      (bundled with esp8266 board package)
 *  - WiFiUdp          (bundled)
 *  - Wire             (bundled)
 *  - MPU6050_light    by rfetick  v1.x  (lightweight, no DMP)
 *
 * Binary payload layout (28 bytes, little-endian)
 * ------------------------------------------------
 *  Offset  Type     Field
 *  0       uint32   timestamp_us  (micros() — wraps at ~71 min, handled on Pi)
 *  4       uint32   seq_id        (monotonically increasing, never resets)
 *  8       float32  accel_x       (m/s², raw, not gravity-subtracted)
 *  12      float32  accel_y
 *  16      float32  accel_z       (≈9.81 when stationary)
 *  20      float32  temp_c        (NTC, Steinhart-Hart converted)
 *  24      float32  current_a     (CT, Irms; 0.0 if CT not fitted)
 *
 * Configuration — edit the block below then flash.
 */

// ─── User configuration ────────────────────────────────────────────────────
const char* WIFI_SSID     = "YOUR_SSID";          // <-- change
const char* WIFI_PASSWORD = "YOUR_PASSWORD";       // <-- change
const char* PI_IP         = "192.168.1.100";       // <-- Pi's LAN IP
const uint16_t PI_PORT    = 5005;                  // must match main.py --udp-port

// I²C clock: 400000 (Fast Mode) is stable on most breadboard rigs.
// If you see I²C errors, drop to 100000 (Standard Mode).
const uint32_t I2C_CLOCK_HZ = 400000;

// MPU-6050 accelerometer range: 0=±2g, 1=±4g, 2=±8g, 3=±16g
// For a small DC motor, ±4g (1) gives enough range with good resolution.
const uint8_t ACCEL_RANGE = 1;

// NTC thermistor parameters (10 kΩ NTC, 10 kΩ pull-up to 3V3)
const float NTC_NOMINAL_R  = 10000.0f;  // NTC resistance at NOMINAL_TEMP
const float NTC_NOMINAL_T  = 25.0f;     // °C
const float NTC_BETA       = 3950.0f;   // B-coefficient (check your datasheet)
const float NTC_SERIES_R   = 10000.0f;  // series resistor value (Ω)
const float ADC_VREF       = 1.0f;      // ESP8266 ADC range is 0–1 V (NodeMCU has divider)
const int   ADC_BITS       = 1024;      // 10-bit ADC

// Target inter-sample delay in microseconds.
// At 400 kHz I²C + 28-byte UDP, a 2000 µs delay achieves ~450 Hz.
// Decrease to 1000 for ~550 Hz (may hit I²C throughput ceiling).
const uint32_t SAMPLE_PERIOD_US = 2000;  // → ~450 Hz nominal

// Serial diagnostics rate: print measured Hz every N packets
const uint32_t DIAG_EVERY = 500;

// ─── Includes ──────────────────────────────────────────────────────────────
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <MPU6050_light.h>

// ─── Globals ───────────────────────────────────────────────────────────────
MPU6050    mpu(Wire);
WiFiUDP    udp;

// Payload struct — layout MUST match STRUCT_FMT in pi/main.py
// __attribute__((packed)) prevents compiler padding
struct __attribute__((packed)) SensorPayload {
  uint32_t timestamp_us;
  uint32_t seq_id;
  float    accel_x;
  float    accel_y;
  float    accel_z;
  float    temp_c;
  float    current_a;
};
static_assert(sizeof(SensorPayload) == 28, "Payload must be 28 bytes");

SensorPayload payload;    // single pre-allocated instance — no heap in loop()
uint32_t      seq        = 0;
uint32_t      lastDiagUs = 0;
uint32_t      diagCount  = 0;
float         measuredHz = 0.0f;

// ─── NTC helper ─────────────────────────────────────────────────────────────
float ntc_read_celsius() {
  int raw = analogRead(A0);
  // ADC gives voltage across NTC (bottom leg of divider)
  // V_ntc / V_ref = raw / ADC_BITS
  float v_ntc  = (float)raw / (float)ADC_BITS;
  if (v_ntc <= 0.001f || v_ntc >= 0.999f) return 25.0f;  // clamp open/short circuit
  float r_ntc  = NTC_SERIES_R * v_ntc / (ADC_VREF - v_ntc);
  // Steinhart-Hart simplified (B-parameter equation)
  float steinhart = r_ntc / NTC_NOMINAL_R;
  steinhart = log(steinhart);
  steinhart /= NTC_BETA;
  steinhart += 1.0f / (NTC_NOMINAL_T + 273.15f);
  return (1.0f / steinhart) - 273.15f;
}

// ─── setup() ───────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[EdgeGuard] Booting…");

  // ── Wi-Fi ────────────────────────────────────────────────────────────────
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[EdgeGuard] Connecting to WiFi");
  uint8_t attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 60) {
    delay(500);
    Serial.print('.');
    attempts++;
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\n[EdgeGuard] WiFi failed — rebooting in 5 s");
    delay(5000);
    ESP.restart();
  }
  Serial.print("\n[EdgeGuard] WiFi OK  IP=");
  Serial.println(WiFi.localIP());

  // ── I²C + MPU-6050 ──────────────────────────────────────────────────────
  Wire.begin();                         // SDA=D2(GPIO4), SCL=D1(GPIO5)
  Wire.setClock(I2C_CLOCK_HZ);
  Wire.setClockStretchLimit(40000);     // give MPU-6050 time to respond

  byte status = mpu.begin();
  if (status != 0) {
    Serial.print("[EdgeGuard] MPU-6050 init failed, status=");
    Serial.println(status);
    Serial.println("[EdgeGuard] Check wiring — rebooting in 5 s");
    delay(5000);
    ESP.restart();
  }

  // Set accelerometer full-scale range
  // MPU6050_light uses setAccRange(range):  0=2g 1=4g 2=8g 3=16g
  mpu.setAccRange(ACCEL_RANGE);

  // Calibrate gyro (not used for vibration, but prevents drift in accel temp comp)
  Serial.println("[EdgeGuard] Calibrating MPU-6050 — keep motor stationary…");
  mpu.calcOffsets(true, true);   // true,true = accel+gyro calibration
  Serial.println("[EdgeGuard] Calibration done");

  // ── UDP ──────────────────────────────────────────────────────────────────
  udp.begin(PI_PORT);  // source port = dest port (doesn't matter for UDP)
  Serial.print("[EdgeGuard] Streaming to ");
  Serial.print(PI_IP);
  Serial.print(":");
  Serial.println(PI_PORT);

  lastDiagUs = micros();
}

// ─── loop() ────────────────────────────────────────────────────────────────
void loop() {
  uint32_t loopStartUs = micros();

  // ── Read MPU-6050 ────────────────────────────────────────────────────────
  mpu.update();   // fetches latest FIFO data via I²C

  // getAccX/Y/Z return m/s² (MPU6050_light applies range scaling)
  payload.accel_x = mpu.getAccX();
  payload.accel_y = mpu.getAccY();
  payload.accel_z = mpu.getAccZ();

  // ── Read NTC thermistor ─────────────────────────────────────────────────
  payload.temp_c = ntc_read_celsius();

  // ── Current (optional CT) ───────────────────────────────────────────────
  // Set to 0 until CT is wired.  Replace with your own ADC read + Irms calc.
  payload.current_a = 0.0f;

  // ── Fill header ──────────────────────────────────────────────────────────
  payload.timestamp_us = loopStartUs;
  payload.seq_id       = seq++;

  // ── Send UDP ──────────────────────────────────────────────────────────────
  udp.beginPacket(PI_IP, PI_PORT);
  udp.write(reinterpret_cast<const uint8_t*>(&payload), sizeof(payload));
  udp.endPacket();    // non-blocking on ESP8266 — returns immediately

  // ── Diagnostics ───────────────────────────────────────────────────────────
  diagCount++;
  if (diagCount >= DIAG_EVERY) {
    uint32_t nowUs   = micros();
    float    elapsed = (float)(nowUs - lastDiagUs) / 1e6f;
    measuredHz = (float)diagCount / elapsed;
    Serial.print("[EdgeGuard] seq=");
    Serial.print(seq);
    Serial.print("  hz=");
    Serial.print(measuredHz, 1);
    Serial.print("  temp=");
    Serial.print(payload.temp_c, 1);
    Serial.print(" °C  accel_z=");
    Serial.print(payload.accel_z, 3);
    Serial.println(" m/s²");
    lastDiagUs = nowUs;
    diagCount  = 0;
  }

  // ── Precise timing: busy-wait for remainder of SAMPLE_PERIOD_US ──────────
  // delayMicroseconds is accurate to ±1 µs on ESP8266 up to ~16 ms.
  uint32_t elapsed = micros() - loopStartUs;
  if (elapsed < SAMPLE_PERIOD_US) {
    delayMicroseconds(SAMPLE_PERIOD_US - elapsed);
  }
  // If elapsed > SAMPLE_PERIOD_US (heavy I²C), we immediately loop back —
  // no slip accumulation; seq_id gap on the Pi side tracks any real drops.
}
