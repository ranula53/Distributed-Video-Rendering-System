import React, { useState, useEffect, useRef } from 'react';

function App() {
  const [file, setFile] = useState(null);
  const [status, setStatus] = useState(null);
  const [uploading, setUploading] = useState(false);
  const logContainerRef = useRef(null);

  // Is the master reachable? (false = it may have crashed / restarted)
  const [connected, setConnected] = useState(true);
  const missesRef = useRef(0);

  // Health-check popup
  const [healthOpen, setHealthOpen] = useState(false);
  const [healthLoading, setHealthLoading] = useState(false);
  const [healthData, setHealthData] = useState(null);
  const [healthError, setHealthError] = useState(null);

  const [notifications, setNotifications] = useState([]);
  const prevWorkersRef = useRef({});

  // Both masters' PUBLIC Funnel URLs (replace with your tailnet's real URLs from
  // `tailscale funnel --bg 8000` on each VM). The dashboard polls both and routes
  // every API call to whichever is currently PRIMARY, so uploads keep working
  // across a failover. If one master's host dies entirely, open the other URL.
  const MASTERS = [
    "https://vm1-1.tail686c91.ts.net",  // main master (vm1) Funnel URL
    "https://vm10.tail686c91.ts.net",   // backup master (vm10) Funnel URL
  ];
  const [activeUrl, setActiveUrl] = useState(MASTERS[0]);

  useEffect(() => {
    const interval = setInterval(async () => {
      let servingData = null;
      let servingUrl = null;
      for (const url of MASTERS) {
        try {
          const res = await fetch(`${url}/status`);
          if (!res.ok) continue;
          const data = await res.json();
          // Follow the active primary. (Single-master mode reports role "primary" too.)
          if (data.role === undefined || data.role === 'primary') {
            servingData = data;
            servingUrl = url;
            break;
          }
        } catch (error) {
          // this master is unreachable — try the next one
        }
      }
      if (servingData) {
        setStatus(servingData);
        setActiveUrl(servingUrl);
        missesRef.current = 0;
        setConnected(true);
      } else {
        // No reachable primary. Two misses in a row before showing the offline banner.
        missesRef.current += 1;
        if (missesRef.current >= 2) setConnected(false);
      }
    }, 1000);
    return () => clearInterval(interval);
  }, []);

  // Auto-scroll inside the timeline container only (not the whole page)
  useEffect(() => {
    const el = logContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [status?.event_log?.length]);

  // Worker Health Notifications
  useEffect(() => {
    if (!status?.workers) return;
    const prev = prevWorkersRef.current;
    const current = status.workers;

    Object.keys(current).forEach(wid => {
      if (prev[wid] && prev[wid].health !== current[wid].health) {
         const w = current[wid];
         const msg = `${w.name} is now ${w.health.toUpperCase()}`;
         const id = Date.now() + Math.random();
         setNotifications(n => [...n, { id, msg, type: w.health === 'online' ? 'success' : 'error' }]);
         setTimeout(() => {
            setNotifications(n => n.filter(x => x.id !== id));
         }, 5000);
      }
    });

    prevWorkersRef.current = current;
  }, [status?.workers]);

  const handleUpload = async (e) => {
    e.preventDefault();
    if (!file) return;
    setUploading(true);

    const formData = new FormData();
    formData.append("file", file);

    try {
      await fetch(`${activeUrl}/upload`, { method: "POST", body: formData });
    } catch (err) {
      console.error("Upload failed", err);
    }
    setUploading(false);
  };

  const handleDownload = () => {
    if (status?.job_id) {
      window.open(`${activeUrl}/download/${status.job_id}`, "_blank");
    }
  };

  const handleHealthCheck = async () => {
    setHealthOpen(true);
    setHealthLoading(true);
    setHealthError(null);
    try {
      const res = await fetch(`${activeUrl}/health`);
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      const data = await res.json();
      setHealthData(data);
    } catch (err) {
      console.error("Health check failed", err);
      setHealthError("Could not reach the master server.");
    } finally {
      setHealthLoading(false);
    }
  };

  const closeHealth = () => setHealthOpen(false);

  // Close the health popup with the Escape key
  useEffect(() => {
    if (!healthOpen) return;
    const onKey = (e) => {
      if (e.key === 'Escape') setHealthOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [healthOpen]);

  const workers = status?.workers ? Object.entries(status.workers) : [];
  const totalChunks = status?.total_chunks || 0;
  const completedChunks = status?.completed_chunks || 0;
  const progressPct = totalChunks > 0 ? Math.round((completedChunks / totalChunks) * 100) : 0;
  const eventLog = status?.event_log || [];

  const formatTime = (timeStr) => {
    if (!timeStr) return '';
    try {
      const date = new Date(timeStr);
      if (isNaN(date.getTime())) return timeStr;
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    } catch (e) {
      return timeStr;
    }
  };

  const getBannerStyles = (state) => {
    switch (state) {
      case 'Completed':
        return {
          bg: 'bg-[#E4F5F3] border-[#E4E7EC] text-[#0E7C74]',
          led: 'bg-[#0E7C74]',
          pulse: false,
        };
      case 'Processing':
      case 'Resuming':
      case 'splitting':
      case 'rendering':
      case 'merging':
        return {
          bg: 'bg-[#FDF3E2] border-[#E4E7EC] text-[#B7791E]',
          led: 'bg-[#B7791E]',
          pulse: true,
        };
      case 'Error':
        return {
          bg: 'bg-[#FBEAE8] border-[#E4E7EC] text-[#C0392B]',
          led: 'bg-[#C0392B]',
          pulse: true,
        };
      default:
        return {
          bg: 'bg-white border-[#E4E7EC] text-[#6B7480]',
          led: 'bg-[#98A0AC]',
          pulse: false,
        };
    }
  };

  const bannerStyle = getBannerStyles(status?.state);

  return (
    <div className="min-h-screen bg-[#F4F5F7] text-[#171B21] p-6 sm:p-12 font-mono">
      <style>{`
        @keyframes renderGlow {
          0%, 100% {
            box-shadow: 0 0 0 3px rgba(183,121,30,0.22), 0 0 14px rgba(183,121,30,0.30),
                        0 12px 28px rgba(23,27,33,0.14), 0 3px 8px rgba(23,27,33,0.08);
          }
          50% {
            box-shadow: 0 0 0 4px rgba(183,121,30,0.38), 0 0 34px rgba(183,121,30,0.55),
                        0 12px 28px rgba(23,27,33,0.14), 0 3px 8px rgba(23,27,33,0.08);
          }
        }
        .render-glow { animation: renderGlow 1.8s ease-in-out infinite; }
      `}</style>
      <main className="max-w-[960px] mx-auto space-y-8">

        {/* Header Block */}
        <header className="text-center space-y-3">
          <span className="text-xs uppercase tracking-widest text-[#98A0AC] font-bold font-mono">
            Distributed System Orchestrator
          </span>
          <h1 className="text-4xl sm:text-5xl font-semibold font-sans text-[#171B21] tracking-tight">
            Distributed Render Dashboard
          </h1>
          <p className="text-[#6B7480] text-base font-sans">
            Fault-tolerant video rendering across Tailscale nodes
          </p>

          {status?.node && (
            <div className="inline-flex items-center gap-2 bg-[#E4F5F3] border border-[#E4E7EC] px-3 py-1.5 rounded-full">
              <span className="flex h-2 w-2 relative">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#0E7C74] opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-[#0E7C74]"></span>
              </span>
              <span className="text-sm font-semibold text-[#0E7C74] font-mono uppercase tracking-wider">
                Active Master: {status.node}
              </span>
            </div>
          )}
        </header>

        {/* Upload Panel */}
        <section className="bg-white border border-[#E4E7EC] rounded-[10px] p-6 shadow-sm space-y-4 text-left">
          <h2 className="text-xl font-semibold font-sans text-[#171B21]">Upload Video Source</h2>
          <form onSubmit={handleUpload} className="space-y-4">
            <div className="flex flex-col space-y-2">
              <label htmlFor="file-upload" className="text-sm font-semibold font-mono text-[#6B7480] uppercase tracking-wider">
                Select MP4 File
              </label>
              <input
                id="file-upload"
                type="file"
                accept="video/mp4"
                onChange={(e) => setFile(e.target.files[0])}
                className="block w-full text-base text-[#6B7480] font-mono
                           file:mr-4 file:py-2.5 file:px-5 file:rounded-[6px] file:border file:border-[#BFE4E0]
                           file:bg-[#E4F5F3] file:text-[#0E7C74] file:cursor-pointer file:text-sm
                           file:hover:bg-[#d5efec] file:transition file:font-semibold"
              />
            </div>
            <div className="flex gap-3 pt-2">
              <button
                id="btn-upload"
                type="submit"
                disabled={uploading || !file}
                className="flex-grow bg-[#0E7C74] hover:bg-[#0c6a63] disabled:bg-[#E4E7EC] disabled:text-[#98A0AC] text-white font-mono font-semibold text-base py-3 px-5 rounded-[6px] transition-all duration-200"
              >
                {uploading ? "UPLOADING..." : "UPLOAD & RENDER"}
              </button>
              <button
                id="btn-health-check"
                type="button"
                onClick={handleHealthCheck}
                className="bg-[#FDF3E2] hover:bg-[#fbe8c9] text-[#B7791E] font-mono font-semibold text-base py-3 px-5 rounded-[6px] border border-[#F0DDB0] transition-all duration-200"
                title="Ping all workers"
              >
                HEALTH CHECK
              </button>
            </div>
          </form>
        </section>

        {/* Master Offline Banner */}
        {!connected && (
          <div className="rounded-lg border bg-[#FBEAE8] border-[#E4E7EC] text-[#C0392B] px-5 py-4 flex items-center gap-3 text-left">
            <span className="relative flex h-2.5 w-2.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#C0392B] opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-[#C0392B]"></span>
            </span>
            <div className="font-mono text-base">
              <span className="font-bold uppercase tracking-wider">OFFLINE: </span>
              <span className="opacity-90">Cannot reach coordinator master server. Retrying...</span>
            </div>
          </div>
        )}

        {/* System State Banner */}
        {status && (
          <div className={`rounded-lg border px-5 py-4 flex items-center justify-between text-left ${bannerStyle.bg}`}>
            <div className="flex items-center gap-3">
              <span className="relative flex h-2.5 w-2.5">
                {bannerStyle.pulse && (
                  <span className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${bannerStyle.led}`}></span>
                )}
                <span className={`relative inline-flex rounded-full h-2.5 w-2.5 ${bannerStyle.led}`}></span>
              </span>
              <div className="font-mono text-base">
                <span className="font-bold uppercase tracking-wider">{status.state}: </span>
                <span className="opacity-95">{status.master}</span>
              </div>
            </div>
            {status.state === 'Completed' && (
              <button
                id="btn-download"
                onClick={handleDownload}
                className="bg-[#0E7C74] hover:bg-[#0c6a63] text-white font-mono font-semibold text-sm py-2 px-5 rounded-[4px] shadow-sm transition shrink-0 ml-4"
              >
                DOWNLOAD VIDEO
              </button>
            )}
          </div>
        )}

        {/* Segmented Chunk Progress */}
        {(status?.state === 'Processing' || status?.state === 'Resuming' || status?.state === 'rendering' || status?.state === 'merging' || status?.state === 'splitting') && totalChunks > 0 && (
          <div className="space-y-2 text-left">
            <div className="flex items-center justify-between text-sm font-mono text-[#6B7480] uppercase tracking-wider">
              <span>Render Progress</span>
              <span>{completedChunks} / {totalChunks} Chunks ({progressPct}%)</span>
            </div>
            <div className="flex gap-1">
              {Array.from({ length: totalChunks }).map((_, i) => {
                const isCompleted = i < completedChunks;
                return (
                  <div
                    key={i}
                    className={`flex-grow h-3 rounded-[4px] transition-all duration-300 ${
                      isCompleted
                        ? 'bg-gradient-to-b from-[#6FD6A8] to-[#0E7C74]'
                        : 'bg-[#E4E7EC]'
                    }`}
                    title={`Chunk ${i + 1}: ${isCompleted ? 'Completed' : 'Pending'}`}
                  />
                );
              })}
            </div>
          </div>
        )}

        {/* Worker Cards (Worker Rack) */}
        {workers.length > 0 && (
          <section className="space-y-4 text-left">
            <h2 className="text-xl font-semibold font-sans text-[#171B21]">Worker Node Rack</h2>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-5">
              {workers.map(([wid, w]) => (
                <WorkerCard key={wid} worker={w} />
              ))}
            </div>
          </section>
        )}

        {/* Event Timeline */}
        {eventLog.length > 0 && (
          <section
            className="bg-white border border-[#E4E7EC] rounded-[10px] p-6 space-y-4 text-left"
            style={{ boxShadow: '0 12px 28px rgba(23,27,33,0.14), 0 3px 8px rgba(23,27,33,0.08)' }}
          >
            <h2 className="text-xl font-semibold font-sans text-[#171B21]">Event Timeline</h2>
            <div ref={logContainerRef} className="max-h-64 overflow-y-auto divide-y divide-[#E4E7EC] pr-1">
              {eventLog.map((ev, i) => {
                let tagColor = 'text-[#0E7C74] bg-[#E4F5F3]';
                if (ev.type === 'warn') tagColor = 'text-[#B7791E] bg-[#FDF3E2]';
                if (ev.type === 'error') tagColor = 'text-[#C0392B] bg-[#FBEAE8]';

                return (
                  <div key={i} className="flex items-start gap-4 py-2.5 text-sm font-mono font-semibold">
                    <span className="text-[#98A0AC] shrink-0 font-semibold">{formatTime(ev.time)}</span>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider shrink-0 ${tagColor}`}>
                      {ev.type}
                    </span>
                    <span className="text-[#171B21] font-semibold break-all">{ev.msg}</span>
                  </div>
                );
              })}
            </div>
          </section>
        )}

      </main>

      {/* Toast Notifications */}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 font-mono">
        {notifications.map(n => (
          <div key={n.id} className={`px-4 py-3 rounded-lg shadow-lg border flex items-center gap-3 animate-fade-in ${
            n.type === 'success' ? 'bg-[#E4F5F3] border-[#E4E7EC] text-[#0E7C74]' : 'bg-[#FBEAE8] border-[#E4E7EC] text-[#C0392B]'
          }`}>
            <span className={`w-2 h-2 rounded-full ${n.type === 'success' ? 'bg-[#0E7C74]' : 'bg-[#C0392B]'}`}></span>
            <span className="font-semibold text-sm">{n.msg}</span>
          </div>
        ))}
      </div>

      {/* Health Check Modal */}
      {healthOpen && (
        <HealthModal
          loading={healthLoading}
          data={healthData}
          error={healthError}
          onClose={closeHealth}
          onRecheck={handleHealthCheck}
        />
      )}
    </div>
  );
}


/* ──────────── Health Check Modal ──────────── */
const HealthModal = ({ loading, data, error, onClose, onRecheck }) => {
  const workers = data?.workers || [];

  const formatTime = (timeStr) => {
    if (!timeStr) return '';
    try {
      const date = new Date(timeStr);
      if (isNaN(date.getTime())) return timeStr;
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    } catch (e) {
      return timeStr;
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-[#171B21]/30 backdrop-blur-sm animate-fade-in"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md bg-white border border-[#E4E7EC] rounded-lg shadow-double-soft overflow-hidden animate-pop-in"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#E4E7EC] bg-[#F4F5F7]">
          <h2 className="text-base font-semibold font-sans text-[#171B21] uppercase tracking-wider">
            Worker Health Registry
          </h2>
          <button
            id="btn-modal-close"
            onClick={onClose}
            className="text-[#6B7480] hover:text-[#171B21] transition text-xl w-6 h-6 flex items-center justify-center rounded hover:bg-[#E4E7EC]"
            title="Close"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {/* Body */}
        <div className="p-5 font-mono text-sm text-[#171B21]">
          {loading && (
            <div className="flex flex-col items-center justify-center py-10 text-[#6B7480]">
              <span className="w-6 h-6 border-2 border-[#E4E7EC] border-t-[#0E7C74] rounded-full animate-spin mb-3"></span>
              <span>PINGING SYSTEM WORKERS...</span>
            </div>
          )}

          {!loading && error && (
            <div className="py-8 text-center text-[#C0392B]">
              <p className="text-base font-bold">REGISTRY UNREACHABLE</p>
              <p className="text-xs mt-1">{error}</p>
            </div>
          )}

          {!loading && !error && data && (
            <div className="space-y-4">
              <div className="flex items-center justify-between border-b border-[#E4E7EC] pb-2">
                <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${
                  data.online === data.total ? 'bg-[#E4F5F3] text-[#0E7C74]' : 'bg-[#FDF3E2] text-[#B7791E]'
                }`}>
                  {data.online} / {data.total} ONLINE
                </span>
                {data.checked_at && (
                  <span className="text-xs text-[#98A0AC]">
                    TS: {formatTime(data.checked_at)}
                  </span>
                )}
              </div>

              {/* Worker rows */}
              <div className="space-y-2 max-h-60 overflow-y-auto pr-1">
                {workers.map((w) => (
                  <div
                    key={w.id}
                    className="flex items-center justify-between bg-[#F4F5F7] rounded-[6px] px-3 py-2 border border-[#E4E7EC]"
                  >
                    <div className="min-w-0 pr-4">
                      <div className="font-semibold text-sm truncate">{w.name}</div>
                      <div className="text-xs text-[#6B7480] truncate">{w.address}</div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className={`w-1.5 h-1.5 rounded-full ${
                        w.health === 'online' ? 'bg-[#0E7C74]' : 'bg-[#C0392B]'
                      }`} />
                      <span className={`text-xs font-bold uppercase ${
                        w.health === 'online' ? 'text-[#0E7C74]' : 'text-[#C0392B]'
                      }`}>
                        {w.health}
                      </span>
                    </div>
                  </div>
                ))}
                {workers.length === 0 && (
                  <p className="text-center text-[#98A0AC] py-4">No active nodes registered.</p>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-5 py-4 border-t border-[#E4E7EC] bg-[#F4F5F7]">
          <button
            id="btn-modal-recheck"
            onClick={onRecheck}
            disabled={loading}
            className="bg-white hover:bg-[#F4F5F7] disabled:opacity-50 text-[#171B21] font-semibold py-2 px-4 rounded-[6px] border border-[#E4E7EC] transition text-sm"
          >
            {loading ? 'RUNNING...' : 'RE-PROBE'}
          </button>
          <button
            onClick={onClose}
            className="bg-[#0E7C74] hover:bg-[#0c6a63] text-white font-bold py-2 px-5 rounded-[6px] transition text-sm"
          >
            CLOSE
          </button>
        </div>
      </div>
    </div>
  );
}


/* ──────────── Worker Card ──────────── */
const STATUS_BADGES = {
  Rendering:   'bg-[#FDF3E2] text-[#B7791E] border border-[#B7791E]/20',
  Receiving:   'bg-[#FDF3E2] text-[#B7791E] border border-[#B7791E]/20',
  Transferring:'bg-[#FDF3E2] text-[#B7791E] border border-[#B7791E]/20',
  Completed:   'bg-[#E4F5F3] text-[#0E7C74] border border-[#0E7C74]/20',
  Failed:      'bg-[#FBEAE8] text-[#C0392B] border border-[#C0392B]/20',
  Reassigned:  'bg-[#FDF3E2] text-[#B7791E] border border-[#B7791E]/20',
  Online:      'bg-[#E4F5F3] text-[#0E7C74] border border-[#0E7C74]/20',
  Offline:     'bg-[#FBEAE8] text-[#C0392B] border border-[#C0392B]/20',
  Idle:        'bg-[#F4F5F7] text-[#6B7480] border border-[#E4E7EC]',
};

const WorkerCard = ({ worker }) => {
  const isOnline = worker.health === 'online';
  const isOffline = worker.health === 'offline';
  const isRendering = worker.status === 'Rendering';
  const badgeCls = STATUS_BADGES[worker.status] || STATUS_BADGES.Idle;

  return (
    <div
      className={`relative bg-white border rounded-r-lg p-6 border-l-[3px] ${
        isOnline ? 'border-l-[#0E7C74]' : 'border-l-[#C0392B]'
      } ${isRendering ? 'border-[#F0DDB0] render-glow' : 'border-[#E4E7EC]'} flex flex-col justify-between space-y-4 transition-transform duration-200 hover:scale-105`}
      style={!isRendering ? { boxShadow: '0 12px 28px rgba(23,27,33,0.14), 0 3px 8px rgba(23,27,33,0.08)' } : undefined}
    >

      {/* Online / Offline Indicator */}
      <div className="absolute top-5 right-5 flex items-center gap-2">
        <span className={`w-3 h-3 rounded-full ${
          isOnline ? 'bg-[#0E7C74]' :
          isOffline ? 'bg-[#C0392B]' :
          'bg-[#98A0AC]'
        }`}></span>
        <span className={`text-sm font-bold uppercase tracking-wider ${
          isOnline ? 'text-[#0E7C74]' :
          isOffline ? 'text-[#C0392B]' :
          'text-[#6B7480]'
        }`}>{worker.health}</span>
      </div>

      {/* Header */}
      <div className="space-y-1">
        <h3 className="font-extrabold text-lg text-[#171B21] font-mono truncate pr-20">{worker.name}</h3>
        <p className="text-xs text-[#98A0AC] font-mono truncate">{worker.address}</p>
      </div>

      {/* Divider */}
      <div className="border-t border-[#E4E7EC] opacity-60"></div>

      {/* Details */}
      <div className="space-y-2">
        <span
          className={`inline-block text-xs font-extrabold px-3 py-1.5 rounded-[6px] uppercase font-mono tracking-wider ${
            isRendering ? 'bg-[#B7791E] text-white shadow-sm' : badgeCls
          }`}
        >
          {worker.status}
        </span>
        {worker.chunk && (
          <div className="text-xs text-[#6B7480] font-mono truncate">
            <span className="text-[#98A0AC] uppercase text-[10px] tracking-wide font-semibold mr-1">Chunk:</span>
            <span className="font-semibold text-[#171B21]">{worker.chunk}</span>
          </div>
        )}
        {worker.progress && (
          <p className="text-[10px] text-[#98A0AC] font-mono truncate" title={worker.progress}>
            {worker.progress}
          </p>
        )}
      </div>
    </div>
  );
};


export default App;