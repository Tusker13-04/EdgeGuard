/**
 * EdgeGuard — uno_q_main.ino
 * Arduino UNO Q (STM32U585 MCU / Qualcomm MPU / Zephyr RTOS)
 *
 * Architecture:
 *   acq_thread  -->  [EI Reflex]  -->  Bridge.notify("anomaly_trigger" | "sensor_batch")
 *                        |
 *                        +-->  REFLEX_ALERT_PIN toggle  (us-latency safety reflex)
 *
 * Remote-Tuning feedback loop:
 *   MPU  -->  Bridge.put("remote_tune", JSON)  -->  onCommand()  -->  hot-patch threshold
 */

#include <Arduino.h>
#include <zephyr/kernel.h>
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

// -- Edge Impulse inferencing stub ----------------------------------
// Replace with the actual EI Arduino library header once exported:
//   #include <edgeguard_vibration_inferencing.h>
// The stub below keeps the sketch compilable during integration.
#ifndef EI_CLASSIFIER_RAW_SAMPLE_COUNT
  #define EI_CLASSIFIER_RAW_SAMPLE_COUNT 100
  #define EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE (EI_CLASSIFIER_RAW_SAMPLE_COUNT * 3)
  struct ei_impulse_result_t { struct { float value; int index; } classification[2]; };
  enum EI_IMPULSE_ERROR { EI_IMPULSE_OK = 0 };
  static inline EI_IMPULSE_ERROR run_classifier(float*, size_t, ei_impulse_result_t*, bool) {
    return EI_IMPULSE_OK;
  }
  #define EI_CLASS_NORMAL   0
  #define EI_CLASS_ANOMALY  1
#endif
// -------------------------------------------------------------------

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
volatile bool temp_req_pending = false;
volatile uint32_t temp_req_ms = 0;

__attribute__((aligned(32))) SensorPayload batch[FIFO_WATERMARK];
uint32_t seq_counter = 0;

// Runtime-tunable threshold (hot-patched via remote_tune command)
volatile int g_reflex_threshold = REFLEX_THRESHOLD;
volatile int g_active_threshold = REFLEX_THRESHOLD; // Cache non-safe mode threshold
volatile uint32_t g_last_heartbeat_ts = 0;
volatile bool g_local_safe_mode = false;
volatile int g_sampling_interval_ms = 0;

volatile uint32_t g_reflex_pin_high_ts = 0;
volatile bool g_reflex_pin_active = false;

// -- Acquisition + Reflex thread -----------------------------------
void acq_thread_func(void*, void*, void*) {
  int batch_idx = 0;

  while (true) {
    k_sleep(K_MSEC(10)); // 100 Hz sampling

    if (batch_idx < FIFO_WATERMARK) {
      batch[batch_idx].timestamp_us = micros();
      batch[batch_idx].sequence_id  = seq_counter++;
      sensors_event_t event;
      imu.getEvent(&event);
      batch[batch_idx].accel_x      = event.acceleration.x;
      batch[batch_idx].accel_y      = event.acceleration.y;
      batch[batch_idx].accel_z      = event.acceleration.z;
      batch[batch_idx].board_temp   = last_temp_c;
      batch_idx++;
    }

    // 2. Once full batch accumulated, run EI Reflex
    if (batch_idx >= FIFO_WATERMARK) {
      batch_idx = 0;

      // Adaptive Sampling throttling: drop batches if we are within the interval
      static uint32_t last_batch_ts = 0;
      if (g_sampling_interval_ms > 0 && (millis() - last_batch_ts < (uint32_t)g_sampling_interval_ms)) {
        continue;
      }
      last_batch_ts = millis();

      // Extract raw acceleration values for Edge Impulse classifier
      static float ei_batch[FIFO_WATERMARK * 3];
      for (int i = 0; i < FIFO_WATERMARK; i++) {
        ei_batch[i * 3 + 0] = batch[i].accel_x;
        ei_batch[i * 3 + 1] = batch[i].accel_y;
        ei_batch[i * 3 + 2] = batch[i].accel_z;
      }

      // Phase 1: EI inference
      ei_impulse_result_t result = {};
      bool anomaly_detected = false;

      EI_IMPULSE_ERROR ei_err = run_classifier(
          ei_batch,
          EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE,
          &result,
          false
      );

      if (ei_err == EI_IMPULSE_OK) {
        float anomaly_confidence = result.classification[EI_CLASS_ANOMALY].value;
        anomaly_detected = (anomaly_confidence > 0.75f);
      } else {
        // EI model unavailable — RMS fallback (scaled to mg to match threshold)
        float rms_sq = 0.0f;
        for (int i = 0; i < FIFO_WATERMARK; i++) {
          float x = batch[i].accel_x;
          float y = batch[i].accel_y;
          float z = batch[i].accel_z;
          rms_sq += x * x + y * y + z * z;
        }
        float rms_mg = sqrtf(rms_sq / (FIFO_WATERMARK * 3)) * 1000.0f;
        anomaly_detected = (rms_mg > g_reflex_threshold);
      }

      if (anomaly_detected) {
        // Significance Filter: Avoid triggering MPU on noise spikes
        // Calculate variance on the magnitude of the 3D acceleration vectors
        double sum = 0, sq_sum = 0;
        int n = FIFO_WATERMARK;
        for(int i = 0; i < n; i++) {
          float x = batch[i].accel_x;
          float y = batch[i].accel_y;
          float z = batch[i].accel_z;
          double mag = sqrt(x*x + y*y + z*z) * 1000.0; // Scale to mg
          sum += mag;
          sq_sum += mag * mag;
        }
        double variance = (sq_sum / n) - ((sum/n)*(sum/n));

        if (variance < 50.0) { 
          // Ignore insignificant trigger (variance < 50 mg^2, std dev < 7.07 mg)
          anomaly_detected = false; 
        }
      }

      // Anomaly Alert Rate-Limiting to prevent high-frequency flapping & MPU thread congestion
      static uint32_t last_alert_ts = 0;
      if (anomaly_detected) {
        if (millis() - last_alert_ts >= 1000) {
          last_alert_ts = millis();
          // us-latency reflex: toggle physical pin FIRST (non-blocking)
          digitalWrite(REFLEX_ALERT_PIN, HIGH);
          g_reflex_pin_high_ts = millis();
          g_reflex_pin_active = true;

          // Then notify MPU for Cognition layer
          std::vector<uint8_t> payload((uint8_t*)batch, (uint8_t*)batch + sizeof(batch));
          Bridge.notify("anomaly_trigger", payload);
        } else {
          // Rate-limited: Demote to standard sensor batch
          std::vector<uint8_t> payload((uint8_t*)batch, (uint8_t*)batch + sizeof(batch));
          Bridge.notify("sensor_batch", payload);
        }
      } else {
        // Normal batch — cheaper packet
        std::vector<uint8_t> payload((uint8_t*)batch, (uint8_t*)batch + sizeof(batch));
        Bridge.notify("sensor_batch", payload);
      }
    }
  }
}

K_THREAD_DEFINE(acq_thread, 4096, acq_thread_func, NULL, NULL, NULL, 5, 0, 0);

// -- Phase 3: Remote Tuning & Control handlers ---------------------
void onRemoteTune(String payload) {
  // Expected payload: {"threshold": 1800}
  StaticJsonDocument<128> doc;
  DeserializationError error = deserializeJson(doc, payload);
  if (error) {
    Serial.println("[MCU] ERROR: Malformed JSON in onRemoteTune");
    return;
  }
  
  if (doc.containsKey("threshold")) {
    int new_thresh = doc["threshold"];
    if (new_thresh > 0) {
      g_reflex_threshold = new_thresh;
      g_active_threshold = new_thresh; // Cache latest tuned threshold
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
    g_reflex_threshold = g_active_threshold; // Restore MPU-tuned threshold
    Serial.println("[MCU] HEARTBEAT RESTORED: Resuming normal threshold");
  }
}

void onSamplingMode(String payload) {
  // Expected payload: {"interval_ms": 100}
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
  Serial.begin(115200);
  pinMode(REFLEX_ALERT_PIN, OUTPUT);
  digitalWrite(REFLEX_ALERT_PIN, LOW);

  Wire.begin();
  if (!imu.begin(0x18)) {
    Serial.println("[MCU] ERROR: LIS3DH accelerometer initialization failed!");
  }

  Bridge.begin();
  Bridge.provide("remote_tune", onRemoteTune);
  Bridge.provide("heartbeat", onHeartbeat);
  Bridge.provide("sampling_mode", onSamplingMode);

  tempSensor.begin();
  tempSensor.setResolution(12);
  tempSensor.setWaitForConversion(false);
  tempSensor.requestTemperatures();
  temp_req_pending = true;
  temp_req_ms = millis();

  g_last_heartbeat_ts = millis();
}

void loop() {
  // --- Non-blocking Reflex Pin Reset ---
  if (g_reflex_pin_active && (millis() - g_reflex_pin_high_ts >= 50)) {
    digitalWrite(REFLEX_ALERT_PIN, LOW);
    g_reflex_pin_active = false;
  }

  // --- Cognition Heartbeat Watchdog ---
  if (millis() - g_last_heartbeat_ts > 5000) {
    if (!g_local_safe_mode) {
      g_local_safe_mode = true;
      g_reflex_threshold = 1200; // High sensitivity fallback
      Serial.println("[MCU] HEARTBEAT LOST: Entering Safe-Local Mode");
    }
  }

  // --- Async temperature read (non-blocking) ---
  if (temp_req_pending && (millis() - temp_req_ms >= DS18B20_CONV_MS)) {
    float t = tempSensor.getTempCByIndex(0);
    if (t > -100.0f) last_temp_c = t;
    temp_req_pending = false;
    tempSensor.requestTemperatures();
    temp_req_pending = true;
    temp_req_ms = millis();
  }

  k_sleep(K_MSEC(100));
}
