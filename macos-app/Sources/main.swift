import AppKit
import Darwin
import Foundation

final class BridgeController: NSObject, NSApplicationDelegate {
    private let pairingURL = "https://pair.tofukanban.uk"
    private let port = 8765
    private var statusItem: NSStatusItem!
    private var statusMenuItem: NSMenuItem!
    private var pairMenuItem: NSMenuItem!
    private var startMenuItem: NSMenuItem!
    private var stopMenuItem: NSMenuItem!
    private var process: Process?
    private var healthTimer: Timer?
    private var pairingInProgress = false
    private var bridgeOutput = ""
    private var pendingPairCode: String?
    private var expectedStop = false
    private var isStopping = false

    private var language: String {
        if let saved = UserDefaults.standard.string(forKey: "language") { return saved }
        return Locale.preferredLanguages.first?.hasPrefix("zh") == true ? "zh" : "en"
    }

    private func t(_ zh: String, _ en: String) -> String { language == "zh" ? zh : en }

    private var supportDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("PlayMac", isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        startBridge(pairCode: nil)
        healthTimer = Timer.scheduledTimer(timeInterval: 2, target: self,
                                           selector: #selector(checkHealth),
                                           userInfo: nil, repeats: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        healthTimer?.invalidate()
        stopBridge()
    }

    private func buildMenu() {
        if statusItem != nil { NSStatusBar.system.removeStatusItem(statusItem) }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "N▶"
        statusItem.button?.toolTip = "PlayMac"

        let menu = NSMenu()
        statusMenuItem = NSMenuItem(title: t("正在启动…", "Starting…"), action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        menu.addItem(.separator())

        pairMenuItem = NSMenuItem(title: t("配对 Playdate…", "Pair Playdate…"), action: #selector(showPairing), keyEquivalent: "p")
        pairMenuItem.target = self
        menu.addItem(pairMenuItem)

        startMenuItem = NSMenuItem(title: t("启动 Bridge", "Start Bridge"), action: #selector(startFromMenu), keyEquivalent: "")
        startMenuItem.target = self
        menu.addItem(startMenuItem)

        stopMenuItem = NSMenuItem(title: t("停止 Bridge", "Stop Bridge"), action: #selector(stopFromMenu), keyEquivalent: "")
        stopMenuItem.target = self
        menu.addItem(stopMenuItem)

        let folderItem = NSMenuItem(title: t("打开数据文件夹", "Open Data Folder"), action: #selector(openDataFolder), keyEquivalent: "")
        folderItem.target = self
        menu.addItem(folderItem)
        let languageMenu = NSMenu()
        let chinese = NSMenuItem(title: "中文", action: #selector(useChinese), keyEquivalent: "")
        chinese.target = self
        chinese.state = language == "zh" ? .on : .off
        languageMenu.addItem(chinese)
        let english = NSMenuItem(title: "English", action: #selector(useEnglish), keyEquivalent: "")
        english.target = self
        english.state = language == "en" ? .on : .off
        languageMenu.addItem(english)
        let languageItem = NSMenuItem(title: t("语言", "Language"), action: nil, keyEquivalent: "")
        languageItem.submenu = languageMenu
        menu.addItem(languageItem)
        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: t("退出 PlayMac", "Quit PlayMac"), action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        statusItem.menu = menu
        updateMenu(running: process?.isRunning == true, text: process?.isRunning == true ? t("Bridge 在线", "Bridge Online") : t("未运行", "Not Running"))
    }

    private func updateMenu(running: Bool, text: String) {
        DispatchQueue.main.async {
            self.statusMenuItem.title = text
            self.startMenuItem.isEnabled = !running
            self.stopMenuItem.isEnabled = running
            self.pairMenuItem.isEnabled = !self.isStopping && !self.pairingInProgress
            self.statusItem.button?.title = running ? "N●" : "N○"
        }
    }

    private func bridgeExecutable() -> URL? {
        Bundle.main.resourceURL?.appendingPathComponent("PlayMacServer")
    }

    private func startBridge(pairCode: String?) {
        guard process?.isRunning != true else { return }
        guard let executable = bridgeExecutable(), FileManager.default.fileExists(atPath: executable.path) else {
            updateMenu(running: false, text: t("Bridge 文件缺失", "Bridge Files Missing"))
            return
        }
        try? FileManager.default.createDirectory(at: supportDirectory,
                                                 withIntermediateDirectories: true)
        let dataFile = supportDirectory.appendingPathComponent("data.json")
        if !FileManager.default.fileExists(atPath: dataFile.path) {
            let oldDirectory = supportDirectory.deletingLastPathComponent()
                .appendingPathComponent("NotiPlay Bridge", isDirectory: true)
            let oldData = oldDirectory.appendingPathComponent("data.json")
            if FileManager.default.fileExists(atPath: oldData.path) {
                try? FileManager.default.copyItem(at: oldData, to: dataFile)
            }
        }

        let task = Process()
        task.executableURL = executable
        var arguments = ["--host", "0.0.0.0", "--port", String(port),
                         "--data", dataFile.path]
        if let pairCode {
            arguments += ["--pairing-url", pairingURL, "--pair-code", pairCode,
                          "--companion-name", Host.current().localizedName ?? "Mac"]
        }
        task.arguments = arguments
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        task.environment = environment
        task.currentDirectoryURL = executable.deletingLastPathComponent()

        let output = Pipe()
        bridgeOutput = ""
        task.standardOutput = output
        task.standardError = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let message = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                guard let self, self.process === task else { return }
                self.bridgeOutput += message
                if message.contains("Paired code") {
                    self.pairingInProgress = false
                    self.updateMenu(running: true, text: self.t("已配对 · Bridge 在线", "Paired · Bridge Online"))
                }
            }
        }
        task.terminationHandler = { [weak self] stopped in
            DispatchQueue.main.async {
                guard let self, self.process === stopped else { return }
                output.fileHandleForReading.readabilityHandler = nil
                self.process = nil
                self.isStopping = false
                if self.expectedStop {
                    self.expectedStop = false
                    let code = self.pendingPairCode
                    self.pendingPairCode = nil
                    if let code {
                        self.pairingInProgress = true
                        self.startBridge(pairCode: code)
                    } else {
                        self.pairingInProgress = false
                        self.updateMenu(running: false, text: self.t("Bridge 已停止", "Bridge Stopped"))
                    }
                    return
                }
                if self.pairingInProgress {
                    self.pairingInProgress = false
                    let finalLine = self.bridgeOutput.split(separator: "\n").last.map(String.init) ?? ""
                    let detail = finalLine.isEmpty
                        ? self.t("请确认配对码未过期，并且 Mac 已连接互联网。", "Check that the code is valid and the Mac is online.")
                        : finalLine
                    self.showError(self.t("配对失败", "Pairing Failed"), detail: detail)
                }
                self.updateMenu(running: false, text: self.t("Bridge 已停止", "Bridge Stopped"))
            }
        }
        do {
            try task.run()
            process = task
            updateMenu(running: true, text: pairCode == nil ? t("Bridge 在线", "Bridge Online") : t("正在配对…", "Pairing…"))
        } catch {
            showError(t("无法启动 Bridge", "Cannot Start Bridge"), detail: error.localizedDescription)
            updateMenu(running: false, text: t("启动失败", "Start Failed"))
        }
    }

    private func stopBridge(restartWith pairCode: String? = nil) {
        pendingPairCode = pairCode
        guard let task = process, task.isRunning else {
            if let pairCode {
                pendingPairCode = nil
                pairingInProgress = true
                startBridge(pairCode: pairCode)
            }
            return
        }
        expectedStop = true
        isStopping = true
        updateMenu(running: true, text: pairCode == nil ? t("正在停止…", "Stopping…") : t("正在重新启动并配对…", "Restarting to Pair…"))
        task.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self, weak task] in
            guard let self, let task, self.process === task, task.isRunning else { return }
            kill(task.processIdentifier, SIGKILL)
        }
    }

    @objc private func checkHealth() {
        guard process?.isRunning == true, !pairingInProgress, !isStopping else { return }
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, response, _ in
            guard let self, let http = response as? HTTPURLResponse, http.statusCode == 200,
                  let data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let media = (json["mediaRemote"] as? Bool) == true
            self.updateMenu(running: true,
                            text: media ? self.t("Bridge 在线 · 媒体控制可用", "Bridge Online · Media Ready") : self.t("Bridge 在线 · 未安装媒体后端", "Bridge Online · Media Backend Missing"))
        }.resume()
    }

    @objc private func showPairing() {
        let alert = NSAlert()
        alert.messageText = t("配对 Playdate", "Pair Playdate")
        alert.informativeText = t("输入 Playdate 屏幕上显示的 6 位数字。", "Enter the 6-digit code shown on Playdate.")
        alert.addButton(withTitle: t("配对", "Pair"))
        alert.addButton(withTitle: t("取消", "Cancel"))
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 28))
        input.placeholderString = "123456"
        input.alignment = .center
        input.font = .monospacedDigitSystemFont(ofSize: 20, weight: .medium)
        alert.accessoryView = input
        alert.window.initialFirstResponder = input
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let code = input.stringValue.filter(\.isNumber)
        guard code.count == 6 else {
            showError(t("配对码格式不正确", "Invalid Pairing Code"), detail: t("请输入 6 位数字。", "Enter six digits."))
            return
        }
        stopBridge(restartWith: code)
    }

    @objc private func startFromMenu() { startBridge(pairCode: nil) }
    @objc private func stopFromMenu() { stopBridge() }
    @objc private func useChinese() { setLanguage("zh") }
    @objc private func useEnglish() { setLanguage("en") }
    private func setLanguage(_ value: String) {
        UserDefaults.standard.set(value, forKey: "language")
        buildMenu()
    }
    @objc private func openDataFolder() {
        try? FileManager.default.createDirectory(at: supportDirectory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(supportDirectory)
    }
    @objc private func quit() { NSApplication.shared.terminate(nil) }

    private func showError(_ title: String, detail: String) {
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = title
            alert.informativeText = detail
            alert.runModal()
        }
    }
}

let application = NSApplication.shared
let delegate = BridgeController()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
