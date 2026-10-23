"""
Receptor / decodificador LoRa (capa física, chirps CSS) para RTL-SDR (NooElec R820T).

Escucha continuamente y decodifica cada paquete LoRa que oye, mostrando:
  - Payload (texto y hex), longitud, coding rate, CRC del payload y checksum del header
  - SNR, potencia (dBFS), offset de frecuencia (Hz y ppm), sync word

Decodifica LoRa "crudo" (el que envía la librería LoRa.h de Arduino / SX127x, header explícito).
LoRaWAN va encima de esto: el payload saldrá como bytes cifrados (MHDR, DevAddr, ...).

Uso (antes cierra SDR++, solo un programa puede usar el dongle):
  # Sin --sf/--bw detecta automáticamente SF y BW con el primer paquete que oiga
  python sdr_lora.py --freq 915

  # Fijando los parámetros (p. ej. los del sketch GPS_LoRa_TTGO: BW 500 kHz, SF7)
  python sdr_lora.py --freq 915 --sf 7 --bw 500

  # Desde una grabación baseband IQ (WAV de SDR++ o .npy complejo)
  python sdr_lora.py --archivo captura.wav --centro 914.5 --freq 915

Ctrl+C para detener.
"""

import argparse
import datetime as dt
import queue
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
from scipy import signal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHUNK = 262144          # muestras IQ por bloque (múltiplo de 512, requerido por librtlsdr)
OS = 4                  # sobremuestreo interno (muestras por chip) para sincronizar con 1/4 de muestra
PREAMBULO_MIN = 4       # upchirps iguales seguidos para declarar un preámbulo
PAPR_MIN = 8.0          # pico/promedio mínimo del FFT para considerar que hay un chirp


# ----------------------------------------------------------------------------------------
# Tablas y codificación LoRa (según el trabajo de ingeniería inversa de gr-lora_sdr, EPFL)
# ----------------------------------------------------------------------------------------

def _blanqueo():
    """Secuencia de whitening del SX127x: LFSR x^8+x^6+x^5+x^4+1, semilla 0xFF."""
    s, out = 0xFF, []
    for _ in range(255):
        out.append(s)
        s = ((s << 1) & 0xFF) | (((s >> 7) ^ (s >> 5) ^ (s >> 4) ^ (s >> 3)) & 1)
    return np.array(out, np.uint8)


BLANQUEO = _blanqueo()


def hamming_cod(nib, cr):
    """Codifica un nibble con CR 4/(4+cr). Devuelve el codeword (MSB primero = bit 0 del dato)."""
    d0, d1, d2, d3 = [(nib >> i) & 1 for i in range(4)]
    if cr == 1:
        return d0 << 4 | d1 << 3 | d2 << 2 | d3 << 1 | (d0 ^ d1 ^ d2 ^ d3)
    p0 = d0 ^ d1 ^ d2
    p1 = d1 ^ d2 ^ d3
    p2 = d0 ^ d1 ^ d3
    p3 = d0 ^ d2 ^ d3
    return (d0 << 7 | d1 << 6 | d2 << 5 | d3 << 4 | p0 << 3 | p1 << 2 | p2 << 1 | p3) >> (4 - cr)


CODEWORDS = {cr: np.array([hamming_cod(n, cr) for n in range(16)]) for cr in (1, 2, 3, 4)}


def hamming_dec(cw, cr):
    """Decodifica por distancia mínima. Devuelve (nibble, bits_corregidos)."""
    dist = np.array([bin(int(c) ^ int(cw)).count("1") for c in CODEWORDS[cr]])
    n = int(dist.argmin())
    return n, int(dist[n])


def checksum_header(n0, n1, n2):
    b = lambda v, i: (v >> i) & 1
    c4 = b(n0, 3) ^ b(n0, 2) ^ b(n0, 1) ^ b(n0, 0)
    c3 = b(n0, 3) ^ b(n1, 3) ^ b(n1, 2) ^ b(n1, 1) ^ b(n2, 0)
    c2 = b(n0, 2) ^ b(n1, 3) ^ b(n1, 0) ^ b(n2, 3) ^ b(n2, 1)
    c1 = b(n0, 1) ^ b(n1, 2) ^ b(n1, 0) ^ b(n2, 2) ^ b(n2, 1) ^ b(n2, 0)
    c0 = b(n0, 0) ^ b(n1, 1) ^ b(n2, 3) ^ b(n2, 2) ^ b(n2, 1) ^ b(n2, 0)
    return c4 << 4 | c3 << 3 | c2 << 2 | c1 << 1 | c0


def crc16_lora(datos):
    """CRC del payload tal como lo calcula el SX127x (CCITT 0x1021 sobre len-2 bytes, XOR con los 2 últimos)."""
    if len(datos) < 2:
        return 0
    crc = 0
    for byte in datos[:-2]:
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if ((crc >> 8) ^ byte) & 0x80 else (crc << 1)
            crc &= 0xFFFF
            byte = (byte << 1) & 0xFF
    return crc ^ datos[-1] ^ (datos[-2] << 8)


def desentrelazar(simbolos, sf_app, cw_len):
    """Bloque de cw_len símbolos (sf_app bits c/u) -> sf_app codewords de cw_len bits."""
    cws = [0] * sf_app
    for i, s in enumerate(simbolos):
        for j in range(sf_app):
            bit = (s >> (sf_app - 1 - j)) & 1
            fila = (i - j - 1) % sf_app
            cws[fila] |= bit << (cw_len - 1 - i)
    return cws


# ----------------------------------------------------------------------------------------
# Demodulador
# ----------------------------------------------------------------------------------------

class LoRa:
    def __init__(self, sf, bw, ldro=None):
        self.sf, self.bw = sf, bw
        self.N = 1 << sf
        n = np.arange(self.N)
        self.up = np.exp(2j * np.pi * (n * n / (2 * self.N) - n / 2)).astype(np.complex64)
        self.down = np.conj(self.up)
        # LoRa.h activa Low Data Rate Optimize si el símbolo dura más de 16 ms
        self.ldro = (self.N / bw > 16e-3) if ldro is None else ldro

    def fft_dechirp(self, ventanas, bajada=False):
        ref = self.up if bajada else self.down
        return np.fft.fft(ventanas * ref, axis=-1)

    def pico_fino(self, x, bajada=False, zp=8):
        """Bin fraccional del tono tras dechirp (promedia ventanas no coherentemente). Rango [-N/2, N/2)."""
        X = np.abs(np.fft.fft(x * (self.up if bajada else self.down), n=self.N * zp, axis=-1)) ** 2
        if X.ndim > 1:
            X = X.mean(0)
        k = int(X.argmax())
        a, b, c = X[k - 1], X[k], X[(k + 1) % len(X)]
        d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
        f = (k + d) / zp
        return (f + self.N / 2) % self.N - self.N / 2

    def demod(self, x1, n_sim, est, n_reducidos):
        """Bins de cada símbolo siguiendo la deriva de reloj TX/RX (que en SF altos mueve bins enteros).

        est: desfase inicial en bins (medido en el preámbulo). Los primeros n_reducidos símbolos
        son de tasa reducida (solo usan bins múltiplos de 4), lo que da una referencia más robusta.
        """
        zp = 4
        v = x1[: n_sim * self.N].reshape(n_sim, self.N)
        X = np.abs(np.fft.fft(v * self.down, n=self.N * zp, axis=-1)) ** 2
        bins = np.zeros(n_sim, int)
        for i in range(n_sim):
            k = int(X[i].argmax())
            a, b, c = X[i, k - 1], X[i, k], X[i, (k + 1) % len(X[i])]
            den = a - 2 * b + c
            f = (k + (0.5 * (a - c) / den if den else 0.0)) / zp
            r = f - est
            paso = 4 if i < n_reducidos else 1
            q = paso * np.round(r / paso)
            e = ((r - q + self.N / 2) % self.N) - self.N / 2
            est += 0.3 * e if paso == 1 else 0.5 * e
            bins[i] = int(q) % self.N
        return bins

    def n_simbolos_payload(self, largo, cr, crc):
        sf_app = self.sf - 2 if self.ldro else self.sf
        nibbles = 2 * largo + (4 if crc else 0) - (self.sf - 2 - 5)   # el 1er bloque lleva sf-2-5 nibbles
        return 8 + max(0, int(np.ceil(nibbles / sf_app))) * (4 + cr)


def simbolos_a_valores(bins, sf, reducido, off=0):
    """bin del FFT (relativo al preámbulo) -> valor del símbolo, con mapeo Gray.

    En tasa reducida (header y LDRO) el SX127x solo usa bins múltiplos de 4 (verificado con una
    TTGO real). En tasa normal se prueba off = 0 / -1 y se queda el que pase el CRC."""
    N = 1 << sf
    b = np.asarray(bins).astype(int)
    v = (b // 4) % (N // 4) if reducido else (b + off) % N
    return v ^ (v >> 1)


def decodificar_header(bins, sf):
    vals = simbolos_a_valores(bins[:8], sf, True)
    cws = desentrelazar(vals, sf - 2, 8)
    nibs, corr = zip(*[hamming_dec(c, 4) for c in cws])
    largo = nibs[0] << 4 | nibs[1]
    cr = nibs[2] >> 1
    crc = nibs[2] & 1
    chk = (nibs[3] & 1) << 4 | nibs[4]
    ok = chk == checksum_header(*nibs[:3]) and 1 <= cr <= 4
    return {"largo": largo, "cr": cr, "crc": bool(crc), "ok": ok, "extra": list(nibs[5:]),
            "bits_corregidos": sum(corr)}


def decodificar_payload(bins, sf, ldro, h, off=0):
    nibs = list(h["extra"])
    corr = 0
    sf_app = sf - 2 if ldro else sf
    cw_len = 4 + h["cr"]
    resto = bins[8:]
    for b in range(0, len(resto) - cw_len + 1, cw_len):
        vals = simbolos_a_valores(resto[b:b + cw_len], sf, ldro, off)
        for c in desentrelazar(vals, sf_app, cw_len):
            n, k = hamming_dec(c, h["cr"])
            nibs.append(n)
            corr += k
    L = h["largo"]
    raw = bytes((nibs[2 * i] | nibs[2 * i + 1] << 4) for i in range(L) if 2 * i + 1 < len(nibs))
    datos = bytes(np.frombuffer(raw, np.uint8) ^ BLANQUEO[: len(raw)])
    crc_ok = None
    if h["crc"] and len(nibs) >= 2 * L + 4:
        c = nibs[2 * L: 2 * L + 4]
        crc_rx = c[0] | c[1] << 4 | c[2] << 8 | c[3] << 12
        crc_ok = crc_rx == crc16_lora(datos)
    return datos, crc_ok, corr


# ----------------------------------------------------------------------------------------
# Receptor en flujo continuo
# ----------------------------------------------------------------------------------------

class Receptor:
    """Recibe bloques IQ crudos (fs), los baja a OS muestras/chip y busca/decodifica paquetes."""

    def __init__(self, fs, offset_hz, sf, bw, ldro=None, freq_hz=None):
        self.lora = LoRa(sf, bw, ldro)
        self.fs, self.offset = fs, offset_hz
        self.freq_hz = freq_hz
        d = fs / (OS * bw)
        if abs(d - round(d)) > 1e-6:
            raise ValueError(f"fs={fs} no es múltiplo entero de {OS}*BW={OS * bw}")
        self.D = int(round(d))
        self.h = signal.firwin(24 * self.D + 1, 0.55 * bw, fs=fs).astype(np.float32)
        self.cola = np.zeros(len(self.h) - 1, np.complex64)
        self.n_raw = 0                     # muestras crudas consumidas
        self.buf = np.zeros(0, np.complex64)  # señal a OS muestras/chip
        self.base = 0                      # índice absoluto (en OS) de buf[0]
        self.p = 0                         # puntero de búsqueda (índice absoluto en OS)
        self.racha, self.bin_prev = 0, None
        self.pend = None                   # paquete en curso
        self.max_raw = 0.0
        self.off_normal = 0                # offset de símbolo en tasa normal (se autoajusta con el CRC)
        self.headers_malos = 0

    # ---- entrada ----
    def alimentar(self, x):
        n = np.arange(len(x))
        fase = (self.n_raw * self.offset / self.fs) % 1.0
        x = x * np.exp(-2j * np.pi * (fase + n * self.offset / self.fs)).astype(np.complex64)
        self.max_raw = max(self.max_raw, float(np.max(np.abs(x))))
        y = signal.oaconvolve(np.concatenate([self.cola, x]), self.h, mode="valid").astype(np.complex64)
        self.cola = np.concatenate([self.cola, x])[-(len(self.h) - 1):]
        primero = (-self.n_raw) % self.D
        self.n_raw += len(x)
        self.buf = np.concatenate([self.buf, y[primero::self.D]])
        return self._procesar()

    def _x(self, ini, largo):
        """Tramo [ini, ini+largo) en índices absolutos OS, o None si aún no llegó."""
        a = ini - self.base
        if a < 0 or a + largo > len(self.buf):
            return None
        return self.buf[a:a + largo]

    def _fin_buf(self):
        return self.base + len(self.buf)

    # ---- búsqueda de preámbulo ----
    def _procesar(self):
        paquetes = []
        N, Ns = self.lora.N, self.lora.N * OS
        while True:
            if self.pend is not None:
                r = self._decodificar(self.pend)
                if r is None:
                    break                   # faltan muestras
                if isinstance(r, dict):
                    paquetes.append(r)
                    self.p = r["_fin"]
                else:
                    self.p = r              # falló la sincronización: seguir buscando después
                self.pend = None
                self.racha, self.bin_prev = 0, None
                continue

            n_v = (self._fin_buf() - self.p) // Ns
            if n_v <= 0:
                break
            x = self._x(self.p, n_v * Ns)[::OS].reshape(n_v, N)
            P = np.abs(self.lora.fft_dechirp(x)) ** 2
            k = P.argmax(1)
            papr = P.max(1) / P.mean(1)
            encontrado = False
            for i in range(n_v):
                if papr[i] > PAPR_MIN and self.bin_prev is not None and \
                        min((k[i] - self.bin_prev) % N, (self.bin_prev - k[i]) % N) <= 1:
                    self.racha += 1
                elif papr[i] > PAPR_MIN:
                    self.racha = 1
                else:
                    self.racha = 0
                self.bin_prev = k[i] if papr[i] > PAPR_MIN else None
                if self.racha >= PREAMBULO_MIN:
                    ini = self.p + (i - self.racha + 1) * Ns
                    self.pend = {"ini": ini, "k_up": int(k[i])}
                    self.p = self.p + (i + 1) * Ns
                    encontrado = True
                    break
            if not encontrado:
                self.p += n_v * Ns
        # descartar muestras viejas
        conservar = min(self.p, self.pend["ini"] if self.pend else self.p) - (PREAMBULO_MIN + 2) * Ns
        if conservar > self.base:
            self.buf = self.buf[conservar - self.base:]
            self.base = conservar
        return paquetes

    # ---- sincronización y decodificación ----
    def _decodificar(self, pk):
        L = self.lora
        N, Ns = L.N, L.N * OS
        if "sync" not in pk:
            # necesitamos preámbulo + sync word + SFD + header (+ margen)
            if self._fin_buf() < pk["ini"] + 32 * Ns:
                return None
            # 1) ubicar el SFD (downchirps) en la rejilla sin alinear
            x = self._x(pk["ini"], 16 * Ns)
            if x is None:
                return pk["ini"] + PREAMBULO_MIN * Ns
            x = x[::OS].reshape(16, N)
            Pu = (np.abs(L.fft_dechirp(x)) ** 2).max(1)
            Pd = (np.abs(L.fft_dechirp(x, bajada=True)) ** 2).max(1)
            idx = np.flatnonzero(Pd > Pu)
            if len(idx) == 0 or idx[0] + 1 >= 16 or idx[0] < 2:
                return pk["ini"] + PREAMBULO_MIN * Ns
            m = int(idx[0])
            n_pre = max(1, min(m - 3, 6))   # ventanas de preámbulo seguras (antes del sync word)
            fu = L.pico_fino(x[:n_pre])
            fd = L.pico_fino(x[m + 1], bajada=True)
            # 2) separar offset de frecuencia (cfo) y de tiempo (tau): fu = cfo - tau, fd = cfo + tau
            cfo = (fu + fd) / 2
            if cfo >= N / 4:
                cfo -= N / 2
            elif cfo < -N / 4:
                cfo += N / 2
            tau = ((fd - cfo + N / 2) % N) - N / 2
            ini = pk["ini"] + int(round(tau * OS))
            # 3) con la rejilla alineada y la frecuencia corregida, ubicar el SFD exacto
            x = self._x(ini, 16 * Ns)
            if x is None:
                return pk["ini"] + PREAMBULO_MIN * Ns
            rot = np.exp(-2j * np.pi * cfo * np.arange(16 * N) / N).astype(np.complex64)
            x = (x[::OS] * rot).reshape(16, N)
            Xu = np.abs(L.fft_dechirp(x)) ** 2
            Pd = (np.abs(L.fft_dechirp(x, bajada=True)) ** 2).max(1)
            idx = np.flatnonzero(Pd > Xu.max(1))
            if len(idx) == 0 or idx[0] < 2:
                return pk["ini"] + PREAMBULO_MIN * Ns
            m = int(idx[0])
            pre = Xu[:max(1, m - 2)]
            pot_pico = pre.max(1)
            ruido = (pre.sum(1) - pot_pico) / (N - 1)
            snr = float(np.median((pot_pico - ruido) / (N * ruido)))
            seg = self._x(ini, (m - 2) * Ns)
            # desfase residual (bins) al final del preámbulo: punto de partida del seguimiento
            est = float(np.median([L.pico_fino(x[j]) for j in range(max(0, m - 4), m - 2)]))
            sw = [int(round((L.pico_fino(x[j]) - est) / 8)) % 16 for j in (m - 2, m - 1)]
            pk["sync"] = {
                "est": est,
                "ini": ini, "cfo": cfo, "sfd": m,
                "datos": ini + int(round((m + 2.25) * Ns)),
                "sync_word": f"0x{sw[0] << 4 | sw[1]:02X}",
                "snr_db": 10 * np.log10(max(snr, 1e-6)),
                "pot_dbfs": 10 * np.log10(np.mean(np.abs(seg) ** 2) + 1e-20),
            }

        s = pk["sync"]
        # símbolos del header
        if "header" not in pk:
            x1 = self._simbolos(s, 8)
            if x1 is None:
                return None
            bins = L.demod(x1, 8, s["est"], 8)
            h = decodificar_header(bins, L.sf)
            pk["header"] = h
            if not h["ok"]:
                self.headers_malos += 1
                return s["datos"]
            pk["n_sim"] = L.n_simbolos_payload(h["largo"], h["cr"], h["crc"])

        x1 = self._simbolos(s, pk["n_sim"])
        if x1 is None:
            return None
        bins = L.demod(x1, pk["n_sim"], s["est"], pk["n_sim"] if L.ldro else 8)
        intentos = [0] if L.ldro else [self.off_normal, -1 - self.off_normal]
        for off in intentos:
            datos, crc_ok, corr = decodificar_payload(bins, L.sf, L.ldro, pk["header"], off)
            if crc_ok:
                self.off_normal = off if not L.ldro else self.off_normal
                break
        else:
            datos, crc_ok, corr = decodificar_payload(bins, L.sf, L.ldro, pk["header"], intentos[0])
        cfo_hz = s["cfo"] * L.bw / N
        return {
            "hora": dt.datetime.now(),
            "datos": datos,
            "largo": pk["header"]["largo"],
            "cr": f"4/{4 + pk['header']['cr']}",
            "crc_ok": crc_ok,
            "crc_presente": pk["header"]["crc"],
            "bits_corregidos": corr + pk["header"]["bits_corregidos"],
            "snr_db": s["snr_db"],
            "pot_dbfs": s["pot_dbfs"],
            "cfo_hz": cfo_hz,
            "cfo_ppm": cfo_hz / self.freq_hz * 1e6 if self.freq_hz else None,
            "sync_word": s["sync_word"],
            "n_simbolos": pk["n_sim"],
            "duracion_ms": (8 + 4.25 + pk["n_sim"]) * N / L.bw * 1e3,   # preámbulo de 8 (LoRa.h)
            "_fin": s["datos"] + pk["n_sim"] * Ns,
        }

    def _simbolos(self, s, n):
        Ns = self.lora.N * OS
        x = self._x(s["datos"], n * Ns)
        if x is None:
            return None
        N = self.lora.N
        x1 = x[::OS]
        # la fase de la corrección debe seguir desde el inicio alineado del paquete
        n0 = (s["datos"] - s["ini"]) // OS
        return x1 * np.exp(-2j * np.pi * s["cfo"] * (n0 + np.arange(len(x1))) / N).astype(np.complex64)


# ----------------------------------------------------------------------------------------
# Detección automática de SF / BW
# ----------------------------------------------------------------------------------------

def detectar_parametros(x, fs, offset_hz, bws=(125e3, 250e3, 500e3), sfs=range(7, 13)):
    """Prueba combinaciones SF/BW y devuelve la que produce un preámbulo más limpio."""
    n = np.arange(len(x))
    x = x * np.exp(-2j * np.pi * offset_hz * n / fs)
    mejor = None
    for bw in bws:
        D = int(round(fs / bw))
        y = signal.resample_poly(x, 1, D) if D > 1 else x
        for sf in sfs:
            L = LoRa(sf, bw)
            nv = len(y) // L.N
            if nv < 8:
                continue
            P = np.abs(L.fft_dechirp(y[: nv * L.N].reshape(nv, L.N))) ** 2
            k, frac = P.argmax(1), P.max(1) / P.sum(1)
            racha, mejor_racha, puntaje = 0, 0, 0.0
            for i in range(1, nv):
                if frac[i] > 0.1 and abs(((k[i] - k[i - 1] + L.N // 2) % L.N) - L.N // 2) <= 1:
                    racha += 1
                    if racha >= mejor_racha:
                        mejor_racha = racha
                        puntaje = float(np.median(frac[i - racha:i + 1]))
                else:
                    racha = 0
            if mejor_racha >= PREAMBULO_MIN and (mejor is None or puntaje > mejor[2]):
                mejor = (sf, bw, puntaje)
    return mejor


# ----------------------------------------------------------------------------------------
# Fuentes de muestras
# ----------------------------------------------------------------------------------------

def abrir_rtlsdr(centro_hz, fs, ganancia, ppm):
    from rtlsdr import RtlSdr
    from rtlsdr.rtlsdr import LibUSBError
    try:
        sdr = RtlSdr()
    except LibUSBError as e:
        sys.exit(f"No se pudo abrir el RTL-SDR ({e}).\n"
                 "-> Detén o cierra SDR++ (u otro programa): solo uno puede usar el dongle a la vez.")
    sdr.sample_rate = fs
    sdr.center_freq = centro_hz
    if ppm:
        sdr.freq_correction = ppm
    sdr.gain = ganancia
    return sdr


def leer_rtlsdr(sdr, cola, parar):
    """Hilo lector asíncrono (librtlsdr con buffers propios): no pierde muestras entre bloques.
    Con lecturas síncronas se pierden trozos entre llamadas y los paquetes largos (SF12) se rompen."""
    def cb(buf, _ctx):
        if parar.is_set():
            sdr.cancel_read_async()
            return
        u = (np.ctypeslib.as_array(buf).astype(np.float32) - 127.5) / 127.5
        try:
            cola.put_nowait((u[0::2] + 1j * u[1::2]).astype(np.complex64))
        except queue.Full:
            leer_rtlsdr.perdidos += 1   # el procesamiento no da abasto: se descarta el bloque
    leer_rtlsdr.perdidos = 0
    try:
        sdr.read_bytes_async(cb, 2 * CHUNK)
    except Exception as e:
        cola.put(e)


def leer_archivo(ruta):
    """IQ desde .npy complejo o WAV estéreo (I/Q) de SDR++. Devuelve (fs o None, generador)."""
    ruta = Path(ruta)
    if ruta.suffix.lower() == ".npy":
        x = np.load(ruta).astype(np.complex64)
        return None, (x[i:i + CHUNK] for i in range(0, len(x), CHUNK))
    from scipy.io import wavfile
    fs, d = wavfile.read(ruta, mmap=True)
    if d.ndim != 2 or d.shape[1] != 2:
        sys.exit("El WAV no es IQ (debe tener 2 canales: grabación Baseband).")
    esc = {np.dtype(np.int16): 32768.0, np.dtype(np.int32): 2147483648.0}.get(d.dtype, 1.0)
    gen = ((d[i:i + CHUNK, 0].astype(np.float32) + 1j * d[i:i + CHUNK, 1].astype(np.float32)) / esc
           for i in range(0, len(d), CHUNK))
    return fs, gen


# ----------------------------------------------------------------------------------------
# Salida
# ----------------------------------------------------------------------------------------

def mostrar(p, n):
    txt = p["datos"].decode("utf-8", errors="replace")
    imprimible = all(32 <= c < 127 or c in (9, 10, 13) or c >= 0xC0 for c in p["datos"])
    crc = {True: "OK", False: "ERROR", None: "-"}[p["crc_ok"]]
    print(f"\n[{p['hora']:%H:%M:%S.%f}"[:-3] + f"] Paquete #{n}  CRC {crc}")
    if imprimible:
        print(f"  Texto     : {txt!r}")
    print(f"  Hex       : {p['datos'].hex(' ')}")
    ppm = f" ({p['cfo_ppm']:+.1f} ppm)" if p["cfo_ppm"] is not None else ""
    print(f"  Largo {p['largo']} B | CR {p['cr']} | {p['n_simbolos']} símbolos | {p['duracion_ms']:.1f} ms al aire"
          f" | bits corregidos {p['bits_corregidos']}")
    print(f"  SNR {p['snr_db']:+.1f} dB | potencia {p['pot_dbfs']:.1f} dBFS | offset {p['cfo_hz']:+.0f} Hz{ppm}"
          f" | sync word {p['sync_word']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=915.0, help="frecuencia LoRa en MHz (def. 915)")
    ap.add_argument("--sf", type=int, help="spreading factor 7..12 (si se omite junto con --bw: autodetección)")
    ap.add_argument("--bw", type=float, help="ancho de banda en kHz: 125, 250, 500")
    ap.add_argument("--auto", action="store_true", help="detectar SF y BW automáticamente antes de escuchar")
    ap.add_argument("--ldro", choices=["auto", "si", "no"], default="auto",
                    help="Low Data Rate Optimize (auto = como LoRa.h: símbolo > 16 ms)")
    ap.add_argument("--fs", type=float, default=2.0e6, help="muestras/s del dongle (def. 2e6)")
    ap.add_argument("--ganancia", default="10", help="dB (ej. 0, 10, 30) o 'auto'. Bájala si avisa saturación")
    ap.add_argument("--ppm", type=int, default=49, help="corrección de frecuencia del dongle (NooElec: ~49)")
    ap.add_argument("--archivo", help="en vez del dongle, leer IQ de un WAV baseband o .npy")
    ap.add_argument("--centro", type=float, help="frecuencia central de la grabación en MHz (modo archivo)")
    a = ap.parse_args()

    freq_hz = a.freq * 1e6
    ldro = {"auto": None, "si": True, "no": False}[a.ldro]
    sdr = None
    cola, parar = queue.Queue(maxsize=64), threading.Event()

    if a.archivo:
        fs, gen = leer_archivo(a.archivo)
        fs = fs or a.fs
        if a.centro:
            centro_hz = a.centro * 1e6
        else:
            mm = re.search(r"(\d{6,})Hz", Path(a.archivo).name)
            centro_hz = float(mm.group(1)) if mm else freq_hz - fs / 4
        bloques = gen
    else:
        fs = a.fs
        centro_hz = freq_hz - fs / 4          # sintonizar desplazado para alejar el pico de DC
        ganancia = "auto" if str(a.ganancia).lower() == "auto" else float(a.ganancia)
        sdr = abrir_rtlsdr(centro_hz, fs, ganancia, a.ppm)
        fs = sdr.sample_rate
        print(f"RTL-SDR: centro {sdr.center_freq / 1e6:.4f} MHz, fs {fs / 1e6:.3f} MS/s, "
              f"ganancia {a.ganancia} dB, ppm {a.ppm}")
        hilo = threading.Thread(target=leer_rtlsdr, args=(sdr, cola, parar), daemon=True)
        hilo.start()

        def gen_vivo():
            while True:
                try:
                    x = cola.get(timeout=0.5)   # con timeout para que Ctrl+C funcione en Windows
                except queue.Empty:
                    continue
                if isinstance(x, Exception):
                    raise x
                if leer_rtlsdr.perdidos:
                    leer_rtlsdr.perdidos = 0
                    print("\n  [aviso] se perdieron muestras (el PC no alcanzó a procesar)")
                yield x
        bloques = gen_vivo()

    offset = freq_hz - centro_hz
    if a.sf is None and a.bw is None:
        a.auto = True
    sf, bw = a.sf or 7, (a.bw or 500) * 1e3     # si falla la autodetección: valores del sketch

    if a.auto:
        print("Detectando SF/BW: esperando un preámbulo", end="", flush=True)
        acum, t0 = [], time.time()
        for x in bloques:
            acum.append(x)
            if sum(map(len, acum)) * 1.0 / fs >= 1.5:
                r = detectar_parametros(np.concatenate(acum), fs, offset)
                if r:
                    sf, bw = r[0], r[1]
                    print(f"\n  -> detectado SF{sf}, BW {bw / 1e3:g} kHz (pureza {r[2]:.2f})")
                    break
                acum = acum[len(acum) // 2:]   # ventana deslizante con traslape
                print(".", end="", flush=True)
                if time.time() - t0 > 30 and not a.archivo:
                    print("\n  No se detectó nada en 30 s. Revisa frecuencia/antena; uso los valores dados.")
                    break
        else:   # el archivo se acabó antes de juntar 1.5 s: probar con lo que haya
            r = detectar_parametros(np.concatenate(acum), fs, offset) if acum else None
            if not r:
                sys.exit("\nNo se detectó ningún preámbulo LoRa en el archivo.")
            sf, bw = r[0], r[1]
            print(f"\n  -> detectado SF{sf}, BW {bw / 1e3:g} kHz (pureza {r[2]:.2f})")

    rx = Receptor(fs, offset, sf, bw, ldro, freq_hz)
    print(f"Escuchando LoRa en {a.freq:.3f} MHz | SF{sf} | BW {bw / 1e3:g} kHz | "
          f"LDRO {'sí' if rx.lora.ldro else 'no'} | símbolo {rx.lora.N / bw * 1e3:.3f} ms  (Ctrl+C para salir)")

    n_ok = n_tot = 0
    t_ultimo = t_sat = time.time()
    previos = acum if a.auto else []
    try:
        for x in (y for fuente in (previos, bloques) for y in fuente):
            for p in rx.alimentar(x):
                n_tot += 1
                n_ok += bool(p["crc_ok"])
                mostrar(p, n_tot)
                t_ultimo = time.time()
            if rx.max_raw > 0.97 and time.time() - t_sat > 10:
                print("  [aviso] saturación del ADC: baja --ganancia (aun así suele decodificar)")
                t_sat = time.time()
            rx.max_raw = 0.0
            if sdr and time.time() - t_ultimo > 15:
                print(f"\n  ... 15 s sin paquetes (SF{sf}/BW{bw / 1e3:g}). Si la TTGO transmite con otra "
                      "configuración, ejecuta sin --sf/--bw para autodetectar")
                t_ultimo = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
        if sdr:
            try:
                sdr.cancel_read_async()
            except Exception:
                pass
            time.sleep(0.3)
            sdr.close()
    print(f"\nFin. Paquetes decodificados: {n_tot} (CRC OK: {n_ok}), headers inválidos: {rx.headers_malos}")


if __name__ == "__main__":
    main()
