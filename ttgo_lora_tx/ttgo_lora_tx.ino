// Transmisor LoRa de prueba para TTGO (ESP32 + SX1276), librería "LoRa" de Sandeep Mistry.
// Envía "Hola mundo N" cada segundo. Se recibe con ../sdr_lora.py
//
// OJO (18-10-2026): la TTGO conectada al PC estaba transmitiendo con SF12 / BW 125 kHz
// (verificado con el RTL-SDR), no con la configuración de abajo: probablemente tiene grabada
// otra versión del sketch. Si se vuelve a flashear este, debería verse SF7 / BW 500 kHz.

#include <SPI.h>
#include <LoRa.h>

#define LORA_SCK   5
#define LORA_MISO  19
#define LORA_MOSI  27
#define LORA_SS    18
#define LORA_RST   23
#define LORA_DIO0  26

int contador = 0;

void setup() {
  Serial.begin(115200);
  delay(1000);

  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_SS);
  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);

  Serial.println("Iniciando LoRa transmisor...");

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

  Serial.println("LoRa TX OK");
  Serial.println("915 MHz | BW 500 kHz | SF7 | CR 4/5 | CRC ON");
}

void loop() {
  String mensaje = "Hola mundo " + String(contador);

  LoRa.beginPacket();
  LoRa.print(mensaje);
  LoRa.endPacket();

  Serial.print("Enviado: ");
  Serial.println(mensaje);

  contador++;
  delay(1000);
}
