// Menu bar view of today's writing. `wr0te.py install` compiles this into wr0te-menu.app.
//
// It only reads files next to the report: today.json, which each background run writes, and
// notify.json, left when the daily goal is reached so the notification comes from wr0te itself
// rather than Script Editor. It needs no access to your writing. Open Report asks wr0te.app (which
// has that access) for a fresh scan and report, then opens it.
import AppKit
import UserNotifications

let home = FileManager.default.homeDirectoryForCurrentUser
let local = home.appendingPathComponent("Library/Application Support/wr0te")
let todayFile = local.appendingPathComponent("today.json")
let notifyFile = local.appendingPathComponent("notify.json")
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
    let days_30: Int?
    let goal: Int
    let updated: Double
}

struct Note: Decodable {
    let title: String
    let body: String
    let at: Double
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

func dayFormatter(_ format: String) -> DateFormatter {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = format
    return f
}

/// The writing day as wr0te.py counts it: before day_start_hour belongs to the day before.
func writingDay(startHour: Int) -> String {
    dayFormatter("yyyy-MM-dd").string(from: Date().addingTimeInterval(-Double(startHour) * 3600))
}

/// The menu bar icon: a pencil, with a check mark beside it once the day's goal is reached.
func barImage(goalMet: Bool) -> NSImage? {
    guard let pencil = NSImage(systemSymbolName: "pencil", accessibilityDescription: nil)?
        .withSymbolConfiguration(.init(pointSize: 14, weight: .regular)) else { return nil }
    guard goalMet, let check = NSImage(systemSymbolName: "checkmark", accessibilityDescription: nil)?
        .withSymbolConfiguration(.init(pointSize: 10, weight: .bold)) else {
        pencil.isTemplate = true
        return pencil
    }
    let gap: CGFloat = 2
    let size = NSSize(width: pencil.size.width + gap + check.size.width, height: max(pencil.size.height, check.size.height))
    let image = NSImage(size: size, flipped: false) { _ in
        pencil.draw(in: NSRect(x: 0, y: (size.height - pencil.size.height) / 2,
                               width: pencil.size.width, height: pencil.size.height))
        check.draw(in: NSRect(x: pencil.size.width + gap, y: (size.height - check.size.height) / 2,
                              width: check.size.width, height: check.size.height))
        return true
    }
    image.isTemplate = true
    return image
}

/// osascript's notification, which macOS shows as Script Editor: used when wr0te's own isn't allowed.
func scriptNotification(_ note: Note) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                   "-e", "end run", note.title, note.body]
    try? p.run()
}

final class Bar: NSObject, NSMenuDelegate, UNUserNotificationCenterDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var building = false

    override init() {
        super.init()
        item.button?.imagePosition = .imageOnly
        menu.autoenablesItems = false
        menu.delegate = self
        item.menu = menu
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
        refresh()
        Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.refresh() }
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.deliver() }
    }

    func load() -> Today? {
        guard let data = try? Data(contentsOf: todayFile) else { return nil }
        return try? JSONDecoder().decode(Today.self, from: data)
    }

    /// Today's numbers, or zeros (with the streak kept) when the file is from an earlier writing day.
    func current(_ t: Today) -> (fresh: Bool, written: Int) {
        let fresh = t.day == writingDay(startHour: t.day_start_hour)
        return (fresh, fresh ? t.written : 0)
    }

    func refresh() {
        let t = load()
        let (fresh, written) = t.map(current) ?? (false, 0)
        let met = t.map { $0.goal > 0 && written >= $0.goal } ?? false
        item.button?.image = barImage(goalMet: met)
        item.button?.setAccessibilityLabel(met ? "wr0te, goal reached" : "wr0te, \(plural(written, "word")) today")

        menu.removeAllItems()
        func line(_ text: String) {
            menu.addItem(NSMenuItem(title: text, action: nil, keyEquivalent: ""))
        }
        if let t = t {
            line(plural(written, "word") + " today")
            if fresh && (t.removed > 0 || t.files > 0) {
                line("−\(number(t.removed)) removed · \(plural(t.files, "file")) · \(minutes(t.active_minutes)) active")
            }
            if t.goal > 0 {
                line(met ? "Goal of \(number(t.goal)) reached" : "\(written * 100 / t.goal)% of \(number(t.goal)) goal")
            }
            if let d = t.days_30 {
                line("\(d) of the last 30 days · streak \(t.streak)")
            } else {
                line("Streak: " + plural(t.streak, "day"))
            }
            let updated = NSMenuItem(title: "Updated " + dayFormatter("HH:mm").string(from: Date(timeIntervalSince1970: t.updated)),
                                     action: nil, keyEquivalent: "")
            updated.isEnabled = false
            menu.addItem(updated)
        } else {
            line("No stats yet")
        }
        menu.addItem(.separator())
        let copy = NSMenuItem(title: "Copy Today's Stats", action: #selector(copyStats), keyEquivalent: "c")
        copy.target = self
        copy.isEnabled = t != nil
        menu.addItem(copy)
        let report = NSMenuItem(title: building ? "Building Report…" : "Open Report",
                                action: #selector(openReport), keyEquivalent: "r")
        report.target = self
        report.isEnabled = !building
        menu.addItem(report)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        refresh()
    }

    /// One line, ready to paste: "Wed Nov 4: 2,065 words · goal 2,000 reached · 1,082 removed · …"
    @objc func copyStats() {
        guard let t = load() else { return }
        let (fresh, written) = current(t)
        let date = dayFormatter("EEE MMM d").string(from: Date().addingTimeInterval(-Double(t.day_start_hour) * 3600))
        var parts = [plural(written, "word")]
        if t.goal > 0 {
            parts.append(written >= t.goal ? "goal \(number(t.goal)) reached" : "\(written * 100 / t.goal)% of \(number(t.goal)) goal")
        }
        if fresh {
            if t.removed > 0 { parts.append("\(number(t.removed)) removed") }
            parts.append(plural(t.files, "file"))
            parts.append("\(minutes(t.active_minutes)) active")
        }
        if let d = t.days_30 { parts.append("\(d) of the last 30 days") }
        let text = "\(date): " + parts.joined(separator: " · ")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    /// wr0te.py leaves notify.json when the daily goal is reached; post it as wr0te's own notification.
    func deliver() {
        guard let data = try? Data(contentsOf: notifyFile) else { return }
        try? FileManager.default.removeItem(at: notifyFile)
        guard let note = try? JSONDecoder().decode(Note.self, from: data),
              Date().timeIntervalSince1970 - note.at < 3600 else { return }  // not a stale one from hours ago
        refresh()
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                scriptNotification(note)
                return
            }
            let content = UNMutableNotificationContent()
            content.title = note.title
            content.body = note.body
            center.add(UNNotificationRequest(identifier: "goal", content: content, trigger: nil)) { error in
                if error != nil { scriptNotification(note) }
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
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
