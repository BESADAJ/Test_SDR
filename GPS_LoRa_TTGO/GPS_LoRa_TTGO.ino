#include <SPI.h>
#include <LoRa.h>
#include <TinyGPSPlus.h>

#define LORA_SCK   5
#define LORA_MISO  19
#define LORA_MOSI  27
#define LORA_SS    18
#define LORA_RST   23
#define LORA_DIO0  26

#define GPS_RX     13  // al TX del NEO-6M
#define GPS_TX     12  // al RX del NEO-6M

HardwareSerial gpsSerial(2);
TinyGPSPlus gps;

const int NUM_MUESTRAS = 10;

double sumaLat = 0;
double sumaLon = 0;
int muestras = 0;

unsigned long ultimaSalida = 0;

void setup() {
  Serial.begin(115200);
  delay(1000);

  gpsSerial.begin(9600, SERIAL_8N1, GPS_RX, GPS_TX);

  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_SS);
  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);

  if (!LoRa.begin(915E6)) {
    Serial.println("Error iniciando LoRa");
    while (true) {
      delay(1000);
    }
  }

  LoRa.setTxPower(14);
  LoRa.setSignalBandwidth(500E3);
  LoRa.setSpreadingFactor(7);
  LoRa.setCodingRate4(5);
  LoRa.enableCrc();

  Serial.println("GPS + LoRa TX OK. Esperando posiciones validas...");
}

void loop() {
  while (gpsSerial.available()) {
    gps.encode(gpsSerial.read());
  }

  if (gps.location.isUpdated() &&
      gps.location.isValid() &&
      gps.satellites.isValid() &&
      gps.satellites.value() >= 4 &&
      gps.hdop.isValid() &&
      gps.hdop.hdop() <= 3.0) {

    sumaLat += gps.location.lat();
    sumaLon += gps.location.lng();
    muestras++;
  }

  if (muestras >= NUM_MUESTRAS &&
      millis() - ultimaSalida >= 1000) {

    String mensaje = String(sumaLat / muestras, 7) + "," + String(sumaLon / muestras, 7);

    LoRa.beginPacket();
    LoRa.print(mensaje);
    LoRa.endPacket();

    Serial.println(mensaje);

    sumaLat = 0;
    sumaLon = 0;
    muestras = 0;
    ultimaSalida = millis();
  }
}
