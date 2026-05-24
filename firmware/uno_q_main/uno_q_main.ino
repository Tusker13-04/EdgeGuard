/**
 * EdgeGuard — uno_q_main.ino
 * Arduino UNO R4 WiFi (RA4M1 / Zephyr RTOS)
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
#include <LIS3DH.h>
#include <ArduinoBridge.h>
#include "config.h"

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

LIS3DH imu(Wire1);          // Qwiic / I2C4 on RA4M1

K_SEM_DEFINE(fifo_sem, 0, FIFO_WATERMARK / SAMPLES_PER_IRQ);  // FLAW-02 fix

// Runtime-tunable threshold (hot-patched via remote_tune command)
volatile int g_reflex_threshold = REFLEX_THRESHOLD;

// -- FIFO watermark ISR --------------------------------------------
void fifo_isr() {
  k_sem_give(&fifo_sem);
}

// -- Acquisition + Reflex thread -----------------------------------
void acq_thread_func(void*, void*, void*) {
  float batch[FIFO_WATERMARK * 3];  // x,y,z interleaved
  int   batch_idx = 0;

  while (true) {
    k_sem_take(&fifo_sem, K_FOREVER);

    // 1. Drain SAMPLES_PER_IRQ entries from LIS3DH FIFO
    for (int i = 0; i < SAMPLES_PER_IRQ; i++) {
      float x, y, z;
      imu.readFIFO(x, y, z);
      batch[batch_idx * 3 + 0] = x;
      batch[batch_idx * 3 + 1] = y;
      batch[batch_idx * 3 + 2] = z;
      batch_idx++;
    }

    // 2. Once full batch accumulated, run EI Reflex
    if (batch_idx >= FIFO_WATERMARK) {
      batch_idx = 0;

      // Phase 1: EI inference
      ei_impulse_result_t result = {};
      bool anomaly_detected = false;

      EI_IMPULSE_ERROR ei_err = run_classifier(
          batch,
          EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE,
          &result,
          false
      );

      if (ei_err == EI_IMPULSE_OK) {
        float anomaly_confidence = result.classification[EI_CLASS_ANOMALY].value;
        anomaly_detected = (anomaly_confidence > 0.75f);
      } else {
        // EI model unavailable — RMS fallback
        float rms_sq = 0.0f;
        for (int i = 0; i < FIFO_WATERMARK * 3; i++) rms_sq += batch[i] * batch[i];
        anomaly_detected = (sqrtf(rms_sq / (FIFO_WATERMARK * 3)) > g_reflex_threshold);
      }

      if (anomaly_detected) {
        // us-latency reflex: toggle physical pin FIRST
        digitalWrite(REFLEX_ALERT_PIN, HIGH);
        delay(50);
        digitalWrite(REFLEX_ALERT_PIN, LOW);

        // Then notify MPU for Cognition layer
        Bridge.notify("anomaly_trigger", batch, sizeof(batch));
      } else {
        // Normal batch — cheaper packet
        Bridge.notify("sensor_batch", batch, sizeof(batch));
      }
    }
  }
}

K_THREAD_DEFINE(acq_thread, 4096, acq_thread_func, NULL, NULL, NULL, 5, 0, 0);

// -- Phase 3: Remote Tuning command handler ------------------------
void onRemoteTune(const String& cmd, const String& payload) {
  // Expected payload: {"threshold": 1800}
  int idx = payload.indexOf("\"threshold\"");
  if (idx >= 0) {
    int colon = payload.indexOf(':', idx);
    if (colon >= 0) {
      int new_thresh = payload.substring(colon + 1).toInt();
      if (new_thresh > 0) {
        g_reflex_threshold = new_thresh;
        Bridge.notify("tune_ack", String(new_thresh).c_str(), String(new_thresh).length());
      }
    }
  }
}

// -- Setup ---------------------------------------------------------
void setup() {
  Serial.begin(115200);
  pinMode(REFLEX_ALERT_PIN, OUTPUT);
  digitalWrite(REFLEX_ALERT_PIN, LOW);

  Wire1.begin();
  imu.begin();
  imu.setFIFOMode(LIS3DH_FIFO_STREAM, FIFO_WATERMARK);
  imu.attachInterrupt(fifo_isr);

  Bridge.begin();
  Bridge.onCommand(REMOTE_TUNE_CMD, onRemoteTune);
}

void loop() {
  k_sleep(K_FOREVER);
}
