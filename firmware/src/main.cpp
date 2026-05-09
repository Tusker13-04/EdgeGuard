// firmware/src/main.cpp
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>

const char* ssid = "YOUR_SSID";
const char* password = "YOUR_PASSWORD";
const char* hostIP = "192.168.1.100"; // Target RPi IP
const int udpPort = 4444;

WiFiUDP udp;

struct __attribute__((packed)) SensorPayload {
  uint32_t timestamp_us;
  uint32_t sequence_id;
  float accel_x;
  float accel_y;
  float accel_z;
  float temp;
  float current;
};

SensorPayload payload;
uint32_t seq_counter = 0;
unsigned long last_sample_time = 0;

void setup() {
  Serial.begin(115200);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) { delay(500); }
  udp.begin(udpPort);
}

void loop() {
  unsigned long current_time = micros();
  if (current_time - last_sample_time >= 1000) {
    last_sample_time = current_time;
    payload.timestamp_us = current_time;
    payload.sequence_id = seq_counter++;
    // Replace with real ADC/I2C reads when sensors are wired
    payload.accel_x = 1.0; payload.accel_y = 1.0; payload.accel_z = 9.8;
    payload.temp = 25.5; payload.current = 5.0;
    udp.beginPacket(hostIP, udpPort);
    udp.write((const uint8_t*)&payload, sizeof(SensorPayload));
    udp.endPacket();
  }
}
