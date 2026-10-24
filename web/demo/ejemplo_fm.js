/* Reporte de ejemplo: sdr_analisis.py sobre una señal FM estéreo + RDS sintética. */
window.SDR = window.SDR || {};
window.SDR.EJEMPLO_FM = {
 "fuente": {
  "modo": "archivo",
  "ruta": "grabaciones/baseband_102850000Hz_12-00-00_23-10-2026.wav",
  "duracion_s": 8.0,
  "centro_MHz": 102.85,
  "emisora_MHz": 103.1,
  "fs_Hz": 1200000
 },
 "rf": {
  "potencia_canal_dBFS": -6.1,
  "piso_ruido_dBFS_por_bin": -67.12,
  "SNR_dB": 35.7,
  "pico_espectro_MHz": 103.1008,
  "potencia_canal_min_max_dBFS": [
   -6.1,
   -6.09
  ],
  "resolucion_fft_Hz": 585.9,
  "muestras_saturadas_pct": 0.0
 },
 "fm": {
  "error_sintonia_Hz": -1.1,
  "error_ppm_estimado": -0.01,
  "sumar_a_--ppm": 0,
  "desviacion_pico_kHz": 38.86,
  "desviacion_rms_kHz": 11.28,
  "modulacion_pct": 51.8
 },
 "mpx": {
  "piloto_estereo": true,
  "piloto_nivel_kHz": 6.68,
  "piloto_pct_desviacion": 8.9,
  "piloto_frecuencia_medida_Hz": 19000.0,
  "error_reloj_muestreo_ppm": -0.0,
  "rds_nivel_kHz": 2.53,
  "rds_SNR_dB": 21.2
 },
 "audio": {
  "archivo": "resultados/ejemplo/audio.wav",
  "frecuencia_muestreo_Hz": 48000,
  "estereo": true,
  "rms_izq_dBFS": -11.4,
  "rms_der_dBFS": -11.4,
  "correlacion_izq_der": 0.04,
  "deenfasis_us": 75
 },
 "rds": {
  "grupos_validos": 88,
  "tipos_grupo": {
   "0A": 38,
   "2A": 40,
   "4A": 10
  },
  "PI": "0xC52E",
  "PTY": 10,
  "TP": true,
  "TA": false,
  "MS": "música",
  "estereo_DI": true,
  "AF_MHz": [
   95.5,
   103.1
  ],
  "CT": "2026-10-23 11:16 (UTC-5.0h)",
  "PS": "UNINORTE",
  "RadioText": "Reto SDR Grupo 5 - Rastreo vehicular GPS/LoRa en 915 MHz",
  "PTY_nombre": {
   "RDS (Europa/LatAm)": "Música pop",
   "RBDS (Norteamérica)": "Country"
  },
  "bits_recibidos": 9261
 }
};
