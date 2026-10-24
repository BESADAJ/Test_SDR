/* Utilidades compartidas: formatos, geodesia, colores y descargas. */
window.SDR = window.SDR || {};

(function (S) {
  "use strict";

  const U = (S.util = {});

  U.$ = (sel, raiz = document) => raiz.querySelector(sel);
  U.$$ = (sel, raiz = document) => Array.from(raiz.querySelectorAll(sel));

  U.esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const MENOS = "−";
  /** Número con decimales fijos y signo menos tipográfico. */
  U.num = (v, dec = 1, conSigno = false) => {
    if (v === null || v === undefined || !Number.isFinite(v)) return "—";
    const s = Math.abs(v).toFixed(dec);
    if (v < 0 && Number(Math.abs(v).toFixed(dec)) !== 0) return MENOS + s;
    return (conSigno && v > 0 ? "+" : "") + s;
  };
  U.coord = (v) => (Number.isFinite(v) ? (v < 0 ? MENOS : "") + Math.abs(v).toFixed(7) : "—");

  U.distancia = (m) => {
    if (!Number.isFinite(m)) return "—";
    if (m < 1000) return `${Math.round(m)} m`;
    return `${U.num(m / 1000, m < 10000 ? 2 : 1)} km`;
  };
  U.mhz = (hz, dec = 3) => (Number.isFinite(hz) ? `${U.num(hz / 1e6, dec)} MHz` : "—");
  U.khz = (hz) => (Number.isFinite(hz) ? `${U.num(hz / 1e3, hz % 1000 ? 1 : 0)} kHz` : "—");

  U.hace = (ms) => {
    if (!Number.isFinite(ms)) return "—";
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `hace ${s} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `hace ${m} min ${s % 60} s`;
    return `hace ${Math.floor(m / 60)} h ${m % 60} min`;
  };
  U.horaCorta = (t) => new Date(t).toLocaleTimeString("es-CO", { hour12: false });

  /* ---------------------------------------------------------- geodesia */
  const R = 6371008.8;
  const rad = (g) => (g * Math.PI) / 180;
  U.haversine = (a, b) => {
    const dp = rad(b[0] - a[0]), dl = rad(b[1] - a[1]);
    const h = Math.sin(dp / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(dl / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  };
  U.rumbo = (a, b) => {
    const p1 = rad(a[0]), p2 = rad(b[0]), dl = rad(b[1] - a[1]);
    const y = Math.sin(dl) * Math.cos(p2);
    const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
    return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  };
  const PUNTOS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"];
  U.cardinal = (g) => PUNTOS[Math.round(g / 45) % 8];

  /* ---------------------------------------------------------- LoRa */
  /** Límite típico de demodulación del SX127x por spreading factor (datasheet Semtech). */
  U.LIMITE_SNR = { 6: -5, 7: -7.5, 8: -10, 9: -12.5, 10: -15, 11: -17.5, 12: -20 };
  U.SYNC = { "0x12": "LoRa privado", "0x34": "LoRaWAN público" };

  U.nSimbolos = (sf, cr, largo, crc = true, ldro = false) => {
    const sfApp = ldro ? sf - 2 : sf;
    const nib = 2 * largo + (crc ? 4 : 0) - (sf - 2 - 5);
    return 8 + Math.max(0, Math.ceil(nib / sfApp)) * (4 + cr);
  };

  /* ---------------------------------------------------------- color por SNR */
  const PARADAS = [
    [-10, [207, 68, 54]],
    [-2.5, [227, 121, 46]],
    [3.5, [230, 162, 30]],
    [8.6, [155, 184, 74]],
    [14, [47, 158, 106]],
    [20, [31, 138, 122]],
  ];
  U.colorSNR = (snr) => {
    if (!Number.isFinite(snr)) return "#8a96a0";
    if (snr <= PARADAS[0][0]) return rgb(PARADAS[0][1]);
    for (let i = 1; i < PARADAS.length; i++) {
      const [x1, c1] = PARADAS[i];
      if (snr <= x1) {
        const [x0, c0] = PARADAS[i - 1];
        const t = (snr - x0) / (x1 - x0);
        return rgb(c0.map((v, k) => v + (c1[k] - v) * t));
      }
    }
    return rgb(PARADAS[PARADAS.length - 1][1]);
  };
  function rgb(c) {
    return `rgb(${c.map((v) => Math.round(v)).join(",")})`;
  }

  /* ---------------------------------------------------------- estadística */
  U.media = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : NaN);
  /** Regresión lineal por mínimos cuadrados: y = a + b x. */
  U.regresion = (xs, ys) => {
    const n = xs.length;
    if (n < 3) return null;
    const mx = U.media(xs), my = U.media(ys);
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < n; i++) {
      sxy += (xs[i] - mx) * (ys[i] - my);
      sxx += (xs[i] - mx) ** 2;
      syy += (ys[i] - my) ** 2;
    }
    if (sxx === 0) return null;
    const b = sxy / sxx;
    const r2 = syy ? (sxy * sxy) / (sxx * syy) : 0;
    return { a: my - b * mx, b, r2 };
  };

  /* ---------------------------------------------------------- archivos */
  U.descargar = (nombre, contenido, tipo) => {
    const blob = new Blob([contenido], { type: tipo });
    const url = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement("a"), { href: url, download: nombre });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  };
  U.sello = () => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  };

  U.reducidoMovimiento = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
})(window.SDR);
