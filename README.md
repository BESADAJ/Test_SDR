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

---

# Interfaz gráfica (`servidor_gps.py` + carpeta `web/`)

Une todo en el navegador: el receptor LoRa (`sdr_lora.py`), la posición del GPS de la TTGO en un
mapa y los reportes de FM (`sdr_analisis.py`). `servidor_gps.py` reutiliza el `Receptor` de
`sdr_lora.py` tal cual y le manda a la página cada paquete por Server-Sent Events.

```powershell
# En vivo con el RTL-SDR (cierra SDR++ antes) con los parámetros del sketch
.\venv\Scripts\python.exe servidor_gps.py --freq 915 --sf 7 --bw 500

# Igual que sdr_lora.py: sin --sf/--bw autodetecta
.\venv\Scripts\python.exe servidor_gps.py --freq 915

# Sin hardware: recorrido simulado alrededor de Uninorte
.\venv\Scripts\python.exe servidor_gps.py --sim

# Con una grabación baseband (de SDR++ o de generar_prueba_lora.py), a velocidad real y en bucle
.\venv\Scripts\python.exe generar_prueba_lora.py
.\venv\Scripts\python.exe servidor_gps.py --archivo grabaciones\baseband_914500000Hz_lora_prueba.wav --freq 915 --repetir
```

Se abre solo en `http://localhost:8000`. Con `--host 0.0.0.0` se puede ver desde el celular en la
misma red (`http://<IP del PC>:8000`). No necesita librerías nuevas: usa solo la biblioteca
estándar de Python más lo que ya pide `requirements.txt`.

## Pestaña "Rastreo LoRa"
- **Mapa** (Leaflet; calles, OpenStreetMap, satélite u oscuro): recorrido con cada punto coloreado
  por SNR, vehículo con flecha de rumbo, estación base (clic en "Ubicar base" o tu ubicación) y
  línea de enlace con la distancia. Clic en un punto: detalle del paquete.
- **Posición**: lat/lon, distancia y rumbo desde la base, velocidad, distancia recorrida.
- **Enlace**: SNR con margen sobre el límite de demodulación del SF, potencia (dBFS), offset del
  transmisor (Hz y ppm), % de CRC correcto, paquetes por minuto, bits corregidos por Hamming,
  headers inválidos, tiempo al aire, ciclo de trabajo, coding rate y sync word.
- **Receptor**: frecuencia, centro sintonizado, fs, SF, BW, símbolo, LDRO, ganancia, ppm y nivel
  del ADC (avisa saturación).
- **Cascada** de los 2 MHz capturados con la traza del espectro (retención de máximos opcional)
  y el canal LoRa marcado; cada paquete deja una marca ámbar al borde.
- **Historia del enlace**: SNR, potencia y offset en el tiempo, y **SNR vs distancia** con ajuste
  log-distancia (exponente de pérdidas *n* y alcance estimado para el SF actual).
- **Registro** de paquetes (búsqueda, filtro de CRC) y de eventos; inspector con estructura de la
  trama en símbolos y volcado hex. Exporta CSV, GeoJSON y KML (Google Earth).
- El servidor guarda además cada paquete en `registros/lora_<fecha>.csv`.

## Pestaña "Emisora FM"
- Lista los reportes de `resultados/` y muestra: frecuencia y PS (nombre) estilo radio, RadioText,
  estéreo/RDS/TP/TA/música-voz, PTY, hora CT, PI, AF; RF (SNR, potencia, piso, saturación); % de
  modulación y desviación; esquema de la banda MPX con los niveles medidos de piloto y RDS; audio
  (niveles, correlación I/D, reproductor de `audio.wav`); grupos RDS por tipo; y `graficas.png`.
- **Capturar emisora** corre `sdr_analisis.py en_vivo` desde la página: el receptor LoRa suelta el
  dongle mientras dura la captura y luego lo retoma solo. También analiza un WAV de SDR++.
- Sin servidor, "Abrir reporte.json" carga una carpeta de resultados elegida a mano.

Si abres `web/index.html` directamente (sin servidor) aparece el botón **Ver demostración**, que
simula todo en el navegador. Leaflet y las fuentes van incluidos en `web/vendor/`; solo el fondo
del mapa necesita internet.
