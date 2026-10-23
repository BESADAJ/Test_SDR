"""
Analizador de FM comercial (WFM) para RTL-SDR (NooElec R820T) o grabaciones de SDR++.

Extrae los mismos datos que muestra SDR++ (y algunos más):
  - Espectro RF y cascada (waterfall), potencia del canal, piso de ruido y SNR
  - Error de sintonía / ppm del dongle
  - Desviación FM, espectro MPX, piloto estéreo de 19 kHz, subportadora RDS de 57 kHz
  - Audio estéreo demodulado (WAV 48 kHz) con de-énfasis
  - RDS: PI, PS (nombre de la emisora), RadioText, PTY, TP/TA, MS, AF, hora (CT)

Uso:
  # En vivo (antes DETÉN o cierra SDR++, porque solo un programa puede usar el dongle)
  python sdr_analisis.py en_vivo --freq 103.1 --segundos 10

  # Desde una grabación BASEBAND de SDR++ (Recorder -> Mode: Baseband, formato WAV)
  python sdr_analisis.py archivo "C:\\ruta\\baseband_103100000Hz_12-00-00_06-10-2026.wav" --freq 103.1

Los resultados (reporte.txt, reporte.json, graficas.png, audio.wav) quedan en ./resultados/<fecha>/
"""

import argparse
import datetime as dt
import json
import re
import sys
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

CHUNK = 262144          # muestras IQ por bloque (múltiplo de 512, requerido por librtlsdr)
NFFT = 2048             # puntos de FFT para espectro / cascada
DEV_MAX = 75e3          # desviación máxima de FM comercial (Hz)
CANAL_BW = 100e3        # semiancho del canal WFM para medir potencia (Hz)


# ----------------------------------------------------------------------------------------
# Fuentes de muestras IQ
# ----------------------------------------------------------------------------------------

def fuente_rtlsdr(centro_hz, fs, segundos, ganancia, ppm, wav_salida=None):
    """Lee IQ del dongle en bloques. Opcionalmente guarda un WAV baseband compatible con SDR++."""
    from rtlsdr import RtlSdr
    from rtlsdr.rtlsdr import LibUSBError

    try:
        sdr = RtlSdr()
    except LibUSBError as e:
        sys.exit(f"No se pudo abrir el RTL-SDR ({e}).\n"
                 "-> Detén (botón Stop) o cierra SDR++: solo un programa puede usar el dongle a la vez.")

    wf = None
    try:
        sdr.sample_rate = fs
        sdr.center_freq = centro_hz
        if ppm:
            sdr.freq_correction = ppm
        sdr.gain = ganancia
        fs_real = sdr.sample_rate
        print(f"RTL-SDR abierto: centro {sdr.center_freq/1e6:.4f} MHz, fs {fs_real/1e6:.3f} MS/s, "
              f"ganancia {sdr.gain if ganancia != 'auto' else 'auto'}")

        if wav_salida:
            wf = wave.open(str(wav_salida), "wb")
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(int(fs_real))

        for _ in range(2):  # descartar bloques iniciales mientras el sintonizador se estabiliza
            sdr.read_samples(CHUNK)

        total = int(segundos * fs_real)
        leidas = 0

        def gen():
            nonlocal leidas
            try:
                while leidas < total:
                    x = sdr.read_samples(CHUNK).astype(np.complex64)
                    x = x[: total - leidas]
                    leidas += len(x)
                    if wf:
                        iq16 = np.empty(2 * len(x), np.int16)
                        iq16[0::2] = np.clip(x.real * 32767, -32768, 32767)
                        iq16[1::2] = np.clip(x.imag * 32767, -32768, 32767)
                        wf.writeframes(iq16.tobytes())
                    print(f"\r  capturando... {leidas/fs_real:5.1f} / {segundos:.1f} s", end="", flush=True)
                    yield x
                print()
            finally:
                if wf:
                    wf.close()
                sdr.close()

        return fs_real, gen()
    except Exception:
        if wf:
            wf.close()
        sdr.close()
        raise


def fuente_wav(ruta, inicio, segundos):
    """Lee una grabación baseband IQ (WAV estéreo: canal izq = I, der = Q) como la de SDR++."""
    try:
        fs, data = wavfile.read(ruta, mmap=True)
    except Exception:
        fs, data = wavfile.read(ruta)
    if data.ndim != 2 or data.shape[1] != 2:
        sys.exit("El WAV no es IQ (debe tener 2 canales). ¿Grabaste en modo Audio en vez de Baseband?")

    if data.dtype == np.int16:
        conv = lambda b: b.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        conv = lambda b: b.astype(np.float32) / 2147483648.0
    elif data.dtype == np.float32:
        conv = lambda b: np.asarray(b, np.float32)
    elif data.dtype == np.uint8:
        muestra = np.asarray(data[: min(len(data), 100000)])
        centrado = np.mean((muestra > 64) & (muestra < 192))
        if centrado > 0.5:   # PCM 8 bits estándar (sin signo)
            conv = lambda b: (b.astype(np.float32) - 127.5) / 127.5
        else:                # int8 con signo guardado como 8 bits
            conv = lambda b: np.asarray(b).view(np.int8).astype(np.float32) / 128.0
    else:
        sys.exit(f"Formato de muestra no soportado: {data.dtype}")

    ini = int(inicio * fs)
    fin = len(data) if not segundos else min(len(data), ini + int(segundos * fs))
    if ini >= fin:
        sys.exit("El rango --inicio/--segundos está fuera del archivo.")

    def gen():
        for a in range(ini, fin, CHUNK):
            b = conv(data[a:min(a + CHUNK, fin)])
            yield (b[:, 0] + 1j * b[:, 1]).astype(np.complex64)

    return fs, gen(), (fin - ini) / fs


# ----------------------------------------------------------------------------------------
# Front-end: espectro + mezcla + filtrado/decimación del canal (por bloques, sin cortes)
# ----------------------------------------------------------------------------------------

class FrontEnd:
    def __init__(self, fs, offset_hz):
        self.fs = fs
        self.offset = offset_hz
        self.D = max(1, round(fs / 250e3))
        self.fs_mpx = fs / self.D
        corte = min(110e3, 0.45 * self.fs_mpx)
        ntaps = int(4 * fs / (0.16 * self.fs_mpx)) | 1
        self.h = signal.firwin(ntaps, corte, fs=fs).astype(np.float32)
        self.cola = np.zeros(ntaps - 1, np.complex64)
        self.n = 0
        self.recortes = 0
        self.salida = []
        self.filas = []            # cascada: un espectro promedio por bloque
        self.win = signal.windows.blackmanharris(NFFT).astype(np.float32)
        self.norm = np.sum(self.win) ** 2

    def procesar(self, x):
        self.recortes += int(np.count_nonzero((np.abs(x.real) > 0.98) | (np.abs(x.imag) > 0.98)))
        nseg = len(x) // NFFT
        if nseg:
            segs = x[: nseg * NFFT].reshape(nseg, NFFT) * self.win
            p = np.mean(np.abs(np.fft.fft(segs, axis=1)) ** 2, axis=0) / self.norm
            self.filas.append(np.fft.fftshift(p))

        t = self.n + np.arange(len(x), dtype=np.float64)
        fase = 2 * np.pi * ((-self.offset / self.fs * t) % 1.0)
        xm = x * np.exp(1j * fase).astype(np.complex64)

        ext = np.concatenate([self.cola, xm])
        y = signal.fftconvolve(ext, self.h, mode="valid")
        self.cola = ext[-(len(self.h) - 1):]
        inicio = (-self.n) % self.D
        self.salida.append(y[inicio::self.D].astype(np.complex64))
        self.n += len(x)

    def resultado(self):
        return np.concatenate(self.salida), np.array(self.filas)


# ----------------------------------------------------------------------------------------
# RDS
# ----------------------------------------------------------------------------------------

RDS_POLI = 0x5B9
OFFSETS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "C'": 0x350, "D": 0x1B4}

PTY_RDS = ["Ninguno", "Noticias", "Actualidad", "Información", "Deportes", "Educación", "Drama",
           "Cultura", "Ciencia", "Variado", "Música pop", "Rock", "Música ligera", "Clásica ligera",
           "Clásica seria", "Otra música", "Clima", "Finanzas", "Infantil", "Asuntos sociales",
           "Religión", "Llamadas", "Viajes", "Ocio", "Jazz", "Country", "Música nacional",
           "Oldies", "Folk", "Documental", "Prueba alarma", "Alarma"]
PTY_RBDS = ["Ninguno", "Noticias", "Información", "Deportes", "Talk", "Rock", "Rock clásico",
            "Adult hits", "Soft rock", "Top 40", "Country", "Oldies", "Soft", "Nostalgia", "Jazz",
            "Clásica", "R&B", "Soft R&B", "Idiomas", "Música religiosa", "Talk religioso",
            "Personalidad", "Pública", "Universitaria", "Talk en español", "Música en español",
            "Hip hop", "-", "-", "Clima", "Prueba emergencia", "Emergencia"]

# Tabla de caracteres RDS (EN 50067, tabla E.1) para lo que difiere de ASCII
RDS_CHARS = {0x80: "á", 0x81: "à", 0x82: "é", 0x83: "è", 0x84: "í", 0x85: "ì", 0x86: "ó",
             0x87: "ò", 0x88: "ú", 0x89: "ù", 0x8A: "Ñ", 0x8B: "Ç", 0x8D: "ß", 0x8E: "¡",
             0x90: "â", 0x91: "ä", 0x92: "ê", 0x93: "ë", 0x94: "î", 0x95: "ï", 0x96: "ô",
             0x97: "ö", 0x98: "û", 0x99: "ü", 0x9A: "ñ", 0x9B: "ç", 0xB9: "¿", 0xC0: "Á",
             0xC1: "À", 0xC2: "É", 0xC3: "È", 0xC4: "Í", 0xC5: "Ì", 0xC6: "Ó", 0xC7: "Ò",
             0xC8: "Ú", 0xC9: "Ù", 0xD8: "Ü", 0xD0: "Â", 0xD1: "Ä", 0xD2: "Ê", 0xD3: "Ë"}


def rds_crc(dato16):
    reg = dato16 << 10
    for i in range(25, 9, -1):
        if reg & (1 << i):
            reg ^= RDS_POLI << (i - 10)
    return reg & 0x3FF


def rds_char(c):
    if 0x20 <= c < 0x7F:
        return chr(c)
    return RDS_CHARS.get(c, " ")


def demod_rds(mpx, fs, fase_piloto, f_piloto):
    """Baja la subportadora de 57 kHz (3x piloto, coherente) y recupera los bits RDS."""
    bb = mpx * np.exp(-3j * fase_piloto)
    sos = signal.butter(6, 2400, fs=fs, output="sos")
    bb = signal.sosfiltfilt(sos, bb.real) + 1j * signal.sosfiltfilt(sos, bb.imag)

    # Reloj de símbolo RDS = piloto/16 -> remuestrear a exactamente 16 muestras por símbolo
    r = Fraction(f_piloto / fs).limit_denominator(4000)
    z = signal.resample_poly(bb, r.numerator, r.denominator)
    recorte = int(0.1 * f_piloto)
    z = z[recorte:-recorte]
    if len(z) < 16 * 200:
        return np.zeros(0, np.uint8), np.zeros(0, complex)

    z = z * np.exp(-0.5j * np.angle(np.sum(z ** 2)))          # alinear BPSK al eje real
    plantilla = np.r_[np.ones(8), -np.ones(8)]                 # símbolo bifase (Manchester)
    y = np.convolve(z, plantilla[::-1], mode="valid")
    off = int(np.argmax([np.mean(np.abs(y[o::16].real)) for o in range(16)]))
    sim = y[off::16]
    duro = sim.real > 0
    bits = (duro[1:] != duro[:-1]).astype(np.uint8)            # decodificación diferencial
    return bits, sim


def decodificar_rds(bits):
    n = len(bits)
    cad = "".join("1" if b else "0" for b in bits)

    def bloque(i):
        w = int(cad[i:i + 26], 2)
        dato, chk = w >> 10, w & 0x3FF
        return dato, chk ^ rds_crc(dato)

    info = {"grupos_validos": 0, "tipos_grupo": {}, "PI": None, "PTY": None, "TP": None,
            "TA": None, "MS": None, "estereo_DI": None, "AF_MHz": set(), "CT": None}
    ps = [" "] * 8
    ps_ok = [False] * 8
    rt = [" "] * 64
    rt_ok = [False] * 64
    rt_ab = None
    pis = {}

    i = 0
    while i + 104 <= n:
        a, sa = bloque(i)
        b, sb = bloque(i + 26)
        if sa != OFFSETS["A"] or sb != OFFSETS["B"]:
            i += 1
            continue
        c, sc = bloque(i + 52)
        d, sd = bloque(i + 78)
        c_ok = sc in (OFFSETS["C"], OFFSETS["C'"])
        d_ok = sd == OFFSETS["D"]
        i += 104

        info["grupos_validos"] += 1
        pis[a] = pis.get(a, 0) + 1
        tipo, ver = b >> 12, (b >> 11) & 1
        nombre = f"{tipo}{'AB'[ver]}"
        info["tipos_grupo"][nombre] = info["tipos_grupo"].get(nombre, 0) + 1
        info["TP"] = bool((b >> 10) & 1)
        info["PTY"] = (b >> 5) & 0x1F

        if tipo == 0:
            seg = b & 3
            info["TA"] = bool((b >> 4) & 1)
            info["MS"] = "música" if (b >> 3) & 1 else "voz"
            if seg == 3:
                info["estereo_DI"] = bool((b >> 2) & 1)
            if ver == 0 and c_ok:
                for af in (c >> 8, c & 0xFF):
                    if 1 <= af <= 204:
                        info["AF_MHz"].add(round(87.5 + af * 0.1, 1))
            if d_ok:
                ps[2 * seg], ps[2 * seg + 1] = rds_char(d >> 8), rds_char(d & 0xFF)
                ps_ok[2 * seg] = ps_ok[2 * seg + 1] = True
        elif tipo == 2:
            ab = (b >> 4) & 1
            if rt_ab is not None and ab != rt_ab:     # cambió el texto: borrar
                rt = [" "] * 64
                rt_ok = [False] * 64
            rt_ab = ab
            seg = b & 0xF
            if ver == 0:
                pares = [(c, c_ok), (d, d_ok)]
                base = 4 * seg
            else:
                pares = [(d, d_ok)]
                base = 2 * seg
            for k, (w, ok) in enumerate(pares):
                if ok:
                    for j, ch in enumerate((w >> 8, w & 0xFF)):
                        rt[base + 2 * k + j] = rds_char(ch)
                        rt_ok[base + 2 * k + j] = True
        elif tipo == 4 and ver == 0 and c_ok and d_ok:
            mjd = ((b & 3) << 15) | (c >> 1)
            hora = ((c & 1) << 4) | (d >> 12)
            minuto = (d >> 6) & 0x3F
            off = (d & 0x1F) * 30 * (-1 if (d >> 5) & 1 else 1)
            try:
                utc = dt.datetime(1858, 11, 17) + dt.timedelta(days=mjd, hours=hora, minutes=minuto)
                local = utc + dt.timedelta(minutes=off)
                info["CT"] = f"{local:%Y-%m-%d %H:%M} (UTC{off/60:+.1f}h)"
            except (OverflowError, ValueError):
                pass

    if pis:
        info["PI"] = f"0x{max(pis, key=pis.get):04X}"
    info["PS"] = "".join(ps) if any(ps_ok) else None
    texto = "".join(rt).split("\r")[0].rstrip()
    info["RadioText"] = texto if any(rt_ok) else None
    info["AF_MHz"] = sorted(info["AF_MHz"])
    if info["PTY"] is not None:
        info["PTY_nombre"] = {"RDS (Europa/LatAm)": PTY_RDS[info["PTY"]],
                              "RBDS (Norteamérica)": PTY_RBDS[info["PTY"]]}
    info["bits_recibidos"] = int(n)
    return info


# ----------------------------------------------------------------------------------------
# Análisis principal
# ----------------------------------------------------------------------------------------

def db(x):
    return 10 * np.log10(np.maximum(x, 1e-20))


def analizar(fs, chunks, centro_hz, freq_hz, deemph_us, carpeta):
    offset = freq_hz - centro_hz
    if abs(offset) > fs / 2 - CANAL_BW:
        sys.exit(f"La emisora ({freq_hz/1e6:.3f} MHz) queda fuera de la banda capturada "
                 f"({(centro_hz-fs/2)/1e6:.3f}–{(centro_hz+fs/2)/1e6:.3f} MHz). Revisa --freq / --centro.")

    fe = FrontEnd(fs, offset)
    for x in chunks:
        fe.procesar(x)
    z, filas = fe.resultado()
    fs_mpx = fe.fs_mpx
    if len(z) < fs_mpx * 0.5:
        sys.exit("Muy pocas muestras para analizar (mínimo ~0.5 s).")
    r = {}

    # ---------------- RF ----------------
    f_rel = np.fft.fftshift(np.fft.fftfreq(NFFT, 1 / fs))
    f_abs = (centro_hz + f_rel) / 1e6
    psd = np.mean(filas, axis=0)
    canal = np.abs(f_rel - offset) < CANAL_BW
    util = (np.abs(f_rel) < 0.45 * fs) & (np.abs(f_rel) > 2 * fs / NFFT)   # sin bordes ni DC
    fuera = util & ~canal
    piso = np.percentile(psd[fuera], 25) if np.any(fuera) else np.median(psd)
    pot_t = db(np.sum(filas[:, canal], axis=1))
    r["rf"] = {
        "potencia_canal_dBFS": round(float(db(np.sum(psd[canal]))), 2),
        "piso_ruido_dBFS_por_bin": round(float(db(piso)), 2),
        "SNR_dB": round(float(db(np.mean(psd[canal]) / piso)), 2),
        "pico_espectro_MHz": round(float(f_abs[util][np.argmax(psd[util])]), 4),
        "potencia_canal_min_max_dBFS": [round(float(pot_t.min()), 2), round(float(pot_t.max()), 2)],
        "resolucion_fft_Hz": round(fs / NFFT, 1),
        "muestras_saturadas_pct": round(100 * fe.recortes / max(fe.n, 1), 3),
    }
    if r["rf"]["muestras_saturadas_pct"] > 0.1:
        r["rf"]["aviso"] = "El dongle está saturando: baja la ganancia (ej. --ganancia 20)."

    # ---------------- Demodulación FM -> MPX ----------------
    mpx = np.angle(z[1:] * np.conj(z[:-1])).astype(np.float64) * fs_mpx / (2 * np.pi)
    err_hz = float(np.mean(mpx))
    mpx -= err_hz
    r["fm"] = {
        "error_sintonia_Hz": round(err_hz, 1),
        "error_ppm_estimado": round(err_hz / freq_hz * 1e6, 2),
        "sumar_a_--ppm": int(round(-err_hz / freq_hz * 1e6)),
        "desviacion_pico_kHz": round(float(np.percentile(np.abs(mpx), 99.9)) / 1e3, 2),
        "desviacion_rms_kHz": round(float(np.std(mpx)) / 1e3, 2),
        "modulacion_pct": round(100 * float(np.percentile(np.abs(mpx), 99.9)) / DEV_MAX, 1),
    }

    f_mpx, p_mpx = signal.welch(mpx, fs_mpx, nperseg=8192)

    def nivel_banda(f1, f2):   # amplitud pico (Hz de desviación) de una componente
        m = (f_mpx >= f1) & (f_mpx <= f2)
        return float(np.sqrt(2 * np.sum(p_mpx[m]) * (f_mpx[1] - f_mpx[0])))

    ruido_ref = nivel_banda(70e3, 74e3) / np.sqrt(4e3) if fs_mpx / 2 > 75e3 else 0.0
    piloto_amp = nivel_banda(18.8e3, 19.2e3)
    rds_amp = nivel_banda(54.6e3, 59.4e3)
    rds_snr = db((rds_amp / np.sqrt(4.8e3)) ** 2 / max(ruido_ref ** 2, 1e-12)) if ruido_ref else None

    # ---------------- Piloto 19 kHz ----------------
    sos_p = signal.butter(4, [18.7e3, 19.3e3], btype="band", fs=fs_mpx, output="sos")
    piloto = signal.sosfiltfilt(sos_p, mpx)
    fase = np.unwrap(np.angle(signal.hilbert(piloto)))
    a, b = int(0.05 * len(fase)), int(0.95 * len(fase))
    f_piloto = float(np.polyfit(np.arange(a, b) / fs_mpx, fase[a:b], 1)[0] / (2 * np.pi))
    hay_piloto = piloto_amp > 2e3 and abs(f_piloto - 19e3) < 50
    r["mpx"] = {
        "piloto_estereo": bool(hay_piloto),
        "piloto_nivel_kHz": round(piloto_amp / 1e3, 2),
        "piloto_pct_desviacion": round(100 * piloto_amp / DEV_MAX, 1),
        "piloto_frecuencia_medida_Hz": round(f_piloto, 2) if hay_piloto else None,
        "error_reloj_muestreo_ppm": round((f_piloto / 19e3 - 1) * 1e6, 1) if hay_piloto else None,
        "rds_nivel_kHz": round(rds_amp / 1e3, 2),
        "rds_SNR_dB": round(float(rds_snr), 1) if rds_snr is not None else None,
    }

    # ---------------- Audio ----------------
    sos_a = signal.butter(8, 15e3, fs=fs_mpx, output="sos")
    m = signal.sosfiltfilt(sos_a, mpx)
    if hay_piloto:
        s = signal.sosfiltfilt(sos_a, mpx * 2 * np.cos(2 * fase))
        izq, der = (m + s) / 2, (m - s) / 2
    else:
        izq = der = m
    b_n, a_n = signal.iirnotch(19e3, 30, fs=fs_mpx)       # quitar residuo del piloto
    izq, der = signal.filtfilt(b_n, a_n, izq), signal.filtfilt(b_n, a_n, der)
    if deemph_us > 0:
        al = np.exp(-1 / (fs_mpx * deemph_us * 1e-6))
        izq = signal.lfilter([1 - al], [1, -al], izq)
        der = signal.lfilter([1 - al], [1, -al], der)
    ra = Fraction(48000 / fs_mpx).limit_denominator(1000)
    izq = signal.resample_poly(izq, ra.numerator, ra.denominator) / DEV_MAX
    der = signal.resample_poly(der, ra.numerator, ra.denominator) / DEV_MAX
    fs_audio = fs_mpx * ra.numerator / ra.denominator
    corr = float(np.corrcoef(izq, der)[0, 1]) if hay_piloto else 1.0
    pico = max(np.max(np.abs(izq)), np.max(np.abs(der)), 1e-9)
    ganancia = 0.9 / pico
    audio = np.stack([izq, der], axis=1) * ganancia
    ruta_audio = carpeta / "audio.wav"
    wavfile.write(ruta_audio, int(round(fs_audio)), (audio * 32767).astype(np.int16))
    r["audio"] = {
        "archivo": str(ruta_audio),
        "frecuencia_muestreo_Hz": int(round(fs_audio)),
        "estereo": bool(hay_piloto),
        "rms_izq_dBFS": round(float(db(np.mean((izq * ganancia) ** 2))), 1),
        "rms_der_dBFS": round(float(db(np.mean((der * ganancia) ** 2))), 1),
        "correlacion_izq_der": round(corr, 3),
        "deenfasis_us": deemph_us,
    }

    # ---------------- RDS ----------------
    sim = np.zeros(0, complex)
    if hay_piloto:
        bits, sim = demod_rds(mpx, fs_mpx, fase, f_piloto)
        r["rds"] = decodificar_rds(bits)
    else:
        r["rds"] = {"nota": "Sin piloto estéreo: no se intentó decodificar RDS."}

    # ---------------- Gráficas ----------------
    graficar(carpeta / "graficas.png", f_abs, psd, filas, fs, centro_hz, freq_hz, pot_t,
             f_mpx, p_mpx, sim, izq, der, fs_audio, r)
    return r


def graficar(ruta, f_abs, psd, filas, fs, centro_hz, freq_hz, pot_t, f_mpx, p_mpx, sim,
             izq, der, fs_audio, r):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(3, 2, figsize=(15, 13))
    dur = len(filas) * CHUNK / fs

    ax[0, 0].plot(f_abs, db(psd), lw=0.7)
    ax[0, 0].axvspan((freq_hz - CANAL_BW) / 1e6, (freq_hz + CANAL_BW) / 1e6, color="tab:orange", alpha=0.2)
    ax[0, 0].set(title="Espectro RF (promedio)", xlabel="MHz", ylabel="dBFS/bin")
    ax[0, 0].grid(alpha=0.3)

    ax[0, 1].imshow(db(filas), aspect="auto", origin="upper", cmap="viridis",
                    extent=[f_abs[0], f_abs[-1], dur, 0])
    ax[0, 1].set(title="Cascada (waterfall)", xlabel="MHz", ylabel="tiempo (s)")

    ax[1, 0].plot(f_mpx / 1e3, db(p_mpx), lw=0.7)
    for f, t in [(19, "piloto"), (38, "L-R"), (57, "RDS")]:
        ax[1, 0].axvline(f, color="r", ls="--", lw=0.6)
        ax[1, 0].text(f, ax[1, 0].get_ylim()[1], t, color="r", ha="center", va="bottom", fontsize=8)
    ax[1, 0].set(title="Espectro MPX (señal FM demodulada)", xlabel="kHz", ylabel="dB", xlim=(0, f_mpx[-1] / 1e3))
    ax[1, 0].grid(alpha=0.3)

    if len(sim):
        s = sim / np.mean(np.abs(sim))
        ax[1, 1].scatter(s.real, s.imag, s=2, alpha=0.4)
        ax[1, 1].set(xlim=(-2.5, 2.5), ylim=(-2.5, 2.5), aspect="equal")
    else:
        ax[1, 1].text(0.5, 0.5, "Sin RDS", ha="center", transform=ax[1, 1].transAxes)
    ax[1, 1].set(title="Constelación RDS (BPSK)", xlabel="I", ylabel="Q")
    ax[1, 1].grid(alpha=0.3)

    for x, etq in [(izq, "Izq"), (der, "Der")]:
        f, p = signal.welch(x, fs_audio, nperseg=4096)
        ax[2, 0].plot(f / 1e3, db(p), lw=0.7, label=etq)
    ax[2, 0].set(title="Espectro de audio", xlabel="kHz", ylabel="dB", xlim=(0, 20))
    ax[2, 0].legend()
    ax[2, 0].grid(alpha=0.3)

    ax[2, 1].plot(np.arange(len(pot_t)) * CHUNK / fs, pot_t, marker=".")
    ax[2, 1].set(title="Potencia del canal en el tiempo (S-meter)", xlabel="s", ylabel="dBFS")
    ax[2, 1].grid(alpha=0.3)

    rds = r.get("rds", {})
    fig.suptitle(f"{freq_hz/1e6:.2f} MHz | SNR {r['rf']['SNR_dB']} dB | "
                 f"PS: {rds.get('PS') or '-'} | RT: {rds.get('RadioText') or '-'}", fontsize=12)
    fig.tight_layout()
    fig.savefig(ruta, dpi=110)
    plt.close(fig)


def imprimir(r, archivo):
    lineas = []
    titulos = {"fuente": "FUENTE", "rf": "RF", "fm": "FM", "mpx": "MPX / ESTÉREO",
               "audio": "AUDIO", "rds": "RDS"}
    for sec, datos in r.items():
        lineas.append(f"\n=== {titulos.get(sec, sec.upper())} ===")
        for k, v in datos.items():
            if isinstance(v, dict):
                v = ", ".join(f"{a}: {b}" for a, b in v.items())
            lineas.append(f"  {k:32s} {v}")
    texto = "\n".join(lineas)
    print(texto)
    Path(archivo).write_text(texto, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="modo", required=True)

    v = sub.add_parser("en_vivo", help="capturar directamente del RTL-SDR")
    v.add_argument("--freq", type=float, default=103.1, help="frecuencia de la emisora en MHz")
    v.add_argument("--segundos", type=float, default=10)
    v.add_argument("--fs", type=float, default=1.2e6, help="muestras/s del dongle (def. 1.2e6)")
    v.add_argument("--ganancia", default="auto", help="dB (ej. 30) o 'auto'")
    v.add_argument("--ppm", type=int, default=49, help="corrección de frecuencia del dongle (medido en tu NooElec: ~49)")
    v.add_argument("--sin-iq", action="store_true", help="no guardar el WAV baseband IQ")

    f = sub.add_parser("archivo", help="analizar una grabación baseband WAV de SDR++")
    f.add_argument("ruta")
    f.add_argument("--freq", type=float, default=103.1, help="frecuencia de la emisora en MHz")
    f.add_argument("--centro", type=float, help="frecuencia central de la grabación en MHz "
                                               "(si no, se lee del nombre del archivo)")
    f.add_argument("--inicio", type=float, default=0, help="segundo desde donde empezar")
    f.add_argument("--segundos", type=float, default=15, help="duración a analizar (0 = todo)")

    for p in (v, f):
        p.add_argument("--deenfasis", type=float, default=75, help="µs: 75 (América) o 50 (Europa)")

    # Sin subcomando (p. ej. botón "Run" de VS Code) -> en vivo por defecto
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("en_vivo", "archivo", "-h", "--help"):
        argv = ["en_vivo"] + argv
    a = ap.parse_args(argv)
    freq_hz = a.freq * 1e6
    carpeta = Path(__file__).parent / "resultados" / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{a.modo}"
    carpeta.mkdir(parents=True, exist_ok=True)

    if a.modo == "en_vivo":
        ganancia = "auto" if str(a.ganancia).lower() == "auto" else float(a.ganancia)
        centro_hz = freq_hz - 250e3   # sintonizar desplazado para evitar el pico de DC
        wav_iq = None if a.sin_iq else carpeta / f"baseband_{int(centro_hz)}Hz_{dt.datetime.now():%H-%M-%S_%d-%m-%Y}.wav"
        fs, chunks = fuente_rtlsdr(centro_hz, a.fs, a.segundos, ganancia, a.ppm, wav_iq)
        fuente = {"modo": "en vivo (RTL-SDR)", "iq_guardado": str(wav_iq) if wav_iq else None,
                  "ganancia": a.ganancia, "duracion_s": a.segundos}
    else:
        if a.centro:
            centro_hz = a.centro * 1e6
        else:
            mm = re.search(r"(\d{6,})Hz", Path(a.ruta).name)
            if not mm:
                sys.exit("No pude leer la frecuencia central del nombre del archivo; usa --centro (MHz).")
            centro_hz = float(mm.group(1))
        fs, chunks, dur = fuente_wav(a.ruta, a.inicio, a.segundos)
        fuente = {"modo": "archivo", "ruta": a.ruta, "duracion_s": round(dur, 2)}

    fuente.update({"centro_MHz": centro_hz / 1e6, "emisora_MHz": a.freq, "fs_Hz": fs})
    print(f"Analizando {a.freq} MHz (centro {centro_hz/1e6:.4f} MHz, fs {fs/1e6:.3f} MS/s)...")
    r = {"fuente": fuente}
    r.update(analizar(fs, chunks, centro_hz, freq_hz, a.deenfasis, carpeta))

    imprimir(r, carpeta / "reporte.txt")
    (carpeta / "reporte.json").write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str),
                                          encoding="utf-8")
    print(f"\nResultados en: {carpeta}")


if __name__ == "__main__":
    main()
