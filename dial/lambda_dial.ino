/*  The dial that turns itself.
 *
 *  ESP32 + micro-servo: a physical governance-weight knob on the podium,
 *  driven live by the controller running on the rented GPU node. In FIXED
 *  mode you set it by hand (it holds); in AUTO the servo turns the knob as
 *  the AIMD controller moves lambda -- a feedback loop reading real GPU
 *  thermometers, moving a physical object on stage.
 *
 *  Wiring (see HARDWARE.md):
 *    servo signal -> GPIO 13, servo V+ -> VIN(5V), GND -> GND
 *    optional SSD1306 OLED: SDA -> 21, SCL -> 22
 *
 *  Libraries (Arduino IDE): ESP32Servo, ArduinoJson,
 *    (optional) Adafruit SSD1306 + Adafruit GFX.
 *
 *  Failure mode is graceful: network drop -> the knob freezes at its last
 *  position -- same fallback story as the dashboard.
 */
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

#define USE_OLED 1
#if USE_OLED
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
#endif

// ---------- configure these three ----------
const char *WIFI_SSID = "YOUR_PHONE_HOTSPOT";
const char *WIFI_PASS = "YOUR_HOTSPOT_PASSWORD";
const char *METRICS_URL = "https://YOUR_POD_ID-8000.proxy.runpod.net/metrics";
// -------------------------------------------

const int SERVO_PIN = 13;
const float LAM_MIN = 0.15, LAM_MAX = 0.90;
const int ANG_MIN = 20, ANG_MAX = 160;   // servo margin; SPREAD=left, PACK=right
const float SLEW_DEG_PER_TICK = 3.0;     // smooth, deliberate motion

Servo servo;
float targetDeg = 90, currentDeg = 90;
unsigned long lastPoll = 0;

float lamToDeg(float lam) {
  float f = (lam - LAM_MIN) / (LAM_MAX - LAM_MIN);
  if (f < 0) f = 0; if (f > 1) f = 1;
  return ANG_MIN + f * (ANG_MAX - ANG_MIN);
}

void setup() {
  Serial.begin(115200);
  servo.attach(SERVO_PIN, 500, 2400);
  servo.write((int)currentDeg);
#if USE_OLED
  Wire.begin(21, 22);
  oled.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  oled.clearDisplay(); oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1); oled.setCursor(0, 0); oled.print("lambda dial: wifi...");
  oled.display();
#endif
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.println("\nconnected");
}

void draw(float lam, const char *mode, const char *action) {
#if USE_OLED
  oled.clearDisplay();
  oled.setTextSize(1); oled.setCursor(0, 0);
  oled.print("GOVERNANCE WEIGHT");
  oled.setTextSize(3); oled.setCursor(10, 16); oled.print(lam, 3);
  oled.setTextSize(1); oled.setCursor(0, 46);
  oled.print(mode); oled.print("  "); oled.print(action);
  oled.drawRect(0, 56, 128, 6, SSD1306_WHITE);
  int px = (int)((lam - LAM_MIN) / (LAM_MAX - LAM_MIN) * 126);
  oled.fillRect(px, 54, 3, 10, SSD1306_WHITE);
  oled.display();
#endif
}

void loop() {
  if (millis() - lastPoll > 1000 && WiFi.status() == WL_CONNECTED) {
    lastPoll = millis();
    HTTPClient http;
    http.setTimeout(1500);
    http.begin(METRICS_URL);
    if (http.GET() == 200) {
      StaticJsonDocument<2048> doc;
      if (!deserializeJson(doc, http.getStream())) {
        float lam = doc["controller"]["lam"] | 0.5;
        const char *mode = doc["controller"]["mode"] | "?";
        const char *action = doc["controller"]["action"] | "?";
        targetDeg = lamToDeg(lam);
        draw(lam, mode, action);
      }
    }
    http.end();
  }
  // slew-limited motion: the knob turns visibly, never snaps
  if (fabs(targetDeg - currentDeg) > 0.5) {
    currentDeg += (targetDeg > currentDeg ? 1 : -1) *
                  min(SLEW_DEG_PER_TICK, (float)fabs(targetDeg - currentDeg));
    servo.write((int)currentDeg);
  }
  delay(40);
}
