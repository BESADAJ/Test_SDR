"""
Puente entre el receptor LoRa (sdr_lora.py), el analizador FM (sdr_analisis.py) y la
interfaz web (carpeta web/). Sirve la página y le envía en tiempo real, por Server-Sent
Events, cada paquete decodificado, la cascada del espectro y el estado del receptor.

Uso (Windows, desde la carpeta del proyecto; antes cierra SDR++):
  # En vivo con el RTL-SDR, con los parámetros del sketch GPS_LoRa_TTGO
  .\\venv\\Scripts\\python.exe servidor_gps.py --freq 915 --sf 7 --bw 500

  # En vivo autodetectando SF/BW (igual que sdr_lora.py sin --sf/--bw)
  .\\venv\\Scripts\\python.exe servidor_gps.py --freq 915

  # Reproducir una grabación baseband IQ (WAV de SDR++ o .npy) a velocidad real
  .\\venv\\Scripts\\python.exe servidor_gps.py --archivo captura.wav --centro 914.5 --freq 915 --repetir

  # Sin hardware: recorrido simulado alrededor de Uninorte
  .\\venv\\Scripts\\python.exe servidor_gps.py --sim

Luego abre http://localhost:8000 (se abre solo). Ctrl+C para detener.

Endpoints:
  /                       interfaz (web/index.html)
  /eventos                flujo SSE: estado, paquete, espectro, stats, aviso, fm_log, fm_listo
  /api/estado             configuración actual del receptor (JSON)
  /api/limpiar  (POST)    borra el historial de paquetes del servidor
  /api/fm/reportes        lista las carpetas de resultados/ con reporte.json
  /api/fm/reporte?dir=X   devuelve resultados/X/reporte.json (+ rutas de gráficas y audio)
  /api/fm/capturar (POST) corre sdr_analisis.py (en_vivo o archivo); libera el dongle mientras tanto
  /resultados/...         archivos de resultados (graficas.png, audio.wav)
"""

import argparse
import collections
import csv
import datetime as dt
import json
import math
import mimetypes
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

import sdr_lora as sl

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RAIZ = Path(__file__).resolve().parent
WEB = RAIZ / "web"
RESULTADOS = RAIZ / "resultados"
REGISTROS = RAIZ / "registros"

# Estación base por defecto de la simulación: Universidad del Norte, Barranquilla
BASE_SIM = (11.0190, -74.8505)

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def limpio(v):
    """Convierte a algo serializable en JSON estricto (sin NaN/inf, sin tipos numpy)."""
    if isinstance(v, (np.floating, float)):
        v = float(v)
        return None if not math.isfinite(v) else v
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, dict):
        return {k: limpio(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [limpio(x) for x in v]
    return v


# ----------------------------------------------------------------------------------------
# Bus de eventos (SSE)
# ----------------------------------------------------------------------------------------

class Bus:
    def __init__(self):
        self.lock = threading.Lock()
        self.clientes = set()
        self.historial = collections.deque(maxlen=20000)   # paquetes ya enviados (para recargas)
        self.estado = {}
        self.ultimos_stats = None

    @staticmethod
    def _msg(tipo, datos):
        return f"event: {tipo}\ndata: {json.dumps(limpio(datos), ensure_ascii=False)}\n\n".encode("utf-8")

    def publicar(self, tipo, datos, guardar=False):
        m = self._msg(tipo, datos)
        with self.lock:
            if guardar:
                self.historial.append(m)
            if tipo == "estado":
                self.estado = datos
            if tipo == "stats":
                self.ultimos_stats = m
            for q in list(self.clientes):
                try:
                    q.put_nowait(m)
                except queue.Full:
                    pass   # cliente lento: pierde este mensaje

    def suscribir(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            iniciales = [self._msg("estado", self.estado)] if self.estado else []
            iniciales += list(self.historial)
            if self.ultimos_stats:
                iniciales.append(self.ultimos_stats)
            self.clientes.add(q)
        return q, iniciales

    def desuscribir(self, q):
        with self.lock:
            self.clientes.discard(q)

    def limpiar(self):
        with self.lock:
            self.historial.clear()


# ----------------------------------------------------------------------------------------
# Paquetes -> JSON, posición GPS y registro CSV
# ----------------------------------------------------------------------------------------

RE_POS = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parsear_posicion(texto):
    """'lat,lon' tal como lo envía GPS_LoRa_TTGO.ino -> (lat, lon) o None."""
    if not texto:
        return None
    m = RE_POS.match(texto)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon


def paquete_json(p, n):
    datos = p["datos"]
    imprimible = all(32 <= c < 127 or c in (9, 10, 13) or c >= 0xC0 for c in datos)
    texto = datos.decode("utf-8", errors="replace") if imprimible else None
    pos = parsear_posicion(texto)
    hora = p["hora"]
    return {
        "n": n,
        "t": hora.timestamp() * 1000,
        "hora": hora.strftime("%H:%M:%S.%f")[:-3],
        "texto": texto,
        "hex": datos.hex(" "),
        "largo": p["largo"],
        "cr": p["cr"],
        "crc_ok": p["crc_ok"],
        "crc_presente": p["crc_presente"],
        "bits_corregidos": p["bits_corregidos"],
        "snr_db": round(float(p["snr_db"]), 2),
        "pot_dbfs": round(float(p["pot_dbfs"]), 2),
        "cfo_hz": round(float(p["cfo_hz"]), 1),
        "cfo_ppm": round(float(p["cfo_ppm"]), 3) if p.get("cfo_ppm") is not None else None,
        "sync_word": p["sync_word"],
        "n_simbolos": p["n_simbolos"],
        "duracion_ms": round(float(p["duracion_ms"]), 3),
        "lat": pos[0] if pos else None,
        "lon": pos[1] if pos else None,
    }


class RegistroCSV:
    CAMPOS = ["n", "hora", "lat", "lon", "texto", "hex", "crc_ok", "largo", "cr", "n_simbolos",
              "duracion_ms", "bits_corregidos", "snr_db", "pot_dbfs", "cfo_hz", "cfo_ppm", "sync_word"]

    def __init__(self, activo=True):
        self.f = None
        if activo:
            REGISTROS.mkdir(exist_ok=True)
            self.ruta = REGISTROS / f"lora_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
            self.f = open(self.ruta, "w", newline="", encoding="utf-8")
            self.w = csv.DictWriter(self.f, fieldnames=self.CAMPOS, extrasaction="ignore")
            self.w.writeheader()
            log(f"Registrando paquetes en {self.ruta}")

    def escribir(self, pj):
        if self.f:
            self.w.writerow(pj)
            self.f.flush()

    def cerrar(self):
        if self.f:
            self.f.close()


# ----------------------------------------------------------------------------------------
# Espectro para la cascada
# ----------------------------------------------------------------------------------------

class Espectro:
    """Calcula filas de la cascada (dBFS) a partir de los bloques IQ crudos, a tasa limitada."""

    def __init__(self, nfft=2048, bins=512, filas_por_s=8.0):
        self.nfft, self.bins = nfft, bins
        self.periodo = 1.0 / filas_por_s
        self.win = np.hanning(nfft).astype(np.float32)
        self.norm = float(np.sum(self.win) ** 2)
        self.t = 0.0

    def procesar(self, x, forzar=False):
        ahora = time.time()
        if not forzar and ahora - self.t < self.periodo:
            return None
        self.t = ahora
        nseg = min(24, len(x) // self.nfft)
        if nseg == 0:
            return None
        segs = x[: nseg * self.nfft].reshape(nseg, self.nfft) * self.win
        p = np.mean(np.abs(np.fft.fft(segs, axis=1)) ** 2, axis=0) / self.norm
        p = np.fft.fftshift(p).reshape(self.bins, -1).max(axis=1)   # max-pooling: conserva picos
        return np.round(10 * np.log10(p + 1e-20), 1).tolist()


# ----------------------------------------------------------------------------------------
# Motor de recepción LoRa (dongle o archivo), reutilizando sdr_lora.Receptor
# ----------------------------------------------------------------------------------------

class MotorLoRa(threading.Thread):
    def __init__(self, a, bus, registro):
        super().__init__(daemon=True)
        self.a, self.bus, self.reg = a, bus, registro
        self.salir = threading.Event()
        self.pausa = threading.Event()        # pedida para liberar el dongle (captura FM)
        self.libre = threading.Event()        # el dongle está cerrado
        self.libre.set()
        self.usa_dongle = not a.archivo
        self.n_tot = self.n_ok = 0
        self.headers_malos = 0
        self.archivo_terminado = False

    # -- control externo --
    def pedir_dongle(self, timeout=10):
        self.pausa.set()
        return self.libre.wait(timeout)

    def devolver_dongle(self):
        self.pausa.clear()

    def detener(self):
        self.salir.set()

    # -- bucle --
    def run(self):
        while not self.salir.is_set():
            if self.pausa.is_set() or self.archivo_terminado:
                time.sleep(0.2)
                continue
            try:
                self._sesion()
            except SystemExit as e:           # sdr_lora usa sys.exit() con mensajes útiles
                self._aviso(str(e), "error")
                self._esperar(5)
            except Exception as e:
                self._aviso(f"Error en el receptor: {e!r}", "error")
                self._esperar(5)

    def _esperar(self, s):
        fin = time.time() + s
        while time.time() < fin and not self.salir.is_set():
            time.sleep(0.2)

    def _aviso(self, texto, nivel="aviso"):
        log(texto)
        self.bus.publicar("aviso", {"t": time.time() * 1000, "nivel": nivel, "texto": texto})

    def _sesion(self):
        a = self.a
        freq_hz = a.freq * 1e6
        ldro = {"auto": None, "si": True, "no": False}[a.ldro]
        sdr = None
        cola, parar = queue.Queue(maxsize=64), threading.Event()
        try:
            if a.archivo:
                fs, gen = sl.leer_archivo(a.archivo)
                fs = fs or a.fs
                if a.centro:
                    centro = a.centro * 1e6
                else:
                    mm = re.search(r"(\d{6,})Hz", Path(a.archivo).name)
                    centro = float(mm.group(1)) if mm else freq_hz - fs / 4
                fuente = f"archivo {Path(a.archivo).name}"

                def bloques():
                    t0, n = time.time(), 0
                    for x in gen:
                        if self.salir.is_set():
                            return
                        yield x
                        n += len(x)
                        if not a.rapido:   # reproducir a velocidad real
                            espera = t0 + n / fs - time.time()
                            if espera > 0:
                                time.sleep(espera)
            else:
                fs = a.fs
                centro = freq_hz - fs / 4     # igual que sdr_lora.py: alejar el pico de DC
                ganancia = "auto" if str(a.ganancia).lower() == "auto" else float(a.ganancia)
                self.libre.clear()
                sdr = sl.abrir_rtlsdr(centro, fs, ganancia, a.ppm)
                fs = sdr.sample_rate
                fuente = "RTL-SDR"
                hilo = threading.Thread(target=sl.leer_rtlsdr, args=(sdr, cola, parar), daemon=True)
                hilo.start()
                log(f"RTL-SDR abierto: centro {sdr.center_freq / 1e6:.4f} MHz, fs {fs / 1e6:.3f} MS/s")

                def bloques():
                    while not self.salir.is_set() and not self.pausa.is_set():
                        try:
                            x = cola.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        if isinstance(x, Exception):
                            raise x
                        if getattr(sl.leer_rtlsdr, "perdidos", 0):
                            sl.leer_rtlsdr.perdidos = 0
                            self._aviso("Se perdieron muestras: el PC no alcanzó a procesar.")
                        yield x

            offset = freq_hz - centro
            esp = Espectro()
            estado = {
                "modo": "archivo" if a.archivo else "vivo",
                "fuente": fuente,
                "freq_hz": freq_hz, "centro_hz": centro, "fs": fs,
                "ganancia": a.ganancia, "ppm": a.ppm,
                "sf": None, "bw": None, "ldro": None, "simbolo_ms": None,
                "detectando": False,
                "base": None,
            }
            it = bloques()
            previos = []
            sf, bw = a.sf, (a.bw * 1e3 if a.bw else None)

            if sf is None and bw is None:
                estado["detectando"] = True
                self.bus.publicar("estado", estado)
                self._aviso("Detectando SF/BW: esperando un preámbulo…", "info")
                sf, bw = 7, 500e3
                acum, t0 = [], time.time()
                detectado = False
                for x in it:
                    acum.append(x)
                    fila = esp.procesar(x)
                    if fila:
                        self.bus.publicar("espectro", {"fila": fila})
                    if sum(map(len, acum)) / fs >= 1.5:
                        r = sl.detectar_parametros(np.concatenate(acum), fs, offset)
                        if r:
                            sf, bw = r[0], r[1]
                            self._aviso(f"Detectado SF{sf}, BW {bw / 1e3:g} kHz (pureza {r[2]:.2f})", "info")
                            detectado = True
                            break
                        acum = acum[len(acum) // 2:]
                        if time.time() - t0 > 30 and not a.archivo:
                            self._aviso("Nada detectado en 30 s; uso SF7 / BW 500 kHz (los del sketch).")
                            break
                if not detectado and a.archivo and acum:
                    r = sl.detectar_parametros(np.concatenate(acum), fs, offset)
                    if r:
                        sf, bw = r[0], r[1]
                        self._aviso(f"Detectado SF{sf}, BW {bw / 1e3:g} kHz", "info")
                previos = acum
            sf = sf or 7
            bw = bw or 500e3

            rx = sl.Receptor(fs, offset, sf, bw, ldro, freq_hz)
            estado.update({"sf": sf, "bw": bw, "ldro": bool(rx.lora.ldro),
                           "simbolo_ms": rx.lora.N / bw * 1e3, "detectando": False})
            self.bus.publicar("estado", estado)
            log(f"Escuchando LoRa en {freq_hz / 1e6:.3f} MHz | SF{sf} | BW {bw / 1e3:g} kHz")

            t_stats = t_sat = t_ultimo = time.time()
            for x in (y for f in (previos, it) for y in f):
                fila = esp.procesar(x)
                if fila:
                    self.bus.publicar("espectro", {"fila": fila})
                for p in rx.alimentar(x):
                    self.n_tot += 1
                    self.n_ok += bool(p["crc_ok"])
                    pj = paquete_json(p, self.n_tot)
                    self.bus.publicar("paquete", pj, guardar=True)
                    self.reg.escribir(pj)
                    t_ultimo = time.time()
                    log(f"Paquete #{self.n_tot} CRC {'OK' if p['crc_ok'] else 'ERROR'} "
                        f"{pj['texto']!r} SNR {pj['snr_db']:+.1f} dB")
                ahora = time.time()
                if rx.max_raw > 0.97 and ahora - t_sat > 10:
                    self._aviso("Saturación del ADC: baja la ganancia (aun así suele decodificar).")
                    t_sat = ahora
                sat = rx.max_raw
                rx.max_raw = 0.0
                if ahora - t_stats > 1:
                    self.headers_malos = rx.headers_malos
                    self.bus.publicar("stats", {"n_tot": self.n_tot, "n_ok": self.n_ok,
                                                "headers_malos": rx.headers_malos,
                                                "nivel_adc": round(sat, 3)})
                    t_stats = ahora
                if not a.archivo and ahora - t_ultimo > 15:
                    self._aviso(f"15 s sin paquetes (SF{sf}/BW{bw / 1e3:g}). Revisa antena, frecuencia "
                                "o reinicia sin --sf/--bw para autodetectar.", "info")
                    t_ultimo = ahora
                if self.pausa.is_set() or self.salir.is_set():
                    break

            if a.archivo and not self.salir.is_set():
                if a.repetir:
                    self._aviso("Fin de la grabación: vuelvo a empezar.", "info")
                else:
                    self._aviso("Fin de la grabación.", "info")
                    self.archivo_terminado = True
        finally:
            parar.set()
            if sdr is not None:
                try:
                    sdr.cancel_read_async()
                except Exception:
                    pass
                time.sleep(0.3)
                try:
                    sdr.close()
                except Exception:
                    pass
                log("RTL-SDR cerrado")
            self.libre.set()


# ----------------------------------------------------------------------------------------
# Simulación (sin hardware): recorrido alrededor de Uninorte con un modelo de enlace simple
# ----------------------------------------------------------------------------------------

def distancia_m(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def n_simbolos_payload(sf, cr, largo, crc=True, ldro=False):
    sf_app = sf - 2 if ldro else sf
    nib = 2 * largo + (4 if crc else 0) - (sf - 2 - 5)
    return 8 + max(0, math.ceil(nib / sf_app)) * (4 + cr)


class MotorSim(threading.Thread):
    usa_dongle = False

    def __init__(self, a, bus, registro):
        super().__init__(daemon=True)
        self.a, self.bus, self.reg = a, bus, registro
        self.salir = threading.Event()
        self.sf, self.bw, self.cr = 7, 500e3, 1
        self.freq = a.freq * 1e6
        self.fs, self.centro = 2.0e6, self.freq - 0.5e6
        self.headers_malos = 0

    def pedir_dongle(self, timeout=10):
        return True

    def devolver_dongle(self):
        pass

    def detener(self):
        self.salir.set()

    def posicion(self, s):
        """Recorrido cerrado (~7 km) cerca del campus; s = distancia recorrida en m."""
        L = 7000.0
        th = 2 * math.pi * (s % L) / L
        lat = BASE_SIM[0] + 0.0060 + 0.0085 * math.sin(th) + 0.0022 * math.sin(3 * th + 0.6)
        lon = BASE_SIM[1] + 0.0100 + 0.0115 * math.cos(th) + 0.0030 * math.cos(2 * th)
        return lat, lon

    def fila_espectro(self, pot_paquete=None):
        nb = 512
        f = np.linspace(-1, 1, nb, endpoint=False)
        fila = -86 + 2.0 * np.random.randn(nb) - 3 * f ** 2
        fila[nb // 2 - 1: nb // 2 + 2] += 14                       # pico de DC del dongle
        fila[int(nb * 0.18)] += 22                                 # portadora interferente fija
        fila[int(nb * 0.83)] += 9 + 3 * math.sin(time.time() / 3)
        if pot_paquete is not None:                                # ráfaga LoRa: 915 ± 250 kHz
            ini = int(nb * (0.5 + (self.freq - 250e3 - self.centro) / self.fs))
            fin = int(nb * (0.5 + (self.freq + 250e3 - self.centro) / self.fs))
            fila[ini:fin] = np.maximum(fila[ini:fin], pot_paquete + 1.5 * np.random.randn(fin - ini))
        return np.round(fila, 1).tolist()

    def run(self):
        estado = {"modo": "simulacion", "fuente": "simulación", "freq_hz": self.freq,
                  "centro_hz": self.centro, "fs": self.fs, "ganancia": self.a.ganancia, "ppm": self.a.ppm,
                  "sf": self.sf, "bw": self.bw, "ldro": False, "simbolo_ms": (1 << self.sf) / self.bw * 1e3,
                  "detectando": False, "base": {"lat": BASE_SIM[0], "lon": BASE_SIM[1], "nombre": "Uninorte"}}
        self.bus.publicar("estado", estado)
        log("Modo simulación: recorrido alrededor de Uninorte (sin hardware)")
        s, n, n_ok = 0.0, 0, 0
        vel = 11.0                          # m/s (~40 km/h)
        t_pkt = time.time()
        t_fila = 0.0
        rafaga = 0
        pot_ultimo = None
        while not self.salir.is_set():
            ahora = time.time()
            if ahora - t_fila >= 0.125:
                self.bus.publicar("espectro", {"fila": self.fila_espectro(pot_ultimo if rafaga else None)})
                rafaga = max(0, rafaga - 1)
                t_fila = ahora
            if ahora - t_pkt >= self.a.intervalo:
                dt_s = ahora - t_pkt
                t_pkt = ahora
                s += vel * dt_s
                lat, lon = self.posicion(s)
                lat += random.gauss(0, 1.5e-5)
                lon += random.gauss(0, 1.5e-5)
                d = max(30.0, distancia_m(BASE_SIM, (lat, lon)))
                snr = 30 - 28 * math.log10(d / 100) + random.gauss(0, 1.6)   # log-distancia con n = 2.8
                if snr < -11 or random.random() < 0.03:
                    continue                                    # paquete perdido: ni se detecta
                crc_ok = not (snr < -6 and random.random() < min(0.9, (-6 - snr) / 5))
                texto = f"{lat:.7f},{lon:.7f}"
                datos = bytearray(texto.encode())
                if not crc_ok:
                    for _ in range(random.randint(1, 3)):
                        datos[random.randrange(len(datos))] ^= 1 << random.randrange(8)
                largo = len(datos)
                nsim = n_simbolos_payload(self.sf, self.cr, largo)
                pot = -32 + 0.85 * (snr - 24) + random.gauss(0, 0.8)
                cfo = -2930 + 55 * math.sin(ahora / 40) + random.gauss(0, 12)
                bits = max(0, int(np.random.poisson(max(0.05, (4 - snr) / 2.5))))
                n += 1
                n_ok += crc_ok
                hora = dt.datetime.now()
                pj = {
                    "n": n, "t": hora.timestamp() * 1000, "hora": hora.strftime("%H:%M:%S.%f")[:-3],
                    "texto": datos.decode("utf-8", errors="replace"), "hex": bytes(datos).hex(" "),
                    "largo": largo, "cr": f"4/{4 + self.cr}", "crc_ok": crc_ok, "crc_presente": True,
                    "bits_corregidos": bits, "snr_db": round(snr, 2), "pot_dbfs": round(pot, 2),
                    "cfo_hz": round(cfo, 1), "cfo_ppm": round(cfo / self.freq * 1e6, 3), "sync_word": "0x12",
                    "n_simbolos": nsim, "duracion_ms": round((8 + 4.25 + nsim) * (1 << self.sf) / self.bw * 1e3, 3),
                    "lat": None, "lon": None,
                }
                pos = parsear_posicion(pj["texto"]) if crc_ok else None
                if pos:
                    pj["lat"], pj["lon"] = pos
                self.bus.publicar("paquete", pj, guardar=True)
                self.reg.escribir(pj)
                self.bus.publicar("stats", {"n_tot": n, "n_ok": n_ok, "headers_malos": self.headers_malos,
                                            "nivel_adc": round(min(1.0, 10 ** ((pot + 3) / 20)), 3)})
                pot_ultimo = pot - 24   # nivel aproximado por bin de la cascada
                rafaga = 1
            time.sleep(0.02)


# ----------------------------------------------------------------------------------------
# Capturas FM con sdr_analisis.py (subproceso; el dongle se libera mientras dura)
# ----------------------------------------------------------------------------------------

class CapturaFM:
    def __init__(self, a, bus, motor):
        self.a, self.bus, self.motor = a, bus, motor
        self.lock = threading.Lock()
        self.ocupado = False

    def iniciar(self, pedido):
        with self.lock:
            if self.ocupado:
                return False, "Ya hay una captura en curso."
            self.ocupado = True
        threading.Thread(target=self._correr, args=(pedido,), daemon=True).start()
        return True, "Captura iniciada."

    def _log(self, texto, nivel="info"):
        self.bus.publicar("fm_log", {"t": time.time() * 1000, "texto": texto, "nivel": nivel})

    def _correr(self, p):
        script = RAIZ / "sdr_analisis.py"
        modo = p.get("modo", "en_vivo")
        try:
            freq = float(p.get("freq", 103.1))
            cmd = [sys.executable, "-u", str(script)]
            if modo == "archivo":
                ruta = str(p.get("ruta", "")).strip().strip('"')
                if not ruta or not Path(ruta).is_file():
                    raise ValueError(f"No encuentro el archivo: {ruta or '(vacío)'}")
                cmd += ["archivo", ruta, "--freq", f"{freq}", "--segundos", f"{float(p.get('segundos', 15))}"]
                if p.get("centro"):
                    cmd += ["--centro", f"{float(p['centro'])}"]
            else:
                cmd += ["en_vivo", "--freq", f"{freq}", "--segundos", f"{float(p.get('segundos', 10))}",
                        "--ganancia", str(p.get("ganancia", "auto")), "--ppm", str(self.a.ppm)]
            cmd += ["--deenfasis", f"{float(p.get('deenfasis', 75))}"]
        except (TypeError, ValueError) as e:
            self._log(str(e), "error")
            self.bus.publicar("fm_listo", {"ok": False, "error": str(e)})
            with self.lock:
                self.ocupado = False
            return

        pausado = False
        try:
            if modo == "en_vivo" and self.motor.usa_dongle:
                self._log("Pausando el receptor LoRa para liberar el dongle…")
                pausado = True
                if not self.motor.pedir_dongle(15):
                    raise RuntimeError("El receptor LoRa no soltó el dongle a tiempo.")
            self._log("$ " + " ".join(Path(c).name if i in (0, 2) else c for i, c in enumerate(cmd)))
            env = dict(os.environ, PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
            proc = subprocess.Popen(cmd, cwd=str(RAIZ), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", env=env, bufsize=1)
            carpeta = None
            ultimo_progreso = 0.0
            for linea in proc.stdout:
                linea = linea.rstrip()
                if not linea:
                    continue
                m = re.search(r"Resultados en:\s*(.+)$", linea)
                if m:
                    carpeta = Path(m.group(1).strip()).name
                if linea.lstrip().startswith("capturando...") and time.time() - ultimo_progreso < 0.4:
                    continue
                ultimo_progreso = time.time()
                self._log(linea)
            codigo = proc.wait()
            if codigo == 0 and carpeta:
                self._log(f"Listo: resultados/{carpeta}", "ok")
                self.bus.publicar("fm_listo", {"ok": True, "dir": carpeta})
            else:
                self._log(f"sdr_analisis.py terminó con código {codigo}", "error")
                self.bus.publicar("fm_listo", {"ok": False, "error": f"código {codigo}"})
        except Exception as e:
            self._log(f"Error: {e}", "error")
            self.bus.publicar("fm_listo", {"ok": False, "error": str(e)})
        finally:
            if pausado:
                self.motor.devolver_dongle()
                self._log("Receptor LoRa reanudado.")
            with self.lock:
                self.ocupado = False


def listar_reportes():
    salida = []
    if not RESULTADOS.is_dir():
        return salida
    for d in sorted(RESULTADOS.iterdir(), reverse=True):
        rj = d / "reporte.json"
        if not rj.is_file():
            continue
        try:
            r = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            continue
        fuente, rds = r.get("fuente", {}), r.get("rds", {})
        salida.append({
            "dir": d.name,
            "fecha": dt.datetime.fromtimestamp(rj.stat().st_mtime).isoformat(timespec="seconds"),
            "emisora_MHz": fuente.get("emisora_MHz"),
            "modo": fuente.get("modo"),
            "PS": rds.get("PS"),
            "SNR_dB": r.get("rf", {}).get("SNR_dB"),
        })
    return salida


# ----------------------------------------------------------------------------------------
# Servidor HTTP
# ----------------------------------------------------------------------------------------

def crear_handler(bus, motor, captura, a):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SDR-GPS/1.0"

        def log_message(self, fmt, *args):
            if a.verbose:
                super().log_message(fmt, *args)

        # -- utilidades --
        def _json(self, obj, codigo=200):
            cuerpo = json.dumps(limpio(obj), ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)

        def _archivo(self, ruta, base):
            try:
                ruta = ruta.resolve()
                ruta.relative_to(base.resolve())
            except (ValueError, OSError):
                return self._json({"error": "ruta no permitida"}, 403)
            if not ruta.is_file():
                return self._json({"error": "no existe"}, 404)
            tam = ruta.stat().st_size
            tipo = mimetypes.guess_type(str(ruta))[0] or "application/octet-stream"
            ini, fin, codigo = 0, tam - 1, 200
            rng = self.headers.get("Range")
            m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
            if m and tam:
                if m.group(1):
                    ini = int(m.group(1))
                    fin = int(m.group(2)) if m.group(2) else tam - 1
                else:
                    ini = max(0, tam - int(m.group(2) or 0))
                fin = min(fin, tam - 1)
                if ini > fin:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{tam}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                codigo = 206
            self.send_response(codigo)
            self.send_header("Content-Type", tipo + ("; charset=utf-8" if tipo.startswith("text/") or
                                                     tipo.endswith("javascript") else ""))
            self.send_header("Content-Length", str(fin - ini + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            if codigo == 206:
                self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(ruta, "rb") as f:
                f.seek(ini)
                quedan = fin - ini + 1
                while quedan > 0:
                    trozo = f.read(min(65536, quedan))
                    if not trozo:
                        break
                    self.wfile.write(trozo)
                    quedan -= len(trozo)

        def _cuerpo_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 100000:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        # -- SSE --
        def _eventos(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q, iniciales = bus.suscribir()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.write(b"event: historial_inicio\ndata: {}\n\n")
                for m in iniciales:
                    self.wfile.write(m)
                self.wfile.write(b"event: historial_fin\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        m = q.get(timeout=15)
                    except queue.Empty:
                        m = b": ping\n\n"
                    self.wfile.write(m)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                bus.desuscribir(q)
                self.close_connection = True

        # -- rutas --
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            ruta = unquote(u.path)
            if ruta == "/eventos":
                return self._eventos()
            if ruta == "/api/estado":
                return self._json({"estado": bus.estado, "captura_fm": captura.ocupado})
            if ruta == "/api/fm/reportes":
                return self._json(listar_reportes())
            if ruta == "/api/fm/reporte":
                nombre = Path(parse_qs(u.query).get("dir", [""])[0]).name
                rj = RESULTADOS / nombre / "reporte.json"
                if not nombre or not rj.is_file():
                    return self._json({"error": "no existe ese reporte"}, 404)
                r = json.loads(rj.read_text(encoding="utf-8"))
                extra = {"dir": nombre}
                for clave, archivo in (("graficas", "graficas.png"), ("audio", "audio.wav")):
                    if (RESULTADOS / nombre / archivo).is_file():
                        extra[clave] = f"/resultados/{nombre}/{archivo}"
                return self._json({"reporte": r, "archivos": extra})
            if ruta.startswith("/resultados/"):
                return self._archivo(RESULTADOS / ruta[len("/resultados/"):], RESULTADOS)
            if ruta in ("", "/"):
                ruta = "/index.html"
            return self._archivo(WEB / ruta.lstrip("/"), WEB)

        def do_POST(self):
            ruta = urlparse(self.path).path
            if ruta == "/api/limpiar":
                bus.limpiar()
                return self._json({"ok": True})
            if ruta == "/api/fm/capturar":
                ok, msg = captura.iniciar(self._cuerpo_json())
                return self._json({"ok": ok, "mensaje": msg}, 200 if ok else 409)
            return self._json({"error": "no encontrado"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=915.0, help="frecuencia LoRa en MHz (def. 915)")
    ap.add_argument("--sf", type=int, help="spreading factor 7..12 (sin --sf ni --bw: autodetección)")
    ap.add_argument("--bw", type=float, help="ancho de banda en kHz: 125, 250, 500")
    ap.add_argument("--ldro", choices=["auto", "si", "no"], default="auto")
    ap.add_argument("--fs", type=float, default=2.0e6, help="muestras/s del dongle (def. 2e6)")
    ap.add_argument("--ganancia", default="10", help="dB o 'auto' (def. 10)")
    ap.add_argument("--ppm", type=int, default=49, help="corrección del cristal del dongle (NooElec: ~49)")
    ap.add_argument("--archivo", help="leer IQ de un WAV baseband o .npy en vez del dongle")
    ap.add_argument("--centro", type=float, help="frecuencia central de la grabación en MHz")
    ap.add_argument("--repetir", action="store_true", help="con --archivo: reproducir en bucle")
    ap.add_argument("--rapido", action="store_true", help="con --archivo: no esperar (más rápido que real)")
    ap.add_argument("--sim", action="store_true", help="simular un recorrido (no necesita el dongle)")
    ap.add_argument("--intervalo", type=float, default=2.0, help="con --sim: segundos entre paquetes")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para verlo desde el celular en la misma red")
    ap.add_argument("--puerto", type=int, default=8000)
    ap.add_argument("--sin-navegador", action="store_true", help="no abrir el navegador automáticamente")
    ap.add_argument("--sin-csv", action="store_true", help="no guardar registros/lora_*.csv")
    ap.add_argument("--verbose", action="store_true", help="mostrar cada petición HTTP")
    a = ap.parse_args()

    if not (WEB / "index.html").is_file():
        sys.exit(f"No encuentro {WEB / 'index.html'}: la carpeta web/ debe estar junto a este script.")

    bus = Bus()
    registro = RegistroCSV(not a.sin_csv)
    motor = MotorSim(a, bus, registro) if a.sim else MotorLoRa(a, bus, registro)
    captura = CapturaFM(a, bus, motor)
    srv = ThreadingHTTPServer((a.host, a.puerto), crear_handler(bus, motor, captura, a))
    srv.daemon_threads = True
    motor.start()

    url = f"http://{'localhost' if a.host in ('127.0.0.1', '0.0.0.0') else a.host}:{a.puerto}"
    log(f"Interfaz en {url}  (Ctrl+C para salir)")
    if not a.sin_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        log("Cerrando…")
        motor.detener()
        motor.join(timeout=3)
        srv.server_close()
        registro.cerrar()


if __name__ == "__main__":
    main()
