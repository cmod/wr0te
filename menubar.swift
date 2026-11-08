// Menu bar view of today's writing. `wr0te.py install` compiles this into wr0te-menu.app.
//
// It only reads files next to the report: today.json, which each background run writes, and
// notify.json, left when the daily goal is reached so the notification comes from wr0te itself
// rather than Script Editor. It needs no access to your writing. Open Report asks wr0te.app (which
// has that access) for a fresh scan and report, then opens it. Settings edits config.json.
import AppKit
import UserNotifications

let home = FileManager.default.homeDirectoryForCurrentUser
let local = home.appendingPathComponent("Library/Application Support/wr0te")
let todayFile = local.appendingPathComponent("today.json")
let notifyFile = local.appendingPathComponent("notify.json")
let reportFile = local.appendingPathComponent("report.html")
let requestFile = local.appendingPathComponent("report-request")
let launcher = home.appendingPathComponent("Applications/wr0te.app")
let configFile = local.appendingPathComponent("config.json")

/// config.json as a dictionary, so keys the panel doesn't show are written back untouched.
/// nil if it exists but can't be read: better to refuse than to overwrite it.
func readConfig() -> [String: Any]? {
    guard let data = try? Data(contentsOf: configFile) else {
        return FileManager.default.fileExists(atPath: configFile.path) ? nil : [:]
    }
    return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
}

func tilde(_ path: String) -> String {
    let h = home.path
    return path == h || path.hasPrefix(h + "/") ? "~" + path.dropFirst(h.count) : path
}

func expand(_ path: String) -> String {
    (path as NSString).expandingTildeInPath
}

/// Folder names in also_watch, as wr0te.py accepts them: one path component, not hidden.
func validName(_ n: String) -> Bool {
    !n.isEmpty && !n.hasPrefix(".") && !n.contains("/") && !n.contains("\\")
}

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
            let showStreak = readConfig()?["show_streak"] as? Bool ?? true
            if showStreak, let d = t.days_30 {
                line("\(d) of the last 30 days · streak \(t.streak)")
            } else if showStreak {
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
        let update = NSMenuItem(title: building ? "Updating…" : "Update Now", action: #selector(updateNow),
                                keyEquivalent: "u")
        update.target = self
        update.isEnabled = !building
        menu.addItem(update)
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
        let settings = NSMenuItem(title: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        menu.addItem(settings)
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
        if let d = t.days_30, readConfig()?["show_streak"] as? Bool ?? true {
            parts.append("\(d) of the last 30 days")
        }
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

    @objc func updateNow() {
        rebuild()
    }

    @objc func openReport() {
        rebuild { NSWorkspace.shared.open(reportFile) }
    }

    /// Ask wr0te.app for a scan and a fresh report and today.json, then run `done`.
    func rebuild(then done: @escaping () -> Void = {}) {
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
                done()
            }
        }
    }

    var settingsWindow: SettingsWindow?

    @objc func openSettings() {
        NSApp.activate(ignoringOtherApps: true)
        if let w = settingsWindow {
            w.window.makeKeyAndOrderFront(nil)
            return
        }
        guard let config = readConfig() else {
            let alert = NSAlert()
            alert.messageText = "Can't read settings"
            alert.informativeText = "\(tilde(configFile.path)) isn't valid JSON. Fix or delete it, then try again."
            alert.runModal()
            return
        }
        let w = SettingsWindow(config: config) { [weak self] in self?.rebuild() }
        w.onClose = { [weak self] in self?.settingsWindow = nil }
        settingsWindow = w
        w.window.center()
        w.window.makeKeyAndOrderFront(nil)
    }
}

/// The Settings window: the folders to watch, the daily goal, when the day starts, the streak.
/// Saving writes config.json, which wr0te.py reads on its next run.
final class SettingsWindow: NSObject, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 400),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
    var config: [String: Any]
    var folders: [(name: String, path: String)]  // name "" = files show up as they are
    let saved: () -> Void
    var onClose: () -> Void = {}

    let table = NSTableView()
    let addRemove = NSSegmentedControl(labels: ["+", "−"], trackingMode: .momentary, target: nil, action: nil)
    let goalField = NSTextField()
    let dayStart = NSPopUpButton()
    let streakBox = NSButton(checkboxWithTitle: "Show streak and days written", target: nil, action: nil)

    init(config: [String: Any], saved: @escaping () -> Void) {
        self.config = config
        self.saved = saved
        folders = SettingsWindow.folders(in: config)
        super.init()
        window.title = "wr0te Settings"
        window.isReleasedWhenClosed = false
        window.delegate = self
        build()
    }

    /// The folders in config.json, in either its current form or the older root + also_watch.
    static func folders(in c: [String: Any]) -> [(name: String, path: String)] {
        if let list = c["folders"] as? [[String: String]] {
            return list.compactMap { f in f["path"].map { (name: f["name"] ?? "", path: $0) } }
        }
        let root = (c["root"] as? String).map { [(name: "", path: $0)] } ?? []
        return root + ((c["also_watch"] as? [String: String]) ?? [:]).sorted { $0.key < $1.key }
            .map { (name: $0.key, path: $0.value) }
    }

    func build() {
        func label(_ s: String) -> NSTextField {
            let l = NSTextField(labelWithString: s)
            l.alignment = .right
            return l
        }
        func note(_ s: String) -> NSTextField {
            let l = NSTextField(wrappingLabelWithString: s)
            l.preferredMaxLayoutWidth = 390
            l.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            l.textColor = .secondaryLabelColor
            return l
        }
        func button(_ title: String, _ action: Selector) -> NSButton {
            let b = NSButton(title: title, target: self, action: action)
            b.bezelStyle = .rounded
            return b
        }

        for (id, title, width) in [("path", "Folder", 270.0), ("name", "Shown as", 110.0)] {
            let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            col.title = title
            col.width = CGFloat(width)
            col.isEditable = id == "name"
            table.addTableColumn(col)
        }
        table.dataSource = self
        table.delegate = self
        table.usesAlternatingRowBackgroundColors = true
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.heightAnchor.constraint(equalToConstant: 110).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 390).isActive = true
        addRemove.target = self
        addRemove.action = #selector(addOrRemove)
        let folderBox = NSStackView(views: [scroll, addRemove, note(
            "Files show up in reports under the folder's name (name/…); double-click to change it. "
            + "One folder can go without a name, and its files show up as they are.")])
        folderBox.orientation = .vertical
        folderBox.alignment = .leading

        let fmt = NumberFormatter()
        fmt.allowsFloats = false
        fmt.minimum = 0
        fmt.maximum = 1_000_000
        goalField.formatter = fmt
        goalField.integerValue = config["daily_goal"] as? Int ?? 2000
        goalField.translatesAutoresizingMaskIntoConstraints = false
        goalField.widthAnchor.constraint(equalToConstant: 80).isActive = true
        let goalRow = NSStackView(views: [goalField, NSTextField(labelWithString: "words a day (0 for none)")])

        for h in 0..<12 {
            dayStart.addItem(withTitle: h == 0 ? "Midnight" : "\(h):00 am")
        }
        dayStart.selectItem(at: min(max(config["day_start_hour"] as? Int ?? 4, 0), 11))
        let dayRow = NSStackView(views: [dayStart, note("Writing before then counts toward the day before.")])

        streakBox.state = (config["show_streak"] as? Bool ?? true) ? .on : .off

        let grid = NSGridView(views: [
            [label("Folders to watch:"), folderBox],
            [label("Daily goal:"), goalRow],
            [label("Day starts at:"), dayRow],
            [NSGridCell.emptyContentView, streakBox],
        ])
        grid.rowSpacing = 14
        grid.columnSpacing = 10
        grid.rowAlignment = .firstBaseline
        grid.row(at: 0).rowAlignment = .none
        grid.cell(atColumnIndex: 0, rowIndex: 0).yPlacement = .top

        let cancel = button("Cancel", #selector(cancel))
        cancel.keyEquivalent = "\u{1b}"
        let save = button("Save", #selector(save))
        save.keyEquivalent = "\r"
        let spacer = NSView()
        spacer.setContentHuggingPriority(.init(1), for: .horizontal)
        let buttons = NSStackView(views: [spacer, cancel, save])

        let all = NSStackView(views: [grid, buttons])
        all.orientation = .vertical
        all.alignment = .leading
        all.spacing = 20
        buttons.widthAnchor.constraint(equalTo: grid.widthAnchor).isActive = true
        all.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(all)
        NSLayoutConstraint.activate([
            all.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            all.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20),
            all.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            all.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
        ])
        window.contentView = content
        window.setContentSize(content.fittingSize)
    }

    func pickFolders(multiple: Bool) -> [URL] {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = multiple
        panel.prompt = "Watch"
        return panel.runModal() == .OK ? panel.urls : []
    }

    @objc func addOrRemove() {
        if addRemove.selectedSegment == 1 {
            let row = table.selectedRow
            if row >= 0 {
                folders.remove(at: row)
                table.reloadData()
            }
            return
        }
        for url in pickFolders(multiple: true) where !folders.contains(where: { expand($0.path) == url.path }) {
            var base = url.lastPathComponent
            while base.hasPrefix(".") { base.removeFirst() }
            if !validName(base) { base = "folder" }
            var name = base, n = 2
            while folders.contains(where: { $0.name == name }) {
                name = "\(base)-\(n)"
                n += 1
            }
            folders.append((name: name, path: tilde(url.path)))
        }
        table.reloadData()
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        folders.count
    }

    func tableView(_ tableView: NSTableView, objectValueFor col: NSTableColumn?, row: Int) -> Any? {
        col?.identifier.rawValue == "name" ? folders[row].name : folders[row].path
    }

    /// A name must be plain and not used by another folder; empty is fine for one folder.
    func tableView(_ tableView: NSTableView, setObjectValue value: Any?, for col: NSTableColumn?, row: Int) {
        let name = ((value as? String) ?? "").trimmingCharacters(in: .whitespaces)
        let taken = folders.enumerated().contains { $0.offset != row && $0.element.name == name }
        if (name.isEmpty || validName(name)) && !taken {
            folders[row].name = name
        } else {
            NSSound.beep()
        }
        tableView.reloadData()
    }

    @objc func cancel() {
        window.close()
    }

    @objc func save() {
        window.makeFirstResponder(nil)  // commit a name or goal still being edited
        guard let fresh = readConfig() else { return }
        guard !folders.isEmpty else {
            NSSound.beep()
            return
        }
        var c = fresh  // reread, in case the file changed while the window was open
        // History defaults to wr0te-data next to the folder without a name: pin it where it is now,
        // so changing folders never leaves it behind.
        let old = SettingsWindow.folders(in: c)
        if c["data"] == nil, let main = old.first(where: { $0.name.isEmpty }) ?? old.first {
            let dir = URL(fileURLWithPath: expand(main.path)).standardizedFileURL.deletingLastPathComponent()
            c["data"] = tilde(dir.appendingPathComponent("wr0te-data").path)
        }
        c["folders"] = folders.map { $0.name.isEmpty ? ["path": $0.path] : ["name": $0.name, "path": $0.path] }
        c.removeValue(forKey: "root")
        c.removeValue(forKey: "also_watch")
        c["daily_goal"] = goalField.integerValue
        c["day_start_hour"] = dayStart.indexOfSelectedItem
        c["show_streak"] = streakBox.state == .on
        do {
            let data = try JSONSerialization.data(withJSONObject: c,
                                                  options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            try FileManager.default.createDirectory(at: local, withIntermediateDirectories: true)
            try (data + Data("\n".utf8)).write(to: configFile, options: .atomic)
        } catch {
            NSAlert(error: error).runModal()
            return
        }
        window.close()
        saved()
    }

    func windowWillClose(_ notification: Notification) {
        onClose()
    }
}

/// Accessory apps have no menu bar, but text fields still need ⌘X/⌘C/⌘V/⌘A/⌘Z to work.
func editMenu() -> NSMenu {
    let main = NSMenu()
    let edit = NSMenu(title: "Edit")
    for (title, action, key) in [("Undo", Selector(("undo:")), "z"), ("Redo", Selector(("redo:")), "Z"),
                                 ("Cut", #selector(NSText.cut(_:)), "x"), ("Copy", #selector(NSText.copy(_:)), "c"),
                                 ("Paste", #selector(NSText.paste(_:)), "v"),
                                 ("Select All", #selector(NSText.selectAll(_:)), "a")] {
        edit.addItem(NSMenuItem(title: title, action: action, keyEquivalent: key))
    }
    let item = NSMenuItem()
    item.submenu = edit
    main.addItem(item)
    let close = NSMenu(title: "Window")
    close.addItem(NSMenuItem(title: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
    let closeItem = NSMenuItem()
    closeItem.submenu = close
    main.addItem(closeItem)
    return main
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.mainMenu = editMenu()
let bar = Bar()
if CommandLine.arguments.contains("--settings") {  // open the window at launch, e.g. to try it out
    DispatchQueue.main.async { bar.openSettings() }
}
app.run()
