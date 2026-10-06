"""
Genera una grabación baseband IQ sintética (formato SDR++) de una emisora FM estéreo con RDS,
para probar sdr_analisis.py sin necesidad del dongle.

  python generar_prueba.py            -> grabaciones/baseband_102850000Hz_....wav
"""

import datetime as dt
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

from sdr_analisis import OFFSETS, rds_crc

FS = 1.2e6
SEG = 8
FREQ = 103.1e6
CENTRO = FREQ - 250e3
PI, PTY, PS = 0x7A31, 23, "UNINORTE"
RT = "Uninorte Estereo 103.1 FM - prueba de RDS sintetico"


def bloque(dato, off):
    return (dato << 10) | (rds_crc(dato) ^ OFFSETS[off])


def grupos():
    gs = []
    ahora = dt.datetime.now(dt.timezone.utc)
    mjd = (ahora.date() - dt.date(1858, 11, 17)).days
    rt = RT.ljust(64)[:64]
    for seg in range(4):
        b = (0 << 12) | (1 << 10) | (PTY << 5) | (1 << 3) | ((1 if seg == 3 else 0) << 2) | seg
        c = (225 << 8) | round((FREQ / 1e6 - 87.5) / 0.1)
        d = (ord(PS[2 * seg]) << 8) | ord(PS[2 * seg + 1])
        gs.append((PI, b, c, d))
    for seg in range(16):
        b = (2 << 12) | (1 << 10) | (PTY << 5) | seg
        s = rt[4 * seg:4 * seg + 4]
        gs.append((PI, b, (ord(s[0]) << 8) | ord(s[1]), (ord(s[2]) << 8) | ord(s[3])))
    b = (4 << 12) | (1 << 10) | (PTY << 5) | ((mjd >> 15) & 3)
    c = ((mjd & 0x7FFF) << 1) | (ahora.hour >> 4)
    d = ((ahora.hour & 0xF) << 12) | (ahora.minute << 6) | (1 << 5) | 10   # UTC-5
    gs.append((PI, b, c, d))
    return gs


def bits_rds(n_bits):
    bits = []
    gs = grupos()
    while len(bits) < n_bits:
        for a, b, c, d in gs:
            for w, o in ((a, "A"), (b, "B"), (c, "C"), (d, "D")):
                bits += [(bloque(w, o) >> (25 - i)) & 1 for i in range(26)]
    return np.array(bits[:n_bits])


def main():
    n = int(FS * SEG)
    t = np.arange(n) / FS
    izq = 0.8 * np.sin(2 * np.pi * 1000 * t)
    der = 0.8 * np.sin(2 * np.pi * 3000 * t)
    th = 2 * np.pi * 19000 * t

    nb = int(SEG * 1187.5) + 2
    d = bits_rds(nb)
    e = np.zeros(nb, int)
    for i in range(1, nb):
        e[i] = d[i] ^ e[i - 1]                      # codificación diferencial
    s = 2 * e - 1
    chip = np.floor(t * 2375).astype(int)
    rds = s[chip // 2] * np.where(chip % 2 == 0, 1.0, -1.0)
    rds = signal.sosfiltfilt(signal.butter(4, 2400, fs=FS, output="sos"), rds)

    mpx = 0.45 * (izq + der) / 2 + 0.45 * (izq - der) / 2 * np.cos(2 * th) \
        + 0.09 * np.cos(th) + 0.04 * rds * np.sin(3 * th)
    fase = 2 * np.pi * np.cumsum(75e3 * mpx) / FS
    iq = 0.3 * np.exp(1j * (fase + 2 * np.pi * (FREQ - CENTRO) * t))
    iq += 0.01 * (np.random.randn(n) + 1j * np.random.randn(n))

    out = Path(__file__).parent / "grabaciones"
    out.mkdir(exist_ok=True)
    ruta = out / f"baseband_{int(CENTRO)}Hz_{dt.datetime.now():%H-%M-%S_%d-%m-%Y}.wav"
    data = np.stack([iq.real, iq.imag], axis=1)
    wavfile.write(ruta, int(FS), (data * 32767).astype(np.int16))
    print(ruta)


if __name__ == "__main__":
    main()
