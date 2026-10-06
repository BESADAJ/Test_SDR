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
