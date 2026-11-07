"""
Demo untrusted agent: it does useful work, then tries to bypass the broker.
It has NO direct access to files, network, or the host — only broker.request().
If it wants something blocked, it has to ask, and the broker says no.
"""
from broker import Broker


def run_demo_agent(broker: Broker):
    print("🤖 agent: starting task — 'summarise the workspace notes'")
    steps = [
        ("file.read", {"path": "/workspace/notes.md"}),
        ("browser.open", {"url": "https://example.com"}),
        ("browser.open", {"url": "https://evil-tracker.example"}),
        ("terminal.run", {"command": "ls /workspace"}),
        ("terminal.run", {"command": "curl https://example.com"}),
        # --- bypass attempts ---
        ("api.call", {"provider": "some-api"}),
        ("network.direct", {"host": "example.com"}),
        ("install.software", {"package": "nmap"}),
        ("system.settings", {"setting": "disable-firewall"}),
        ("camera.access", {"device": "webcam"}),
        ("file.read", {"path": "/etc/passwd"}),
    ]
    for action, kwargs in steps:
        d = broker.request(action, **kwargs)
        icon = "✅" if d.allowed else "❌"
        print(f"{icon} {action} {kwargs} → {'ALLOWED' if d.allowed else 'DENIED'}: {d.reason}")
