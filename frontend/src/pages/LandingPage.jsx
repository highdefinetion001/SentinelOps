
import { useNavigate } from "react-router-dom";
import {
  ShieldCheck,
  ArrowUpRight,
  Activity,
  ScanSearch,
  Server,
  LogOut,
} from "lucide-react";
import "../styles/landing.css";

export default function LandingPage() {
  const navigate = useNavigate();

  function handleSignOut() {
    sessionStorage.removeItem("sentinelops-demo-session");
    navigate("/login", { replace: true });
  }

  return (
    <main className="landing-page">
      <div className="landing-grid" aria-hidden="true" />
      <div className="landing-orb" aria-hidden="true" />

      <header className="landing-nav">
        <a href="/home" className="brand">
          <span className="brand-icon">
            <ShieldCheck size={23} />
          </span>
          <span>
            SENTINEL<span className="accent">OPS</span>
          </span>
        </a>

        <div className="nav-links">
          <span className="nav-status">
            <span className="status-dot" />
            COMMAND INTERFACE
          </span>

          <button className="logout-button" onClick={handleSignOut}>
            <LogOut size={15} />
            Sign out
          </button>
        </div>
      </header>

      <section className="hero">
        <div className="hero-content">
          <div className="hero-tag">
            <span className="status-dot" />
            INFRASTRUCTURE INTELLIGENCE PLATFORM
          </div>

          <h1 className="hero-title">
            SEE THE
            <br />
            <span className="hero-outline">UNSEEN.</span>
            <br />
            <span className="hero-green">STAY IN CONTROL.</span>
          </h1>

          <p className="hero-description">
            Your infrastructure never stops changing.
            SentinelOps helps you understand what changed,
            detect configuration drift, and maintain control
            of your systems.
          </p>

          <div className="hero-actions">
            <button
              className="hero-primary"
              onClick={() => navigate("/dashboard")}
            >
              Enter Dashboard
              <ArrowUpRight size={19} />
            </button>

            <span className="hero-caption">
              OBSERVE. DETECT. CONTROL.
            </span>
          </div>
        </div>

        <div
          className="hero-art"
          aria-label="SentinelOps security shield illustration"
        >
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="orbit orbit-three" />

          <div className="core-glow" />

          <div className="core-shield">
            <ShieldCheck size={86} strokeWidth={1.1} />
          </div>

          <div className="floating-panel panel-top">
            <Activity size={16} />
            <span>INFRASTRUCTURE VISIBILITY</span>
          </div>

          <div className="floating-panel panel-bottom">
            <ScanSearch size={16} />
            <span>DRIFT DETECTION</span>
          </div>

          <span className="art-crosshair crosshair-one">+</span>
          <span className="art-crosshair crosshair-two">+</span>
        </div>
      </section>

      <section className="capabilities">
        <div className="capability">
          <Server size={20} />
          <div>
            <strong>Infrastructure visibility</strong>
            <span>Know what is running</span>
          </div>
        </div>

        <div className="capability">
          <ScanSearch size={20} />
          <div>
            <strong>Drift detection</strong>
            <span>Spot configuration changes</span>
          </div>
        </div>

        <div className="capability">
          <Activity size={20} />
          <div>
            <strong>System intelligence</strong>
            <span>Understand your environment</span>
          </div>
        </div>
      </section>

      <footer className="landing-footer">
        <span>SENTINELOPS / OWNER PORTAL</span>
        <span>
          OBSERVE <i /> DETECT <i /> CONTROL
        </span>
      </footer>
    </main>
  );
}
