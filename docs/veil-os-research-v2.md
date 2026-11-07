# VEIL OS: Technical Research and Build Strategy

**Project definition:** An Arch-based Linux operating system designed for AI agents, with humans in control.

## The corrected thesis

VEIL should not be marketed or architected as “an AI that controls the whole OS.” That framing creates the wrong security model. The stronger idea is:

> **VEIL is a Linux desktop that exposes controlled operating-system capabilities to AI agents through an independent permission broker.**

The model may plan tasks and request actions. It must not become the security authority. The broker, operating-system policy, and isolated executors decide what can actually happen.

That is a meaningful product distinction from ordinary AI desktop assistants. Most computer-use agents imitate a human: they inspect screenshots, click, type, and hope the application behaves as expected. VEIL can make the computer itself understandable to the AI through structured resources, typed operations, accessibility trees, application adapters, and reversible workspaces. Pixel-based control remains available for compatibility, but it should not be the primary interface.

## What VEIL should own versus reuse

### Reuse Linux and Arch for the foundation

Arch is a reasonable development base because it provides a minimal system, current kernels, systemd, pacman, a large package ecosystem, and straightforward custom-image tooling. Arch is a rolling-release distribution, however, so it introduces update and compatibility risk for a product that promises a stable security boundary. The first VEIL release should therefore use Arch for development but curate and test a pinned VEIL package set rather than blindly tracking every update. [1]

VEIL should own the user experience, agent runtime, capability vocabulary, broker, environment manager, security center, audit journal, and recovery workflow. It should not initially own a new kernel, package manager, display protocol, browser engine, or general-purpose container runtime.

### The real VEIL innovation is the capability layer

A normal desktop gives applications ambient access through files, sockets, processes, D-Bus, devices, and credentials. VEIL should replace ambient access with explicit capabilities:

```json
{
  "capability": "browser.navigate",
  "scope": {
    "domains": ["docs.example.com", "github.com"],
    "methods": ["GET", "POST"]
  },
  "session": "task-8f21",
  "expires_at": "2026-11-06T01:00:00Z",
  "approval": "required-for-write"
}
```

A capability needs at least five properties: **who** receives it, **what** it permits, **which resources** it covers, **how long** it lasts, and **what risk class** it has. Permissions should expire by default and be bound to a task or workspace instead of becoming permanent global settings.

## VEIL architecture

Use six layers, but make the trust boundaries explicit.

### 1. Host Linux layer

This is the Arch kernel, firmware, drivers, storage, networking, systemd, compositor, and login manager. Keep it as small and ordinary as possible. The AI should have no direct host root session.

The host owns hardware and the final enforcement points. A model instruction cannot override host firewall rules, mount restrictions, process ownership, or kernel security policy.

### 2. VEIL system services

These are signed, locally installed services:

- `veil-broker`: the independent policy and authorization service.
- `veil-exec`: the only service allowed to launch task executors.
- `veil-net`: the network policy and egress proxy layer.
- `veil-secrets`: short-lived credential release and redaction service.
- `veil-journal`: append-only action and policy event log.
- `veil-snapshot`: workspace checkpoint and rollback service.
- `veil-session`: task lifecycle, quotas, cancellation, and cleanup.

The model runtime should communicate with `veil-broker` over a narrow local RPC interface. It should not be able to call `systemd`, D-Bus, Docker, podman, pacman, the host filesystem, or arbitrary sockets directly.

### 3. AI runtime

The AI runtime plans and observes. It may use local models, cloud models, or both. It should be model-independent and should treat all tool output, webpage content, documents, filenames, and terminal output as untrusted data.

The runtime should produce structured proposals:

```text
propose(
  action = "filesystem.write",
  target = "/workspace/research/report.md",
  expected_effect = "create or replace one Markdown file",
  rollback = "snapshot-required"
)
```

The broker then returns one of four outcomes: `allow`, `deny`, `needs_user_confirmation`, or `needs_step_up_authentication`.

### 4. Prepared environments

This is one of VEIL’s strongest ideas. Do not make every task begin with an AI searching for applications and installing dependencies. Ship reproducible environment profiles:

- **Developer:** editor, language runtimes, Git, test runner, terminal adapter.
- **Researcher:** isolated browser, notes, citation workspace, download quarantine.
- **Analyst:** Python, notebooks, approved dataset locations, plotting tools.
- **Designer:** graphics applications, asset workspace, export folder.
- **Assistant:** documents, calendar connector, mail connector, no shell by default.

Each profile should be a signed manifest that describes packages, tools, filesystem mounts, network policy, device policy, model policy, and reset behavior. An environment is not merely a folder of applications. It is a permission bundle plus an isolated workspace.

### 5. Desktop and security center

The VEIL desktop should make the active security state visible:

- Current task and objective.
- Active capabilities and expiration times.
- Files and domains currently in scope.
- Pending approvals.
- Recent actions and results.
- Snapshot and rollback controls.
- A “pause AI” and “kill task” control that does not depend on the model.

The security center must not be a decorative set of toggles. Turning off network access must remove the actual network capability, stop or reconfigure the egress path, and be reflected in the executor. The UI is only a view and control client for policy state.

### 6. Executor layer

Use different isolation levels based on risk:

- **Low-risk local adapters:** a process with Landlock, seccomp, resource limits, and a carefully scoped environment.
- **Desktop applications:** Flatpak-style sandboxing and portals where compatible.
- **Untrusted code, web content, plugins, or autonomous tasks:** a container or microVM.
- **Host administration:** a separate privileged helper with explicit, narrow operations; never a general root shell.

## Which Linux primitives fit VEIL

### Landlock: best for per-process capability restrictions

Landlock is a Linux security module that lets even unprivileged processes restrict their own ambient rights. Current kernel documentation describes filesystem, network-port, capability, and namespace rules. This makes it a strong fit for a VEIL executor that must say, for example, “this process may read `/workspace/input`, write `/workspace/output`, and connect only to approved ports.” [2]

Landlock is not enough for domain-level internet policy. It understands network ports, not whether a connection is really going to `github.com`. VEIL therefore needs an egress proxy or firewall layer that can enforce domains, DNS behavior, TLS policy, and request methods. The broker should grant a domain capability to the proxy, not directly to arbitrary processes.

### Bubblewrap: excellent prototype substrate, not the whole policy

Bubblewrap builds unprivileged sandboxes using user namespaces and other Linux namespaces. Its own documentation makes an important warning: bubblewrap constructs a sandbox, but the command-line arguments supplied by the caller define the actual security policy. Mounting a D-Bus socket, host directory, device, or powerful IPC endpoint can undermine the boundary. [3]

Use bubblewrap in VEIL Core because it is lightweight and fast to iterate with. Do not let the model compose bubblewrap arguments. `veil-exec` should generate a fixed profile from a validated manifest.

### Flatpak portals: borrow the user-consent pattern

Flatpak applications start with very limited access to host files, devices, processes, network, and services. Portals provide controlled access to resources such as file selection, URI opening, screenshots, notifications, and printing. The user’s selection can grant access to a specific resource instead of granting an entire home directory. [4]

VEIL should learn from this model but cannot simply treat Flatpak permissions as its complete agent permission system. Flatpak permissions are application-oriented and often static. VEIL needs task-oriented, expiring, auditable capabilities such as “allow this agent to read these three files for 20 minutes.” A VEIL portal service can provide that missing dynamic layer.

### Seccomp: reduce attack surface, never use it alone

Seccomp reduces the set of Linux system calls a process may invoke. The Linux kernel documentation explicitly says system-call filtering is not itself a sandbox. Pair it with namespaces, filesystem restrictions, dropped privileges, cgroups, device controls, and network isolation. [5]

### MicroVMs: the stronger boundary

Firecracker is designed for fast, isolated multi-tenant workloads. Its project describes a small device model, KVM isolation, sub-125 ms startup claims, low overhead, resource rate limiting, and a jailer defense layer. A VEIL task VM could contain a full prepared environment while protecting the host from more dangerous workloads than a simple process sandbox. [6]

Start with bubblewrap and Landlock to move quickly. Add Firecracker when VEIL runs arbitrary code, hostile downloads, untrusted plugins, or multiple mutually untrusted users.

## Browser and network design

The browser is one of the highest-risk components because webpages can contain indirect prompt injections. A page can tell the model to ignore the user, exfiltrate files, or visit a malicious URL. The agent must treat webpage text as data, not authority.

VEIL Browser should have:

1. A separate browser profile per task.
2. A separate filesystem and download quarantine.
3. A broker-controlled egress proxy.
4. Domain allowlists and explicit handling for redirects.
5. No access to host cookies, password stores, SSH agents, or personal browser profiles.
6. A confirmation boundary before login, upload, publish, purchase, send, or consent actions.
7. A record of the page, domain, action, and policy decision.

A simple “internet allowed” switch is not enough. The useful policy states are `offline`, `approved-domains`, `research-web`, and `unrestricted-with-confirmation`, with network requests routed through an enforcement component.

## Tool hierarchy: faster than screenshot control

VEIL should use this order:

1. **Native semantic adapter:** direct API or structured application state.
2. **Accessibility tree:** inspect roles, labels, values, and actions instead of pixels.
3. **Command or RPC adapter:** a restricted operation with a typed schema.
4. **Screenshot and mouse/keyboard:** fallback for applications with no usable structure.

This is supported by the direction of current tooling. Vercel Labs’ `agent-desktop`, for example, exposes desktop applications through accessibility trees and stable element references rather than relying only on screenshots or pixel matching. Its Linux support uses AT-SPI, while the model loop remains outside the tool. [7]

VEIL’s own advantage should be that its desktop is designed with accessibility and semantic state from the beginning. Every VEIL-native app should expose an action schema and state model to the broker.

## Security rule that must be non-negotiable

The AI can be powerful, but it can never be more powerful than the permissions the user grants it.

Implement this as an external invariant, not a prompt instruction:

```text
model proposal
      |
      v
veil-broker: identity + capability + target + risk + expiry + approval
      |
      v
veil-exec: isolated executor + quotas + network/filesystem/device policy
      |
      v
result + audit event + optional rollback
```

The broker must fail closed when policy evaluation, approval verification, audit logging, or resource resolution fails. Approval must bind to the exact normalized action—not merely to a vague task name. This matches current agent-security guidance from OWASP, which recommends least-privilege tools, external authorization middleware, exact-action approval binding, prompt-injection defenses, output validation, rate limits, and audit logs. [8]

## Arch-specific product strategy

### Development version: Arch-based sandbox first

Do not begin by building a polished ISO. Build VEIL as a set of packages and services on a normal Arch installation or an Arch VM:

```text
veil-core
veil-broker
veil-exec
veil-net
veil-secrets
veil-journal
veil-snapshot
veil-desktop
veil-env-developer
veil-env-researcher
veil-cli
```

Build the first user experience as a local desktop application plus a command-line interface. This makes it possible to test the core idea on existing Linux systems and makes debugging much easier than debugging a bootable distribution.

### Distribution version: use Archiso when the core works

Archiso is specifically intended for customizable Arch live images, installers, rescue systems, and bootstrap artifacts. It can start from a baseline or release profile, include selected packages, add files to the image, and build a custom ISO with `mkarchiso`. That is the correct route for a VEIL installer or live environment once the services are stable. [9]

For reproducible development and specialized disk images, compare Archiso with `mkosi`, which can build bespoke images using package managers including pacman. [10]

### Stability recommendation

The first VEIL ISO should not simply be “latest Arch plus AI packages.” Treat the base system as a tested release:

- Pin a known-good package snapshot.
- Build and sign VEIL packages in a dedicated repository.
- Test kernel, compositor, portals, browser, broker, and executor together.
- Provide a recovery image and rollback path.
- Test updates inside a cloned environment before applying them to the host.
- Keep the host OS minimal; put applications inside prepared environments.

Arch gives VEIL speed and flexibility during development. VEIL must add the release engineering and recovery discipline that a rolling distribution does not provide automatically.

## Recommended VEIL MVP

The first convincing milestone should be one complete task, not “AI controls everything.”

**MVP task:** The user selects the Developer environment and says: “Create a small project, write the requested code, run the tests, and show me the result.”

VEIL should then:

1. Create a fresh workspace snapshot.
2. Start an isolated executor.
3. Grant the AI only workspace read/write and approved package-cache access.
4. Start the editor and terminal adapters.
5. Let the model use semantic tools first.
6. Require confirmation for network access or package installation.
7. Run tests with CPU, memory, process, and time limits.
8. Show every action in the security center.
9. Offer rollback or preserve the workspace as a named artifact.

After that, implement a Researcher environment with an isolated browser, approved-domain proxy, download quarantine, notes, and citations. These two environments demonstrate the core promise better than a broad but unreliable “AI controls the desktop” demo.

## Build order

### Stage 1: VEIL Core

Implement the broker, capability schema, task IDs, policy decisions, JSONL journal, cancellation, expiration, and a small CLI. The broker should be usable without any AI model so it can be tested deterministically.

### Stage 2: VEIL Sandbox

Implement workspace creation, snapshots, bubblewrap profiles, Landlock restrictions, seccomp profiles, cgroups, and a default-deny network namespace. Add tests that prove blocked file, process, device, and network operations fail.

### Stage 3: VEIL Runtime

Add one model provider, structured tool calls, plan state, observation, retries, and recovery. Add prompt-injection test fixtures. Keep model output untrusted and validate every action.

### Stage 4: VEIL Desktop

Build the task view and Security Center. Make active capabilities, pending approvals, recent actions, and rollback visible. Add environment selection and reset.

### Stage 5: Native adapters

Add filesystem, process, browser, editor, terminal, and accessibility-tree adapters. Use screenshots only when a semantic interface is not available.

### Stage 6: VEIL OS image

Create the Archiso profile, installer, signed repository, recovery path, update channel, and hardware compatibility list. Only at this stage should “VEIL OS” become the primary bootable distribution rather than the name of a software stack.

## Evaluation plan

Use executable tests rather than judging the model’s explanations. OSWorld provides a useful reference because it evaluates real workflows across operating systems and applications with setup and execution-based evaluation. Its published baseline illustrates that computer agents can sound capable while still failing many end-state tasks. [11]

VEIL should measure:

- Task completion rate.
- Time and number of model calls.
- Semantic-tool versus pixel-tool usage.
- Recovery after a failed action.
- Unauthorized-action rate.
- Prompt-injection resistance.
- Data-exfiltration resistance.
- Permission-expiration correctness.
- Snapshot and rollback success.
- CPU, memory, disk, and network quota enforcement.
- Update and recovery success.

The most important security tests are negative tests: prove the AI cannot read a blocked file, connect to a blocked domain, use a host credential, access a disabled device, or turn a denied tool call into an equivalent shell command.

## Final recommendation

Keep the name **VEIL OS** and the central rule. Change the implementation emphasis:

> **VEIL is not a new kernel with an AI attached. It is a capability-secured Linux desktop that makes AI a first-class user of the computer without making AI the owner of the computer.**

Build VEIL Core on Arch first. Use a broker with an external policy engine. Use Landlock, namespaces, cgroups, seccomp, and carefully generated bubblewrap profiles for the first sandbox. Use portals and accessibility trees for structured interaction. Put domain policy in a network proxy. Use Firecracker for high-risk or multi-tenant execution. Use Archiso only after the core system works.

That path preserves your larger OS vision while giving you a product that can be demonstrated, tested, and improved much sooner.

## References

[1]: https://wiki.archlinux.org/title/Arch_Linux "Arch Linux — ArchWiki"
[2]: https://docs.kernel.org/userspace-api/landlock.html "Landlock: unprivileged access control — Linux Kernel Documentation"
[3]: https://github.com/containers/bubblewrap "Bubblewrap — containers/bubblewrap"
[4]: https://docs.flatpak.org/en/latest/sandbox-permissions.html "Sandbox Permissions — Flatpak Documentation"
[5]: https://docs.kernel.org/userspace-api/seccomp_filter.html "Seccomp BPF — Linux Kernel Documentation"
[6]: https://firecracker-microvm.github.io/ "Firecracker — Secure and fast microVMs"
[7]: https://github.com/vercel-labs/agent-desktop "agent-desktop — Vercel Labs"
[8]: https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html "AI Agent Security Cheat Sheet — OWASP"
[9]: https://wiki.archlinux.org/title/Archiso "Archiso — ArchWiki"
[10]: https://mkosi.systemd.io/ "mkosi — Build Bespoke OS Images"
[11]: https://os-world.github.io/ "OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments"
