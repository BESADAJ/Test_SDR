/* Aplicación principal: conexión SSE con servidor_gps.py, estado y vista de rastreo LoRa. */
(function (S) {
  "use strict";
  const U = S.util;
  const { $, $$ } = U;

  const MAX_FILAS_TABLA = 500;
  const CLAVE_BASE = "sdr.base";

  const E = {
    paquetes: [],
    fixes: [],
    estado: {},
    stats: null,
    base: null,
    baseDelUsuario: false,
    seleccion: null,
    eventos: [],
    cargandoHistorial: false,
    fuente: null,
    demo: null,
    graf: "snr",
    conectado: false,
    alguna: false,
  };
  S.estado = E;

  /* ================================================================== componentes */
  const cascada = new S.Cascada($("#cv-espectro"), $("#cv-cascada"), $("#casc-ejes"), $("#casc-rango"));
  const grafica = new S.Grafica($("#cv-grafica"), $("#tooltip"));
  grafica.onElegir = (p) => {
    seleccionar(p);
    mapa.resaltar(p);
  };
  const mapa = new S.Mapa("map", {
    onElegir: (p) => seleccionar(p),
    onSeguirCambia: (v) => $("#btn-seguir").setAttribute("aria-pressed", String(v)),
    onBaseFijada: (b) => {
      E.base = b;
      E.baseDelUsuario = true;
      try { localStorage.setItem(CLAVE_BASE, JSON.stringify(b)); } catch (_) { /* almacenamiento bloqueado */ }
      $("#btn-base").setAttribute("aria-pressed", "false");
      $("#mapa-ayuda").hidden = true;
      refrescarTodo();
    },
  });

  try {
    const b = JSON.parse(localStorage.getItem(CLAVE_BASE) || "null");
    if (b && Number.isFinite(b.lat)) {
      E.base = b;
      E.baseDelUsuario = true;
      mapa.setBase(b.lat, b.lon, b.nombre);
    }
  } catch (_) { /* sin base guardada */ }

  /* ================================================================== manejo de eventos */
  function manejar(tipo, d, silencioso = false) {
    switch (tipo) {
      case "historial_inicio":
        limpiarLocal();
        E.cargandoHistorial = true;
        break;
      case "historial_fin":
        E.cargandoHistorial = false;
        refrescarTodo();
        break;
      case "estado":
        onEstado(d);
        break;
      case "paquete":
        onPaquete(d, silencioso || E.cargandoHistorial);
        break;
      case "espectro":
        cascada.agregarFila(d.fila);
        break;
      case "stats":
        E.stats = d;
        if (!E.cargandoHistorial) pintarEnlace();
        pintarADC();
        break;
      case "aviso":
        agregarEvento(d);
        break;
      case "fm_log":
        S.fm && S.fm.onLog(d);
        break;
      case "fm_listo":
        S.fm && S.fm.onListo(d);
        break;
    }
  }

  function onEstado(d) {
    E.estado = d || {};
    const chip = $("#chip-modo");
    const nombres = { vivo: "En vivo", archivo: "Grabación", simulacion: "Simulación", demo: "Demostración" };
    chip.dataset.modo = d.modo || "";
    chip.textContent = d.detectando ? "Detectando SF/BW…" : nombres[d.modo] || "—";
    const partes = [U.mhz(d.freq_hz)];
    if (d.sf) partes.push(`SF${d.sf}`);
    if (d.bw) partes.push(`BW ${U.khz(d.bw)}`);
    $("#receptor-linea").innerHTML = partes.map((p) => `<span>${U.esc(p)}</span>`).join("");

    $("#rx-fuente").textContent = d.fuente || "—";
    $("#rx-freq").textContent = U.mhz(d.freq_hz);
    $("#rx-centro").textContent = U.mhz(d.centro_hz);
    $("#rx-fs").textContent = Number.isFinite(d.fs) ? `${U.num(d.fs / 1e6, 3)} MS/s` : "—";
    $("#rx-sf").textContent = d.sf ? `SF${d.sf}` : d.detectando ? "detectando" : "—";
    $("#rx-bw").textContent = d.bw ? U.khz(d.bw) : "—";
    $("#rx-sim").textContent = Number.isFinite(d.simbolo_ms) ? `${U.num(d.simbolo_ms, 3)} ms` : "—";
    $("#rx-ldro").textContent = d.ldro === null || d.ldro === undefined ? "—" : d.ldro ? "activado" : "apagado";
    $("#rx-gan").textContent = d.ganancia === undefined ? "—" : String(d.ganancia).toLowerCase() === "auto" ? "automática" : `${d.ganancia} dB`;
    $("#rx-ppm").textContent = Number.isFinite(d.ppm) ? `${d.ppm} ppm` : "—";

    const lim = U.LIMITE_SNR[d.sf];
    $("#umbral-snr").style.left = Number.isFinite(lim) ? `${pctSNR(lim)}%` : "0";
    $("#umbral-snr").hidden = !Number.isFinite(lim);

    cascada.setRango(d.centro_hz, d.fs, d.freq_hz, d.bw);
    if (d.base && !E.baseDelUsuario) {
      E.base = { ...d.base };
      mapa.setBase(d.base.lat, d.base.lon, d.base.nombre);
    }
    if (!E.cargandoHistorial) refrescarTodo();
  }

  function onPaquete(p, silencioso) {
    E.paquetes.push(p);
    const fix = Number.isFinite(p.lat) && Number.isFinite(p.lon);
    if (fix) E.fixes.push(p);
    if (fix) mapa.agregar(p, !silencioso);
    $("#mapa-vacio").hidden = E.fixes.length > 0;
    if (silencioso) return;
    cascada.marcarPaquete();
    const lamp = $("#lamp-senal");
    lamp.classList.remove("destello");
    void lamp.offsetWidth;
    lamp.classList.add("destello");
    agregarFila(p, true);
    if (!E.seleccion) pintarInspector(p);
    pintarPosicion();
    pintarEnlace();
    pedirGrafica();
    tic();
  }

  function limpiarLocal() {
    E.paquetes = [];
    E.fixes = [];
    E.seleccion = null;
    E.stats = null;
    mapa.limpiar();
    $("#tbody-pkt").innerHTML = "";
    $("#mapa-vacio").hidden = false;
    pintarInspector(null);
  }

  function refrescarTodo() {
    pintarTabla();
    pintarPosicion();
    pintarEnlace();
    pintarADC();
    pintarInspector(E.seleccion || E.paquetes[E.paquetes.length - 1] || null);
    pedirGrafica();
    tic();
  }

  /* ================================================================== posición */
  function distanciaBase(p) {
    return E.base && Number.isFinite(p.lat) ? U.haversine([E.base.lat, E.base.lon], [p.lat, p.lon]) : NaN;
  }

  function pintarPosicion() {
    const f = E.fixes, ult = f[f.length - 1];
    $("#pos-lat").textContent = ult ? U.coord(ult.lat) : "—";
    $("#pos-lon").textContent = ult ? U.coord(ult.lon) : "—";
    $("#btn-copiar").disabled = !ult;
    const gm = $("#lnk-gmaps");
    if (ult) {
      gm.href = `https://www.google.com/maps?q=${ult.lat},${ult.lon}`;
      gm.setAttribute("aria-disabled", "false");
    } else {
      gm.removeAttribute("href");
      gm.setAttribute("aria-disabled", "true");
    }
    if (!ult) {
      ["#pos-dist", "#pos-rumbo", "#pos-vel", "#pos-recorrido"].forEach((s) => ($(s).textContent = "—"));
      return;
    }
    const d = distanciaBase(ult);
    $("#pos-dist").textContent = Number.isFinite(d) ? U.distancia(d) : "ubica la base";
    if (E.base) {
      const r = U.rumbo([E.base.lat, E.base.lon], [ult.lat, ult.lon]);
      $("#pos-rumbo").textContent = `${Math.round(r)}° ${U.cardinal(r)}`;
    } else $("#pos-rumbo").textContent = "—";
    // velocidad entre los dos últimos fixes y distancia recorrida
    let total = 0;
    for (let i = 1; i < f.length; i++) {
      if (f[i].t - f[i - 1].t < 300000) total += U.haversine([f[i - 1].lat, f[i - 1].lon], [f[i].lat, f[i].lon]);
    }
    $("#pos-recorrido").textContent = f.length > 1 ? U.distancia(total) : "—";
    if (f.length > 1) {
      const a = f[f.length - 2], dt = (ult.t - a.t) / 1000;
      const v = dt > 0 && dt < 120 ? (U.haversine([a.lat, a.lon], [ult.lat, ult.lon]) / dt) * 3.6 : NaN;
      $("#pos-vel").textContent = Number.isFinite(v) ? `${U.num(v, v < 10 ? 1 : 0)} km/h` : "—";
    } else $("#pos-vel").textContent = "—";
  }

  /* ================================================================== enlace */
  const pctSNR = (v) => Math.max(0, Math.min(100, ((v + 20) / 40) * 100));

  function pintarEnlace() {
    const P = E.paquetes, ult = P[P.length - 1];
    $("#enlace-n").textContent = `${P.length} ${P.length === 1 ? "paquete" : "paquetes"}`;
    if (!ult) return;
    const ultimos = P.slice(-10);
    const sf = E.estado.sf;
    const lim = U.LIMITE_SNR[sf];

    // SNR
    $("#v-snr").textContent = U.num(ult.snr_db, 1, true);
    $("#m-snr").style.width = `${pctSNR(ult.snr_db)}%`;
    $("#m-snr").style.background = U.colorSNR(ult.snr_db);
    const prom = U.media(ultimos.map((p) => p.snr_db));
    const margen = Number.isFinite(lim) ? ult.snr_db - lim : NaN;
    $("#v-snr-pie").textContent = Number.isFinite(margen)
      ? `margen ${U.num(margen, 1)} dB sobre SF${sf}`
      : `promedio ${U.num(prom, 1, true)} dB`;
    $("#pan-snr").dataset.calidad = !Number.isFinite(margen) ? "" : margen > 10 ? "buena" : margen > 3 ? "media" : "mala";

    // potencia
    $("#v-pot").textContent = U.num(ult.pot_dbfs, 1);
    $("#m-pot").style.width = `${Math.max(0, Math.min(100, ((ult.pot_dbfs + 90) / 90) * 100))}%`;
    const pots = P.slice(-20).map((p) => p.pot_dbfs);
    $("#v-pot-pie").textContent = `rango ${U.num(Math.min(...pots), 1)} a ${U.num(Math.max(...pots), 1)}`;

    // offset
    $("#v-cfo").textContent = U.num(ult.cfo_hz, 0, true);
    const cfos = P.slice(-20).map((p) => p.cfo_hz);
    const m = U.media(cfos);
    const de = Math.sqrt(U.media(cfos.map((c) => (c - m) ** 2)));
    $("#v-cfo-pie").textContent = `${U.num(ult.cfo_ppm, 2, true)} ppm, variación ±${U.num(de, 0)} Hz`;

    // CRC
    const nTot = E.stats ? E.stats.n_tot : P.length;
    const nOk = E.stats ? E.stats.n_ok : P.filter((p) => p.crc_ok).length;
    const pct = nTot ? (100 * nOk) / nTot : NaN;
    $("#v-crc").textContent = U.num(pct, pct === 100 ? 0 : 1);
    $("#m-crc").style.width = `${pct || 0}%`;
    $("#v-crc-pie").textContent = `${nOk} de ${nTot} paquetes`;
    $("#pan-crc").dataset.calidad = !nTot ? "" : pct >= 95 ? "buena" : pct >= 80 ? "media" : "mala";

    // tabla de datos
    const intervalos = [];
    const rec = P.slice(-21);
    for (let i = 1; i < rec.length; i++) intervalos.push((rec[i].t - rec[i - 1].t) / 1000);
    const intervalo = U.media(intervalos);
    $("#v-intervalo").textContent = Number.isFinite(intervalo) ? `${U.num(intervalo, 1)} s` : "—";
    $("#v-bits").textContent = `${ult.bits_corregidos} (prom. ${U.num(U.media(ultimos.map((p) => p.bits_corregidos)), 1)})`;
    $("#v-headers").textContent = E.stats ? String(E.stats.headers_malos) : "—";
    $("#v-toa").textContent = `${U.num(ult.duracion_ms, 1)} ms`;
    $("#v-duty").textContent = Number.isFinite(intervalo) && intervalo > 0 ? `${U.num((ult.duracion_ms / 10 / intervalo), 2)} %` : "—";
    $("#v-cr").textContent = ult.cr || "—";
    const sw = U.SYNC[ult.sync_word];
    $("#v-sync").innerHTML = `${U.esc(ult.sync_word)}${sw ? ` <small>${sw}</small>` : ""}`;
    pintarRitmo();
  }

  function pintarRitmo() {
    const ahora = E.demo || E.estado.modo !== "archivo" ? Date.now() : (E.paquetes[E.paquetes.length - 1] || {}).t;
    const n = E.paquetes.filter((p) => ahora - p.t <= 60000).length;
    $("#v-ppm").textContent = E.paquetes.length ? String(n) : "—";
  }

  function pintarADC() {
    const s = E.stats;
    const i = $("#m-adc");
    if (!s || !Number.isFinite(s.nivel_adc)) {
      $("#v-adc").textContent = "—";
      i.style.width = "0";
      return;
    }
    i.style.width = `${Math.min(100, s.nivel_adc * 100)}%`;
    i.classList.toggle("saturado", s.nivel_adc > 0.97);
    $("#v-adc").textContent = s.nivel_adc > 0.97 ? "saturando" : `${U.num(s.nivel_adc * 100, 0)} %`;
  }

  /* ================================================================== inspector */
  function hexdump(p) {
    const bytes = (p.hex || "").split(" ").filter(Boolean).map((h) => parseInt(h, 16));
    const filas = [];
    for (let i = 0; i < bytes.length; i += 8) {
      const trozo = bytes.slice(i, i + 8);
      const hx = trozo.map((b) => b.toString(16).padStart(2, "0")).join(" ").padEnd(23, " ");
      const asc = trozo.map((b) => (b >= 32 && b < 127 ? String.fromCharCode(b) : "·")).join("");
      filas.push(`<span class="off">${i.toString(16).padStart(2, "0")}</span>  ${hx}  <span class="${p.crc_ok === false ? "mal" : "asc"}">${U.esc(asc)}</span>`);
    }
    return filas.join("\n") || '<span class="off">sin bytes</span>';
  }

  function pintarInspector(p) {
    const el = $("#inspector");
    const ult = E.paquetes[E.paquetes.length - 1];
    $("#btn-seguir-ultimo").hidden = !E.seleccion || E.seleccion === ult;
    if (!p) {
      el.innerHTML = '<p class="tenue">Aquí aparece el contenido del último paquete, o del que elijas en el mapa o en el registro.</p>';
      return;
    }
    const crc = p.crc_ok === true ? '<span class="etiqueta etiqueta-ok">CRC correcto</span>'
      : p.crc_ok === false ? '<span class="etiqueta etiqueta-mal">CRC incorrecto</span>'
      : '<span class="etiqueta etiqueta-neutra">sin CRC</span>';
    const pos = Number.isFinite(p.lat) ? '<span class="etiqueta etiqueta-neutra">con posición</span>' : "";
    // estructura de la trama en símbolos
    const pre = 12.25, hdr = 8, pay = Math.max(0, p.n_simbolos - 8);
    const tot = pre + hdr + pay;
    const ts = Number.isFinite(E.estado.simbolo_ms) ? E.estado.simbolo_ms : p.duracion_ms / tot;
    const seg = (n, color, txt) => `<span style="flex:${n};background:${color}" title="${txt}">${n / tot > 0.14 ? txt : ""}</span>`;
    const d = distanciaBase(p);
    el.innerHTML = `
      <div class="insp-cab"><strong>Paquete ${p.n}</strong><span class="tenue">${U.esc(p.hora)}</span>${crc}${pos}</div>
      <div class="insp-texto">${p.texto ? U.esc(p.texto) : '<span class="tenue">contenido binario</span>'}</div>
      <div class="trama" aria-label="Estructura de la trama">
        ${seg(pre, "#4F6B80", "Preámbulo")}${seg(hdr, "#24557F", "Header")}${seg(pay, "#C9861A", "Payload y CRC")}
      </div>
      <div class="trama-leyenda">${U.num(pre, 2)} símbolos de preámbulo, sync y SFD, ${hdr} de header y ${pay} de payload: ${U.num(p.duracion_ms, 2)} ms con símbolos de ${U.num(ts, 3)} ms</div>
      <div class="hexdump">${hexdump(p)}</div>
      <dl class="datos datos-3">
        <div><dt>Largo</dt><dd>${p.largo} B</dd></div>
        <div><dt>Coding rate</dt><dd>${U.esc(p.cr)}</dd></div>
        <div><dt>Bits corregidos</dt><dd>${p.bits_corregidos}</dd></div>
        <div><dt>SNR</dt><dd>${U.num(p.snr_db, 1, true)} dB</dd></div>
        <div><dt>Potencia</dt><dd>${U.num(p.pot_dbfs, 1)} dBFS</dd></div>
        <div><dt>Offset</dt><dd>${U.num(p.cfo_hz, 0, true)} Hz</dd></div>
        <div><dt>Símbolos</dt><dd>${p.n_simbolos}</dd></div>
        <div><dt>Sync word</dt><dd>${U.esc(p.sync_word)}</dd></div>
        <div><dt>Distancia</dt><dd>${Number.isFinite(d) ? U.distancia(d) : "—"}</dd></div>
      </dl>`;
  }

  function seleccionar(p) {
    E.seleccion = p;
    pintarInspector(p);
    $$("#tbody-pkt tr").forEach((tr) => tr.setAttribute("aria-selected", String(Number(tr.dataset.n) === p.n)));
  }

  /* ================================================================== tabla */
  function filtrar(p) {
    if ($("#chk-solo-ok").checked && !p.crc_ok) return false;
    const q = $("#filtro").value.trim().toLowerCase();
    if (q && !((p.texto || "").toLowerCase().includes(q) || (p.hex || "").includes(q))) return false;
    return true;
  }

  function filaHTML(p) {
    const d = distanciaBase(p);
    const crc = p.crc_ok === true ? "correcto" : p.crc_ok === false ? "incorrecto" : "—";
    return `<td>${p.n}</td><td>${U.esc(p.hora)}</td>
      <td class="contenido" title="${U.esc(p.texto || p.hex)}">${U.esc(p.texto ?? p.hex)}</td>
      <td>${crc}</td>
      <td class="num"><span class="punto-snr" style="background:${U.colorSNR(p.snr_db)}"></span>${U.num(p.snr_db, 1, true)}</td>
      <td class="num">${U.num(p.pot_dbfs, 1)}</td><td class="num">${U.num(p.cfo_hz, 0, true)}</td>
      <td class="num">${p.bits_corregidos}</td><td class="num">${Number.isFinite(d) ? U.distancia(d) : "—"}</td>
      <td class="num">${U.num(p.duracion_ms, 1)}</td>`;
  }

  function crearFila(p) {
    const tr = document.createElement("tr");
    tr.dataset.n = p.n;
    tr.tabIndex = 0;
    if (p.crc_ok === false) tr.classList.add("error");
    if (E.seleccion === p) tr.setAttribute("aria-selected", "true");
    tr.innerHTML = filaHTML(p);
    return tr;
  }

  function agregarFila(p, nueva) {
    $("#cuenta-pkt").textContent = E.paquetes.length;
    $("#tabla-vacia").hidden = E.paquetes.length > 0;
    if (!filtrar(p)) return;
    const tb = $("#tbody-pkt");
    const tr = crearFila(p);
    if (nueva && !U.reducidoMovimiento()) tr.classList.add("nueva");
    tb.prepend(tr);
    while (tb.children.length > MAX_FILAS_TABLA) tb.lastElementChild.remove();
  }

  function pintarTabla() {
    const tb = $("#tbody-pkt");
    const frag = document.createDocumentFragment();
    let k = 0;
    for (let i = E.paquetes.length - 1; i >= 0 && k < MAX_FILAS_TABLA; i--) {
      if (!filtrar(E.paquetes[i])) continue;
      frag.appendChild(crearFila(E.paquetes[i]));
      k++;
    }
    tb.replaceChildren(frag);
    $("#cuenta-pkt").textContent = E.paquetes.length;
    $("#tabla-vacia").hidden = E.paquetes.length > 0;
  }

  const porNumero = (n) => E.paquetes.find((p) => p.n === n);
  $("#tbody-pkt").addEventListener("click", (e) => {
    const tr = e.target.closest("tr");
    const p = tr && porNumero(Number(tr.dataset.n));
    if (!p) return;
    seleccionar(p);
    if (Number.isFinite(p.lat)) mapa.resaltar(p);
  });
  $("#tbody-pkt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      e.target.closest("tr")?.click();
    }
  });
  $("#filtro").addEventListener("input", pintarTabla);
  $("#chk-solo-ok").addEventListener("change", pintarTabla);

  /* ================================================================== eventos */
  function agregarEvento(d) {
    E.eventos.unshift(d);
    if (E.eventos.length > 300) E.eventos.pop();
    const li = document.createElement("li");
    li.dataset.nivel = d.nivel || "info";
    li.innerHTML = `<time>${U.horaCorta(d.t || Date.now())}</time><span>${U.esc(d.texto)}</span>`;
    $("#reg-eventos").prepend(li);
    while ($("#reg-eventos").children.length > 300) $("#reg-eventos").lastElementChild.remove();
    $("#cuenta-ev").textContent = E.eventos.length;
  }

  /* ================================================================== gráficas */
  let graficaPendiente = false;
  function pedirGrafica() {
    if (graficaPendiente) return;
    graficaPendiente = true;
    requestAnimationFrame(() => {
      graficaPendiente = false;
      pintarGrafica();
    });
  }

  function pintarGrafica() {
    const P = E.paquetes.slice(-600);
    const nota = $("#grafica-nota");
    const sf = E.estado.sf, lim = U.LIMITE_SNR[sf];
    const tipTiempo = (unidad, dec, signo) => (q) =>
      `<b>Paquete ${q.ref.n}</b> ${U.esc(q.ref.hora)}<br>${U.num(q.y, dec, signo)} ${unidad}${q.ref.crc_ok === false ? "<br>CRC incorrecto" : ""}`;
    const hueco = Math.max(15000, 4 * U.media(P.slice(1).map((p, i) => p.t - P[i].t)) || 15000);

    if (E.graf === "snr") {
      grafica.set({
        tipo: "tiempo", unidad: "dB", hueco, margenMin: 2,
        puntos: P.map((p) => ({ x: p.t, y: p.snr_db, color: p.crc_ok === false ? S.COLORES_PANTALLA.error : U.colorSNR(p.snr_db), ref: p })),
        refs: Number.isFinite(lim) ? [{ y: lim, texto: `Límite de SF${sf}: ${U.num(lim, 1)} dB` }] : [],
        tip: tipTiempo("dB", 1, true), vacio: "La SNR de cada paquete aparecerá aquí",
      });
      const m = U.media(P.map((p) => p.snr_db));
      nota.innerHTML = P.length
        ? `SNR media <b>${U.num(m, 1, true)} dB</b>${Number.isFinite(lim) ? `, margen medio de <b>${U.num(m - lim, 1)} dB</b> sobre el límite de demodulación` : ""}. Los puntos rojos llegaron con CRC incorrecto.`
        : "";
    } else if (E.graf === "pot") {
      grafica.set({
        tipo: "tiempo", unidad: "dBFS", hueco, margenMin: 2,
        puntos: P.map((p) => ({ x: p.t, y: p.pot_dbfs, color: p.crc_ok === false ? S.COLORES_PANTALLA.error : S.COLORES_PANTALLA.traza, ref: p })),
        tip: tipTiempo("dBFS", 1), vacio: "La potencia de cada paquete aparecerá aquí",
      });
      nota.innerHTML = P.length
        ? `Potencia del paquete a la entrada del ADC, relativa a escala completa. Depende de la ganancia del dongle (${U.esc(E.estado.ganancia ?? "—")} dB).`
        : "";
    } else if (E.graf === "cfo") {
      grafica.set({
        tipo: "tiempo", unidad: "Hz", hueco, margenMin: 20,
        puntos: P.map((p) => ({ x: p.t, y: p.cfo_hz, color: p.crc_ok === false ? S.COLORES_PANTALLA.error : S.COLORES_PANTALLA.traza, ref: p })),
        tip: (q) => `<b>Paquete ${q.ref.n}</b> ${U.esc(q.ref.hora)}<br>${U.num(q.y, 0, true)} Hz (${U.num(q.ref.cfo_ppm, 2, true)} ppm)`,
        vacio: "El offset de frecuencia de cada paquete aparecerá aquí",
      });
      const m = U.media(P.map((p) => p.cfo_hz));
      const ppm = U.media(P.map((p) => p.cfo_ppm).filter(Number.isFinite));
      nota.innerHTML = P.length
        ? `Offset medio <b>${U.num(m, 0, true)} Hz</b> (${U.num(ppm, 2, true)} ppm): diferencia entre el cristal de la TTGO y el del dongle, ya corregido con <code>--ppm ${U.esc(E.estado.ppm ?? 49)}</code>.`
        : "";
    } else {
      const pts = E.base
        ? E.paquetes.filter((p) => Number.isFinite(p.lat)).map((p) => ({ x: Math.max(1, distanciaBase(p)), y: p.snr_db, color: U.colorSNR(p.snr_db), ref: p }))
        : [];
      const ajuste = pts.length >= 5 ? U.regresion(pts.map((q) => Math.log10(q.x)), pts.map((q) => q.y)) : null;
      grafica.set({
        tipo: "log", unidad: "SNR dB", margenMin: 2, puntos: pts,
        refs: Number.isFinite(lim) ? [{ y: lim, texto: `Límite de SF${sf}` }] : [],
        ajuste: ajuste && ajuste.b < 0 ? ajuste : null,
        tip: (q) => `<b>Paquete ${q.ref.n}</b><br>${U.distancia(q.x)} de la base<br>SNR ${U.num(q.y, 1, true)} dB`,
        vacio: E.base ? "Hacen falta posiciones válidas" : "Ubica la estación base en el mapa para ver esta gráfica",
      });
      if (ajuste && ajuste.b < 0) {
        const n = -ajuste.b / 10;
        const alcance = Number.isFinite(lim) ? Math.pow(10, (lim - ajuste.a) / ajuste.b) : NaN;
        nota.innerHTML = `Ajuste log-distancia: exponente de pérdidas <b>n ≈ ${U.num(n, 2)}</b> (R² ${U.num(ajuste.r2, 2)}; en espacio libre n = 2)` +
          (Number.isFinite(alcance) ? `. Alcance estimado con SF${sf}: <b>${U.distancia(alcance)}</b>.` : ".");
      } else nota.innerHTML = E.base ? "Con 5 posiciones o más se ajusta el modelo de pérdidas log-distancia." : "";
    }
  }

  $$(".zona-senal [data-graf]").forEach((b) =>
    b.addEventListener("click", () => {
      E.graf = b.dataset.graf;
      $$(".zona-senal [data-graf]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      pintarGrafica();
    })
  );
  $("#chk-maxhold").addEventListener("change", (e) => {
    cascada.maxHold = e.target.checked;
    cascada.dibujarEspectro();
  });

  /* ================================================================== reloj (cada segundo) */
  function tic() {
    const ult = E.paquetes[E.paquetes.length - 1];
    const lamp = $("#lamp-senal");
    const enArchivo = E.estado.modo === "archivo" && !E.demo;
    if (!ult) {
      lamp.dataset.estado = "espera";
      $("#txt-senal").textContent = E.estado.detectando ? "Buscando preámbulo" : "Sin paquetes";
    } else {
      const edad = Date.now() - ult.t;
      lamp.dataset.estado = enArchivo || edad < 15000 ? "ok" : edad < 60000 ? "alerta" : "mal";
      $("#txt-senal").textContent = `Último paquete ${U.hace(edad)}`;
    }
    const uf = E.fixes[E.fixes.length - 1];
    $("#pos-edad").textContent = uf ? `fix ${U.hace(Date.now() - uf.t)}` : "—";
    pintarRitmo();
  }
  setInterval(tic, 1000);

  /* ================================================================== herramientas del mapa */
  $("#btn-seguir").addEventListener("click", (e) => {
    const v = e.currentTarget.getAttribute("aria-pressed") !== "true";
    e.currentTarget.setAttribute("aria-pressed", String(v));
    mapa.setSeguir(v);
  });
  $("#btn-ajustar").addEventListener("click", () => {
    mapa.setSeguir(false);
    $("#btn-seguir").setAttribute("aria-pressed", "false");
    mapa.ajustar();
  });
  $("#btn-base").addEventListener("click", (e) => {
    const activo = e.currentTarget.getAttribute("aria-pressed") !== "true";
    e.currentTarget.setAttribute("aria-pressed", String(activo));
    mapa.modoBase(activo);
    $("#mapa-ayuda").hidden = !activo;
  });
  $("#btn-base-cancelar").addEventListener("click", () => {
    mapa.modoBase(false);
    $("#btn-base").setAttribute("aria-pressed", "false");
    $("#mapa-ayuda").hidden = true;
  });
  $("#btn-geo").addEventListener("click", () => {
    if (!navigator.geolocation) return agregarEvento({ t: Date.now(), nivel: "aviso", texto: "Este navegador no da la ubicación." });
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        mapa.modoBase(false);
        mapa.setBase(pos.coords.latitude, pos.coords.longitude, "Estación base");
        mapa.onBaseFijada(mapa.base);
      },
      (err) => agregarEvento({ t: Date.now(), nivel: "aviso", texto: `No se pudo obtener la ubicación: ${err.message}` }),
      { enableHighAccuracy: true, timeout: 10000 }
    );
  });
  $("#btn-borrar").addEventListener("click", () => {
    if (E.paquetes.length && !confirm("¿Borrar el recorrido y los paquetes recibidos? El CSV del servidor se conserva.")) return;
    limpiarLocal();
    if (E.conectado) fetch("api/limpiar", { method: "POST" }).catch(() => {});
    refrescarTodo();
  });
  $("#btn-copiar").addEventListener("click", async () => {
    const u = E.fixes[E.fixes.length - 1];
    if (!u) return;
    const txt = `${u.lat.toFixed(7)},${u.lon.toFixed(7)}`;
    try {
      await navigator.clipboard.writeText(txt);
      $("#btn-copiar").textContent = "Copiadas";
    } catch (_) {
      $("#btn-copiar").textContent = txt;
    }
    setTimeout(() => ($("#btn-copiar").textContent = "Copiar coordenadas"), 1800);
  });
  $("#btn-seguir-ultimo").addEventListener("click", () => {
    E.seleccion = null;
    $$("#tbody-pkt tr[aria-selected]").forEach((tr) => tr.removeAttribute("aria-selected"));
    pintarInspector(E.paquetes[E.paquetes.length - 1] || null);
  });

  /* ================================================================== registro: pestañas y exportación */
  $$(".zona-registro [data-reg]").forEach((b) =>
    b.addEventListener("click", () => {
      $$(".zona-registro [data-reg]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      $("#reg-paquetes").hidden = b.dataset.reg !== "paquetes";
      $("#reg-eventos").hidden = b.dataset.reg !== "eventos";
    })
  );

  const CAMPOS = ["n", "hora", "lat", "lon", "texto", "hex", "crc_ok", "largo", "cr", "n_simbolos", "duracion_ms",
    "bits_corregidos", "snr_db", "pot_dbfs", "cfo_hz", "cfo_ppm", "sync_word"];
  function exportar(tipo) {
    const P = E.paquetes;
    if (!P.length) return agregarEvento({ t: Date.now(), nivel: "aviso", texto: "No hay paquetes para exportar." });
    const nombre = `recorrido_lora_${U.sello()}`;
    if (tipo === "csv") {
      const celda = (v) => (v === null || v === undefined ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
      const filas = [CAMPOS.join(","), ...P.map((p) => CAMPOS.map((c) => celda(p[c])).join(","))];
      U.descargar(`${nombre}.csv`, "﻿" + filas.join("\r\n"), "text/csv;charset=utf-8");
    } else if (tipo === "geojson") {
      const F = E.fixes;
      const props = (p) => Object.fromEntries(CAMPOS.filter((c) => !["lat", "lon", "hex"].includes(c)).map((c) => [c, p[c]]));
      const gj = {
        type: "FeatureCollection",
        features: [
          ...(F.length > 1 ? [{ type: "Feature", properties: { nombre: "Recorrido" }, geometry: { type: "LineString", coordinates: F.map((p) => [p.lon, p.lat]) } }] : []),
          ...F.map((p) => ({ type: "Feature", properties: props(p), geometry: { type: "Point", coordinates: [p.lon, p.lat] } })),
          ...(E.base ? [{ type: "Feature", properties: { nombre: E.base.nombre || "Estación base" }, geometry: { type: "Point", coordinates: [E.base.lon, E.base.lat] } }] : []),
        ],
      };
      U.descargar(`${nombre}.geojson`, JSON.stringify(gj, null, 1), "application/geo+json");
    } else {
      const F = E.fixes;
      const pm = F.map((p) => `    <Placemark><name>${p.n}</name><description>${U.esc(`${p.hora} SNR ${p.snr_db} dB, ${p.pot_dbfs} dBFS`)}</description><Point><coordinates>${p.lon},${p.lat},0</coordinates></Point></Placemark>`).join("\n");
      const kml = `<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Recorrido LoRa</name>
  <Style id="ruta"><LineStyle><color>ff24a3ef</color><width>4</width></LineStyle></Style>
  <Placemark><name>Recorrido</name><styleUrl>#ruta</styleUrl><LineString><tessellate>1</tessellate><coordinates>${F.map((p) => `${p.lon},${p.lat},0`).join(" ")}</coordinates></LineString></Placemark>
  <Folder><name>Paquetes</name>
${pm}
  </Folder>${E.base ? `\n  <Placemark><name>${U.esc(E.base.nombre || "Estación base")}</name><Point><coordinates>${E.base.lon},${E.base.lat},0</coordinates></Point></Placemark>` : ""}
</Document></kml>`;
      U.descargar(`${nombre}.kml`, kml, "application/vnd.google-earth.kml+xml");
    }
  }
  $$("[data-exportar]").forEach((b) => b.addEventListener("click", () => exportar(b.dataset.exportar)));

  /* ================================================================== pestañas principales */
  function mostrarVista(v) {
    $$(".pestanas [data-vista]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.vista === v)));
    $("#vista-lora").hidden = v !== "lora";
    $("#vista-fm").hidden = v !== "fm";
    $("#subtitulo").textContent = v === "fm" ? "Analizador de FM comercial" : "Rastreo vehicular GPS/LoRa";
    if (v === "lora") {
      setTimeout(() => {
        mapa.map.invalidateSize();
        cascada.dibujarEspectro();
        pintarGrafica();
      }, 0);
    } else S.fm && S.fm.alMostrar();
    if (location.hash !== `#${v}`) history.replaceState(null, "", `#${v}`);
  }
  $$(".pestanas [data-vista]").forEach((b) => b.addEventListener("click", () => mostrarVista(b.dataset.vista)));
  $(".pestanas").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const bs = $$(".pestanas [data-vista]");
    const i = bs.indexOf(document.activeElement);
    const sig = bs[(i + (e.key === "ArrowRight" ? 1 : bs.length - 1)) % bs.length];
    sig.focus();
    sig.click();
  });

  /* ================================================================== conexión */
  let tFallo = null;
  function lampServidor(estado, titulo) {
    const l = $("#lamp-servidor");
    l.dataset.estado = estado;
    if (titulo) l.title = titulo;
  }

  function conectar() {
    if (E.demo) return;
    if (location.protocol === "file:" || !window.EventSource) {
      lampServidor("mal", "Abierto como archivo: no hay servidor");
      $("#chip-modo").textContent = "Sin servidor";
      $("#aviso-conexion").hidden = false;
      return;
    }
    if (E.fuente) E.fuente.close();
    const es = new EventSource("eventos");
    E.fuente = es;
    const tipos = ["historial_inicio", "historial_fin", "estado", "paquete", "espectro", "stats", "aviso", "fm_log", "fm_listo"];
    tipos.forEach((t) =>
      es.addEventListener(t, (ev) => {
        let d = {};
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        manejar(t, d);
      })
    );
    es.onopen = () => {
      E.conectado = true;
      E.alguna = true;
      clearTimeout(tFallo);
      lampServidor("ok", "Conectado a servidor_gps.py");
      $("#aviso-conexion").hidden = true;
      S.fm && S.fm.conexion(true);
    };
    es.onerror = () => {
      E.conectado = false;
      lampServidor("mal", "Sin conexión con servidor_gps.py; reintentando");
      S.fm && S.fm.conexion(false);
      clearTimeout(tFallo);
      tFallo = setTimeout(() => {
        if (!E.conectado && !E.demo) {
          $("#aviso-conexion").hidden = false;
          if (!E.alguna) $("#chip-modo").textContent = "Sin servidor";
        }
      }, E.alguna ? 8000 : 2500);
    };
  }

  function iniciarDemo() {
    if (E.fuente) {
      E.fuente.close();
      E.fuente = null;
    }
    E.conectado = false;
    $("#aviso-conexion").hidden = true;
    lampServidor("espera", "Demostración sin servidor");
    limpiarLocal();
    if (!E.baseDelUsuario) E.base = null;
    E.demo = new S.Demo((tipo, d, silencioso) => manejar(tipo, d, silencioso));
    E.cargandoHistorial = true;
    E.demo.iniciar();
    E.cargandoHistorial = false;
    refrescarTodo();
    mapa.ajustar();
    mapa.setSeguir(true);
    $("#btn-seguir").setAttribute("aria-pressed", "true");
  }
  S.iniciarDemo = iniciarDemo;

  $("#btn-demo").addEventListener("click", iniciarDemo);
  $("#btn-reintentar").addEventListener("click", () => {
    $("#aviso-conexion").hidden = true;
    lampServidor("espera");
    conectar();
  });

  /* ================================================================== arranque */
  const params = new URLSearchParams(location.search);
  mostrarVista(location.hash === "#fm" ? "fm" : "lora");
  pintarInspector(null);
  pintarGrafica();
  if (params.has("demo")) iniciarDemo();
  else conectar();
})(window.SDR);
