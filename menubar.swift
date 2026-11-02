// Menu bar view of today's writing. `wr0te.py install` compiles this into wr0te-menu.app.
//
// It only reads today.json, which each background run writes next to the report, so it needs
// no access to your writing. Open Report asks wr0te.app (which has that access) for a fresh scan
// and report, then opens it.
import AppKit

let home = FileManager.default.homeDirectoryForCurrentUser
let local = home.appendingPathComponent("Library/Application Support/wr0te")
let todayFile = local.appendingPathComponent("today.json")
let reportFile = local.appendingPathComponent("report.html")
let requestFile = local.appendingPathComponent("report-request")
let launcher = home.appendingPathComponent("Applications/wr0te.app")

struct Today: Decodable {
    let day: String
    let day_start_hour: Int
    let written: Int
    let removed: Int
    let files: Int
    let active_minutes: Int
    let streak: Int
    let goal: Int
    let updated: Double
}

func number(_ n: Int) -> String {
    NumberFormatter.localizedString(from: NSNumber(value: n), number: .decimal)
}

func minutes(_ m: Int) -> String {
    m >= 60 ? String(format: "%dh%02dm", m / 60, m % 60) : "\(m)m"
}

func plural(_ n: Int, _ word: String) -> String {
    "\(number(n)) \(word)\(n == 1 ? "" : "s")"
}

/// The writing day as wr0te.py counts it: before day_start_hour belongs to the day before.
func writingDay(startHour: Int) -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd"
    return f.string(from: Date().addingTimeInterval(-Double(startHour) * 3600))
}

final class Bar: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var building = false

    override init() {
        super.init()
        item.button?.image = NSImage(systemSymbolName: "pencil", accessibilityDescription: "wr0te")
        item.button?.imagePosition = .imageLeading
        menu.autoenablesItems = false
        menu.delegate = self
        item.menu = menu
        refresh()
        Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func load() -> Today? {
        guard let data = try? Data(contentsOf: todayFile) else { return nil }
        return try? JSONDecoder().decode(Today.self, from: data)
    }

    func refresh() {
        let t = load()
        // A file from yesterday means nothing written yet today; the streak still stands.
        let current = t.map { $0.day == writingDay(startHour: $0.day_start_hour) } ?? false
        let written = current ? t!.written : 0
        item.button?.title = t == nil ? " –" : " " + number(written)

        menu.removeAllItems()
        func line(_ text: String) {
            menu.addItem(NSMenuItem(title: text, action: nil, keyEquivalent: ""))
        }
        if let t = t {
            line(plural(written, "word") + " today")
            if current && (t.removed > 0 || t.files > 0) {
                line("−\(number(t.removed)) removed · \(plural(t.files, "file")) · \(minutes(t.active_minutes)) active")
            }
            if t.goal > 0 {
                line(written >= t.goal ? "Goal of \(number(t.goal)) reached"
                                       : "\(written * 100 / t.goal)% of \(number(t.goal)) goal")
            }
            line("Streak: " + plural(t.streak, "day"))
            let f = DateFormatter()
            f.dateFormat = "HH:mm"
            let updated = NSMenuItem(title: "Updated " + f.string(from: Date(timeIntervalSince1970: t.updated)),
                                     action: nil, keyEquivalent: "")
            updated.isEnabled = false
            menu.addItem(updated)
        } else {
            line("No stats yet")
        }
        menu.addItem(.separator())
        let report = NSMenuItem(title: building ? "Building Report…" : "Open Report",
                                action: #selector(openReport), keyEquivalent: "r")
        report.target = self
        report.isEnabled = !building
        menu.addItem(report)
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        refresh()
    }

    @objc func openReport() {
        building = true
        refresh()
        // wr0te.app takes no arguments; this file tells its run to rebuild the report.
        FileManager.default.createFile(atPath: requestFile.path, contents: nil,
                                       attributes: [.posixPermissions: 0o600])
        DispatchQueue.global().async {
            let open = Process()
            open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            open.arguments = ["-W", "-g", "-a", launcher.path]  // wait for the scan to finish
            try? open.run()
            open.waitUntilExit()
            DispatchQueue.main.async {
                self.building = false
                self.refresh()
                NSWorkspace.shared.open(reportFile)
            }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let bar = Bar()
app.run()
