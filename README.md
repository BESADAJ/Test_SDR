# Test_SDR — Analizador de FM (WFM) con RTL-SDR

Script en Python que captura directamente de un RTL-SDR (probado con NooElec R820T) y extrae
los datos de una emisora FM comercial, sin necesidad de SDR++.

## Qué entrega
- **RF**: espectro, cascada (waterfall), potencia del canal, piso de ruido, SNR, saturación
- **FM**: desviación pico/RMS, % de modulación, error de sintonía y ppm del dongle
- **MPX**: piloto estéreo de 19 kHz, nivel/SNR de la subportadora RDS de 57 kHz
- **Audio**: `audio.wav` estéreo 48 kHz con de-énfasis
- **RDS**: PI, PS (nombre), RadioText, PTY, TP/TA, música/voz, AF, hora (CT)

Resultados en `resultados/<fecha>/`: `reporte.txt`, `reporte.json`, `graficas.png`, `audio.wav`
y el IQ crudo `baseband_...wav` (reproducible en SDR++ con Source → File).

## Instalación (Windows)
```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```
El dongle debe tener el driver WinUSB (instalado con Zadig, igual que para SDR++).

## Uso
Cierra SDR++ u otro programa que use el dongle.

```powershell
# En vivo
.\venv\Scripts\python.exe sdr_analisis.py en_vivo --freq 103.1 --segundos 10 --ganancia 20

# Desde una grabación baseband WAV de SDR++
.\venv\Scripts\python.exe sdr_analisis.py archivo "baseband_103100000Hz_....wav" --freq 103.1
```

Opciones útiles:
- `--ganancia` dB o `auto` (si el reporte avisa saturación, bájala)
- `--ppm` corrección del cristal del dongle (por defecto 49, medido en un NooElec; cada dongle
  es distinto: ajústalo con el valor `sumar_a_--ppm` del reporte)
- `--deenfasis` 75 µs (América) o 50 µs (Europa)

## Prueba sin hardware
`generar_prueba.py` crea una señal FM estéreo sintética con RDS (PS "UNINORTE") en
`grabaciones/`, para probar el modo `archivo`:
```powershell
.\venv\Scripts\python.exe generar_prueba.py
```

---

# Receptor LoRa (`sdr_lora.py`)

Escucha continuamente con el RTL-SDR y decodifica los paquetes LoRa (capa física CSS, header
explícito, como los envía la librería `LoRa.h` / SX127x). No guarda nada: imprime cada paquete
hasta que pulses Ctrl+C.

Por paquete muestra: texto y hex del payload, CRC, largo, coding rate, símbolos y tiempo al aire,
bits corregidos por Hamming, SNR, potencia (dBFS), offset de frecuencia del transmisor (Hz/ppm)
y sync word (0x12 = LoRa privado, 0x34 = LoRaWAN).

```powershell
# Autodetecta SF y BW con el primer paquete que oiga
.\venv\Scripts\python.exe sdr_lora.py --freq 915

# Parámetros fijos
.\venv\Scripts\python.exe sdr_lora.py --freq 915 --sf 7 --bw 500

# Desde una grabación baseband IQ (WAV de SDR++ o .npy)
.\venv\Scripts\python.exe sdr_lora.py --archivo captura.wav --centro 914.5 --freq 915
```

- `--ganancia` (def. 10 dB): con la TTGO al lado del dongle el ADC satura incluso en 0 dB;
  igual decodifica, pero el SNR mostrado es poco fiable. Súbela si el transmisor está lejos.
- Se sintoniza 500 kHz por debajo (`--fs`/4) para alejar el pico de DC.
- Con LoRaWAN, el payload sale como bytes cifrados (MHDR, DevAddr, FCnt, ... MIC).

El sketch del transmisor está en [`GPS_LoRa_TTGO/`](GPS_LoRa_TTGO/GPS_LoRa_TTGO.ino): la TTGO lee un GPS NEO-6M
(TinyGPSPlus), promedia 10 posiciones válidas (≥4 satélites, HDOP ≤ 3) y envía `lat,lon` por LoRa.
