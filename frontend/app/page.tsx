"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownToLine,
  Check,
  ChevronDown,
  CircleHelp,
  Clock3,
  Database,
  Fingerprint,
  Play,
  RefreshCw,
  RotateCcw,
  Shield,
  ShieldAlert,
  Server,
  Wifi,
  WifiOff,
} from "lucide-react";
import {
  api,
  describeValue,
  type Baseline,
  type InventoryServer,
  type Readiness,
  type Scan,
  type ScanSummary,
} from "@/lib/api";

type Health = { status: string; application: string; dependencies_checked: boolean };
type FleetState = {
  server_id: string;
  status: "success" | "failed";
  fingerprint: string | null;
  role: string | null;
  error: { kind: string; message: string } | null;
};
type CandidateGroups = Record<string, Baseline[]>;
type Operation = { serverId: string; kind: "approve" | "drift" | "reset" } | null;

const formatDate = (value?: string | null) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

const shortHash = (value?: string | null) => (value ? `${value.slice(0, 12)}…${value.slice(-6)}` : "Not collected");

function StatusPill({ value }: { value: string }) {
  return <span className={`status-pill status-${value.toLowerCase().replaceAll("_", "-")}`}>{value.replaceAll("_", " ")}</span>;
}

function Metric({ label, value, tone, icon: Icon }: { label: string; value: number | string; tone: string; icon: typeof Server }) {
  return (
    <article className="metric-card">
      <span className={`metric-icon ${tone}`}><Icon size={17} /></span>
      <div><span className="metric-label">{label}</span><strong>{value}</strong></div>
    </article>
  );
}

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [servers, setServers] = useState<InventoryServer[]>([]);
  const [fleetStates, setFleetStates] = useState<FleetState[]>([]);
  const [candidates, setCandidates] = useState<CandidateGroups>({});
  const [history, setHistory] = useState<ScanSummary[]>([]);
  const [activeScan, setActiveScan] = useState<Scan | null>(null);
  const [selectedScanId, setSelectedScanId] = useState("");
  const [actor, setActor] = useState("demo-operator");
  const [scenario, setScenario] = useState("security_drift");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState<Operation>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dataLoadedAt, setDataLoadedAt] = useState("");

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    setError("");
    const [healthResult, readyResult, collectionResult, historyResult] = await Promise.allSettled([
      api<Health>("/api/health"),
      api<Readiness>("/api/ready"),
      api<{ servers: FleetState[] }>("/api/fleet/states"),
      api<{ items: ScanSummary[] }>("/api/scans?limit=20&offset=0"),
    ]);
    // Fleet collection reconciles discoveries and may create pending candidates.
    // Read inventory afterward so the displayed enrollment state reflects that result.
    const [inventoryResult] = await Promise.allSettled([
      api<{ items: InventoryServer[] }>("/api/servers?limit=100&offset=0"),
    ]);
    if (healthResult.status === "fulfilled") setHealth(healthResult.value);
    else setHealth(null);
    if (readyResult.status === "fulfilled") setReadiness(readyResult.value);
    else setReadiness(null);
    if (inventoryResult.status === "fulfilled") setServers(inventoryResult.value.items);
    if (collectionResult.status === "fulfilled") setFleetStates(collectionResult.value.servers);
    if (historyResult.status === "fulfilled") setHistory(historyResult.value.items);

    const failures = [inventoryResult, collectionResult, historyResult].filter((result) => result.status === "rejected");
    if (failures.length) setError((failures[0] as PromiseRejectedResult).reason instanceof Error
      ? (failures[0] as PromiseRejectedResult).reason.message
      : "Monitor data could not be loaded");

    if (inventoryResult.status === "fulfilled") {
      const candidateResults = await Promise.allSettled(inventoryResult.value.items.map(async (server) => {
        const response = await api<{ items: Baseline[] }>(`/api/servers/${encodeURIComponent(server.server_id)}/baseline-candidates`);
        return [server.server_id, response.items] as const;
      }));
      const nextCandidates: CandidateGroups = {};
      for (const result of candidateResults) {
        if (result.status === "fulfilled") nextCandidates[result.value[0]] = result.value[1];
      }
      setCandidates(nextCandidates);
    }

    if (historyResult.status === "fulfilled") {
      const latest = historyResult.value.items[0];
      if (latest) {
        setSelectedScanId((current) => current || latest.scan_id);
        const detail = await api<{ result: Scan }>(`/api/scans/${latest.scan_id}`).catch(() => null);
        if (detail) setActiveScan(detail.result);
      } else {
        setActiveScan(null);
        setSelectedScanId("");
      }
    }
    setDataLoadedAt(new Date().toISOString());
    if (!quiet) setLoading(false);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);

  const latestScan = useMemo(() => history[0] ?? null, [history]);
  const fleetById = useMemo(() => new Map(fleetStates.map((item) => [item.server_id, item])), [fleetStates]);
  const pendingCount = activeScan?.pending_baseline ?? servers.filter((server) => !server.current_baseline_id && server.status !== "decommissioned").length;
  const scanServers = activeScan?.servers ?? [];
  const openScan = async (scanId: string) => {
    setSelectedScanId(scanId);
    try {
      const detail = await api<{ result: Scan }>(`/api/scans/${scanId}`);
      setActiveScan(detail.result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scan details could not be loaded");
    }
  };

  const runScan = async (announce = true) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const scan = await api<Scan>("/api/scans", { method: "POST" });
      setActiveScan(scan);
      setSelectedScanId(scan.scan_id);
      await refresh(true);
      setActiveScan(scan);
      setSelectedScanId(scan.scan_id);
      if (announce) setNotice(`Scan ${scan.status}: ${scan.drifted} drifted, ${scan.pending_baseline} awaiting baseline approval, ${scan.failed} collection failures.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scan could not be completed");
    } finally {
      setBusy(false);
    }
  };

  const approve = async (server: InventoryServer, baseline: Baseline) => {
    if (!actor.trim()) {
      setError("Enter an audit actor before approving a baseline.");
      return;
    }
    const confirmed = window.confirm(`Approve baseline v${baseline.version} for ${server.display_name} (${server.server_id})? This becomes the trusted reference for this server.`);
    if (!confirmed) return;
    setOperation({ serverId: server.server_id, kind: "approve" });
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`/api/servers/${encodeURIComponent(server.server_id)}/baselines/${baseline.baseline_id}/approve`, {
        method: "POST",
        body: JSON.stringify({ confirm: true, approved_by: actor.trim(), reason: "Approved in MVP demonstration" }),
      });
      setNotice(`Baseline v${baseline.version} approved for ${server.display_name}. The actor is audit metadata only.`);
      await refresh(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Baseline approval failed");
    } finally {
      setBusy(false);
      setOperation(null);
    }
  };

  const performDemoAction = async (server: InventoryServer, kind: "drift" | "reset") => {
    if (!readiness?.demo_controls_enabled) return;
    const message = kind === "drift"
      ? `Apply ${scenario.replaceAll("_", " ")} to ${server.display_name}? This changes its simulated configuration.`
      : `Reset ${server.display_name} to its original configuration?`;
    if (!window.confirm(message)) return;
    setOperation({ serverId: server.server_id, kind });
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (kind === "drift") {
        await api<{ status: string }>(`/api/fleet/${encodeURIComponent(server.server_id)}/simulate-drift`, {
          method: "POST", body: JSON.stringify({ scenario }),
        })
      } else {
        await api<{ status: string; verified: boolean }>(`/api/fleet/${encodeURIComponent(server.server_id)}/reset`, { method: "POST" });
      }
      setNotice(kind === "drift"
        ? `Monitor confirmed ${scenario.replaceAll("_", " ")} on ${server.display_name}. Running a scan…`
        : `Monitor confirmed reset and verified ${server.display_name}. Running a scan…`);
      const scan = await api<Scan>("/api/scans", { method: "POST" });
      setActiveScan(scan);
      setSelectedScanId(scan.scan_id);
      await refresh(true);
      setActiveScan(scan);
      setSelectedScanId(scan.scan_id);
      setNotice(`${kind === "drift" ? "Drift detected" : "Reset verified"}: scan ${scan.status}, ${scan.drifted} drifted server(s).`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Demo ${kind} action failed`);
    } finally {
      setBusy(false);
      setOperation(null);
    }
  };

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="SentinelOps overview"><span className="brand-mark"><Shield size={20} /></span><span><b>SentinelOps</b><small>CONFIGURATION MONITOR</small></span></a>
        <div className="topbar-right">
          <span className={`connection ${readiness?.status === "ready" ? "is-up" : "is-down"}`}><span className="pulse-dot" />{readiness?.status === "ready" ? "Monitor connected" : "Monitor unavailable"}</span>
          <button className="icon-button" onClick={() => void refresh()} disabled={loading || busy} aria-label="Refresh dashboard"><RefreshCw size={16} className={loading ? "spin" : ""} /></button>
        </div>
      </header>

      <div className="content" id="top">
        <section className="hero-row">
          <div><div className="eyebrow"><Activity size={14} /> FLEET OPERATIONS <span className="live-tag">LIVE API DATA</span></div><h1>Configuration posture</h1><p>Track approved server state, assess drift, and walk through a controlled demo.</p></div>
          <button className="primary-button" onClick={() => void runScan()} disabled={busy || !readiness}><Play size={15} fill="currentColor" />{busy ? "Working…" : "Run scan"}</button>
        </section>

        {(error || notice) && <div className={`banner ${error ? "banner-error" : "banner-notice"}`} role="status">{error ? <AlertTriangle size={17} /> : <Check size={17} />}<span>{error || notice}</span>{error && <button onClick={() => setError("")} aria-label="Dismiss error">×</button>}</div>}

        <section className="metric-grid" aria-label="Latest scan summary">
          <Metric label="Registered servers" value={servers.length} tone="tone-blue" icon={Server} />
          <Metric label="Clean" value={activeScan ? activeScan.servers.filter((item) => item.outcome === "clean").length : "—"} tone="tone-green" icon={Shield} />
          <Metric label="Drifted" value={activeScan?.drifted ?? "—"} tone="tone-red" icon={ShieldAlert} />
          <Metric label="Pending baseline" value={pendingCount} tone="tone-amber" icon={CircleHelp} />
          <Metric label="Unable to assess" value={activeScan?.unable_to_assess ?? activeScan?.failed ?? "—"} tone="tone-slate" icon={WifiOff} />
        </section>

        <section className="overview-strip">
          <div className="overview-item"><span className="overview-icon green"><Activity size={16} /></span><div><small>Application health</small><strong>{health?.status ?? "Unknown"}</strong><span>{health?.dependencies_checked ? "Dependencies checked" : "Process status only"}</span></div></div>
          <div className="overview-item"><span className="overview-icon blue"><Database size={16} /></span><div><small>Monitor readiness</small><strong>{readiness?.status ?? "Unknown"}</strong><span>{readiness?.database === "ready" ? "SQLite available" : "Database not confirmed"}</span></div></div>
          <div className="overview-item"><span className="overview-icon violet"><Clock3 size={16} /></span><div><small>Latest scan</small><strong>{latestScan ? formatDate(latestScan.completed_at) : "No saved scans"}</strong><span>{latestScan ? `${latestScan.status} · ${latestScan.scan_id.slice(0, 8)}` : "Run a scan to create history"}</span></div></div>
          <div className="overview-item"><span className="overview-icon amber"><Wifi size={16} /></span><div><small>Last dashboard refresh</small><strong>{dataLoadedAt ? formatDate(dataLoadedAt) : "Loading"}</strong><span>{readiness?.demo_controls_enabled ? "Demo controls enabled" : "Demo controls disabled"}</span></div></div>
        </section>

        <section className="section-card inventory-card">
          <div className="section-heading"><div><div className="section-kicker">FLEET</div><h2>Server inventory</h2><p>Each server is compared with its own explicitly approved baseline.</p></div><span className="count-chip">{servers.length} servers</span></div>
          {loading && servers.length === 0 ? <div className="empty-state"><RefreshCw size={19} className="spin" />Loading server inventory…</div> : servers.length === 0 ? <div className="empty-state">No registered servers were returned by the Monitor.</div> : (
            <div className="table-wrap"><table><thead><tr><th>SERVER</th><th>ROLE</th><th>ENROLLMENT</th><th>COLLECTION</th><th>BASELINE</th><th>OBSERVED FINGERPRINT</th></tr></thead><tbody>
              {servers.map((server) => {
                const collected = fleetById.get(server.server_id);
                return <tr key={server.server_id}>
                  <td><div className="server-name"><span className="server-avatar"><Server size={15} /></span><span><b>{server.display_name}</b><small>{server.server_id}</small></span></div></td>
                  <td><span className="role-label">{server.role ?? "Unknown"}</span></td>
                  <td><StatusPill value={server.status} /></td>
                  <td><StatusPill value={collected?.status === "success" ? "reachable" : collected?.status === "failed" ? "unreachable" : server.reachability} /></td>
                  <td>{server.current_baseline_version ? <span className="baseline-version">v{server.current_baseline_version}</span> : <span className="muted">Awaiting approval</span>}</td>
                  <td><span className="fingerprint"><Fingerprint size={13} />{shortHash(collected?.fingerprint)}</span></td>
                </tr>;
              })}
            </tbody></table></div>
          )}
          <div className="table-foot"><span>Latest successful collection state from the Monitor</span><span>Refresh: {dataLoadedAt ? new Date(dataLoadedAt).toLocaleTimeString() : "—"}</span></div>
        </section>

        <section className="two-column">
          <div className="section-card enrollment-card">
            <div className="section-heading compact"><div><div className="section-kicker">TRUSTED CONFIGURATION</div><h2>Baseline enrollment</h2><p>Review and approve each server independently.</p></div><span className="count-chip amber-chip">{Object.values(candidates).flat().length} pending</span></div>
            <div className="actor-field"><label htmlFor="audit-actor">Approval actor <span>audit metadata only</span></label><input id="audit-actor" value={actor} maxLength={128} onChange={(event) => setActor(event.target.value)} placeholder="operator name" /></div>
            <div className="disclosure"><AlertTriangle size={15} /><span>There is no real authentication in this MVP. This name records who performed the demo action; it does not authorize the request.</span></div>
            <div className="candidate-list">
              {servers.flatMap((server) => (candidates[server.server_id] ?? []).map((candidate) => <article className="candidate" key={candidate.baseline_id}>
                <div className="candidate-top"><div><b>{server.display_name}</b><span>{server.server_id} · candidate v{candidate.version}</span></div><StatusPill value="pending" /></div>
                <div className="candidate-meta"><span>Service group <b>{candidate.snapshot.service_group}</b></span><span>Created {formatDate(candidate.created_at)}</span></div>
                <div className="snapshot-preview"><span>CONFIG</span>{Object.entries(candidate.snapshot.config).map(([key, value]) => <code key={key}>{key}: {String(value)}</code>)}<span>ENVIRONMENT</span>{Object.entries(candidate.snapshot.environment).map(([key, value]) => <code key={key}>{key}: {String(value)}</code>)}</div>
                <div className="candidate-bottom"><span className="fingerprint"><Fingerprint size={13} />{shortHash(candidate.fingerprint)}</span><button className="approve-button" disabled={busy || !actor.trim()} onClick={() => void approve(server, candidate)}>{operation?.serverId === server.server_id && operation.kind === "approve" ? <RefreshCw size={14} className="spin" /> : <Check size={14} />}Approve baseline</button></div>
              </article>))}
              {Object.values(candidates).flat().length === 0 && <div className="empty-inline"><Check size={17} />No pending candidate. New server state is never approved automatically.</div>}
            </div>
          </div>

          <div className="section-card demo-card">
            <div className="section-heading compact"><div><div className="section-kicker">DEMO ONLY</div><h2>Drift controls</h2><p>Change a simulated server, scan it, then restore it.</p></div><span className={`mode-chip ${readiness?.demo_controls_enabled ? "mode-on" : "mode-off"}`}>{readiness?.demo_controls_enabled ? "ENABLED" : "DISABLED"}</span></div>
            <div className="demo-warning"><ShieldAlert size={18} /><div><b>Predefined scenarios only</b><span>These actions call the server&apos;s allowlisted demo endpoints. No arbitrary commands are accepted.</span></div></div>
            <label className="select-label" htmlFor="scenario">Drift scenario</label>
            <div className="select-wrap"><select id="scenario" value={scenario} onChange={(event) => setScenario(event.target.value)} disabled={!readiness?.demo_controls_enabled || busy}><option value="security_drift">Security drift · debug mode</option><option value="performance_drift">Performance drift · connection pool</option></select><ChevronDown size={15} /></div>
            <div className="demo-server-list">{servers.map((server) => <div className="demo-server" key={server.server_id}><div className="demo-server-id"><span className="server-avatar"><Server size={14} /></span><span><b>{server.display_name}</b><small>{server.server_id} · {server.current_baseline_id ? `baseline v${server.current_baseline_version}` : "baseline required for assessment"}</small></span></div><div className="demo-actions"><button className="drift-button" disabled={!readiness?.demo_controls_enabled || busy} onClick={() => void performDemoAction(server, "drift")} title={readiness?.demo_controls_enabled ? "Apply predefined demo drift" : "Enable DEMO_MODE on the Monitor and server containers"}>{operation?.serverId === server.server_id && operation.kind === "drift" ? <RefreshCw size={14} className="spin" /> : <AlertTriangle size={14} />}Drift</button><button className="reset-button" disabled={!readiness?.demo_controls_enabled || busy} onClick={() => void performDemoAction(server, "reset")} title="Reset and verify original state">{operation?.serverId === server.server_id && operation.kind === "reset" ? <RefreshCw size={14} className="spin" /> : <RotateCcw size={14} />}Reset</button></div></div>)}</div>
            {!readiness?.demo_controls_enabled && <div className="empty-inline"><CircleHelp size={16} />Demo controls are disabled by the Monitor configuration.</div>}
          </div>
        </section>

        <section className="two-column history-grid">
          <div className="section-card history-card">
            <div className="section-heading compact"><div><div className="section-kicker">PERSISTED RESULTS</div><h2>Scan history</h2><p>Saved assessments include the baseline version used.</p></div><span className="count-chip">{history.length} recent</span></div>
            {history.length === 0 ? <div className="empty-state">No persisted scans yet. Run a scan to begin.</div> : <div className="history-list">{history.map((scan) => <button className={`history-row ${selectedScanId === scan.scan_id ? "selected" : ""}`} key={scan.scan_id} onClick={() => void openScan(scan.scan_id)}><span className="history-state"><span className={`result-dot ${scan.drifted ? "dot-red" : scan.failed ? "dot-amber" : "dot-green"}`} /><span><b>{formatDate(scan.completed_at)}</b><small>{scan.scan_id.slice(0, 13)}</small></span></span><span className="history-counts"><b className={scan.drifted ? "text-red" : ""}>{scan.drifted} drift</b><span>{scan.pending_baseline} pending · {scan.failed} failed</span></span><StatusPill value={scan.status} /></button>)}</div>}
          </div>

          <div className="section-card findings-card">
            <div className="section-heading compact"><div><div className="section-kicker">DETERMINISTIC EVIDENCE</div><h2>Assessment details</h2><p>{selectedScanId ? `Scan ${selectedScanId.slice(0, 13)}` : "Select a saved scan"}</p></div><span className="count-chip">{scanServers.length} assessed</span></div>
            {!activeScan ? <div className="empty-state"><ArrowDownToLine size={17} />Run or select a scan to view its findings.</div> : <div className="assessment-list">{activeScan.servers.map((assessment) => <details className="assessment" key={`${activeScan.scan_id}-${assessment.server_id}`} open={assessment.outcome === "drifted"}>
              <summary><span className="assessment-title"><span className={`result-dot ${assessment.outcome === "drifted" ? "dot-red" : assessment.outcome === "clean" ? "dot-green" : "dot-amber"}`} /><b>{servers.find((item) => item.server_id === assessment.server_id)?.display_name ?? assessment.server_id}</b><small>{assessment.server_id}</small></span><span className="assessment-provenance">{assessment.baseline_version ? `baseline v${assessment.baseline_version}` : "No approved baseline"}</span><StatusPill value={assessment.outcome} /></summary>
              <div className="assessment-body">
                {assessment.collection_error && <div className="collection-error"><WifiOff size={15} /><span>Collection failed ({assessment.collection_error.kind}): {assessment.collection_error.message}</span></div>}
                {assessment.assessment_error && <div className="collection-error"><AlertTriangle size={15} /><span>{assessment.assessment_error}</span></div>}
                {assessment.findings.map((finding, index) => <div className="finding" key={`${finding.evidence.field_path}-${index}`}><div className="finding-heading"><code>{finding.evidence.field_path}</code><span className={`severity severity-${finding.assessment.severity.toLowerCase()}`}>{finding.assessment.severity}</span></div><div className="evidence-pair"><span><small>EXPECTED</small><code>{describeValue(finding.evidence.expected)}</code></span><span><small>OBSERVED</small><code>{describeValue(finding.evidence.actual)}</code></span></div>{finding.assessment.policy_ids.length > 0 && <div className="policy-ref">Policy: {finding.assessment.policy_ids.join(", ")}</div>}</div>)}
                {assessment.policy_violations.map((violation, index) => <div className="finding policy-finding" key={`${violation.policy_id}-${index}`}><div className="finding-heading"><code>{violation.field_path}</code><span className={`severity severity-${violation.severity.toLowerCase()}`}>{violation.severity}</span></div><p>Policy violation: {violation.policy_id}</p><div className="evidence-pair"><span><small>OBSERVED</small><code>{describeValue(violation.observed)}</code></span></div></div>)}
                {assessment.outcome === "clean" && <div className="clean-note"><Check size={15} />Configuration matches approved baseline.</div>}
                {assessment.outcome === "pending_baseline" && <div className="empty-inline"><CircleHelp size={15} />Assessment is pending explicit baseline approval.</div>}
                {assessment.findings.length === 0 && assessment.policy_violations.length === 0 && assessment.outcome === "unable_to_assess" && !assessment.collection_error && <div className="empty-inline"><CircleHelp size={15} />No assessment evidence is available.</div>}
              </div>
            </details>)}</div>}
          </div>
        </section>

        <footer className="footer-note"><span><Database size={14} /> Data is returned by the live Monitor API and saved to SQLite.</span><span>Audit actors are not authenticated · No automatic remediation</span></footer>
      </div>
    </main>
  );
}
