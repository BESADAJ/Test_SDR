/* Vista FM: muestra los reportes de sdr_analisis.py y lanza capturas desde la interfaz. */
(function (S) {
  "use strict";
  const U = S.util;
  const { $, $$ } = U;

  const DESCRIPCION_GRUPOS = {
    "0A": "nombre (PS) y frecuencias alternativas",
    "0B": "nombre (PS)",
    "1A": "código de programa extendido",
    "2A": "RadioText de 64 caracteres",
    "2B": "RadioText de 32 caracteres",
    "3A": "aplicaciones abiertas (ODA)",
    "4A": "fecha y hora",
    "8A": "mensajes de tráfico (TMC)",
    "10A": "nombre del tipo de programa",
    "14A": "otras redes (EON)",
    "15B": "información rápida",
  };

  const F = {
    conectado: false,
    actual: null,
    urlsLocales: [],
    capturando: false,
  };

  /* ---------------------------------------------------------- utilidades */
  const fila = (dt, dd) => `<div><dt>${dt}</dt><dd>${dd}</dd></div>`;
  const val = (v, dec, unidad, signo) => (Number.isFinite(v) ? `${U.num(v, dec, signo)}${unidad ? ` <small>${unidad}</small>` : ""}` : "—");
  const si = (b) => (b === true ? "sí" : b === false ? "no" : "—");

  function consola(texto, nivel) {
    const c = $("#fm-consola");
    c.hidden = false;
    const linea = document.createElement("div");
    if (nivel === "error" || nivel === "ok") linea.className = nivel;
    linea.textContent = texto;
    c.appendChild(linea);
    while (c.children.length > 400) c.firstElementChild.remove();
    c.scrollTop = c.scrollHeight;
  }

  /* ---------------------------------------------------------- lista de reportes */
  async function cargarLista(seleccionar) {
    const ul = $("#lista-reportes");
    if (!F.conectado) {
      ul.innerHTML = '<li class="tenue">Sin conexión con el servidor. Abre un reporte.json o mira el ejemplo.</li>';
      return;
    }
    try {
      const r = await fetch("api/fm/reportes", { cache: "no-store" });
      const lista = await r.json();
      if (!lista.length) {
        ul.innerHTML = '<li class="tenue">Todavía no hay reportes en <code>resultados/</code>. Captura una emisora para crear el primero.</li>';
        return;
      }
      ul.innerHTML = lista
        .map((x) => {
          const fecha = x.fecha ? new Date(x.fecha).toLocaleString("es-CO", { dateStyle: "medium", timeStyle: "short" }) : x.dir;
          return `<li><button data-dir="${U.esc(x.dir)}" aria-current="${F.actual === x.dir}">
            <span class="r-ps">${U.esc((x.PS || "").trim() || "Sin RDS")}</span>
            <span class="r-freq">${Number.isFinite(x.emisora_MHz) ? `${U.num(x.emisora_MHz, 1)} MHz` : ""}</span>
            <span class="r-meta">${U.esc(fecha)}${Number.isFinite(x.SNR_dB) ? `, SNR ${U.num(x.SNR_dB, 1)} dB` : ""}${x.modo ? `, ${U.esc(x.modo)}` : ""}</span>
          </button></li>`;
        })
        .join("");
      if (seleccionar) abrirDelServidor(seleccionar);
      else if (!F.actual && lista.length) abrirDelServidor(lista[0].dir);
    } catch (e) {
      ul.innerHTML = `<li class="tenue">No se pudo leer la lista: ${U.esc(e.message)}</li>`;
    }
  }

  async function abrirDelServidor(dir) {
    try {
      const r = await fetch(`api/fm/reporte?dir=${encodeURIComponent(dir)}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      F.actual = dir;
      $$("#lista-reportes button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.dir === dir)));
      pintar(d.reporte, d.archivos || {}, `resultados/${dir}`);
    } catch (e) {
      consola(`No se pudo abrir ${dir}: ${e.message}`, "error");
    }
  }

  $("#lista-reportes").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-dir]");
    if (b) abrirDelServidor(b.dataset.dir);
  });
  $("#btn-fm-refrescar").addEventListener("click", () => cargarLista());

  /* ---------------------------------------------------------- archivos locales y ejemplo */
  $("#fm-archivos").addEventListener("change", async (e) => {
    const archivos = Array.from(e.target.files || []);
    e.target.value = "";
    const json = archivos.find((f) => f.name.toLowerCase().endsWith(".json"));
    if (!json) return consola("Elige al menos el archivo reporte.json.", "error");
    F.urlsLocales.forEach(URL.revokeObjectURL);
    F.urlsLocales = [];
    const url = (ext) => {
      const f = archivos.find((x) => x.name.toLowerCase().endsWith(ext));
      if (!f) return undefined;
      const u = URL.createObjectURL(f);
      F.urlsLocales.push(u);
      return u;
    };
    try {
      const r = JSON.parse(await json.text());
      F.actual = null;
      $$("#lista-reportes button").forEach((b) => b.setAttribute("aria-current", "false"));
      pintar(r, { graficas: url(".png"), audio: url(".wav") }, json.name);
    } catch (err) {
      consola(`${json.name} no es un reporte válido: ${err.message}`, "error");
    }
  });

  $("#btn-fm-ejemplo").addEventListener("click", () => {
    const mostrar = () => {
      F.actual = null;
      $$("#lista-reportes button").forEach((b) => b.setAttribute("aria-current", "false"));
      pintar(S.EJEMPLO_FM, { graficas: "demo/graficas.png", audio: "demo/audio.wav" }, "ejemplo sintético");
    };
    if (S.EJEMPLO_FM) return mostrar();
    const s = document.createElement("script");
    s.src = "demo/ejemplo_fm.js";
    s.onload = mostrar;
    s.onerror = () => consola("No encuentro web/demo/ejemplo_fm.js.", "error");
    document.head.appendChild(s);
  });

  /* ---------------------------------------------------------- capturas */
  async function pedirCaptura(cuerpo) {
    if (!F.conectado) return consola("La captura necesita servidor_gps.py corriendo.", "error");
    if (F.capturando) return;
    $("#fm-consola").innerHTML = "";
    try {
      const r = await fetch("api/fm/capturar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cuerpo) });
      const d = await r.json();
      if (!d.ok) return consola(d.mensaje || "No se pudo iniciar la captura.", "error");
      ocupado(true);
    } catch (e) {
      consola(`Error al pedir la captura: ${e.message}`, "error");
    }
  }

  function ocupado(v) {
    F.capturando = v;
    const b = $("#btn-capturar");
    b.disabled = v;
    b.textContent = v ? "Capturando…" : "Capturar emisora";
  }

  $("#form-captura").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    pedirCaptura({ modo: "en_vivo", freq: Number(f.get("freq")), segundos: Number(f.get("segundos")), ganancia: String(f.get("ganancia") || "auto").trim() });
  });
  $("#form-archivo").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    pedirCaptura({
      modo: "archivo", ruta: String(f.get("ruta")), freq: Number(f.get("freq")),
      centro: f.get("centro") ? Number(f.get("centro")) : null, segundos: Number(f.get("segundos") || 0),
    });
  });

  /* ---------------------------------------------------------- dibujo del reporte */
  function esquemaMPX(r) {
    // Esquema de la banda base MPX con los niveles medidos (en kHz de desviación)
    const mpx = r.mpx || {}, fm = r.fm || {};
    const W = 400, H = 150, x0 = 12, x1 = W - 8, yB = H - 24;
    const fx = (k) => x0 + (k / 62) * (x1 - x0);
    const escala = Math.max(8, (fm.desviacion_rms_kHz || 10) * 1.4, (mpx.piloto_nivel_kHz || 0) * 1.3);
    const hy = (k) => Math.max(3, Math.min(yB - 14, (k / escala) * (yB - 18)));
    const audio = fm.desviacion_rms_kHz || 0;
    const est = !!mpx.piloto_estereo;
    const bloque = (f0, f1, k, color, txt) => {
      const h = hy(k);
      return `<rect x="${fx(f0)}" y="${yB - h}" width="${fx(f1) - fx(f0)}" height="${h}" rx="3" fill="${color}"/>
        <text x="${(fx(f0) + fx(f1)) / 2}" y="${yB - h - 5}" text-anchor="middle">${txt}</text>`;
    };
    const linea = (f, k, color, txt) => {
      const h = hy(k);
      return `<line x1="${fx(f)}" x2="${fx(f)}" y1="${yB}" y2="${yB - h}" stroke="${color}" stroke-width="3"/>
        <text x="${fx(f) - 4}" y="${Math.max(12, Math.min(yB - h - 5, yB - hy(audio) - 20))}" text-anchor="start">${txt}</text>`;
    };
    const ejes = [0, 15, 19, 38, 53, 57]
      .map((k) => `<line x1="${fx(k)}" x2="${fx(k)}" y1="${yB}" y2="${yB + 4}" stroke="currentColor"/><text x="${fx(k)}" y="${yB + 16}" text-anchor="middle">${k}</text>`)
      .join("");
    return `<svg class="mpx-esquema" viewBox="0 0 ${W} ${H}" role="img" aria-label="Esquema de la banda base MPX" font-family="Barlow Semi Condensed, sans-serif" font-size="12.5" fill="currentColor">
      <line x1="${x0}" x2="${x1}" y1="${yB}" y2="${yB}" stroke="currentColor"/>
      ${bloque(0.05, 15, audio, "#24557F", "L+R")}
      ${est ? bloque(23, 53, audio * 0.6, "#5C86AB", "L−R") : ""}
      ${est ? linea(19, mpx.piloto_nivel_kHz || 0, "#2F9E6A", `piloto ${U.num(mpx.piloto_nivel_kHz, 1)} kHz`) : `<text x="${fx(19)}" y="${yB - 8}" text-anchor="middle" opacity=".6">sin piloto</text>`}
      ${bloque(54.6, 59.4, mpx.rds_nivel_kHz || 0, "#EFA324", `RDS ${U.num(mpx.rds_nivel_kHz, 1)}`)}
      ${ejes}
      <text x="${x1}" y="${yB + 16}" text-anchor="end">kHz</text>
    </svg>`;
  }

  function vu(etq, dbfs) {
    const pct = Number.isFinite(dbfs) ? Math.max(0, Math.min(100, ((dbfs + 40) / 40) * 100)) : 0;
    return `<div class="vu"><span>${etq}</span><div class="medidor"><i style="width:${pct}%;--ancho-vu:${pct ? (100 / pct) * 100 : 100}%"></i></div><span>${val(dbfs, 1, "dBFS")}</span></div>`;
  }

  function pintar(r, archivos, origen) {
    if (!r || typeof r !== "object") return;
    const fuente = r.fuente || {}, rf = r.rf || {}, fm = r.fm || {}, mpx = r.mpx || {}, au = r.audio || {}, rds = r.rds || {};
    const hayRDS = (rds.grupos_validos || 0) > 0;
    const ps = (rds.PS || "").padEnd(8, " ").slice(0, 8);
    const freq = fuente.emisora_MHz;
    const pty = rds.PTY_nombre ? rds.PTY_nombre["RDS (Europa/LatAm)"] : null;

    const celdas = Array.from(ps)
      .map((c) => `<span class="${rds.PS ? "" : "vacio"}">${c.trim() ? U.esc(c) : rds.PS ? "&nbsp;" : "·"}</span>`)
      .join("");
    const ind = (on, txt) => `<span class="ind ${on ? "on" : ""}"><i></i>${txt}</span>`;

    const mod = fm.modulacion_pct;
    const modPct = Number.isFinite(mod) ? Math.min(100, (mod / 120) * 100) : 0;
    const ppmSugerido = fm["sumar_a_--ppm"];

    const tipos = Object.entries(rds.tipos_grupo || {}).sort((a, b) => b[1] - a[1]);
    const maxT = Math.max(1, ...tipos.map((t) => t[1]));

    const audioSrc = archivos.audio;
    const html = `
      <section class="radio" aria-label="Emisora">
        <div class="radio-frec"><b>${Number.isFinite(freq) ? U.num(freq, 1) : "—"}</b><small>MHz${rds.PI ? `, PI ${U.esc(rds.PI)}` : ""}</small></div>
        <div class="ps" aria-label="Nombre de la emisora (PS): ${U.esc(rds.PS || "sin RDS")}">${celdas}</div>
        <div class="radio-rt ${rds.RadioText ? "" : "vacio"}">${rds.RadioText ? U.esc(rds.RadioText) : hayRDS ? "La emisora no envió RadioText en esta captura." : U.esc(rds.nota || "No se decodificó RDS en esta captura.")}</div>
        <div class="indicadores">
          ${ind(mpx.piloto_estereo, "Estéreo")}
          ${ind(hayRDS, "RDS")}
          ${ind(rds.TP, "Tráfico (TP)")}
          ${ind(rds.TA, "Anuncio (TA)")}
          ${rds.MS ? `<span class="ind on"><i></i>${U.esc(rds.MS === "música" ? "Música" : "Voz")}</span>` : ""}
          ${pty ? `<span class="ind">Tipo de programa <b>${U.esc(pty)}</b></span>` : ""}
          ${rds.CT ? `<span class="ind">Hora de la emisora <b>${U.esc(rds.CT)}</b></span>` : ""}
        </div>
      </section>

      <div class="fm-grupos">
        <section class="modulo">
          <header class="modulo-cab"><h2>Señal de RF</h2><span class="tenue">resolución ${val(rf.resolucion_fft_Hz, 0, "Hz")}</span></header>
          <div class="indicador-grande"><b>${U.num(rf.SNR_dB, 1)}</b><span>dB de SNR en el canal</span></div>
          <dl class="datos datos-2">
            ${fila("Potencia del canal", val(rf.potencia_canal_dBFS, 1, "dBFS"))}
            ${fila("Piso de ruido por bin", val(rf.piso_ruido_dBFS_por_bin, 1, "dBFS"))}
            ${fila("Pico del espectro", val(rf.pico_espectro_MHz, 4, "MHz"))}
            ${fila("Variación de potencia", Array.isArray(rf.potencia_canal_min_max_dBFS) ? `${U.num(rf.potencia_canal_min_max_dBFS[0], 1)} a ${U.num(rf.potencia_canal_min_max_dBFS[1], 1)} <small>dBFS</small>` : "—")}
            ${fila("Muestras saturadas", val(rf.muestras_saturadas_pct, 3, "%"))}
          </dl>
          ${rf.aviso ? `<p class="aviso-linea">${U.esc(rf.aviso)}</p>` : ""}
        </section>

        <section class="modulo">
          <header class="modulo-cab"><h2>Modulación FM</h2><span class="tenue">100 % = 75 kHz</span></header>
          <div class="indicador-grande"><b>${U.num(mod, 1)}</b><span>% de modulación</span></div>
          <div class="barra-mod" aria-hidden="true"><i class="${mod > 100 ? "alto" : ""}" style="width:${modPct}%"></i></div>
          <div class="escala-mod"><span>0</span><span>25</span><span>50</span><span>75</span><span>90 kHz</span></div>
          <dl class="datos datos-2">
            ${fila("Desviación pico", val(fm.desviacion_pico_kHz, 2, "kHz"))}
            ${fila("Desviación RMS", val(fm.desviacion_rms_kHz, 2, "kHz"))}
            ${fila("Error de sintonía", val(fm.error_sintonia_Hz, 0, "Hz", true))}
            ${fila("Error estimado", val(fm.error_ppm_estimado, 2, "ppm", true))}
          </dl>
          ${Number.isFinite(ppmSugerido) && ppmSugerido !== 0 ? `<p class="nota-modulo" style="margin:10px 0 0">Para corregir el dongle suma <b>${ppmSugerido > 0 ? "+" : ""}${ppmSugerido}</b> al valor de <code>--ppm</code>.</p>` : ""}
        </section>

        <section class="modulo">
          <header class="modulo-cab"><h2>Banda base MPX</h2><span class="tenue">esquema con niveles medidos</span></header>
          ${esquemaMPX(r)}
          <dl class="datos datos-2">
            ${fila("Piloto de 19 kHz", mpx.piloto_estereo ? `${val(mpx.piloto_nivel_kHz, 2, "kHz")} <small>(${U.num(mpx.piloto_pct_desviacion, 1)} %)</small>` : "no detectado")}
            ${fila("Frecuencia del piloto", val(mpx.piloto_frecuencia_medida_Hz, 2, "Hz"))}
            ${fila("Error de reloj del muestreo", val(mpx.error_reloj_muestreo_ppm, 1, "ppm", true))}
            ${fila("Subportadora RDS", `${val(mpx.rds_nivel_kHz, 2, "kHz")}${Number.isFinite(mpx.rds_SNR_dB) ? ` <small>SNR ${U.num(mpx.rds_SNR_dB, 1)} dB</small>` : ""}`)}
          </dl>
        </section>

        <section class="modulo">
          <header class="modulo-cab"><h2>Audio demodulado</h2><span class="tenue">${au.estereo ? "estéreo" : "mono"}, ${val(au.frecuencia_muestreo_Hz / 1000, 0, "kHz")}</span></header>
          ${vu("I", au.rms_izq_dBFS)}
          ${vu("D", au.rms_der_dBFS)}
          <dl class="datos datos-2" style="margin-top:10px">
            ${fila("Correlación izquierda/derecha", Number.isFinite(au.correlacion_izq_der) ? `${U.num(au.correlacion_izq_der, 3)} <small>${au.correlacion_izq_der > 0.95 ? "casi mono" : au.correlacion_izq_der < 0.3 ? "canales muy distintos" : "estéreo"}</small>` : "—")}
            ${fila("De-énfasis", val(au.deenfasis_us, 0, "µs"))}
          </dl>
          ${audioSrc ? `<audio class="reproductor" controls preload="none" src="${U.esc(audioSrc)}"></audio>` : '<p class="tenue nota-modulo" style="margin:10px 0 0">Para escucharlo abre también el <code>audio.wav</code> de esa carpeta.</p>'}
        </section>

        <section class="modulo">
          <header class="modulo-cab"><h2>RDS</h2><span class="tenue">${hayRDS ? `${rds.grupos_validos} grupos válidos` : "sin datos"}</span></header>
          ${hayRDS ? `
          <dl class="datos datos-2">
            ${fila("Código PI", U.esc(rds.PI || "—"))}
            ${fila("Tipo de programa", Number.isFinite(rds.PTY) ? `${rds.PTY} <small>${U.esc(pty || "")}</small>` : "—")}
            ${fila("En norma RBDS", U.esc(rds.PTY_nombre ? rds.PTY_nombre["RBDS (Norteamérica)"] : "—"))}
            ${fila("Bits recibidos", Number.isFinite(rds.bits_recibidos) ? String(rds.bits_recibidos) : "—")}
            ${fila("Estéreo según DI", si(rds.estereo_DI))}
            ${fila("Tráfico / anuncio", `${si(rds.TP)} / ${si(rds.TA)}`)}
          </dl>
          <p class="nota-modulo" style="margin:12px 0 6px">Frecuencias alternativas</p>
          <div class="chips">${(rds.AF_MHz || []).length ? rds.AF_MHz.map((f) => `<span class="chip">${U.num(f, 1)} MHz</span>`).join("") : '<span class="tenue">ninguna</span>'}</div>
          <p class="nota-modulo" style="margin:12px 0 4px">Grupos recibidos por tipo</p>
          <div class="barras-grupos">${tipos.map(([t, n]) => `<div title="${U.esc(DESCRIPCION_GRUPOS[t] || "")}"><span>${U.esc(t)}</span><i style="width:${(n / maxT) * 100}%"></i><span>${n}</span></div>`).join("")}</div>
          <p class="tenue nota-modulo" style="margin:8px 0 0">${tipos.slice(0, 3).map(([t]) => `${U.esc(t)}: ${U.esc(DESCRIPCION_GRUPOS[t] || "otro servicio")}`).join(". ")}.</p>`
          : `<p class="tenue">${U.esc(rds.nota || "La captura no tuvo grupos RDS válidos. Prueba más segundos o una emisora más fuerte.")}</p>`}
        </section>

        <section class="modulo">
          <header class="modulo-cab"><h2>Captura</h2><span class="tenue">${U.esc(origen || "")}</span></header>
          <dl class="datos datos-2">
            ${fila("Modo", U.esc(fuente.modo || "—"))}
            ${fila("Duración", val(fuente.duracion_s, 1, "s"))}
            ${fila("Centro sintonizado", val(fuente.centro_MHz, 4, "MHz"))}
            ${fila("Muestreo", val(fuente.fs_Hz / 1e6, 3, "MS/s"))}
            ${fila("Ganancia", fuente.ganancia !== undefined ? U.esc(String(fuente.ganancia)) : "—")}
            ${fila("IQ guardado", fuente.iq_guardado ? `<span title="${U.esc(fuente.iq_guardado)}">${U.esc(fuente.iq_guardado.split(/[\\/]/).pop())}</span>` : fuente.ruta ? `<span title="${U.esc(fuente.ruta)}">${U.esc(String(fuente.ruta).split(/[\\/]/).pop())}</span>` : "—")}
          </dl>
        </section>
      </div>

      ${archivos.graficas ? `<figure class="fm-figura"><img src="${U.esc(archivos.graficas)}" alt="Gráficas de sdr_analisis.py: espectro RF, cascada, MPX, constelación RDS, audio y potencia en el tiempo" loading="lazy"><figcaption>Gráficas generadas por sdr_analisis.py. Haz clic para ampliarlas.</figcaption></figure>` : ""}`;

    $("#fm-vacio").hidden = true;
    const cont = $("#fm-contenido");
    cont.hidden = false;
    cont.innerHTML = html;
    const img = cont.querySelector(".fm-figura img");
    if (img) img.addEventListener("click", () => abrirVisor(img.src));
  }

  function abrirVisor(src) {
    const v = $("#visor");
    v.querySelector("img").src = src;
    v.hidden = false;
    v.querySelector(".visor-cerrar").focus();
  }
  $("#visor").addEventListener("click", (e) => {
    if (e.target.tagName !== "IMG") $("#visor").hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("#visor").hidden = true;
  });

  /* ---------------------------------------------------------- interfaz pública */
  S.fm = {
    alMostrar() {
      if (F.conectado && !$("#lista-reportes button")) cargarLista();
    },
    conexion(ok) {
      const cambio = ok !== F.conectado;
      F.conectado = ok;
      if (cambio && !$("#vista-fm").hidden) cargarLista();
      else if (cambio && ok) cargarLista();
      if (!ok && !$("#lista-reportes button")) cargarLista();
    },
    onLog(d) {
      if (!F.capturando) ocupado(true);
      consola(d.texto, d.nivel);
    },
    onListo(d) {
      ocupado(false);
      if (d.ok && d.dir) cargarLista(d.dir);
    },
    pintar,
  };
})(window.SDR);
