// Menu bar companion for claude-rotator.
//
// Reads ~/.claude-rotator/state.json (written by `claude-rotator serve` every ~30 s)
// and shows the active account plus 5h/weekly usage. It never touches credentials:
// actions shell out to launchctl and to the CLI command recorded in the state file.
//
// Build: `claude-rotator install` (or see build_app() in claude_rotator/service.py).

import AppKit
import SwiftUI

let rotatorHome = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude-rotator")
let serveLabel = "com.user.claude-rotator"

struct UsageRow: Decodable {
    let name: String
    let session_pct: Double?
    let session_reset: String?
    let weekly_pct: Double?
    let weekly_reset: String?
    let error: String?
    let source: String?
}

struct RotatorState: Decodable {
    let current: String?
    let usages: [String: UsageRow]?
    let blocked_until: [String: Double]?
    let dead: [String: String]?
    let requests: [String: Int]?
    let updated_at: String?
    let accounts: [String]?
    let primary: String?
    let emails: [String: String?]?
    let next_ping: String?
    let command: [String]?  // argv that runs the CLI, e.g. [python, "-m", "claude_rotator"]
}

private let isoParser = ISO8601DateFormatter()

func parseDate(_ s: String?) -> Date? {
    guard let s else { return nil }
    return isoParser.date(from: s)
}

func fmtReset(_ date: Date?) -> String {
    guard let date else { return "–" }
    let secs = date.timeIntervalSinceNow
    if secs <= 0 { return "expired" }
    let mins = Int(secs / 60)
    let h = mins / 60, m = mins % 60
    return h >= 24 ? "\(h / 24)d \(h % 24)h" : "\(h)h \(String(format: "%02d", m))m"
}

@MainActor
final class Model: ObservableObject {
    @Published var state: RotatorState?
    @Published var message: String?
    @Published var busy = false
    private var timer: Timer?

    init() {
        reload()
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.reload() }
        }
        // Refresh straight after wake instead of waiting for the next tick.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in Task { @MainActor in self?.reload() } }
    }

    func reload() {
        guard let data = try? Data(contentsOf: rotatorHome.appendingPathComponent("state.json")) else {
            state = nil
            return
        }
        state = try? JSONDecoder().decode(RotatorState.self, from: data)
    }

    var updatedAt: Date? { parseDate(state?.updated_at) }

    /// serve rewrites the state file every ~30 s; older than 2 min means it isn't running.
    var running: Bool {
        guard let u = updatedAt else { return false }
        return Date().timeIntervalSince(u) < 120
    }

    var accounts: [String] { state?.accounts ?? (state?.usages.map { Array($0.keys).sorted() } ?? []) }

    func usage(_ name: String) -> UsageRow? { state?.usages?[name] }

    func email(_ name: String) -> String? { state?.emails?[name] ?? nil }

    var labelText: String {
        guard running else { return "off" }
        guard let cur = state?.current else { return "–" }
        if let pct = usage(cur)?.session_pct { return "\(cur) \(Int(pct.rounded()))%" }
        return cur
    }

    func blockedUntil(_ name: String) -> Date? {
        guard let t = state?.blocked_until?[name] else { return nil }
        let d = Date(timeIntervalSince1970: t)
        return d > Date() ? d : nil
    }

    // MARK: actions

    func pingNow() {
        guard let cmd = state?.command, let exe = cmd.first else {
            message = "Start the service first."
            return
        }
        run(exe, Array(cmd.dropFirst()) + ["prime"], label: "Ping")
    }

    func restartService() {
        run("/bin/launchctl", ["kickstart", "-k", "gui/\(getuid())/\(serveLabel)"], label: "Restart")
    }

    func openLog() {
        NSWorkspace.shared.open(rotatorHome.appendingPathComponent("serve.log"))
    }

    func openConfig() {
        NSWorkspace.shared.open(rotatorHome)
    }

    private func run(_ exe: String, _ args: [String], label: String) {
        busy = true
        message = "\(label)…"
        DispatchQueue.global().async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: exe)
            p.arguments = args
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            var text: String
            do {
                try p.run()
                p.waitUntilExit()
                let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                let last = out.split(separator: "\n").last.map(String.init) ?? ""
                text = p.terminationStatus == 0 ? "\(label): done. \(last)" : "\(label) failed: \(last)"
            } catch {
                text = "\(label) failed: \(error.localizedDescription)"
            }
            DispatchQueue.main.async {
                self.busy = false
                self.message = String(text.prefix(160))
                self.reload()
            }
        }
    }
}

struct UsageBar: View {
    let title: String
    let pct: Double?
    let reset: Date?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(title).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text(pct.map { "\(Int($0.rounded()))%" } ?? "–").font(.caption.monospacedDigit())
                Text("· \(fmtReset(reset))").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            ProgressView(value: min(max((pct ?? 0) / 100, 0), 1))
                .tint((pct ?? 0) >= 90 ? .red : (pct ?? 0) >= 70 ? .orange : .accentColor)
        }
    }
}

struct AccountView: View {
    @ObservedObject var m: Model
    let name: String

    var body: some View {
        let u = m.usage(name)
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: m.state?.current == name ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(m.state?.current == name ? Color.accentColor : .secondary)
                Text(name).font(.headline)
                if m.state?.primary == name {
                    Text("primary").font(.caption2).padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Capsule().fill(Color.secondary.opacity(0.2)))
                }
                Spacer()
                if let n = m.state?.requests?[name] {
                    Text("\(n) req").font(.caption2).foregroundStyle(.secondary)
                }
            }
            if let email = m.email(name) {
                Text(email).font(.caption).foregroundStyle(.secondary)
            }
            if let dead = m.state?.dead?[name] {
                Text(dead).font(.caption).foregroundStyle(.red)
            } else if let err = u?.error {
                Text(err).font(.caption).foregroundStyle(.orange)
            } else {
                if let until = m.blockedUntil(name) {
                    Text("Rate-limited until \(until.formatted(date: .omitted, time: .shortened))")
                        .font(.caption).foregroundStyle(.red)
                }
                UsageBar(title: "5-hour", pct: u?.session_pct, reset: parseDate(u?.session_reset))
                UsageBar(title: "Weekly", pct: u?.weekly_pct, reset: parseDate(u?.weekly_reset))
            }
        }
    }
}

struct Panel: View {
    @ObservedObject var m: Model

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Circle().fill(m.running ? Color.green : Color.red).frame(width: 8, height: 8)
                Text(m.running ? "Proxy running" : "Proxy not running").font(.headline)
                Spacer()
                if let u = m.updatedAt {
                    Text(u, style: .relative).font(.caption).foregroundStyle(.secondary)
                }
            }
            if !m.running && m.state != nil {
                Text("If Restart doesn't help, check the log. After moving the repo or updating Python, run `claude-rotator install` again.")
                    .font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            if m.accounts.isEmpty {
                Text("No data yet. Run `claude-rotator add` and `install`.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(m.accounts, id: \.self) { name in
                AccountView(m: m, name: name)
                if name != m.accounts.last { Divider() }
            }
            Divider()
            if let next = m.state?.next_ping {
                Label("Next window ping: \(next)", systemImage: "alarm").font(.caption)
            }
            if let msg = m.message {
                Text(msg).font(.caption).foregroundStyle(.secondary).lineLimit(3)
            }
            HStack {
                Button("Ping now") { m.pingNow() }.disabled(m.busy)
                Button(m.running ? "Restart" : "Start") { m.restartService() }.disabled(m.busy)
                Button("Log") { m.openLog() }
                Button("Folder") { m.openConfig() }
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
            }
            .controlSize(.small)
        }
        .padding(14)
        .frame(width: 340)
        .onAppear { m.reload() }
    }
}

struct BarLabel: View {
    @ObservedObject var m: Model

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: m.running ? "arrow.triangle.2.circlepath" : "exclamationmark.triangle")
            Text(m.labelText).monospacedDigit()
        }
    }
}

@main
struct ClaudeRotatorBar: App {
    @StateObject private var model = Model()

    var body: some Scene {
        MenuBarExtra {
            Panel(m: model)
        } label: {
            BarLabel(m: model)
        }
        .menuBarExtraStyle(.window)
    }
}
