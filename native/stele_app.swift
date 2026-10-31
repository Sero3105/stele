// Stele.app: menu bar icon that starts the local server, opens the browser once it answers and
// stops the server on quit. Built by `make app`; the project folder is read from Info.plist.
import AppKit

final class SteleApp: NSObject, NSApplicationDelegate {
    let root = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "SteleRoot") as? String ?? "")
    // shortcut: fixed port, read STELE_PORT from a settings file if the app ever needs another one
    let url = URL(string: "http://127.0.0.1:8000/")!
    let log = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Stele.log")
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let status = NSMenuItem(title: "Avvio in corso…", action: nil, keyEquivalent: "")
    var server: Process?
    var opened = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        item.button?.image = NSImage(systemSymbolName: "doc.text.viewfinder", accessibilityDescription: "Stele")
        let menu = NSMenu()
        status.isEnabled = false
        menu.addItem(status)
        menu.addItem(.separator())
        for (title, action, key) in [("Apri Stele", #selector(openBrowser), "o"),
                                     ("Mostra i dati nel Finder", #selector(showData), ""),
                                     ("Mostra il log", #selector(showLog), "")] {
            menu.addItem(withTitle: title, action: action, keyEquivalent: key).target = self
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Esci da Stele", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu
        startServer()
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.check() }
    }

    func startServer() {
        let p = Process()
        p.executableURL = root.appendingPathComponent(".venv/bin/python")
        p.arguments = ["-m", "app.main"]
        p.currentDirectoryURL = root
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let fh = try? FileHandle(forWritingTo: log) {
            p.standardOutput = fh
            p.standardError = fh
        }
        do {
            try p.run()
            server = p
        } catch {
            status.title = "Avvio non riuscito: esegui «make install» in \(root.path)"
        }
    }

    // A server already started from the terminal (`make run`) also counts: this app's own copy then
    // exits on the busy port, and quitting the app leaves the other one running.
    func check() {
        URLSession.shared.dataTask(with: url.appendingPathComponent("api/jobs")) { _, response, _ in
            let up = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { self.update(up) }
        }.resume()
    }

    func update(_ up: Bool) {
        if up {
            status.title = "In esecuzione su 127.0.0.1:8000"
            if !opened {
                opened = true
                openBrowser()
            }
        } else if let p = server, !p.isRunning {
            status.title = "Server fermo: vedi il log"
        }
    }

    @objc func openBrowser() { NSWorkspace.shared.open(url) }

    @objc func showData() {
        let data = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Stele")
        NSWorkspace.shared.activateFileViewerSelecting([data])
    }

    @objc func showLog() { NSWorkspace.shared.open(log) }

    func applicationWillTerminate(_ notification: Notification) {
        guard let p = server, p.isRunning else { return }
        p.terminate()  // SIGTERM: uvicorn shuts down; interrupted conversions resume from their checkpoints
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { usleep(100_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }
}

let app = NSApplication.shared
let delegate = SteleApp()
app.delegate = delegate
app.setActivationPolicy(.accessory)  // menu bar only, no Dock icon
app.run()
