#include <Arduino.h>
#include <vector>
#include <MsgPack.h>

void setup() {
  std::vector<uint8_t> vec;
  MsgPack::Packer packer;
  packer.serialize(vec);
}
void loop() {}
