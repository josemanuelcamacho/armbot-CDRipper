void setup() {
  Serial.begin(115200);
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd == "PING") {
      Serial.println("PONG");
    }
    else if (cmd == "OP:START_MEASUREMENT") {
      // Do the action here...
      bool success = true;

      if (success) {
        Serial.println("OK: START_MEASUREMENT completed");
      } else {
        Serial.println("ERROR: measurement failed");
      }
    }
    else {
      Serial.println("ERROR: unknown command");
    }
  }
}