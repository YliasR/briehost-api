#include "esp_camera.h"
#include <WiFi.h>
#include <ESPmDNS.h>
#include <EEPROM.h>
#include <WebServer.h>
#include <Arduino.h>

#define EEPROM_SIZE 128  // EEPROMのサイズを拡張
#define SSID_ADDR 0
#define PASSWD_ADDR 32

//M5STACK-U017-PCBA
#define CAMERA_MODEL_M5STACK_PSRAM

//M5STACK-U082-F
//#define CAMERA_MODEL_M5STACK_UNITCAM　

#include "camera_pins.h"

WebServer server(80); // Corrected to default HTTP port 80

void startCameraServer();
void setupLedFlash(int pin);
void handleRoot();
void handleSave();
void setupWebServer();
void connectToWifi();

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(true);
  Serial.println();

  // Initialize EEPROM for reading/writing
  EEPROM.begin(EEPROM_SIZE);

  // Initialize the camera configuration
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.frame_size = FRAMESIZE_VGA;
  config.pixel_format = PIXFORMAT_JPEG;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 15; 
  config.fb_count = 1;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed with error 0x%x\n", err);
    return;
  }

  // WiFi接続の試行を無効化
   connectToWifi();

   //WiFi接続部分も無効化
   if (WiFi.status() != WL_CONNECTED) {
     setupWebServer();
     return;
   }

  // Start mDNS service
  if (!MDNS.begin("nanoface")) {
    Serial.println("Error setting up MDNS responder!");
    while (1) {
      delay(1000);
    }
  }

  // Start the camera server
  startCameraServer();
  Serial.print("Camera Ready! Use 'http://");
  Serial.print(WiFi.localIP());
  Serial.println("' to connect");

  // Set up the LED on pin 14
  pinMode(14, OUTPUT);
  digitalWrite(14, LOW); // Turn LED on
}

void connectToWifi() {
  String ssid = "";
  String password = "";

  for (int i = 0; i < 32; ++i) {
    char s = EEPROM.read(SSID_ADDR + i);
    if (s == 0) break; // Stop reading if we hit a null character
    ssid += s;
  }

  for (int i = 0; i < 32; ++i) {
    char p = EEPROM.read(PASSWD_ADDR + i);
    if (p == 0) break; // Stop reading if we hit a null character
    password += p;
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), password.c_str());
  Serial.print("Connecting to WiFi..");

  unsigned long startTime = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startTime < 20000) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi connected.");
  } else {
    Serial.println("Failed to connect, entering AP Mode...");
    WiFi.mode(WIFI_AP);
    WiFi.softAP("NanoFace", "nanofacepass");
    setupWebServer();
  }
}

void setupWebServer() {
  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.begin();
  Serial.println("HTTP server started.");
}



void handleRoot() {


  String html = "<!DOCTYPE html><html><body>"
                "<h1>Setup WiFi</h1>"
                "<form action=\"/save\" method=\"post\">"
                "SSID:<br><input type=\"text\" name=\"ssid\"><br>"
                "Password:<br><input type=\"password\" name=\"password\"><br><br>"
                "<input type=\"submit\" value=\"Save\">"
                "</form>"
                "</body></html>";
  server.send(200, "text/html", html);
}

void handleSave() {
  String ssid = server.arg("ssid");
  String password = server.arg("password");

  // Save WiFi settings to EEPROM
  for (int i = 0; i < ssid.length() && i < 32; i++) {
    EEPROM.write(SSID_ADDR + i, ssid[i]);
  }
  for (int i = ssid.length(); i < 32; i++) {
    EEPROM.write(SSID_ADDR + i, 0); // Clear the rest
  }
  for (int i = 0; i < password.length() && i < 32; i++) {
    EEPROM.write(PASSWD_ADDR + i, password[i]);
  }
  for (int i = password.length(); i < 32; i++) {
    EEPROM.write(PASSWD_ADDR + i, 0); // Clear the rest
  }
  EEPROM.commit();

  server.send(200, "text/html", "<h1>Settings saved. Please reset the device.</h1>");
  delay(3000);
  ESP.restart();
}

void loop() {
  // WiFi接続を確認しない
  // if (WiFi.status() != WL_CONNECTED) {
    server.handleClient();
  // }
  
  // Keep the LED on
  digitalWrite(14, LOW);
}
