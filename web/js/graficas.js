/* Gráficas en canvas (sin dependencias): cascada + espectro, y series del enlace. */
(function (S) {
  "use strict";

  const COL = {
    fondo: "#0F1D29",
    reticula: "#24394B",
    reticulaFina: "#1A2D3C",
    etiqueta: "#8BA4B6",
    traza: "#8FD3E8",
    retenido: "#F4C35C",
    canal: "rgba(239,163,36,0.13)",
    canalBorde: "rgba(239,163,36,0.55)",
    error: "#FF8A7A",
    ambar: "#EFA324",
    texto: "#E6EFF4",
  };
  S.COLORES_PANTALLA = COL;

  function leerColoresCSS() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n, d) => (cs.getPropertyValue(n).trim() || d);
    COL.fondo = v("--pantalla", COL.fondo);
    COL.reticula = v("--reticula", COL.reticula);
  }

  /** Ajusta el tamaño interno del canvas a su tamaño en pantalla (nitidez en pantallas HiDPI). */
  function ajustarCanvas(cv) {
    const r = cv.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (cv.width !== w || cv.height !== h) {
      cv.width = w;
      cv.height = h;
    }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w: r.width, h: r.height };
  }

  /** Marcas "redondas" para un eje: 1, 2, 2.5, 5 × 10^n. */
  function marcas(min, max, cuantas = 5) {
    const rango = max - min || 1;
    const bruto = rango / cuantas;
    const mag = Math.pow(10, Math.floor(Math.log10(bruto)));
    const paso = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((p) => rango / p <= cuantas + 0.5) || 10 * mag;
    const ini = Math.ceil(min / paso - 1e-9) * paso;
    const out = [];
    for (let v = ini; v <= max + paso * 1e-6; v += paso) out.push(Number(v.toFixed(10)));
    return { valores: out, paso };
  }
  S.marcas = marcas;

  /* ================================================================== cascada */

  // Paleta de la cascada: fondo de pantalla → azul → cian → ámbar → blanco cálido
  const PALETA = [
    [0.0, [15, 29, 41]],
    [0.22, [24, 62, 104]],
    [0.45, [40, 128, 170]],
    [0.65, [100, 205, 225]],
    [0.82, [242, 193, 78]],
    [1.0, [255, 246, 220]],
  ];
  const LUT = (() => {
    const lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      let k = 1;
      while (k < PALETA.length - 1 && PALETA[k][0] < t) k++;
      const [t0, c0] = PALETA[k - 1], [t1, c1] = PALETA[k];
      const u = Math.min(1, Math.max(0, (t - t0) / (t1 - t0)));
      for (let j = 0; j < 3; j++) lut[i * 3 + j] = c0[j] + (c1[j] - c0[j]) * u;
    }
    return lut;
  })();

  class Cascada {
    constructor(cvEspectro, cvCascada, ejes, etiquetaRango) {
      this.cvE = cvEspectro;
      this.cvC = cvCascada;
      this.ejes = ejes;
      this.etqRango = etiquetaRango;
      this.filas = 260;
      this.ancho = 512;
      this.cvC.width = this.ancho;
      this.cvC.height = this.filas;
      this.ctxC = this.cvC.getContext("2d");
      this.ctxC.fillStyle = COL.fondo;
      this.ctxC.fillRect(0, 0, this.ancho, this.filas);
      this.fila = null;
      this.suave = null;
      this.retenido = null;
      this.maxHold = false;
      this.piso = null;
      this.techo = null;
      this.marcaPendiente = false;
      this.rango = null;
      new ResizeObserver(() => this.dibujarEspectro()).observe(this.cvE);
    }

    setRango(centro, fs, freq, bw) {
      if (!Number.isFinite(centro) || !Number.isFinite(fs)) return;
      this.rango = { centro, fs, freq, bw };
      const f0 = centro - fs / 2, f1 = centro + fs / 2;
      if (this.etqRango) this.etqRango.textContent = `${S.util.num(f0 / 1e6, 3)} a ${S.util.num(f1 / 1e6, 3)} MHz`;
      const { valores } = marcas(f0 / 1e6, f1 / 1e6, 6);
      this.ejes.innerHTML = valores
        .map((v) => {
          const x = ((v * 1e6 - f0) / fs) * 100;
          return x > 3 && x < 97 ? `<span style="left:${x}%">${S.util.num(v, 2)}</span>` : "";
        })
        .join("");
      this.dibujarEspectro();
    }

    reiniciar() {
      this.ctxC.fillStyle = COL.fondo;
      this.ctxC.fillRect(0, 0, this.ancho, this.filas);
      this.fila = this.suave = this.retenido = null;
      this.piso = this.techo = null;
      this.dibujarEspectro();
    }

    marcarPaquete() {
      this.marcaPendiente = true;
    }

    agregarFila(fila) {
      const n = fila.length;
      if (n !== this.ancho) {
        this.ancho = n;
        this.cvC.width = n;
        this.reiniciar();
      }
      // escala automática: piso = mediana, techo = percentil alto (con memoria)
      const orden = Float32Array.from(fila).sort();
      const med = orden[Math.floor(n * 0.5)];
      const alto = orden[Math.floor(n * 0.995)];
      this.piso = this.piso === null ? med : this.piso * 0.95 + med * 0.05;
      const techoObj = Math.max(alto, this.piso + 28);
      this.techo = this.techo === null ? techoObj : Math.max(techoObj, this.techo * 0.995 + techoObj * 0.005);
      const lo = this.piso - 4, hi = this.techo + 2;

      // desplazar la cascada una fila hacia abajo y pintar la nueva arriba
      this.ctxC.drawImage(this.cvC, 0, 0, n, this.filas - 1, 0, 1, n, this.filas - 1);
      const img = this.ctxC.createImageData(n, 1);
      for (let i = 0; i < n; i++) {
        const t = Math.max(0, Math.min(255, Math.round(((fila[i] - lo) / (hi - lo)) * 255)));
        img.data[i * 4] = LUT[t * 3];
        img.data[i * 4 + 1] = LUT[t * 3 + 1];
        img.data[i * 4 + 2] = LUT[t * 3 + 2];
        img.data[i * 4 + 3] = 255;
      }
      if (this.marcaPendiente) {
        for (let i = 0; i < 6; i++) {
          img.data[i * 4] = 239;
          img.data[i * 4 + 1] = 163;
          img.data[i * 4 + 2] = 36;
        }
        this.marcaPendiente = false;
      }
      this.ctxC.putImageData(img, 0, 0);

      this.fila = fila;
      this.suave = this.suave ? this.suave.map((v, i) => v * 0.55 + fila[i] * 0.45) : fila.slice();
      this.retenido = this.retenido ? this.retenido.map((v, i) => Math.max(v - 0.05, fila[i])) : fila.slice();
      this.dibujarEspectro();
    }

    dibujarEspectro() {
      const { ctx, w, h } = ajustarCanvas(this.cvE);
      ctx.fillStyle = COL.fondo;
      ctx.fillRect(0, 0, w, h);
      if (!this.suave) {
        ctx.fillStyle = COL.etiqueta;
        ctx.font = "13px Barlow, sans-serif";
        ctx.fillText("Esperando muestras del receptor…", 10, h / 2 + 4);
        return;
      }
      const lo = this.piso - 6, hi = this.techo + 6;
      const y = (v) => h - ((v - lo) / (hi - lo)) * h;
      // retícula horizontal cada 10 dB
      ctx.strokeStyle = COL.reticulaFina;
      ctx.fillStyle = COL.etiqueta;
      ctx.font = "11px Barlow, sans-serif";
      ctx.lineWidth = 1;
      for (let v = Math.ceil(lo / 10) * 10; v < hi; v += 10) {
        const yy = Math.round(y(v)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(0, yy);
        ctx.lineTo(w, yy);
        ctx.stroke();
        if (yy > 10 && yy < h - 2) ctx.fillText(`${v}`, 4, yy - 2);
      }
      // canal LoRa
      if (this.rango && this.rango.bw) {
        const { centro, fs, freq, bw } = this.rango;
        const x0 = ((freq - bw / 2 - (centro - fs / 2)) / fs) * w;
        const x1 = ((freq + bw / 2 - (centro - fs / 2)) / fs) * w;
        ctx.fillStyle = COL.canal;
        ctx.fillRect(x0, 0, x1 - x0, h);
        ctx.strokeStyle = COL.canalBorde;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(Math.round(x0) + 0.5, 0);
        ctx.lineTo(Math.round(x0) + 0.5, h);
        ctx.moveTo(Math.round(x1) + 0.5, 0);
        ctx.lineTo(Math.round(x1) + 0.5, h);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = COL.ambar;
        ctx.fillText("canal LoRa", Math.min(x1 + 4, w - 60), 12);
      }
      const n = this.suave.length;
      const linea = (datos, color, ancho) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = ancho;
        ctx.beginPath();
        for (let i = 0; i < n; i++) {
          const xx = (i / (n - 1)) * w, yy = y(datos[i]);
          i ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy);
        }
        ctx.stroke();
      };
      if (this.maxHold && this.retenido) linea(this.retenido, COL.retenido, 1);
      // relleno bajo la traza
      ctx.fillStyle = "rgba(143,211,232,0.10)";
      ctx.beginPath();
      ctx.moveTo(0, h);
      for (let i = 0; i < n; i++) ctx.lineTo((i / (n - 1)) * w, y(this.suave[i]));
      ctx.lineTo(w, h);
      ctx.fill();
      linea(this.suave, COL.traza, 1.25);
      ctx.fillStyle = COL.etiqueta;
      ctx.fillText("dBFS", 4, h - 5);
    }
  }
  S.Cascada = Cascada;

  /* ================================================================== series del enlace */

  const PASOS_T = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200].map((s) => s * 1000);

  class Grafica {
    constructor(canvas, tooltip) {
      this.cv = canvas;
      this.tip = tooltip;
      this.cfg = null;
      this.hover = null;
      this.puntosPx = [];
      this.onElegir = null;
      new ResizeObserver(() => this.dibujar()).observe(canvas);
      canvas.addEventListener("mousemove", (e) => this._mover(e));
      canvas.addEventListener("mouseleave", () => {
        this.hover = null;
        this.tip.hidden = true;
        this.dibujar();
      });
      canvas.addEventListener("click", () => {
        if (this.hover && this.onElegir) this.onElegir(this.hover.ref);
      });
    }

    /**
     * cfg = { tipo: 'tiempo'|'log', puntos: [{x, y, color, ref}], unidad, refs: [{y, texto}],
     *         ajuste: {a, b} (y = a + b·log10 x), vacio: 'texto', tip: (p) => html, yMin, yMax }
     */
    set(cfg) {
      this.cfg = cfg;
      this.dibujar();
    }

    _mover(e) {
      const r = this.cv.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      let mejor = null, dmin = 26 * 26;
      for (const p of this.puntosPx) {
        const d = (p.px - mx) ** 2 + (p.py - my) ** 2 * 0.35;
        if (d < dmin) {
          dmin = d;
          mejor = p;
        }
      }
      if (mejor !== this.hover) {
        this.hover = mejor;
        this.dibujar();
      }
      if (mejor && this.cfg.tip) {
        this.tip.innerHTML = this.cfg.tip(mejor);
        this.tip.hidden = false;
        const tw = this.tip.offsetWidth;
        let x = mejor.px + 12;
        if (x + tw > r.width - 4) x = mejor.px - tw - 12;
        this.tip.style.left = `${x}px`;
        this.tip.style.top = `${Math.max(4, mejor.py - 40)}px`;
      } else {
        this.tip.hidden = true;
      }
    }

    dibujar() {
      const { ctx, w, h } = ajustarCanvas(this.cv);
      leerColoresCSS();
      ctx.fillStyle = COL.fondo;
      ctx.fillRect(0, 0, w, h);
      this.puntosPx = [];
      const cfg = this.cfg;
      ctx.font = "11.5px Barlow, sans-serif";
      if (!cfg || !cfg.puntos.length) {
        ctx.fillStyle = COL.etiqueta;
        ctx.font = "14px Barlow, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText((cfg && cfg.vacio) || "Sin datos todavía", w / 2, h / 2);
        ctx.textAlign = "left";
        return;
      }
      const pts = cfg.puntos;
      const izq = 44, der = 12, arr = 10, aba = 24;
      const W = w - izq - der, H = h - arr - aba;

      // rango X
      const tx = cfg.tipo === "log" ? (v) => Math.log10(v) : (v) => v;
      let x0 = Math.min(...pts.map((p) => tx(p.x))), x1 = Math.max(...pts.map((p) => tx(p.x)));
      if (cfg.tipo === "tiempo") {
        if (x1 - x0 < 60000) x0 = x1 - 60000;
        const m = (x1 - x0) * 0.02;
        x1 += m;
      } else {
        x0 = Math.floor(x0 * 4) / 4 - 0.05;
        x1 = Math.ceil(x1 * 4) / 4 + 0.05;
        if (x1 - x0 < 0.5) x1 = x0 + 0.5;
      }
      // rango Y (incluye líneas de referencia cercanas)
      let ys = pts.map((p) => p.y);
      (cfg.refs || []).forEach((r) => ys.push(r.y));
      let y0 = Math.min(...ys), y1 = Math.max(...ys);
      if (cfg.yMin !== undefined) y0 = Math.min(y0, cfg.yMin);
      if (cfg.yMax !== undefined) y1 = Math.max(y1, cfg.yMax);
      const margen = Math.max((y1 - y0) * 0.12, cfg.margenMin || 1);
      y0 -= margen;
      y1 += margen;
      const X = (v) => izq + ((tx(v) - x0) / (x1 - x0)) * W;
      const Xr = (u) => izq + ((u - x0) / (x1 - x0)) * W; // u ya transformado
      const Y = (v) => arr + (1 - (v - y0) / (y1 - y0)) * H;

      // retícula Y
      const my = marcas(y0, y1, 5);
      ctx.lineWidth = 1;
      ctx.textAlign = "right";
      for (const v of my.valores) {
        const yy = Math.round(Y(v)) + 0.5;
        ctx.strokeStyle = COL.reticula;
        ctx.beginPath();
        ctx.moveTo(izq, yy);
        ctx.lineTo(w - der, yy);
        ctx.stroke();
        ctx.fillStyle = COL.etiqueta;
        ctx.fillText(S.util.num(v, my.paso % 1 ? 1 : 0), izq - 6, yy + 4);
      }
      ctx.textAlign = "center";
      // retícula X
      if (cfg.tipo === "tiempo") {
        const span = x1 - x0;
        const paso = PASOS_T.find((p) => span / p <= 6) || 7200000;
        const desfase = new Date().getTimezoneOffset() * 60000;
        for (let t = Math.ceil((x0 - desfase) / paso) * paso + desfase; t <= x1; t += paso) {
          const xx = Math.round(Xr(t)) + 0.5;
          ctx.strokeStyle = COL.reticulaFina;
          ctx.beginPath();
          ctx.moveTo(xx, arr);
          ctx.lineTo(xx, arr + H);
          ctx.stroke();
          ctx.fillStyle = COL.etiqueta;
          const d = new Date(t);
          const p2 = (n) => String(n).padStart(2, "0");
          const txt = paso >= 60000 ? `${p2(d.getHours())}:${p2(d.getMinutes())}` : `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
          ctx.fillText(txt, xx, h - 7);
        }
      } else {
        for (let e = Math.ceil(x0); e <= Math.floor(x1); e++) {
          for (const m of [1, 2, 5]) {
            const u = e + Math.log10(m);
            if (u < x0 || u > x1) continue;
            const xx = Math.round(Xr(u)) + 0.5;
            ctx.strokeStyle = m === 1 ? COL.reticula : COL.reticulaFina;
            ctx.beginPath();
            ctx.moveTo(xx, arr);
            ctx.lineTo(xx, arr + H);
            ctx.stroke();
            ctx.fillStyle = COL.etiqueta;
            const metros = Math.pow(10, u);
            ctx.fillText(metros >= 1000 ? `${S.util.num(metros / 1000, metros >= 10000 ? 0 : 0)} km` : `${Math.round(metros)} m`, xx, h - 7);
          }
        }
      }

      // líneas de referencia
      for (const r of cfg.refs || []) {
        const yy = Math.round(Y(r.y)) + 0.5;
        ctx.strokeStyle = r.color || COL.error;
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(izq, yy);
        ctx.lineTo(w - der, yy);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = r.color || COL.error;
        ctx.textAlign = "right";
        ctx.fillText(r.texto, w - der - 4, yy - 5);
        ctx.textAlign = "center";
      }

      // ajuste log-distancia
      if (cfg.ajuste) {
        const { a, b } = cfg.ajuste;
        ctx.strokeStyle = COL.ambar;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.moveTo(Xr(x0), Y(a + b * x0));
        ctx.lineTo(Xr(x1), Y(a + b * x1));
        ctx.stroke();
        ctx.lineWidth = 1;
      }

      ctx.save();
      ctx.beginPath();
      ctx.rect(izq, arr - 4, W, H + 8);
      ctx.clip();
      // línea de la serie (solo en tiempo)
      if (cfg.tipo === "tiempo") {
        ctx.strokeStyle = "rgba(143,211,232,0.55)";
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        let prev = null;
        for (const p of pts) {
          const xx = X(p.x), yy = Y(p.y);
          // cortar la línea si hay un hueco de más de 3 intervalos típicos
          if (prev && p.x - prev.x > (cfg.hueco || 30000)) ctx.moveTo(xx, yy);
          else prev ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy);
          prev = p;
        }
        ctx.stroke();
      }
      // puntos
      const r = pts.length > 300 ? 2 : 3;
      for (const p of pts) {
        const xx = X(p.x), yy = Y(p.y);
        this.puntosPx.push({ ...p, px: xx, py: yy });
        ctx.fillStyle = p.color || COL.traza;
        ctx.beginPath();
        ctx.arc(xx, yy, r, 0, Math.PI * 2);
        ctx.fill();
      }
      if (this.hover) {
        const p = this.puntosPx.find((q) => q.ref === this.hover.ref) || this.hover;
        ctx.strokeStyle = "rgba(230,239,244,0.35)";
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(p.px, arr);
        ctx.lineTo(p.px, arr + H);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(p.px, p.py, 6, 0, Math.PI * 2);
        ctx.stroke();
        ctx.lineWidth = 1;
      }
      ctx.restore();

      ctx.fillStyle = COL.etiqueta;
      ctx.textAlign = "left";
      ctx.fillText(cfg.unidad || "", 4, arr + 2 > 10 ? arr + 2 : 10);
    }
  }
  S.Grafica = Grafica;
})(window.SDR);
