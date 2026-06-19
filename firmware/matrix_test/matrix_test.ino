// Phase 2: LIS3DH accelerometer + LED matrix arrows on Arduino UNO Q
// Displays UP/DOWN/LEFT/RIGHT arrows on the 8x12 LED matrix
// based on the board's tilt orientation.
//
// CRITICAL UNO Q notes:
//   - Bridge.begin() is REQUIRED to exit boot animation
//   - Qwiic connector = Wire1
//   - Bitmap arrays must NOT be const (loadPixels cast issue)
//   - Router must be restarted after upload for fresh handshake

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_LIS3DH.h>
#include <Adafruit_Sensor.h>
#include <Arduino_RouterBridge.h>
#include "Arduino_LED_Matrix.h"

// LIS3DH on Qwiic = Wire1
Adafruit_LIS3DH imu = Adafruit_LIS3DH(&Wire1);
ArduinoLEDMatrix matrix;

bool sensor_ok = false;

// Arrow bitmaps for 8x12 LED matrix (must NOT be const)
uint8_t arrowUp[8][12] = {
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,1,1,1,1,0,0,0,0},
  {0,0,0,1,1,1,1,1,1,0,0,0},
  {0,0,1,0,0,1,1,0,0,1,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0}
};

uint8_t arrowDown[8][12] = {
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,1,0,0,1,1,0,0,1,0,0},
  {0,0,0,1,1,1,1,1,1,0,0,0},
  {0,0,0,0,1,1,1,1,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0}
};

uint8_t arrowLeft[8][12] = {
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,1,0,0,0,0,0,0,0,0},
  {0,0,1,1,0,0,0,0,0,0,0,0},
  {0,1,1,1,1,1,1,1,1,0,0,0},
  {0,1,1,1,1,1,1,1,1,0,0,0},
  {0,0,1,1,0,0,0,0,0,0,0,0},
  {0,0,0,1,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0}
};

uint8_t arrowRight[8][12] = {
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,1,0,0,0},
  {0,0,0,0,0,0,0,1,1,0,0,0},
  {0,0,0,1,1,1,1,1,1,1,0,0},
  {0,0,0,1,1,1,1,1,1,1,0,0},
  {0,0,0,0,0,0,0,1,1,0,0,0},
  {0,0,0,0,0,0,0,0,1,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0}
};

uint8_t blank[8][12] = {
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0}
};

// Tilt threshold in m/s^2 (gravity is ~9.8, so 3.0 is ~18 degree tilt)
#define TILT_THRESHOLD 3.0

void setup() {
  // Bridge.begin() MUST come first on UNO Q to exit boot animation
  Bridge.begin();

  matrix.begin();

  Wire1.begin();
  if (imu.begin(0x18)) {
    sensor_ok = true;
    imu.setRange(LIS3DH_RANGE_2_G);
    imu.setDataRate(LIS3DH_DATARATE_50_HZ);
    Bridge.notify("status", String("Phase2: LIS3DH + Matrix OK"));
  } else {
    sensor_ok = false;
    Bridge.notify("status", String("Phase2: LIS3DH FAILED"));
  }
}

void loop() {
  if (!sensor_ok) {
    // Blink the matrix to show error
    matrix.renderBitmap(arrowUp, 8, 12);
    delay(300);
    matrix.renderBitmap(blank, 8, 12);
    delay(300);
    return;
  }

  sensors_event_t event;
  imu.getEvent(&event);

  float x = event.acceleration.x;
  float y = event.acceleration.y;

  // Determine dominant tilt axis
  float abs_x = abs(x);
  float abs_y = abs(y);

  if (abs_x > TILT_THRESHOLD || abs_y > TILT_THRESHOLD) {
    if (abs_x > abs_y) {
      // X-axis dominant tilt
      if (x > 0) {
        matrix.renderBitmap(arrowRight, 8, 12);
      } else {
        matrix.renderBitmap(arrowLeft, 8, 12);
      }
    } else {
      // Y-axis dominant tilt
      if (y > 0) {
        matrix.renderBitmap(arrowUp, 8, 12);
      } else {
        matrix.renderBitmap(arrowDown, 8, 12);
      }
    }
  } else {
    // Board is roughly level — blank the matrix
    matrix.renderBitmap(blank, 8, 12);
  }

  // Also send data via Bridge for debugging
  char buf[80];
  snprintf(buf, sizeof(buf), "x=%.2f y=%.2f z=%.2f",
           event.acceleration.x,
           event.acceleration.y,
           event.acceleration.z);
  Bridge.notify("accel", String(buf));

  delay(100);  // 10 Hz update rate for smooth arrow response
}
