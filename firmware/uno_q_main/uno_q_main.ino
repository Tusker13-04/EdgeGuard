#include <Arduino.h>
#include <vector>
#include <Wire.h>
#include <Adafruit_LIS3DH.h>
#include <Adafruit_Sensor.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Arduino_RouterBridge.h>
#include <ArduinoJson.h>
#include "config.h"

#define ONE_WIRE_PIN 4
#define DS18B20_CONV_MS 750

// ── Payload (must match src/schema.py) ─────────────────────────────
struct __attribute__((packed)) SensorPayload {
  uint32_t timestamp_us;   // us since MCU boot
  uint32_t sequence_id;    // monotonic counter for drop detection
  float    accel_x;        // m/s2
  float    accel_y;        // m/s2
  float    accel_z;        // m/s2
  float    board_temp;     // °C
};
static_assert(sizeof(SensorPayload) == 24, "Payload size mismatch");

Adafruit_LIS3DH imu = Adafruit_LIS3DH();
OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature tempSensor(&oneWire);

// Globals
volatile float last_temp_c = 25.0f;
uint32_t seq_counter = 0;
bool g_sensor_connected = false;

volatile int g_reflex_threshold = REFLEX_THRESHOLD;
volatile int g_active_threshold = REFLEX_THRESHOLD;
volatile uint32_t g_last_heartbeat_ts = 0;
volatile bool g_local_safe_mode = false;
volatile int g_sampling_interval_ms = 0;

volatile uint32_t g_reflex_pin_high_ts = 0;
volatile bool g_reflex_pin_active = false;

// -- Phase 3: Remote Tuning & Control handlers ---------------------
void onRemoteTune(String payload) {
  StaticJsonDocument<128> doc;
  DeserializationError error = deserializeJson(doc, payload);
  if (error) return;
  if (doc.containsKey("threshold")) {
    int new_thresh = doc["threshold"];
    if (new_thresh > 0) {
      g_reflex_threshold = new_thresh;
      g_active_threshold = new_thresh;
      char ack_buf[16];
      snprintf(ack_buf, sizeof(ack_buf), "%d", new_thresh);
      Bridge.notify("tune_ack", String(ack_buf));
    }
  }
}

void onHeartbeat(String payload) {
  g_last_heartbeat_ts = millis();
  if (g_local_safe_mode) {
    g_local_safe_mode = false;
    g_reflex_threshold = g_active_threshold;
  }
}

void onSamplingMode(String payload) {
  StaticJsonDocument<128> doc;
  DeserializationError error = deserializeJson(doc, payload);
  if (error) return;
  if (doc.containsKey("interval_ms")) {
    int interval = doc["interval_ms"];
    if (interval >= 0) {
      g_sampling_interval_ms = interval;
    }
  }
}

// -- Setup ---------------------------------------------------------
void setup() {
  pinMode(REFLEX_ALERT_PIN, OUTPUT);
  digitalWrite(REFLEX_ALERT_PIN, LOW);

  // Bypass I2C hardware completely to prevent STM32 I2C hang when sensor is physically disconnected
  // Wire.begin();
  // if (imu.begin(0x18)) {
  //   g_sensor_connected = true;
  // } else {
  //   g_sensor_connected = false;
  // }
  g_sensor_connected = false;

  Bridge.begin();
  Bridge.provide("remote_tune", onRemoteTune);
  Bridge.provide("heartbeat", onHeartbeat);
  Bridge.provide("sampling_mode", onSamplingMode);

  tempSensor.begin();
  tempSensor.setResolution(12);
  tempSensor.setWaitForConversion(false);
  tempSensor.requestTemperatures();

  g_last_heartbeat_ts = millis();
}

void loop() {
  static uint32_t last_sample_ms = 0;
  static uint32_t temp_req_ms = 0;
  static bool temp_req_pending = true;
  static int batch_idx = 0;
  static __attribute__((aligned(32))) SensorPayload batch[FIFO_WATERMARK];

  uint32_t now = millis();

  // 1. High-frequency sampling (100 Hz = 10ms)
  if (now - last_sample_ms >= 10) {
    last_sample_ms = now;

    if (batch_idx < FIFO_WATERMARK) {
      batch[batch_idx].timestamp_us = micros();
      batch[batch_idx].sequence_id  = seq_counter++;
      
      if (g_sensor_connected) {
        sensors_event_t event;
        imu.getEvent(&event);
        batch[batch_idx].accel_x      = event.acceleration.x;
        batch[batch_idx].accel_y      = event.acceleration.y;
        batch[batch_idx].accel_z      = event.acceleration.z;
      } else {
        // Generate dummy sine-wave data for testing when hardware is disconnected
        float t_sec = now / 1000.0f;
        batch[batch_idx].accel_x      = sin(t_sec * 2.0f * PI) * 5.0f;
        batch[batch_idx].accel_y      = cos(t_sec * 2.0f * PI) * 5.0f;
        batch[batch_idx].accel_z      = 9.81f + sin(t_sec * 1.0f * PI) * 2.0f;
      }
      
      batch[batch_idx].board_temp   = last_temp_c;
      batch_idx++;
    }

    if (batch_idx >= FIFO_WATERMARK) {
      batch_idx = 0;
      
      // Adaptive Sampling throttling
      static uint32_t last_batch_sent_ts = 0;
      if (g_sampling_interval_ms == 0 || (now - last_batch_sent_ts >= (uint32_t)g_sampling_interval_ms)) {
        last_batch_sent_ts = now;

        // RMS fallback anomaly detection
        float rms_sq = 0.0f;
        for (int i = 0; i < FIFO_WATERMARK; i++) {
          rms_sq += batch[i].accel_x * batch[i].accel_x + 
                    batch[i].accel_y * batch[i].accel_y + 
                    batch[i].accel_z * batch[i].accel_z;
        }
        float rms_mg = sqrt(rms_sq / (FIFO_WATERMARK * 3.0f)) * 1000.0f;
        bool anomaly_detected = (rms_mg > g_reflex_threshold);

        static uint32_t last_alert_ts = 0;
        if (anomaly_detected && (now - last_alert_ts >= 1000)) {
          last_alert_ts = now;
          digitalWrite(REFLEX_ALERT_PIN, HIGH);
          g_reflex_pin_high_ts = now;
          g_reflex_pin_active = true;

          Bridge.notify("anomaly_trigger", batch[0].accel_x, batch[0].accel_y, batch[0].accel_z);
        } else {
          Bridge.notify("sensor_point", batch[0].accel_x, batch[0].accel_y, batch[0].accel_z);
        }
      }
    }
  }

  // 2. Reflex Pin Reset
  if (g_reflex_pin_active && (now - g_reflex_pin_high_ts >= 50)) {
    digitalWrite(REFLEX_ALERT_PIN, LOW);
    g_reflex_pin_active = false;
  }

  // 3. Cognition Heartbeat Watchdog
  if (now - g_last_heartbeat_ts > 5000) {
    if (!g_local_safe_mode) {
      g_local_safe_mode = true;
      g_reflex_threshold = 1200; // High sensitivity fallback
    }
  }

  // 4. Low-frequency temperature reading
  if (temp_req_pending && (now - temp_req_ms >= DS18B20_CONV_MS)) {
    float t = tempSensor.getTempCByIndex(0);
    if (t > -100.0f) last_temp_c = t;
    temp_req_pending = false;
    tempSensor.requestTemperatures();
    temp_req_pending = true;
    temp_req_ms = now;
  }
}
