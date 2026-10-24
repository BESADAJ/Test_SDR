"""
Genera una grabación baseband IQ (WAV, igual a las de SDR++) con paquetes LoRa sintéticos
'lat,lon', modulados con las mismas tablas que usa sdr_lora.py para decodificar (whitening,
Hamming, entrelazado, Gray, header explícito, CRC). Sirve para probar sdr_lora.py y
servidor_gps.py sin la TTGO ni el dongle.

  .\\venv\\Scripts\\python.exe generar_prueba_lora.py
  .\\venv\\Scripts\\python.exe servidor_gps.py --archivo grabaciones\\baseband_914500000Hz_lora_prueba.wav --freq 915 --repetir

Parámetros: SF7, BW 500 kHz, CR 4/5, CRC activado, sync word 0x12 (como GPS_LoRa_TTGO.ino),
fs = 2 MS/s, centro 914.5 MHz, offset del transmisor de -2.93 kHz.
"""

import argparse
import math
import wave
from pathlib import Path

import numpy as np

import sdr_lora as sl

SF, BW, CR = 7, 500e3, 1
N = 1 << SF


def gray_inv(u):
    v = 0
    while u:
        v ^= u
        u >>= 1
    return v


def entrelazar(cws, sf_app, cw_len):
    """Inverso de sdr_lora.desentrelazar: sf_app codewords -> cw_len símbolos."""
    sims = []
    for i in range(cw_len):
        s = 0
        for j in range(sf_app):
            bit = (cws[(i - j - 1) % sf_app] >> (cw_len - 1 - i)) & 1
            s |= bit << (sf_app - 1 - j)
        sims.append(s)
    return sims


def bins_paquete(datos):
    L = len(datos)
    n0, n1, n2 = L >> 4, L & 0xF, (CR << 1) | 1
    chk = sl.checksum_header(n0, n1, n2)
    header = [n0, n1, n2, (chk >> 4) & 1, chk & 0xF]
    blanqueado = bytes(np.frombuffer(datos, np.uint8) ^ sl.BLANQUEO[:L])
    nibs = []
    for b in blanqueado:
        nibs += [b & 0xF, b >> 4]
    crc = sl.crc16_lora(datos)
    nibs += [crc & 0xF, (crc >> 4) & 0xF, (crc >> 8) & 0xF, (crc >> 12) & 0xF]
    while len(nibs) % SF:
        nibs.append(0)
    bins = [4 * gray_inv(v) for v in entrelazar([sl.hamming_cod(n, 4) for n in header], SF - 2, 8)]
    for k in range(0, len(nibs), SF):
        cws = [sl.hamming_cod(n, CR) for n in nibs[k:k + SF]]
        bins += [gray_inv(v) for v in entrelazar(cws, SF, 4 + CR)]
    return bins


def frecuencia_chirp(bin_, fs, n_sim=1.0, bajada=False):
    """Frecuencia instantánea (Hz) de un chirp de fase continua desplazado bin_ posiciones."""
    t = np.arange(int(round(n_sim * N * fs / BW))) / fs
    f = (bin_ / N) * BW + BW * t / (N / BW)
    f = (f % BW) - BW / 2
    return -f if bajada else f


def paquete(datos, fs):
    partes = [frecuencia_chirp(0, fs) for _ in range(8)]                 # preámbulo (LoRa.h: 8)
    partes += [frecuencia_chirp(8, fs), frecuencia_chirp(16, fs)]        # sync word 0x12
    partes.append(frecuencia_chirp(0, fs, 2.25, bajada=True))            # SFD
    partes += [frecuencia_chirp(b, fs) for b in bins_paquete(datos)]
    return np.concatenate(partes)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--segundos", type=float, default=12, help="duración (2 MS/s: ~8 MB por segundo)")
    ap.add_argument("--intervalo", type=float, default=1.0, help="segundos entre paquetes")
    ap.add_argument("--salida", default="grabaciones")
    a = ap.parse_args()

    fs, freq, centro = 2.0e6, 915e6, 914.5e6
    n = int(a.segundos * fs)
    finst = np.zeros(n)
    amp = np.zeros(n)
    base = (11.0190, -74.8505)          # Uninorte
    t_pkt, k = 0.4, 0
    while t_pkt < a.segundos - 0.1:
        d = 150 + 12 * k * a.intervalo   # se aleja ~43 km/h hacia el nororiente
        lat = base[0] + d / 111320 * math.cos(math.radians(40))
        lon = base[1] + d / (111320 * math.cos(math.radians(base[0]))) * math.sin(math.radians(40))
        f = paquete(f"{lat:.7f},{lon:.7f}".encode(), fs)
        i0 = int(t_pkt * fs)
        f = f[: n - i0]
        finst[i0:i0 + len(f)] = f
        amp[i0:i0 + len(f)] = 0.3 * (150 / d) ** 1.4
        t_pkt += a.intervalo
        k += 1

    t = np.arange(n) / fs
    fase = 2 * np.pi * np.cumsum(finst) / fs + 2 * np.pi * ((freq - centro) - 2930.0) * t
    rng = np.random.default_rng(1)
    iq = amp * np.exp(1j * fase) + 0.004 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    iq += 0.05 * np.exp(1j * 2 * np.pi * -640e3 * t)    # una portadora interferente

    salida = Path(a.salida)
    salida.mkdir(parents=True, exist_ok=True)
    ruta = salida / f"baseband_{int(centro)}Hz_lora_prueba.wav"
    x = np.empty(2 * n, np.int16)
    x[0::2] = np.clip(iq.real * 32767, -32768, 32767)
    x[1::2] = np.clip(iq.imag * 32767, -32768, 32767)
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(fs))
        w.writeframes(x.tobytes())
    print(f"{ruta}  ({k} paquetes, {a.segundos:g} s, {ruta.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
