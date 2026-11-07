"""Run:  python3 demo.py   — from veil-core/"""
from pathlib import Path
from broker import Broker, AUDIT_LOG
from agent_sim import run_demo_agent

if __name__ == "__main__":
    if AUDIT_LOG.exists():
        AUDIT_LOG.unlink()
    broker = Broker(Path(__file__).parent / "policy.example.json")
    print("VEIL Permission Broker — demo")
    print("Policy: browser allowed (example.com, wikipedia.org) · API blocked ·")
    print("        install blocked · system settings blocked · camera blocked")
    print("        internet is broker-only — direct network is denied")
    print("-" * 70)
    run_demo_agent(broker)
    print("-" * 70)
    print(f"Audit log written to {AUDIT_LOG} — every request, allowed or denied.")
