/* Milma AIME dashboard — small client-side glue. */

// Alpine store: live WebSocket state + most-recent-snapshot bag keyed by device id.
document.addEventListener("alpine:init", () => {
  Alpine.store("live", {
    connected: false,
    devices: {},       // device_id -> { ts, ambient_c, channels: {ch -> {pv, pv_error, alarms}} }
    ws: null,

    connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${location.host}/ws/live`;
      this.ws = new WebSocket(url);
      this.ws.onopen    = () => { this.connected = true; };
      this.ws.onclose   = () => { this.connected = false; setTimeout(() => this.connect(), 2000); };
      this.ws.onerror   = () => { try { this.ws.close(); } catch (e) {} };
      this.ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "keepalive") return;
          this.ingest(msg);
        } catch (e) { /* ignore */ }
      };
    },

    ingest(snap) {
      // Snapshot shape from ws/live.py: { device_id, ts, ts_ms, ambient_c, channels:[{channel, pv, pv_error, alarms}], ok }
      const d = {
        ts: snap.ts,
        ambient_c: snap.ambient_c,
        ok: snap.ok !== false,
        channels: {},
      };
      (snap.channels || []).forEach(c => { d.channels[c.channel] = c; });
      this.devices[snap.device_id] = d;
    },

    channel(devId, ch) {
      const dev = this.devices[devId];
      if (!dev) return null;
      return dev.channels[ch] || null;
    },
    ambient(devId) {
      const dev = this.devices[devId];
      return dev ? dev.ambient_c : null;
    },
    deviceTs(devId) {
      const dev = this.devices[devId];
      return dev ? dev.ts : null;
    },
  });
});

// Helpers exposed globally for templates.
window.fmtPv = function (pv, errCode) {
  if (errCode === 1) return "UNDER";
  if (errCode === 2) return "OVER";
  if (errCode === 3) return "OPEN";
  if (errCode === 99) return "FAIL";
  if (pv === null || pv === undefined) return "—";
  return (+pv).toFixed(2);
};
window.tileClass = function (pv, errCode, alarms) {
  if (errCode && errCode !== 0) return "tile err";
  if (alarms && alarms !== 0)  return "tile alarm";
  return "tile";
};
window.alarmLabel = function (alarms) {
  if (!alarms) return "";
  const names = ["HiHi", "Hi", "Lo", "LoLo"];
  const bits = [];
  for (let i = 0; i < 4; i++) if (alarms & (1 << i)) bits.push(names[i]);
  return bits.join(" ");
};
