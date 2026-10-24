/* Mapa Leaflet: recorrido coloreado por SNR, vehículo, estación base y enlace. */
(function (S) {
  "use strict";
  const U = S.util;

  const ICONO_BASE = `<svg viewBox="0 0 34 34" aria-hidden="true">
    <circle cx="17" cy="17" r="15.5" fill="#24557F" stroke="#fff" stroke-width="2"/>
    <path d="M17 15v11M12.5 26h9M11 10.5a8.5 8.5 0 0 1 12 0M13.6 13.1a4.8 4.8 0 0 1 6.8 0" stroke="#EFA324" stroke-width="2" fill="none" stroke-linecap="round"/>
    <circle cx="17" cy="15" r="1.6" fill="#EFA324"/></svg>`;

  class Mapa {
    constructor(id, opciones = {}) {
      this.onElegir = opciones.onElegir || (() => {});
      this.onSeguirCambia = opciones.onSeguirCambia || (() => {});
      this.onBaseFijada = opciones.onBaseFijada || (() => {});
      this.seguir = true;
      this.puntos = []; // {p, marcador}
      this.base = null;
      this.esperandoBase = false;

      this.map = L.map(id, { zoomControl: false, preferCanvas: true }).setView([10.9685, -74.7813], 13);
      L.control.zoom({ position: "topright" }).addTo(this.map);

      const atrOSM = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
      // Fondos sin clave de API (CARTO empezó a exigirla y mostraba "API KEY REQUIRED")
      const esri = (servicio, opciones = {}) => L.tileLayer(
        `https://server.arcgisonline.com/ArcGIS/rest/services/${servicio}/MapServer/tile/{z}/{y}/{x}`,
        { attribution: "&copy; Esri", maxZoom: 19, ...opciones });
      const capas = {
        "Calles": esri("World_Street_Map", { attribution: "&copy; Esri, HERE, Garmin, OpenStreetMap" }),
        "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: atrOSM, maxZoom: 19 }),
        "Satélite": esri("World_Imagery", { attribution: "Imágenes &copy; Esri, Maxar, Earthstar Geographics" }),
        "Oscuro": L.layerGroup([
          esri("Canvas/World_Dark_Gray_Base", { maxNativeZoom: 16, attribution: "&copy; Esri, HERE, Garmin, OpenStreetMap" }),
          esri("Canvas/World_Dark_Gray_Reference", { maxNativeZoom: 16, attribution: "" }),   // nombres de calles
        ]),
      };
      const oscuro = window.matchMedia("(prefers-color-scheme: dark)").matches;
      (oscuro ? capas["Oscuro"] : capas["Calles"]).addTo(this.map);
      L.control.layers(capas, null, { position: "topright" }).addTo(this.map);
      L.control.scale({ position: "bottomright", imperial: false }).addTo(this.map);

      this.linea = L.polyline([], { color: "#24557F", weight: 3, opacity: 0.75, lineJoin: "round" }).addTo(this.map);
      this.capaPuntos = L.layerGroup().addTo(this.map);
      this.enlace = L.polyline([], { color: "#17222D", weight: 1.6, dashArray: "6 6", opacity: 0.8, interactive: false }).addTo(this.map);
      this.etqEnlace = null;

      this.vehiculo = L.marker([0, 0], {
        icon: L.divIcon({ className: "mk-vehiculo", html: '<span class="onda"></span><span class="flecha"></span><span class="nucleo"></span>', iconSize: [18, 18] }),
        zIndexOffset: 1000,
        keyboard: false,
      });

      this.map.on("dragstart", () => {
        if (this.seguir) {
          this.seguir = false;
          this.onSeguirCambia(false);
        }
      });
      this.map.on("click", (e) => {
        if (!this.esperandoBase) return;
        this.modoBase(false);
        this.setBase(e.latlng.lat, e.latlng.lng, "Estación base");
        this.onBaseFijada(this.base);
      });
    }

    /* ---------------------------------------------------------- recorrido */
    agregar(p, animar = true) {
      if (!Number.isFinite(p.lat) || !Number.isFinite(p.lon)) return;
      const ll = [p.lat, p.lon];
      const mk = L.circleMarker(ll, {
        radius: 5, color: "#fff", weight: 1.5, fillColor: U.colorSNR(p.snr_db), fillOpacity: 1,
      });
      mk.bindPopup(() => this.popup(p), { autoPanPadding: [40, 40] });
      mk.on("click", () => this.onElegir(p));
      mk.addTo(this.capaPuntos);
      this.puntos.push({ p, mk });
      this.linea.addLatLng(ll);

      const primero = !this.map.hasLayer(this.vehiculo);
      this.vehiculo.setLatLng(ll);
      if (primero) this.vehiculo.addTo(this.map);
      // flecha de rumbo según los dos últimos puntos (si se movió lo suficiente)
      const el = this.vehiculo.getElement();
      if (el) {
        const flecha = el.querySelector(".flecha");
        const prev = this.puntos.length > 1 ? this.puntos[this.puntos.length - 2].p : null;
        if (prev && U.haversine([prev.lat, prev.lon], ll) > 3) {
          flecha.style.transform = `rotate(${U.rumbo([prev.lat, prev.lon], ll)}deg)`;
          flecha.style.opacity = 1;
        } else if (!prev) flecha.style.opacity = 0;
        if (animar && !U.reducidoMovimiento()) {
          el.classList.remove("nuevo");
          void el.offsetWidth;
          el.classList.add("nuevo");
        }
      }
      this._actualizarEnlace();
      if (primero) this.map.setView(ll, 16, { animate: false });
      else if (this.seguir) this.map.panTo(ll, { animate: animar && !U.reducidoMovimiento(), duration: 0.6 });
    }

    popup(p) {
      const d = this.base ? U.haversine([this.base.lat, this.base.lon], [p.lat, p.lon]) : NaN;
      const fila = (a, b) => `<tr><td>${a}</td><td>${b}</td></tr>`;
      return `<div class="popup-pkt"><strong>Paquete ${p.n}</strong> <span style="color:#66727d">${U.esc(p.hora)}</span>
        <table>
          ${fila("Posición", `${U.coord(p.lat)}, ${U.coord(p.lon)}`)}
          ${fila("SNR", `${U.num(p.snr_db, 1, true)} dB`)}
          ${fila("Potencia", `${U.num(p.pot_dbfs, 1)} dBFS`)}
          ${fila("Offset", `${U.num(p.cfo_hz, 0, true)} Hz`)}
          ${Number.isFinite(d) ? fila("Distancia a la base", U.distancia(d)) : ""}
        </table></div>`;
    }

    resaltar(p) {
      const item = this.puntos.find((q) => q.p === p);
      if (!item) return;
      if (this.seguir) {
        this.seguir = false;
        this.onSeguirCambia(false);
      }
      this.map.flyTo(item.mk.getLatLng(), Math.max(this.map.getZoom(), 16), { duration: U.reducidoMovimiento() ? 0 : 0.6 });
      setTimeout(() => item.mk.openPopup(), U.reducidoMovimiento() ? 0 : 650);
    }

    ajustar() {
      const lls = this.puntos.map((q) => q.mk.getLatLng());
      if (this.base) lls.push(L.latLng(this.base.lat, this.base.lon));
      if (!lls.length) return;
      if (lls.length === 1) this.map.setView(lls[0], 16);
      else this.map.fitBounds(L.latLngBounds(lls), { padding: [50, 50], maxZoom: 17 });
    }

    setSeguir(v) {
      this.seguir = v;
      if (v && this.puntos.length) this.map.panTo(this.puntos[this.puntos.length - 1].mk.getLatLng());
    }

    limpiar() {
      this.capaPuntos.clearLayers();
      this.linea.setLatLngs([]);
      this.puntos = [];
      if (this.map.hasLayer(this.vehiculo)) this.vehiculo.remove();
      this._actualizarEnlace();
    }

    /* ---------------------------------------------------------- estación base */
    modoBase(activo) {
      this.esperandoBase = activo;
      this.map.getContainer().classList.toggle("leaflet-crosshair", activo);
    }

    setBase(lat, lon, nombre) {
      this.base = { lat, lon, nombre: nombre || "Estación base" };
      if (!this.mkBase) {
        this.mkBase = L.marker([lat, lon], {
          icon: L.divIcon({ className: "mk-base", html: ICONO_BASE, iconSize: [34, 34] }),
          draggable: true, title: "Estación base (arrástrala para moverla)", zIndexOffset: 500,
        }).addTo(this.map);
        this.mkBase.on("dragend", () => {
          const ll = this.mkBase.getLatLng();
          this.base.lat = ll.lat;
          this.base.lon = ll.lng;
          this._actualizarEnlace();
          this.onBaseFijada(this.base);
        });
      } else this.mkBase.setLatLng([lat, lon]);
      this.mkBase.bindTooltip(U.esc(this.base.nombre), { direction: "top", offset: [0, -16] });
      this._actualizarEnlace();
      if (!this.puntos.length) this.map.setView([lat, lon], 15);
    }

    _actualizarEnlace() {
      const ult = this.puntos[this.puntos.length - 1];
      if (!this.base || !ult) {
        this.enlace.setLatLngs([]);
        if (this.etqEnlace) {
          this.etqEnlace.remove();
          this.etqEnlace = null;
        }
        return;
      }
      const a = [this.base.lat, this.base.lon], b = [ult.p.lat, ult.p.lon];
      this.enlace.setLatLngs([a, b]);
      const medio = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      const txt = U.distancia(U.haversine(a, b));
      if (!this.etqEnlace) {
        this.etqEnlace = L.tooltip({ permanent: true, direction: "center", className: "etq-dist", interactive: false })
          .setLatLng(medio).setContent(txt).addTo(this.map);
      } else this.etqEnlace.setLatLng(medio).setContent(txt);
    }
  }

  S.Mapa = Mapa;
})(window.SDR);
