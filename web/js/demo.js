/* Demostración en el navegador (sin servidor): imita los eventos de servidor_gps.py --sim. */
(function (S) {
  "use strict";
  const U = S.util;

  const BASE = { lat: 11.019, lon: -74.8505, nombre: "Uninorte (demostración)" };

  class Demo {
    constructor(emitir) {
      this.emitir = emitir;
      this.timers = [];
      this.n = 0;
      this.nOk = 0;
      this.s = 0;
      this.sf = 7;
      this.bw = 500e3;
      this.freq = 915e6;
      this.fs = 2e6;
      this.centro = this.freq - this.fs / 4;
      this.rafaga = 0;
      this.potUlt = -60;
    }

    gauss() {
      let u = 0, v = 0;
      while (!u) u = Math.random();
      while (!v) v = Math.random();
      return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
    }

    posicion(s) {
      const L = 7000, th = (2 * Math.PI * (s % L)) / L;
      return [
        BASE.lat + 0.006 + 0.0085 * Math.sin(th) + 0.0022 * Math.sin(3 * th + 0.6),
        BASE.lon + 0.01 + 0.0115 * Math.cos(th) + 0.003 * Math.cos(2 * th),
      ];
    }

    iniciar() {
      this.emitir("estado", {
        modo: "demo", fuente: "demostración en el navegador", freq_hz: this.freq, centro_hz: this.centro, fs: this.fs,
        ganancia: "10", ppm: 49, sf: this.sf, bw: this.bw, ldro: false, simbolo_ms: ((1 << this.sf) / this.bw) * 1e3,
        detectando: false, base: BASE,
      });
      this.emitir("aviso", { t: Date.now(), nivel: "info", texto: "Demostración: los paquetes son simulados en el navegador." });
      // historia previa para que las gráficas no arranquen vacías
      const t0 = Date.now() - 150 * 2000;
      for (let i = 0; i < 150; i++) this.paquete(t0 + i * 2000, false);
      this.timers.push(setInterval(() => this.fila(), 125));
      this.timers.push(setInterval(() => this.paquete(Date.now(), true), 2000));
    }

    detener() {
      this.timers.forEach(clearInterval);
      this.timers = [];
    }

    fila() {
      const nb = 512, fila = new Array(nb);
      const t = Date.now() / 1000;
      for (let i = 0; i < nb; i++) {
        const f = (i / nb) * 2 - 1;
        fila[i] = -86 + 2 * this.gauss() - 3 * f * f;
      }
      fila[255] += 14; fila[256] += 14; fila[257] += 10;
      fila[Math.floor(nb * 0.18)] += 22;
      fila[Math.floor(nb * 0.83)] += 9 + 3 * Math.sin(t / 3);
      if (this.rafaga > 0) {
        const i0 = Math.floor(nb * (0.5 + (this.freq - 250e3 - this.centro) / this.fs));
        const i1 = Math.floor(nb * (0.5 + (this.freq + 250e3 - this.centro) / this.fs));
        for (let i = i0; i < i1; i++) fila[i] = Math.max(fila[i], this.potUlt + 1.5 * this.gauss());
        this.rafaga--;
      }
      this.emitir("espectro", { fila: fila.map((v) => Math.round(v * 10) / 10) });
    }

    paquete(t, vivo) {
      this.s += 11 * 2; // 40 km/h, un paquete cada 2 s
      let [lat, lon] = this.posicion(this.s);
      lat += this.gauss() * 1.5e-5;
      lon += this.gauss() * 1.5e-5;
      const d = Math.max(30, U.haversine([BASE.lat, BASE.lon], [lat, lon]));
      const snr = 30 - 28 * Math.log10(d / 100) + 1.6 * this.gauss();
      if (snr < -11 || Math.random() < 0.03) return;
      const crcOk = !(snr < -6 && Math.random() < Math.min(0.9, (-6 - snr) / 5));
      let texto = `${lat.toFixed(7)},${lon.toFixed(7)}`;
      const bytes = Array.from(new TextEncoder().encode(texto));
      if (!crcOk) {
        const k = 1 + Math.floor(Math.random() * 3);
        for (let i = 0; i < k; i++) bytes[Math.floor(Math.random() * bytes.length)] ^= 1 << Math.floor(Math.random() * 8);
        texto = String.fromCharCode(...bytes);
      }
      const largo = bytes.length;
      const nsim = U.nSimbolos(this.sf, 1, largo);
      const pot = -32 + 0.85 * (snr - 24) + 0.8 * this.gauss();
      const cfo = -2930 + 55 * Math.sin(t / 40000) + 12 * this.gauss();
      const bits = Math.max(0, Math.round(Math.max(0.05, (4 - snr) / 2.5) * (0.5 + Math.random())));
      this.n++;
      this.nOk += crcOk;
      const d8 = new Date(t);
      const p2 = (x, n = 2) => String(x).padStart(n, "0");
      const p = {
        n: this.n, t, hora: `${p2(d8.getHours())}:${p2(d8.getMinutes())}:${p2(d8.getSeconds())}.${p2(d8.getMilliseconds(), 3)}`,
        texto, hex: bytes.map((b) => b.toString(16).padStart(2, "0")).join(" "),
        largo, cr: "4/5", crc_ok: crcOk, crc_presente: true, bits_corregidos: bits,
        snr_db: Math.round(snr * 100) / 100, pot_dbfs: Math.round(pot * 100) / 100,
        cfo_hz: Math.round(cfo * 10) / 10, cfo_ppm: Math.round((cfo / this.freq) * 1e9) / 1e3, sync_word: "0x12",
        n_simbolos: nsim, duracion_ms: Math.round((8 + 4.25 + nsim) * ((1 << this.sf) / this.bw) * 1e6) / 1e3,
        lat: crcOk ? Number(lat.toFixed(7)) : null, lon: crcOk ? Number(lon.toFixed(7)) : null,
      };
      this.emitir("paquete", p, !vivo);
      this.emitir("stats", { n_tot: this.n, n_ok: this.nOk, headers_malos: 0, nivel_adc: Math.min(1, Math.pow(10, (pot + 3) / 20)) });
      this.potUlt = pot - 24;
      this.rafaga = 1;
    }
  }

  S.Demo = Demo;
})(window.SDR);
